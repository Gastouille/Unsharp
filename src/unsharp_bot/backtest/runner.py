"""Historical replay of the Unsharp strategy.

The backtester drives the **same** detector, planner, sizer and paper broker as
the live engine, so what you measure here is what the bot would have done -
apart from spread/slippage realism, which is configurable.

Conservative assumptions:

* signals are evaluated on **closed** candles only,
* the entry fills at the open of the candle *after* the Execution Candle,
* when a bar touches both the stop and the target, the **stop** is assumed first,
* the session/timing filter is applied exactly as in live trading.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Sequence

from ..broker.paper import PaperBroker, PaperBrokerConfig, PaperFill
from ..broker.base import OrderRequest
from ..config import BotConfig
from ..models import Candle, Direction, SymbolSpec
from ..risk.guards import RiskGuard
from ..risk.sizing import ProgressivePositionSizer, SizingInputs
from ..strategy.indicators import atr as compute_atr
from ..strategy.levels import LevelDetector
from ..strategy.planner import TradePlanner
from ..strategy.unsharp import UnsharpDetector
from ..engine.clock import SessionClock, TimingFilter

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class BacktestTrade:
    """One completed simulated trade."""

    symbol: str
    direction: Direction
    entry_time: datetime
    entry_price: float
    stop_price: float
    target_price: float
    volume: float
    risk_reward: float
    level_price: float
    level_type: str
    exit_time: datetime | None = None
    exit_price: float | None = None
    pnl: float | None = None
    reason: str = ""
    equity_after: float | None = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "direction": self.direction.value,
            "entry_time": self.entry_time.isoformat(),
            "entry_price": round(self.entry_price, 6),
            "stop_price": round(self.stop_price, 6),
            "target_price": round(self.target_price, 6),
            "volume": self.volume,
            "ratio_rr": round(self.risk_reward, 3),
            "level_used": {"price": round(self.level_price, 6), "type": self.level_type},
            "exit_time": self.exit_time.isoformat() if self.exit_time else None,
            "exit_price": round(self.exit_price, 6) if self.exit_price is not None else None,
            "pnl": round(self.pnl, 2) if self.pnl is not None else None,
            "reason": self.reason,
            "equity_after": round(self.equity_after, 2) if self.equity_after is not None else None,
        }


@dataclass(slots=True)
class BacktestReport:
    """Aggregated results of a replay."""

    starting_equity: float
    ending_equity: float
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    signals_detected: int = 0
    signals_rejected: int = 0
    rejection_reasons: dict[str, int] = field(default_factory=dict)

    # -- statistics ----------------------------------------------------- #
    @property
    def closed_trades(self) -> list[BacktestTrade]:
        return [t for t in self.trades if t.pnl is not None]

    @property
    def wins(self) -> list[BacktestTrade]:
        return [t for t in self.closed_trades if (t.pnl or 0) > 0]

    @property
    def losses(self) -> list[BacktestTrade]:
        return [t for t in self.closed_trades if (t.pnl or 0) <= 0]

    @property
    def win_rate(self) -> float:
        closed = self.closed_trades
        return len(self.wins) / len(closed) if closed else 0.0

    @property
    def profit_factor(self) -> float:
        gains = sum(t.pnl or 0 for t in self.wins)
        pains = abs(sum(t.pnl or 0 for t in self.losses))
        if pains == 0:
            return math.inf if gains > 0 else 0.0
        return gains / pains

    @property
    def total_return(self) -> float:
        if self.starting_equity <= 0:
            return 0.0
        return (self.ending_equity - self.starting_equity) / self.starting_equity

    @property
    def max_drawdown(self) -> float:
        """Largest peak-to-trough fall of the equity curve, as a fraction."""
        peak = -math.inf
        worst = 0.0
        for _, equity in self.equity_curve:
            peak = max(peak, equity)
            if peak > 0:
                worst = min(worst, (equity - peak) / peak)
        return abs(worst)

    @property
    def expectancy(self) -> float:
        closed = self.closed_trades
        return sum(t.pnl or 0 for t in closed) / len(closed) if closed else 0.0

    def summary(self) -> dict:
        return {
            "starting_equity": round(self.starting_equity, 2),
            "ending_equity": round(self.ending_equity, 2),
            "total_return_pct": round(self.total_return * 100, 2),
            "trades": len(self.closed_trades),
            "wins": len(self.wins),
            "losses": len(self.losses),
            "win_rate_pct": round(self.win_rate * 100, 2),
            "profit_factor": (
                round(self.profit_factor, 3) if math.isfinite(self.profit_factor) else "inf"
            ),
            "expectancy": round(self.expectancy, 2),
            "max_drawdown_pct": round(self.max_drawdown * 100, 2),
            "signals_detected": self.signals_detected,
            "signals_rejected": self.signals_rejected,
            "top_rejection_reasons": dict(
                sorted(self.rejection_reasons.items(), key=lambda kv: kv[1], reverse=True)[:8]
            ),
        }

    def format_text(self) -> str:
        summary = self.summary()
        width = max(len(key) for key in summary) + 2
        lines = ["", "=" * 52, "  UNSHARP BACKTEST REPORT", "=" * 52]
        for key, value in summary.items():
            if key == "top_rejection_reasons":
                continue
            lines.append(f"  {key.replace('_', ' '):<{width}} {value}")
        if summary["top_rejection_reasons"]:
            lines.append("  -- why setups were skipped " + "-" * 20)
            for reason, count in summary["top_rejection_reasons"].items():
                lines.append(f"     {reason:<40} {count}")
        lines.append("=" * 52)
        return "\n".join(lines)


class Backtester:
    """Replays historical candles through the live strategy components."""

    def __init__(
        self,
        config: BotConfig,
        starting_equity: float = 10_000.0,
        spread: float = 0.0,
        slippage: float = 0.0,
        commission_per_lot: float = 0.0,
        apply_session_filter: bool = True,
    ) -> None:
        self.config = config
        self.starting_equity = starting_equity
        self.apply_session_filter = apply_session_filter

        self.level_detector = LevelDetector(config.levels)
        self.detector = UnsharpDetector(config.unsharp)
        self.planner = TradePlanner(config.unsharp)
        self.guard = RiskGuard(config.risk)
        self.clock = SessionClock(config.sessions)
        self.timing = TimingFilter(config.timing)

        self.paper_config = PaperBrokerConfig(
            starting_balance=starting_equity,
            default_spread=spread,
            slippage=slippage,
            commission_per_lot=commission_per_lot,
        )

    # ------------------------------------------------------------------ #
    def run(
        self,
        symbol: str,
        candles: Sequence[Candle],
        spec: SymbolSpec,
    ) -> BacktestReport:
        """Replay ``candles`` for one instrument."""
        broker = PaperBroker({symbol: spec}, self.paper_config)
        broker.connect()
        sizer = ProgressivePositionSizer(
            self.config.risk, margin_calculator=broker.get_margin_for
        )

        report = BacktestReport(
            starting_equity=self.starting_equity, ending_equity=self.starting_equity
        )
        self.guard.start_day(candles[0].timestamp.date() if candles else datetime.now(timezone.utc).date(),
                             self.starting_equity)

        warmup = max(
            self.config.levels.scan_window,
            self.config.unsharp.atr_period * 3,
            self.config.unsharp.body_average_window * 2,
        )
        history: list[Candle] = []
        pending_entry: dict | None = None
        open_trades: dict[int, BacktestTrade] = {}

        for index, candle in enumerate(candles):
            # 1. Advance the simulated market (resolves stops and targets).
            fills = broker.feed_candle(symbol, candle)
            for fill in fills:
                self._close_trade(open_trades, fill, report, broker)

            account = broker.get_account_state()
            report.equity_curve.append((candle.timestamp, account.equity))
            self.guard.ensure_day(candle.timestamp, account.equity)

            # 2. A signal from the previous bar fills at this bar's open.
            if pending_entry is not None:
                self._open_trade(
                    broker, sizer, spec, symbol, candle, pending_entry, open_trades, report
                )
                pending_entry = None

            history.append(candle)
            if len(history) <= warmup:
                continue
            if len(history) > self.config.market.history_size:
                history = history[-self.config.market.history_size:]

            # 3. Detect on the just-closed candle.
            atr_value = compute_atr(history, self.config.unsharp.atr_period)
            if atr_value <= 0:
                continue
            levels = self.level_detector.detect(history, atr_value)
            result = self.detector.detect(symbol, history, levels, atr_value)
            if not result.found:
                continue

            setup = result.setup
            assert setup is not None
            report.signals_detected += 1

            # 4. Session / timing filter, exactly as live.
            if self.apply_session_filter:
                status = self.clock.status(candle.timestamp)
                allowed, reason = self.timing.allows(status)
                if not allowed:
                    self._reject(report, reason)
                    continue

            # 5. Risk guards.
            on_symbol = len([t for t in open_trades.values() if t.symbol == symbol])
            verdict = self.guard.check(
                symbol, setup.direction, candle.timestamp, account,
                len(open_trades), on_symbol,
            )
            if not verdict.allowed:
                self._reject(report, verdict.reason)
                continue

            # 6. Geometry.
            geometry, why = self.planner.build(setup, levels, spec)
            if geometry is None:
                self._reject(report, why)
                continue

            pending_entry = {"setup": setup, "geometry": geometry, "levels": levels}

        # Close whatever is still open at the end of the data set.
        for fill in broker.force_close_all("end_of_data"):
            self._close_trade(open_trades, fill, report, broker)

        report.ending_equity = broker.get_account_state().equity
        return report

    # ------------------------------------------------------------------ #
    def _open_trade(
        self,
        broker: PaperBroker,
        sizer: ProgressivePositionSizer,
        spec: SymbolSpec,
        symbol: str,
        candle: Candle,
        pending: dict,
        open_trades: dict[int, BacktestTrade],
        report: BacktestReport,
    ) -> None:
        """Fill the pending signal at this candle's open."""
        setup = pending["setup"]
        geometry = pending["geometry"]
        account = broker.get_account_state()

        # The entry moves to the actual open; keep the risk distance constant.
        entry = candle.open
        risk_per_unit = geometry.risk_per_unit
        stop = spec.round_price(entry - setup.direction.sign * risk_per_unit)
        reward = abs(geometry.target_price - geometry.entry_price)
        target = spec.round_price(entry + setup.direction.sign * reward)

        decision = sizer.size(
            SizingInputs(spec, account, setup.direction, entry, stop)
        )
        if not decision.accepted:
            self._reject(report, decision.reason)
            return

        broker.set_quote(symbol, entry, entry, candle.timestamp)
        result = broker.open_trade(
            OrderRequest(symbol, setup.direction, decision.volume, stop, target, "backtest")
        )
        if not result.accepted or result.position_id is None:
            self._reject(report, result.message or "order_rejected")
            return

        trade = BacktestTrade(
            symbol=symbol,
            direction=setup.direction,
            entry_time=candle.timestamp,
            entry_price=result.price or entry,
            stop_price=stop,
            target_price=target,
            volume=decision.volume,
            risk_reward=geometry.risk_reward,
            level_price=setup.level.price,
            level_type=setup.level.type.value,
        )
        open_trades[result.position_id] = trade
        report.trades.append(trade)
        self.guard.register_trade(symbol, candle.timestamp)

    def _close_trade(
        self,
        open_trades: dict[int, BacktestTrade],
        fill: PaperFill,
        report: BacktestReport,
        broker: PaperBroker,
    ) -> None:
        trade = open_trades.pop(fill.position_id, None)
        if trade is None:
            return
        trade.exit_time = fill.close_time
        trade.exit_price = fill.close_price
        trade.pnl = fill.profit
        trade.reason = fill.reason
        trade.equity_after = broker.get_account_state().equity
        self.guard.register_close(fill.symbol, fill.profit, fill.close_time)

    @staticmethod
    def _reject(report: BacktestReport, reason: str) -> None:
        report.signals_rejected += 1
        report.rejection_reasons[reason] = report.rejection_reasons.get(reason, 0) + 1
