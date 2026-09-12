"""Portfolio-level risk guards.

The sizer answers "how big?".  This module answers "should we trade at all?":
daily loss limits, trade counters, cooldowns, position caps.  All state is kept
per trading day and reset by :meth:`RiskGuard.start_day`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from ..config import RiskConfig
from ..models import AccountState, Direction


@dataclass(slots=True)
class GuardVerdict:
    """Outcome of a guard check."""

    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return self.allowed


@dataclass(slots=True)
class DayState:
    """Counters for the current trading day."""

    day: date
    start_equity: float
    trades_taken: int = 0
    realised_pnl: float = 0.0
    wins: int = 0
    losses: int = 0
    halted: bool = False
    halt_reason: str = ""
    last_trade_at: dict[str, datetime] = field(default_factory=dict)
    last_loss_at: dict[str, datetime] = field(default_factory=dict)


class RiskGuard:
    """Enforces the daily and portfolio-level trading limits."""

    def __init__(self, config: RiskConfig) -> None:
        self.config = config
        self.state: DayState | None = None

    # ------------------------------------------------------------------ #
    # Day lifecycle
    # ------------------------------------------------------------------ #
    def start_day(self, day: date, equity: float) -> DayState:
        """Reset the counters for a new trading day."""
        self.state = DayState(day=day, start_equity=max(equity, 0.0))
        return self.state

    def ensure_day(self, now: datetime, equity: float) -> DayState:
        """Start a new day whenever the calendar date changes."""
        today = now.astimezone(timezone.utc).date()
        if self.state is None or self.state.day != today:
            return self.start_day(today, equity)
        return self.state

    # ------------------------------------------------------------------ #
    # Checks
    # ------------------------------------------------------------------ #
    def check(
        self,
        symbol: str,
        direction: Direction,
        now: datetime,
        account: AccountState,
        open_positions: int,
        open_positions_on_symbol: int,
    ) -> GuardVerdict:
        """Global go/no-go before sizing a new trade."""
        cfg = self.config
        state = self.ensure_day(now, account.equity)

        if state.halted:
            return GuardVerdict(False, f"day_halted:{state.halt_reason}")

        if open_positions >= cfg.max_open_positions:
            return GuardVerdict(False, "max_open_positions_reached")

        if open_positions_on_symbol >= cfg.max_positions_per_symbol:
            return GuardVerdict(False, "max_positions_per_symbol_reached")

        if cfg.max_trades_per_day > 0 and state.trades_taken >= cfg.max_trades_per_day:
            return GuardVerdict(False, "max_trades_per_day_reached")

        if state.start_equity > 0:
            floor = cfg.min_equity_fraction * state.start_equity
            if account.equity < floor:
                self.halt("equity_below_daily_floor")
                return GuardVerdict(False, "equity_below_daily_floor")

        # Daily loss / profit limits, evaluated on realised + unrealised equity.
        if state.start_equity > 0:
            change = (account.equity - state.start_equity) / state.start_equity
            if cfg.daily_loss_limit_fraction > 0 and change <= -cfg.daily_loss_limit_fraction:
                self.halt("daily_loss_limit_reached")
                return GuardVerdict(False, "daily_loss_limit_reached")
            if cfg.daily_profit_target_fraction > 0 and change >= cfg.daily_profit_target_fraction:
                self.halt("daily_profit_target_reached")
                return GuardVerdict(False, "daily_profit_target_reached")

        # Cooldowns, per symbol.
        last_loss = state.last_loss_at.get(symbol)
        if last_loss and cfg.cooldown_minutes_after_loss > 0:
            if now - last_loss < timedelta(minutes=cfg.cooldown_minutes_after_loss):
                return GuardVerdict(False, "cooldown_after_loss")

        last_trade = state.last_trade_at.get(symbol)
        if last_trade and cfg.cooldown_minutes_after_trade > 0:
            if now - last_trade < timedelta(minutes=cfg.cooldown_minutes_after_trade):
                return GuardVerdict(False, "cooldown_after_trade")

        return GuardVerdict(True)

    # ------------------------------------------------------------------ #
    # Bookkeeping
    # ------------------------------------------------------------------ #
    def register_trade(self, symbol: str, now: datetime) -> None:
        if self.state is None:
            self.start_day(now.astimezone(timezone.utc).date(), 0.0)
        assert self.state is not None
        self.state.trades_taken += 1
        self.state.last_trade_at[symbol] = now

    def register_close(self, symbol: str, pnl: float, now: datetime) -> None:
        if self.state is None:
            return
        self.state.realised_pnl += pnl
        if pnl < 0:
            self.state.losses += 1
            self.state.last_loss_at[symbol] = now
        elif pnl > 0:
            self.state.wins += 1

    def halt(self, reason: str) -> None:
        """Stop trading for the rest of the day (positions are left untouched)."""
        if self.state is not None:
            self.state.halted = True
            self.state.halt_reason = reason

    @property
    def halted(self) -> bool:
        return bool(self.state and self.state.halted)

    def summary(self) -> dict[str, object]:
        if self.state is None:
            return {}
        return {
            "day": self.state.day.isoformat(),
            "start_equity": round(self.state.start_equity, 2),
            "trades_taken": self.state.trades_taken,
            "realised_pnl": round(self.state.realised_pnl, 2),
            "wins": self.state.wins,
            "losses": self.state.losses,
            "halted": self.state.halted,
            "halt_reason": self.state.halt_reason,
        }
