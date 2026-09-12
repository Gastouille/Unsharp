"""Daily signal journal: one JSON file per day, plus optional CSV and audit trail.

Every detected setup is recorded - including the ones that never became an
order - so a session can be reviewed end to end: what was seen, what was
refused and why, what was sent, and how it finished.

Files written under ``logging.signals_directory``:

* ``signals-YYYY-MM-DD.json``   - authoritative, rewritten on every update
* ``signals-YYYY-MM-DD.csv``    - flat mirror for spreadsheets (optional)
* ``audit-YYYY-MM-DD.jsonl``    - append-only trail of every state change
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .config import LoggingConfig
from .models import (
    Direction,
    Level,
    SignalStatus,
    TradePlan,
    UnsharpSetup,
)

LOGGER = logging.getLogger(__name__)

CSV_COLUMNS = [
    "signal_id", "timestamp", "symbol", "direction", "entry_price", "stop_price",
    "target_price", "volume", "ratio_rr", "level_price", "level_type", "status",
    "pnl", "risk_fraction_used", "capital_available_before_trade",
    "notional_engaged", "risk_amount", "position_id", "close_time",
    "close_price", "close_reason", "rejection_reason",
]


@dataclass(slots=True)
class SignalRecord:
    """One journal entry, from detection to close."""

    signal_id: str
    timestamp: datetime
    symbol: str
    direction: Direction
    status: SignalStatus
    entry_price: float | None = None
    stop_price: float | None = None
    target_price: float | None = None
    volume: float | None = None
    ratio_rr: float | None = None
    level_used: dict[str, Any] | None = None
    pnl: float | None = None
    risk_fraction_used: float | None = None
    capital_available_before_trade: float | None = None
    notional_engaged: float | None = None
    risk_amount: float | None = None
    margin_required: float | None = None
    position_id: int | None = None
    order_id: int | None = None
    close_time: datetime | None = None
    close_price: float | None = None
    close_reason: str | None = None
    rejection_reason: str | None = None
    session: str | None = None
    timeframe_minutes: int | None = None
    notes: list[str] = field(default_factory=list)
    setup: dict[str, Any] | None = None
    account_before: dict[str, Any] | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready mapping (the shape documented in the README)."""
        return {
            "signal_id": self.signal_id,
            "timestamp": _iso(self.timestamp),
            "symbol": self.symbol,
            "direction": self.direction.value,
            "entry_price": _round(self.entry_price),
            "stop_price": _round(self.stop_price),
            "target_price": _round(self.target_price),
            "volume": self.volume,
            "ratio_rr": _round(self.ratio_rr, 3),
            "level_used": self.level_used,
            "status": self.status.value,
            "pnl": _round(self.pnl, 2),
            "risk_fraction_used": self.risk_fraction_used,
            "capital_available_before_trade": _round(self.capital_available_before_trade, 2),
            "notional_engaged": _round(self.notional_engaged, 2),
            "risk_amount": _round(self.risk_amount, 2),
            "margin_required": _round(self.margin_required, 2),
            "position_id": self.position_id,
            "order_id": self.order_id,
            "close_time": _iso(self.close_time),
            "close_price": _round(self.close_price),
            "close_reason": self.close_reason,
            "rejection_reason": self.rejection_reason,
            "session": self.session,
            "timeframe_minutes": self.timeframe_minutes,
            "notes": self.notes,
            "setup": self.setup,
            "account_before": self.account_before,
            "updated_at": _iso(self.updated_at),
        }

    def to_csv_row(self) -> dict[str, Any]:
        level = self.level_used or {}
        return {
            "signal_id": self.signal_id,
            "timestamp": _iso(self.timestamp),
            "symbol": self.symbol,
            "direction": self.direction.value,
            "entry_price": _round(self.entry_price),
            "stop_price": _round(self.stop_price),
            "target_price": _round(self.target_price),
            "volume": self.volume,
            "ratio_rr": _round(self.ratio_rr, 3),
            "level_price": _round(level.get("price")),
            "level_type": level.get("type"),
            "status": self.status.value,
            "pnl": _round(self.pnl, 2),
            "risk_fraction_used": self.risk_fraction_used,
            "capital_available_before_trade": _round(self.capital_available_before_trade, 2),
            "notional_engaged": _round(self.notional_engaged, 2),
            "risk_amount": _round(self.risk_amount, 2),
            "position_id": self.position_id,
            "close_time": _iso(self.close_time),
            "close_price": _round(self.close_price),
            "close_reason": self.close_reason,
            "rejection_reason": self.rejection_reason,
        }


class SignalJournal:
    """Thread-safe daily journal writer."""

    def __init__(self, config: LoggingConfig, directory: Path, timeframe_minutes: int = 0) -> None:
        self.config = config
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.timeframe_minutes = timeframe_minutes
        self._records: dict[str, SignalRecord] = {}
        self._day: date | None = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    # Paths
    # ------------------------------------------------------------------ #
    def json_path(self, day: date) -> Path:
        return self.directory / f"signals-{day.isoformat()}.json"

    def csv_path(self, day: date) -> Path:
        return self.directory / f"signals-{day.isoformat()}.csv"

    def audit_path(self, day: date) -> Path:
        return self.directory / f"audit-{day.isoformat()}.jsonl"

    # ------------------------------------------------------------------ #
    # Day handling
    # ------------------------------------------------------------------ #
    def _ensure_day(self, moment: datetime) -> date:
        """Load (or roll over to) the journal of ``moment``'s day."""
        day = moment.astimezone(timezone.utc).date()
        if self._day == day:
            return day
        self._day = day
        self._records = {}
        path = self.json_path(day)
        if path.is_file():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                for entry in payload.get("signals", []):
                    record = _record_from_dict(entry)
                    self._records[record.signal_id] = record
                LOGGER.info("Reloaded %d signals from %s", len(self._records), path)
            except (ValueError, KeyError) as exc:
                LOGGER.warning("Could not reload %s (%s), starting a fresh journal", path, exc)
        return day

    # ------------------------------------------------------------------ #
    # Writing
    # ------------------------------------------------------------------ #
    def record_detection(
        self,
        setup: UnsharpSetup,
        status: SignalStatus = SignalStatus.DETECTED,
        session: str | None = None,
    ) -> SignalRecord:
        """Create a journal entry for a freshly detected setup."""
        with self._lock:
            self._ensure_day(setup.timestamp)
            record = SignalRecord(
                signal_id=_new_id(setup.symbol, setup.timestamp),
                timestamp=setup.timestamp,
                symbol=setup.symbol,
                direction=setup.direction,
                status=status,
                level_used=setup.level.to_dict(),
                session=session,
                timeframe_minutes=self.timeframe_minutes,
                setup=setup.to_dict(),
            )
            self._records[record.signal_id] = record
            self._flush(record, "detected")
            return record

    def attach_plan(self, record: SignalRecord, plan: TradePlan) -> SignalRecord:
        """Fill in the sizing / geometry once the plan is built."""
        with self._lock:
            record.entry_price = plan.entry_price
            record.stop_price = plan.stop_price
            record.target_price = plan.target_price
            record.volume = plan.volume
            record.ratio_rr = plan.risk_reward
            record.risk_amount = plan.risk_amount
            record.notional_engaged = plan.notional_engaged
            record.capital_available_before_trade = plan.capital_available
            record.risk_fraction_used = plan.risk_fraction_used
            record.margin_required = plan.margin_required
            record.notes = list(plan.notes)
            record.updated_at = datetime.now(timezone.utc)
            self._flush(record, "planned")
            return record

    def update_status(
        self,
        record: SignalRecord,
        status: SignalStatus,
        reason: str | None = None,
        **fields: Any,
    ) -> SignalRecord:
        """Move a record to a new status, optionally patching extra fields."""
        with self._lock:
            record.status = status
            if reason is not None:
                if status is SignalStatus.REJECTED:
                    record.rejection_reason = reason
                elif status is SignalStatus.CLOSED:
                    record.close_reason = reason
                else:
                    record.notes.append(reason)
            for key, value in fields.items():
                if hasattr(record, key):
                    setattr(record, key, value)
            record.updated_at = datetime.now(timezone.utc)
            self._flush(record, status.value.lower())
            return record

    def record_rejection(
        self,
        symbol: str,
        timestamp: datetime,
        direction: Direction | None,
        reason: str,
        level: Level | None = None,
        details: dict[str, Any] | None = None,
    ) -> SignalRecord:
        """Journal a setup that was detected but never traded."""
        with self._lock:
            self._ensure_day(timestamp)
            record = SignalRecord(
                signal_id=_new_id(symbol, timestamp),
                timestamp=timestamp,
                symbol=symbol,
                direction=direction or Direction.LONG,
                status=SignalStatus.REJECTED,
                rejection_reason=reason,
                level_used=level.to_dict() if level else None,
                timeframe_minutes=self.timeframe_minutes,
                setup=details,
            )
            self._records[record.signal_id] = record
            self._flush(record, "rejected")
            return record

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def _flush(self, record: SignalRecord, event: str) -> None:
        day = self._day or datetime.now(timezone.utc).date()
        self._write_json(day)
        if self.config.write_csv:
            self._write_csv(day)
        if self.config.write_jsonl_audit:
            self._append_audit(day, event, record)

    def _write_json(self, day: date) -> None:
        """Atomic rewrite: write to a temp file then rename over the target."""
        path = self.json_path(day)
        records = sorted(self._records.values(), key=lambda r: r.timestamp)
        payload = {
            "date": day.isoformat(),
            "generated_at": _iso(datetime.now(timezone.utc)),
            "count": len(records),
            "signals": [record.to_dict() for record in records],
        }
        _atomic_write(path, json.dumps(payload, indent=2, ensure_ascii=False))

    def _write_csv(self, day: date) -> None:
        path = self.csv_path(day)
        records = sorted(self._records.values(), key=lambda r: r.timestamp)
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            writer.writerow(record.to_csv_row())
        _atomic_write(path, buffer.getvalue())

    def _append_audit(self, day: date, event: str, record: SignalRecord) -> None:
        path = self.audit_path(day)
        entry = {
            "event": event,
            "at": _iso(datetime.now(timezone.utc)),
            "signal": record.to_dict(),
        }
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:  # pragma: no cover
            LOGGER.warning("Could not append to the audit trail %s: %s", path, exc)

    # ------------------------------------------------------------------ #
    # Reading
    # ------------------------------------------------------------------ #
    def open_records(self) -> list[SignalRecord]:
        with self._lock:
            return [
                record for record in self._records.values()
                if record.status in (SignalStatus.OPEN, SignalStatus.PENDING)
            ]

    def find_by_position(self, position_id: int) -> SignalRecord | None:
        with self._lock:
            for record in self._records.values():
                if record.position_id == position_id:
                    return record
        return None

    def all_records(self) -> list[SignalRecord]:
        with self._lock:
            return sorted(self._records.values(), key=lambda r: r.timestamp)

    def daily_summary(self) -> dict[str, Any]:
        records = self.all_records()
        closed = [r for r in records if r.status is SignalStatus.CLOSED and r.pnl is not None]
        wins = [r for r in closed if (r.pnl or 0) > 0]
        return {
            "date": self._day.isoformat() if self._day else None,
            "detected": len(records),
            "traded": len([r for r in records if r.position_id]),
            "rejected": len([r for r in records if r.status is SignalStatus.REJECTED]),
            "open": len([r for r in records if r.status is SignalStatus.OPEN]),
            "closed": len(closed),
            "wins": len(wins),
            "win_rate": round(len(wins) / len(closed), 3) if closed else None,
            "realised_pnl": round(sum(r.pnl or 0.0 for r in closed), 2),
        }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _new_id(symbol: str, timestamp: datetime) -> str:
    stamp = timestamp.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{symbol}-{stamp}-{uuid.uuid4().hex[:6]}"


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def _round(value: float | None, digits: int = 6) -> float | None:
    return round(value, digits) if isinstance(value, (int, float)) else None


def _atomic_write(path: Path, content: str) -> None:
    """Write via a temporary file + ``os.replace`` so readers never see a partial file."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:  # pragma: no cover
        LOGGER.error("Could not write %s: %s", path, exc)
        temporary.unlink(missing_ok=True)


def _record_from_dict(entry: dict[str, Any]) -> SignalRecord:
    """Rebuild a record from its JSON form (used when reloading a day)."""
    return SignalRecord(
        signal_id=entry["signal_id"],
        timestamp=datetime.fromisoformat(entry["timestamp"]),
        symbol=entry["symbol"],
        direction=Direction(entry["direction"]),
        status=SignalStatus(entry["status"]),
        entry_price=entry.get("entry_price"),
        stop_price=entry.get("stop_price"),
        target_price=entry.get("target_price"),
        volume=entry.get("volume"),
        ratio_rr=entry.get("ratio_rr"),
        level_used=entry.get("level_used"),
        pnl=entry.get("pnl"),
        risk_fraction_used=entry.get("risk_fraction_used"),
        capital_available_before_trade=entry.get("capital_available_before_trade"),
        notional_engaged=entry.get("notional_engaged"),
        risk_amount=entry.get("risk_amount"),
        margin_required=entry.get("margin_required"),
        position_id=entry.get("position_id"),
        order_id=entry.get("order_id"),
        close_time=datetime.fromisoformat(entry["close_time"]) if entry.get("close_time") else None,
        close_price=entry.get("close_price"),
        close_reason=entry.get("close_reason"),
        rejection_reason=entry.get("rejection_reason"),
        session=entry.get("session"),
        timeframe_minutes=entry.get("timeframe_minutes"),
        notes=list(entry.get("notes") or []),
        setup=entry.get("setup"),
        account_before=entry.get("account_before"),
    )
