"""Domain models shared by every layer of the bot.

These objects are deliberately broker-agnostic: the strategy, the risk engine
and the journal only ever manipulate the types defined here.  Broker adapters
(``unsharp_bot.broker``) are responsible for translating their own payloads
into these structures.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class Direction(str, Enum):
    """Direction of a trade / setup."""

    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def sign(self) -> int:
        """+1 for a long, -1 for a short (handy for price arithmetic)."""
        return 1 if self is Direction.LONG else -1

    @property
    def opposite(self) -> "Direction":
        return Direction.SHORT if self is Direction.LONG else Direction.LONG


class LevelType(str, Enum):
    """Origin of a key level.  Used for traceability in the signal journal."""

    SWING_HIGH = "swing_high"
    SWING_LOW = "swing_low"
    RECENT_HIGH = "recent_high"
    RECENT_LOW = "recent_low"
    PREVIOUS_DAY_HIGH = "previous_day_high"
    PREVIOUS_DAY_LOW = "previous_day_low"
    PREVIOUS_DAY_CLOSE = "previous_day_close"
    SESSION_HIGH = "session_high"
    SESSION_LOW = "session_low"
    CONSOLIDATION = "consolidation"
    ROUND_NUMBER = "round_number"


class SignalStatus(str, Enum):
    """Life cycle of a signal, as persisted in the daily journal."""

    DETECTED = "DETECTED"      # pattern valid, not yet sized / sent
    REJECTED = "REJECTED"      # filtered out (risk, session, duplicates...)
    PENDING = "PENDING"        # order sent, awaiting broker confirmation
    OPEN = "OPEN"              # position live on the broker
    CLOSED = "CLOSED"          # position closed, pnl known
    CANCELLED = "CANCELLED"    # order never filled / cancelled
    ERROR = "ERROR"            # broker rejected the order


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @classmethod
    def from_direction(cls, direction: Direction) -> "OrderSide":
        return cls.BUY if direction is Direction.LONG else cls.SELL


# --------------------------------------------------------------------------- #
# Market data
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Candle:
    """A single OHLCV bar.  ``timestamp`` is the *open* time, always UTC-aware."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    # -- derived geometry -------------------------------------------------- #
    @property
    def body(self) -> float:
        """Absolute size of the candle body."""
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        """High-to-low amplitude (never negative)."""
        return max(self.high - self.low, 0.0)

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open

    @property
    def direction(self) -> Direction:
        return Direction.LONG if self.close >= self.open else Direction.SHORT

    @property
    def close_position(self) -> float:
        """Where the close sits inside the range: 0.0 = on the low, 1.0 = on the high."""
        if self.range <= 0:
            return 0.5
        return (self.close - self.low) / self.range

    def wick_ratio(self, direction: Direction) -> float:
        """Share of the range taken by the wick that *rejects* ``direction``.

        For a LONG rejection we look at the lower wick (buyers defending),
        for a SHORT rejection at the upper wick (sellers defending).
        """
        if self.range <= 0:
            return 0.0
        wick = self.lower_wick if direction is Direction.LONG else self.upper_wick
        return max(wick, 0.0) / self.range

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


@dataclass(frozen=True, slots=True)
class Level:
    """A key price level (support, resistance, recent extreme, ...)."""

    price: float
    type: LevelType
    touches: int = 1
    #: Half-width of the zone around ``price`` (levels are really zones).
    width: float = 0.0
    #: Timestamp of the most recent candle that formed/tested the level.
    last_touch: datetime | None = None

    @property
    def low(self) -> float:
        return self.price - self.width

    @property
    def high(self) -> float:
        return self.price + self.width

    def contains(self, price: float, tolerance: float = 0.0) -> bool:
        return self.low - tolerance <= price <= self.high + tolerance

    def distance_to(self, price: float) -> float:
        """Distance from ``price`` to the *edge* of the zone (0 when inside)."""
        if self.contains(price):
            return 0.0
        return min(abs(price - self.low), abs(price - self.high))

    @property
    def strength(self) -> float:
        """Crude ranking score: more touches and 'structural' types score higher."""
        base = {
            LevelType.PREVIOUS_DAY_HIGH: 3.0,
            LevelType.PREVIOUS_DAY_LOW: 3.0,
            LevelType.PREVIOUS_DAY_CLOSE: 2.0,
            LevelType.SWING_HIGH: 2.0,
            LevelType.SWING_LOW: 2.0,
            LevelType.CONSOLIDATION: 2.0,
            LevelType.RECENT_HIGH: 1.5,
            LevelType.RECENT_LOW: 1.5,
            LevelType.SESSION_HIGH: 1.5,
            LevelType.SESSION_LOW: 1.5,
            LevelType.ROUND_NUMBER: 1.0,
        }.get(self.type, 1.0)
        return base * (1.0 + 0.5 * (self.touches - 1))

    def to_dict(self) -> dict[str, Any]:
        return {
            "price": round(self.price, 8),
            "type": self.type.value,
            "touches": self.touches,
            "width": round(self.width, 8),
        }


# --------------------------------------------------------------------------- #
# Instrument specification
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class SymbolSpec:
    """Everything the sizer needs to know about a tradable instrument.

    Field names mirror the XTB ``getSymbol`` record but stay generic enough for
    any other broker adapter.
    """

    symbol: str
    description: str = ""
    category: str = ""
    currency: str = ""             # currency of the instrument
    currency_profit: str = ""      # currency in which the P&L is expressed
    contract_size: float = 1.0
    lot_min: float = 0.01
    lot_max: float = 100.0
    lot_step: float = 0.01
    tick_size: float = 0.00001
    tick_value: float = 1.0        # money per tick_size move, per 1 lot
    precision: int = 5             # number of decimals of the price
    leverage: float = 100.0        # percentage form is normalised on load
    spread_raw: float = 0.0
    stops_level: float = 0.0       # min distance (in points) for SL/TP
    time_string: str = ""
    trading_enabled: bool = True

    @property
    def point(self) -> float:
        """Value of one 'point' = 1 unit of the last price decimal."""
        return 10.0 ** (-self.precision)

    @property
    def money_per_price_unit_per_lot(self) -> float:
        """Money (in ``currency_profit``) earned per 1.0 of price move, per lot.

        Falls back to ``contract_size`` when tick data is missing, which is the
        correct identity for most CFD instruments.
        """
        if self.tick_size > 0 and self.tick_value > 0:
            return self.tick_value / self.tick_size
        return self.contract_size or 1.0

    @property
    def min_stop_distance(self) -> float:
        """Broker-imposed minimum distance between price and SL/TP, in price units."""
        return self.stops_level * self.point

    def round_price(self, price: float) -> float:
        return round(price, self.precision)

    def round_volume(self, volume: float) -> float:
        """Floor ``volume`` to a valid lot step (never rounds *up* past a limit)."""
        if self.lot_step <= 0:
            return volume
        steps = math.floor(round(volume / self.lot_step, 9))
        return round(steps * self.lot_step, 9)

    def normalise_volume(self, volume: float) -> float:
        """Clamp to [lot_min, lot_max] after flooring to the lot step."""
        vol = self.round_volume(volume)
        if vol > self.lot_max:
            vol = self.round_volume(self.lot_max)
        return vol


# --------------------------------------------------------------------------- #
# Account / positions
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class AccountState:
    """Snapshot of the trading account (``getMarginLevel`` on XTB)."""

    balance: float
    equity: float
    margin_used: float
    margin_free: float
    currency: str = ""
    margin_level: float = 0.0
    credit: float = 0.0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "balance": self.balance,
            "equity": self.equity,
            "margin_used": self.margin_used,
            "margin_free": self.margin_free,
            "margin_level": self.margin_level,
            "currency": self.currency,
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass(slots=True)
class Position:
    """An open (or recently closed) position as reported by the broker."""

    position_id: int
    symbol: str
    direction: Direction
    volume: float
    open_price: float
    open_time: datetime | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    profit: float = 0.0
    commission: float = 0.0
    swap: float = 0.0
    close_price: float | None = None
    close_time: datetime | None = None
    closed: bool = False
    comment: str = ""
    order_id: int | None = None

    @property
    def net_profit(self) -> float:
        return self.profit + self.commission + self.swap


# --------------------------------------------------------------------------- #
# Strategy output
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class UnsharpSetup:
    """A validated Lead / Confirmation / Execution sequence sitting on a level."""

    symbol: str
    direction: Direction
    lead: Candle
    confirmation: list[Candle]
    execution: Candle
    level: Level
    atr: float
    #: Extreme of the confirmation zone used as the stop reference.
    zone_extreme: float
    #: Diagnostics, surfaced in the logs and in the journal.
    metrics: dict[str, float] = field(default_factory=dict)

    @property
    def timestamp(self) -> datetime:
        return self.execution.timestamp

    @property
    def entry_reference(self) -> float:
        """Price used to plan the trade: the Execution Candle close."""
        return self.execution.close

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": self.direction.value,
            "lead": self.lead.to_dict(),
            "confirmation": [c.to_dict() for c in self.confirmation],
            "execution": self.execution.to_dict(),
            "level": self.level.to_dict(),
            "atr": round(self.atr, 8),
            "zone_extreme": round(self.zone_extreme, 8),
            "metrics": {k: round(v, 6) for k, v in self.metrics.items()},
        }


@dataclass(slots=True)
class TradePlan:
    """A setup turned into an actionable order (entry / stop / target / volume)."""

    setup: UnsharpSetup
    entry_price: float
    stop_price: float
    target_price: float
    volume: float
    risk_reward: float
    risk_amount: float            # money lost if the stop is hit
    notional_engaged: float       # money committed (margin or exposure, cf. config)
    capital_available: float
    risk_fraction_used: float
    margin_required: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def symbol(self) -> str:
        return self.setup.symbol

    @property
    def direction(self) -> Direction:
        return self.setup.direction

    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry_price - self.stop_price)


@dataclass(slots=True)
class SizingDecision:
    """Result of the position sizing pass (may be a refusal)."""

    accepted: bool
    volume: float = 0.0
    reason: str = ""
    risk_amount: float = 0.0
    notional_engaged: float = 0.0
    capital_available: float = 0.0
    margin_required: float = 0.0
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RejectedSetup:
    """A setup (or near-setup) that did not make it to an order."""

    symbol: str
    timestamp: datetime
    reason: str
    direction: Direction | None = None
    details: dict[str, Any] = field(default_factory=dict)
