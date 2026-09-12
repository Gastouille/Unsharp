"""Main orchestration loop.

Cycle, repeated once per candle close:

1. Are we inside a configured trading session?  If not, sleep until the next one.
2. Wait for the next candle boundary (+ a small offset for server-side lag).
3. For each symbol: pull the freshly closed bars, rebuild the level map, run the
   Unsharp detector.
4. For a valid setup: build the geometry (stop / target / R-R), run the risk
   guards, size the position, send the order.
5. Reconcile open positions, manage stops, journal everything.

Decisions are taken **on closed candles only**, which is what makes the signal
stream reproducible: a signal emitted at bar *t* is never revised later.
"""

from __future__ import annotations

import logging
import signal as signal_module
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Iterable

from ..broker.base import Broker, BrokerConnectionError, BrokerError
from ..config import BotConfig
from ..journal import SignalJournal
from ..models import (
    AccountState,
    Candle,
    Level,
    Position,
    SignalStatus,
    SymbolSpec,
    TradePlan,
)
from ..risk.guards import RiskGuard
from ..risk.sizing import ProgressivePositionSizer, SizingInputs
from ..strategy.indicators import atr as compute_atr
from ..strategy.levels import LevelDetector
from ..strategy.planner import TradePlanner
from ..strategy.unsharp import UnsharpDetector
from .clock import SessionClock, TimingFilter, next_candle_boundary
from .trader import TradeExecutor

LOGGER = logging.getLogger(__name__)


class SymbolState:
    """Per-symbol rolling state: candles, levels and last processed bar."""

    def __init__(self, symbol: str, spec: SymbolSpec, history_size: int) -> None:
        self.symbol = symbol
        self.spec = spec
        self.candles: deque[Candle] = deque(maxlen=history_size)
        self.daily_candles: list[Candle] = []
        self.levels: list[Level] = []
        self.atr: float = 0.0
        self.last_processed: datetime | None = None
        self.last_level_refresh: datetime | None = None

    def series(self) -> list[Candle]:
        return list(self.candles)

    def merge(self, candles: Iterable[Candle]) -> list[Candle]:
        """Append only genuinely new bars; returns the ones that were added."""
        known = self.candles[-1].timestamp if self.candles else None
        fresh = [c for c in candles if known is None or c.timestamp > known]
        for candle in fresh:
            self.candles.append(candle)
        return fresh

    def session_candles(self, since: datetime | None) -> list[Candle]:
        if since is None:
            return []
        return [c for c in self.candles if c.timestamp >= since]


class UnsharpBot:
    """Wires every component together and runs the trading loop."""

    def __init__(
        self,
        config: BotConfig,
        broker: Broker,
        journal: SignalJournal | None = None,
    ) -> None:
        self.config = config
        self.broker = broker
        self.journal = journal or SignalJournal(
            config.logging, config.signals_path, config.market.timeframe_minutes
        )

        self.clock = SessionClock(config.sessions)
        self.timing = TimingFilter(config.timing)
        self.level_detector = LevelDetector(config.levels)
        self.detector = UnsharpDetector(config.unsharp)
        self.planner = TradePlanner(config.unsharp)
        self.guard = RiskGuard(config.risk)
        self.sizer = ProgressivePositionSizer(
            config.risk, margin_calculator=self._margin_calculator
        )
        self.executor = TradeExecutor(broker, config.execution, self.journal)

        self.states: dict[str, SymbolState] = {}
        #: Signal ids whose realised P&L was already fed to the risk guard.
        self._counted_signals: set[str] = set()
        self._stop_event = threading.Event()
        self._session_started_at: datetime | None = None
        self._flattened_for_session: bool = False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def install_signal_handlers(self) -> None:
        """Ctrl-C / SIGTERM stop the loop cleanly instead of killing the process."""
        def handler(signum, _frame):  # pragma: no cover - signal path
            LOGGER.warning("Signal %s received, shutting down...", signum)
            self._stop_event.set()

        for sig in (signal_module.SIGINT, signal_module.SIGTERM):
            try:
                signal_module.signal(sig, handler)
            except (ValueError, OSError):  # pragma: no cover - not the main thread
                pass

    def stop(self) -> None:
        self._stop_event.set()

    # ------------------------------------------------------------------ #
    # Startup
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Connect, resolve instruments and warm up the candle history."""
        mode = self.config.broker.mode.upper()
        LOGGER.info(
            "Starting Unsharp bot | broker=%s mode=%s dry_run=%s timeframe=%dm symbols=%s",
            self.config.broker.name, mode, self.config.execution.dry_run,
            self.config.market.timeframe_minutes, ", ".join(self.config.market.symbols),
        )
        # Always say which account we are about to authenticate against.  A
        # dry run still logs into the real account, so staying silent about it
        # in dry-run mode is exactly how you end up on 'real' without noticing.
        if self.config.broker.name == "xtb" and mode == "REAL":
            if self.config.execution.dry_run:
                LOGGER.warning(
                    "Connecting to the REAL XTB account (XTB_MODE=real). "
                    "--dry-run means no order will be sent, but this is your live "
                    "account. Set XTB_MODE=demo in your .env to use the demo one."
                )
            else:
                LOGGER.warning(
                    "RUNNING ON A REAL XTB ACCOUNT - real money is at risk. "
                    "Set XTB_MODE=demo to trade the demo account."
                )
        LOGGER.info("Gateway: %s", self.config.broker.main_endpoint
                    if self.config.broker.name == "xtb" else self.config.broker.name)

        self.broker.connect()
        self._resolve_symbols()
        self._warm_up()

        account = self._account()
        LOGGER.info(
            "Account: balance=%.2f equity=%.2f margin=%.2f free=%.2f %s | capital available=%.2f",
            account.balance, account.equity, account.margin_used, account.margin_free,
            account.currency, self.sizer.capital_available(account),
        )
        self.guard.start_day(datetime.now(timezone.utc).date(), account.equity)

        if self.config.broker.use_streaming:
            try:
                self.broker.subscribe_prices(self.states.keys())
            except Exception as exc:  # pragma: no cover
                LOGGER.warning("Price subscription failed: %s", exc)

    def _resolve_symbols(self) -> None:
        """Load the instrument specs for every configured symbol."""
        wanted = list(self.config.market.symbols)
        specs: dict[str, SymbolSpec] = {}
        try:
            for spec in self.broker.get_all_symbols():
                if spec.symbol in wanted:
                    specs[spec.symbol] = spec
        except BrokerError as exc:
            LOGGER.warning("get_all_symbols failed (%s), falling back to per-symbol lookups", exc)

        for symbol in wanted:
            spec = specs.get(symbol)
            if spec is None:
                try:
                    spec = self.broker.get_symbol(symbol)
                except BrokerError as exc:
                    LOGGER.error("Symbol %s is unavailable and will be skipped: %s", symbol, exc)
                    continue
            if self.config.market.skip_disabled_symbols and not spec.trading_enabled:
                LOGGER.warning("Symbol %s is not tradable right now, skipping", symbol)
                continue
            self.states[symbol] = SymbolState(symbol, spec, self.config.market.history_size)
            LOGGER.info(
                "%s ready | lot=[%.2f..%.2f step %.2f] contract=%.2f tickValue=%.5f "
                "precision=%d leverage=%.2f%%",
                symbol, spec.lot_min, spec.lot_max, spec.lot_step, spec.contract_size,
                spec.tick_value, spec.precision, spec.leverage,
            )
        if not self.states:
            raise RuntimeError("No tradable symbol could be resolved: check market.symbols")

    def _warm_up(self) -> None:
        """Pull enough closed candles to compute the ATR and the level map."""
        period = self.config.market.timeframe_minutes
        count = max(self.config.market.warmup_candles, self.config.levels.scan_window + 50)
        for symbol, state in self.states.items():
            try:
                candles = self.broker.get_candles(symbol, period, count=count)
                state.merge(candles)
                if self.config.market.use_daily_levels:
                    state.daily_candles = self.broker.get_candles(symbol, 1440, count=10)
                self._refresh_indicators(state)
                LOGGER.info(
                    "%s warmed up with %d candles (ATR=%.5f, %d levels)",
                    symbol, len(state.candles), state.atr, len(state.levels),
                )
            except BrokerError as exc:
                LOGGER.error("Warm-up failed for %s: %s", symbol, exc)

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #
    def run(self) -> None:
        """Block until stopped, processing one candle boundary at a time."""
        self.install_signal_handlers()
        try:
            self.start()
        except Exception as exc:
            LOGGER.exception("Startup failed: %s", exc)
            raise

        LOGGER.info("Entering the main loop (Ctrl-C to stop)")
        try:
            while not self._stop_event.is_set():
                try:
                    self._tick()
                except BrokerConnectionError as exc:
                    LOGGER.error("Connection lost: %s - reconnecting", exc)
                    self._safe_reconnect()
                except BrokerError as exc:
                    LOGGER.error("Broker error in the main loop: %s", exc)
                    self._sleep(5.0)
                except Exception as exc:  # pragma: no cover - last-resort guard
                    LOGGER.exception("Unexpected error in the main loop: %s", exc)
                    self._sleep(5.0)
        finally:
            self.shutdown()

    def _tick(self) -> None:
        """One iteration: wait for the next bar, then scan every symbol."""
        now = datetime.now(timezone.utc)
        status = self.clock.status(now)

        if not status.is_open:
            self._handle_closed_market(status, now)
            return

        if self._session_started_at is None:
            self._on_session_open(status, now)

        # Flatten before the session close, if configured.
        if self._should_flatten(status):
            self.executor.flatten_all("session_end")
            self._flattened_for_session = True

        # Sleep until just after the next candle close.
        boundary = next_candle_boundary(
            now, self.config.market.timeframe_minutes, self.config.market.poll_offset_seconds
        )
        wait = (boundary - now).total_seconds()
        if wait > 0:
            LOGGER.debug("Waiting %.1fs for the next %dm candle", wait, self.config.market.timeframe_minutes)
            if self._sleep(wait):
                return

        self.broker.ensure_connected()
        account = self._account()
        self.guard.ensure_day(datetime.now(timezone.utc), account.equity)

        positions = self._positions()
        self.executor.reconcile(positions)
        self._register_closed_trades()
        positions = self._positions()
        self.executor.manage_positions(
            positions, {symbol: state.atr for symbol, state in self.states.items()}
        )

        for symbol, state in self.states.items():
            try:
                self._scan_symbol(state, account, positions, status)
            except BrokerError as exc:
                LOGGER.error("Scan failed for %s: %s", symbol, exc)
            except Exception as exc:  # pragma: no cover
                LOGGER.exception("Unexpected error while scanning %s: %s", symbol, exc)

    # ------------------------------------------------------------------ #
    # Symbol scan
    # ------------------------------------------------------------------ #
    def _scan_symbol(
        self,
        state: SymbolState,
        account: AccountState,
        positions: list[Position],
        status,
    ) -> None:
        """Fetch new bars for one symbol and evaluate the Unsharp pattern."""
        period = self.config.market.timeframe_minutes
        candles = self.broker.get_candles(state.symbol, period, count=60)
        fresh = state.merge(candles)
        if not fresh:
            LOGGER.debug("%s: no new closed candle", state.symbol)
            return

        last = state.candles[-1]
        if state.last_processed is not None and last.timestamp <= state.last_processed:
            return
        state.last_processed = last.timestamp

        self._refresh_indicators(state, status)
        if state.atr <= 0:
            LOGGER.debug("%s: ATR not ready", state.symbol)
            return

        result = self.detector.detect(state.symbol, state.series(), state.levels, state.atr)
        if not result.found:
            for rejection in result.rejections:
                LOGGER.debug("%s: no setup (%s)", state.symbol, rejection.reason)
            return

        setup = result.setup
        assert setup is not None
        LOGGER.info(
            "SETUP %s %s @ %s | level %.5f (%s) | lead body %.2f ATR | zone %d candle(s)",
            setup.direction.value, setup.symbol, setup.timestamp.isoformat(),
            setup.level.price, setup.level.type.value,
            setup.metrics.get("lead_body_atr", 0.0), len(setup.confirmation),
        )
        record = self.journal.record_detection(
            setup, session=status.session.name if status.session else None
        )
        record.account_before = account.to_dict()

        # --- Timing filter -------------------------------------------------- #
        allowed, timing_reason = self.timing.allows(status)
        if not allowed:
            LOGGER.info("Setup on %s skipped: %s", setup.symbol, timing_reason)
            self.journal.update_status(record, SignalStatus.REJECTED, reason=timing_reason)
            return

        # --- Risk guards ---------------------------------------------------- #
        on_symbol = sum(1 for p in positions if p.symbol == setup.symbol)
        verdict = self.guard.check(
            setup.symbol, setup.direction, datetime.now(timezone.utc),
            account, len(positions), on_symbol,
        )
        if not verdict.allowed:
            LOGGER.info("Setup on %s skipped: %s", setup.symbol, verdict.reason)
            self.journal.update_status(record, SignalStatus.REJECTED, reason=verdict.reason)
            return

        # --- Trade geometry -------------------------------------------------- #
        entry_override = None
        if self.config.unsharp.entry_mode == "next_open":
            quote = self.broker.get_quote(setup.symbol)
            if quote is not None:
                entry_override = quote.price_for(setup.direction)

        geometry, why = self.planner.build(setup, state.levels, state.spec, entry_override)
        if geometry is None:
            LOGGER.info("Setup on %s skipped: %s", setup.symbol, why)
            self.journal.update_status(record, SignalStatus.REJECTED, reason=why)
            return

        # --- Sizing ---------------------------------------------------------- #
        decision = self.sizer.size(
            SizingInputs(
                symbol_spec=state.spec,
                account=account,
                direction=setup.direction,
                entry_price=geometry.entry_price,
                stop_price=geometry.stop_price,
            )
        )
        if not decision.accepted:
            LOGGER.info(
                "Setup on %s skipped by the sizer: %s %s",
                setup.symbol, decision.reason, decision.notes,
            )
            self.journal.update_status(record, SignalStatus.REJECTED, reason=decision.reason)
            return

        plan = TradePlan(
            setup=setup,
            entry_price=geometry.entry_price,
            stop_price=geometry.stop_price,
            target_price=geometry.target_price,
            volume=decision.volume,
            risk_reward=geometry.risk_reward,
            risk_amount=decision.risk_amount,
            notional_engaged=decision.notional_engaged,
            capital_available=decision.capital_available,
            risk_fraction_used=self.config.risk.risk_fraction_per_trade,
            margin_required=decision.margin_required,
            notes=list(geometry.notes) + list(decision.notes),
        )
        self.journal.attach_plan(record, plan)

        LOGGER.info(
            "PLAN %s %s | entry=%.5f stop=%.5f target=%.5f | vol=%.4f R/R=%.2f "
            "risk=%.2f%s notional=%.2f capital=%.2f",
            plan.direction.value, plan.symbol, plan.entry_price, plan.stop_price,
            plan.target_price, plan.volume, plan.risk_reward, plan.risk_amount,
            f" ({account.currency})" if account.currency else "",
            plan.notional_engaged, plan.capital_available,
        )

        # --- Execution -------------------------------------------------------- #
        outcome = self.executor.execute(plan, record, state.spec)
        if outcome.sent:
            self.guard.register_trade(setup.symbol, datetime.now(timezone.utc))

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _refresh_indicators(self, state: SymbolState, status=None) -> None:
        """Recompute the ATR and rebuild the level map for one symbol."""
        candles = state.series()
        if len(candles) < 3:
            return
        state.atr = compute_atr(candles, self.config.unsharp.atr_period)
        session_candles = (
            state.session_candles(self._session_started_at)
            if self.config.levels.include_session_extremes
            else None
        )
        state.levels = self.level_detector.detect(
            candles,
            atr_value=state.atr,
            daily_candles=state.daily_candles or None,
            session_candles=session_candles,
        )
        state.last_level_refresh = datetime.now(timezone.utc)

    def _account(self) -> AccountState:
        try:
            return self.broker.get_account_state()
        except BrokerError as exc:
            LOGGER.error("Could not read the account state: %s", exc)
            return AccountState(0.0, 0.0, 0.0, 0.0)

    def _positions(self) -> list[Position]:
        try:
            return self.broker.get_trades(opened_only=True)
        except BrokerError as exc:
            LOGGER.error("Could not list open positions: %s", exc)
            return []

    def _margin_calculator(self, symbol: str, volume: float) -> float | None:
        try:
            return self.broker.get_margin_for(symbol, volume)
        except BrokerError:
            return None

    def _register_closed_trades(self) -> None:
        """Feed realised P&L into the daily risk guard (cooldowns, loss limit).

        Bookkeeping is kept in memory rather than in the journal record, so the
        ``notes`` field stays meaningful to a human reading the day's signals.
        """
        for record in self.journal.all_records():
            if record.status is not SignalStatus.CLOSED or record.pnl is None:
                continue
            if record.signal_id in self._counted_signals:
                continue
            self._counted_signals.add(record.signal_id)
            self.guard.register_close(
                record.symbol, record.pnl, record.close_time or datetime.now(timezone.utc)
            )

    def _on_session_open(self, status, now: datetime) -> None:
        session_name = status.session.name if status.session else "?"
        LOGGER.info(
            "Session '%s' is open (local %s)",
            session_name,
            status.local_time.strftime("%Y-%m-%d %H:%M %Z") if status.local_time else "?",
        )
        self._session_started_at = now - timedelta(minutes=status.minutes_since_open or 0)
        self._flattened_for_session = False

    def _should_flatten(self, status) -> bool:
        if not self.config.execution.flatten_at_session_end or self._flattened_for_session:
            return False
        if status.session is None or status.minutes_to_close is None:
            return False
        return status.minutes_to_close <= status.session.flatten_before_close_minutes

    def _handle_closed_market(self, status, now: datetime) -> None:
        """Idle outside sessions, waking up shortly before the next open."""
        if self._session_started_at is not None:
            LOGGER.info("Session closed. %s", self.journal.daily_summary())
            LOGGER.info("Risk guard: %s", self.guard.summary())
            if self.config.execution.flatten_at_session_end and not self._flattened_for_session:
                self.executor.flatten_all("session_end")
            self._session_started_at = None
            self._flattened_for_session = False

        next_open = status.next_open
        if next_open is None:
            LOGGER.warning("No upcoming session found in the next 14 days, sleeping 1h")
            self._sleep(3600)
            return
        wait = max(5.0, (next_open - now).total_seconds() - 30)
        LOGGER.info(
            "Market closed. Next session at %s (in %.1f min)",
            next_open.isoformat(), wait / 60.0,
        )
        # Cap the sleep so a config reload or Ctrl-C is picked up reasonably fast.
        self._sleep(min(wait, 900.0))

    def _safe_reconnect(self) -> None:
        delay = self.config.broker.reconnect_base_delay
        for attempt in range(1, self.config.broker.reconnect_max_attempts + 1):
            if self._stop_event.is_set():
                return
            try:
                self.broker.ensure_connected()
                LOGGER.info("Reconnected to the broker")
                if self.config.broker.use_streaming:
                    self.broker.subscribe_prices(self.states.keys())
                return
            except Exception as exc:
                LOGGER.warning(
                    "Reconnect attempt %d/%d failed: %s",
                    attempt, self.config.broker.reconnect_max_attempts, exc,
                )
                self._sleep(delay)
                delay = min(delay * 2, self.config.broker.reconnect_max_delay)
        LOGGER.error("Could not reconnect, stopping the bot")
        self._stop_event.set()

    def _sleep(self, seconds: float) -> bool:
        """Interruptible sleep.  Returns True when the bot was asked to stop."""
        return self._stop_event.wait(max(0.0, seconds))

    def shutdown(self) -> None:
        LOGGER.info("Shutting down...")
        try:
            LOGGER.info("Daily summary: %s", self.journal.daily_summary())
            LOGGER.info("Risk guard: %s", self.guard.summary())
        except Exception:  # pragma: no cover
            pass
        try:
            self.broker.disconnect()
        except Exception as exc:  # pragma: no cover
            LOGGER.warning("Error while disconnecting: %s", exc)
        LOGGER.info("Bot stopped")
