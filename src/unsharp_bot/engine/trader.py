"""Order execution: turns a :class:`TradePlan` into a live position.

Responsibilities kept here (and out of the strategy):

* slippage control between the planned entry and the live quote,
* attaching the stop-loss / take-profit to the order itself, so a crash of the
  bot never leaves a naked position,
* break-even and ATR trailing stop management,
* reconciliation between broker positions and journal records.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from ..broker.base import Broker, BrokerError, OrderRequest, OrderResult
from ..config import ExecutionConfig
from ..journal import SignalJournal, SignalRecord
from ..models import Position, SignalStatus, SymbolSpec, TradePlan

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ExecutionOutcome:
    """Result of an execution attempt."""

    sent: bool
    reason: str = ""
    position_id: int | None = None
    fill_price: float | None = None


class TradeExecutor:
    """Sends orders and manages live positions."""

    def __init__(
        self,
        broker: Broker,
        config: ExecutionConfig,
        journal: SignalJournal,
    ) -> None:
        self.broker = broker
        self.config = config
        self.journal = journal
        #: Positions that already reached break-even (so we only move the stop once).
        self._breakeven_done: set[int] = set()

    # ------------------------------------------------------------------ #
    # Opening
    # ------------------------------------------------------------------ #
    def execute(
        self,
        plan: TradePlan,
        record: SignalRecord,
        spec: SymbolSpec,
    ) -> ExecutionOutcome:
        """Validate the live price, then send the market order."""
        symbol = plan.symbol
        direction = plan.direction

        if self.config.dry_run:
            LOGGER.info(
                "[DRY-RUN] would open %s %s vol=%.4f entry=%.5f sl=%.5f tp=%.5f rr=%.2f "
                "risk=%.2f notional=%.2f",
                direction.value, symbol, plan.volume, plan.entry_price,
                plan.stop_price, plan.target_price, plan.risk_reward,
                plan.risk_amount, plan.notional_engaged,
            )
            self.journal.update_status(record, SignalStatus.CANCELLED, reason="dry_run")
            return ExecutionOutcome(False, "dry_run")

        # --- Slippage guard ------------------------------------------------ #
        quote = self.broker.get_quote(symbol)
        if quote is None:
            self.journal.update_status(record, SignalStatus.REJECTED, reason="no_quote_at_execution")
            return ExecutionOutcome(False, "no_quote_at_execution")

        live_price = quote.price_for(direction)
        slippage = abs(live_price - plan.entry_price)
        max_slippage = self.config.max_entry_slippage_atr * plan.setup.atr
        if max_slippage > 0 and slippage > max_slippage:
            reason = f"slippage_too_large:{slippage:.5f}>{max_slippage:.5f}"
            LOGGER.warning("Skipping %s %s: %s", direction.value, symbol, reason)
            self.journal.update_status(record, SignalStatus.CANCELLED, reason=reason)
            return ExecutionOutcome(False, reason)

        # The stop distance is what defines the risk, so keep it constant and
        # let the entry drift with the market.
        risk_per_unit = plan.risk_per_unit
        stop = spec.round_price(live_price - direction.sign * risk_per_unit)
        reward = abs(plan.target_price - plan.entry_price)
        target = spec.round_price(live_price + direction.sign * reward)

        request = OrderRequest(
            symbol=symbol,
            direction=direction,
            volume=plan.volume,
            stop_loss=stop if self.config.attach_sl_tp else None,
            take_profit=target if self.config.attach_sl_tp else None,
            comment=f"{self.config.order_comment_prefix}-{record.signal_id[-6:]}",
        )

        self.journal.update_status(record, SignalStatus.PENDING, reason="order_sent")
        try:
            result: OrderResult = self.broker.open_trade(request)
        except BrokerError as exc:
            LOGGER.error("Order failed for %s: %s", symbol, exc)
            self.journal.update_status(record, SignalStatus.ERROR, reason=str(exc))
            return ExecutionOutcome(False, str(exc))

        if not result.accepted:
            LOGGER.error("Order rejected for %s: %s", symbol, result.message)
            self.journal.update_status(record, SignalStatus.ERROR, reason=result.message)
            return ExecutionOutcome(False, result.message)

        fill_price = result.price if result.price else live_price
        self.journal.update_status(
            record,
            SignalStatus.OPEN,
            position_id=result.position_id,
            order_id=result.order_id,
            entry_price=fill_price,
            stop_price=stop,
            target_price=target,
        )
        LOGGER.info(
            "ORDER SENT %s %s vol=%.4f @ %.5f sl=%.5f tp=%.5f (position=%s)",
            direction.value, symbol, plan.volume, fill_price, stop, target, result.position_id,
        )
        return ExecutionOutcome(True, "ok", result.position_id, fill_price)

    # ------------------------------------------------------------------ #
    # Managing open positions
    # ------------------------------------------------------------------ #
    def manage_positions(self, positions: list[Position], atr_by_symbol: dict[str, float]) -> None:
        """Apply break-even and trailing-stop rules to the live positions."""
        if self.config.dry_run:
            return
        if self.config.breakeven_at_r <= 0 and self.config.trailing_atr <= 0:
            return

        for position in positions:
            record = self.journal.find_by_position(position.position_id)
            if record is None or record.entry_price is None or record.stop_price is None:
                continue
            quote = self.broker.get_quote(position.symbol)
            if quote is None:
                continue

            price = quote.exit_price_for(position.direction)
            initial_risk = abs(record.entry_price - record.stop_price)
            if initial_risk <= 0:
                continue
            progress = (price - record.entry_price) * position.direction.sign / initial_risk

            new_stop: float | None = None

            # 1. Break-even once the trade has paid for itself.
            if (
                self.config.breakeven_at_r > 0
                and progress >= self.config.breakeven_at_r
                and position.position_id not in self._breakeven_done
            ):
                new_stop = record.entry_price
                self._breakeven_done.add(position.position_id)

            # 2. ATR trailing stop, only ever in the favourable direction.
            if self.config.trailing_atr > 0 and progress >= max(self.config.breakeven_at_r, 1.0):
                atr_value = atr_by_symbol.get(position.symbol, 0.0)
                if atr_value > 0:
                    trailed = price - position.direction.sign * self.config.trailing_atr * atr_value
                    current = position.stop_loss if position.stop_loss else record.stop_price
                    # Only ever ratchet the stop towards the trade.
                    if (trailed - current) * position.direction.sign > 0:
                        if new_stop is None:
                            new_stop = trailed
                        elif (trailed - new_stop) * position.direction.sign > 0:
                            new_stop = trailed

            if new_stop is None:
                continue
            current_stop = position.stop_loss if position.stop_loss else record.stop_price
            if (new_stop - current_stop) * position.direction.sign <= 0:
                continue

            try:
                result = self.broker.modify_trade(position, stop_loss=new_stop)
                if result.accepted:
                    LOGGER.info(
                        "Stop moved on %s #%s: %.5f -> %.5f (%.2fR)",
                        position.symbol, position.position_id, current_stop, new_stop, progress,
                    )
                    record.stop_price = new_stop
            except BrokerError as exc:
                LOGGER.warning("Could not move the stop on #%s: %s", position.position_id, exc)

    # ------------------------------------------------------------------ #
    # Closing / reconciliation
    # ------------------------------------------------------------------ #
    def close_position(self, position: Position, reason: str = "manual") -> bool:
        """Close a position and mark the matching journal record."""
        if self.config.dry_run:
            return False
        try:
            result = self.broker.close_trade(position)
        except BrokerError as exc:
            LOGGER.error("Could not close #%s: %s", position.position_id, exc)
            return False
        if not result.accepted:
            LOGGER.error("Close rejected for #%s: %s", position.position_id, result.message)
            return False
        record = self.journal.find_by_position(position.position_id)
        if record is not None:
            self.journal.update_status(
                record,
                SignalStatus.CLOSED,
                reason=reason,
                pnl=position.net_profit,
                close_price=result.price,
                close_time=datetime.now(timezone.utc),
            )
        LOGGER.info("Position #%s closed (%s)", position.position_id, reason)
        return True

    def flatten_all(self, reason: str = "session_end") -> int:
        """Close every open position (used at the session close)."""
        closed = 0
        try:
            positions = self.broker.get_trades(opened_only=True)
        except BrokerError as exc:
            LOGGER.error("Could not list positions to flatten: %s", exc)
            return 0
        for position in positions:
            if self.close_position(position, reason):
                closed += 1
        if closed:
            LOGGER.info("Flattened %d position(s) (%s)", closed, reason)
        return closed

    def reconcile(self, positions: list[Position]) -> list[SignalRecord]:
        """Detect positions that closed on the broker side (stop or target hit).

        Returns the journal records that just moved to ``CLOSED``.
        """
        live_ids = {position.position_id for position in positions}
        closed_records: list[SignalRecord] = []

        for record in self.journal.open_records():
            if record.position_id is None:
                continue
            if record.position_id in live_ids:
                continue
            pnl, close_price, close_time = self._lookup_closed_trade(record.position_id)
            reason = self._infer_close_reason(record, close_price)
            self.journal.update_status(
                record,
                SignalStatus.CLOSED,
                reason=reason,
                pnl=pnl,
                close_price=close_price,
                close_time=close_time or datetime.now(timezone.utc),
            )
            self._breakeven_done.discard(record.position_id)
            closed_records.append(record)
            LOGGER.info(
                "Position #%s closed by the broker: %s pnl=%s",
                record.position_id, reason, "n/a" if pnl is None else f"{pnl:.2f}",
            )
        return closed_records

    def _lookup_closed_trade(
        self, position_id: int
    ) -> tuple[float | None, float | None, datetime | None]:
        """Fetch the realised P&L of a position from the broker trade history."""
        history_getter = getattr(self.broker, "get_trades_history", None)
        if history_getter is None:
            return None, None, None
        try:
            start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            for trade in history_getter(start):
                if trade.position_id == position_id:
                    return trade.net_profit, trade.close_price, trade.close_time
        except BrokerError as exc:
            LOGGER.debug("Trade history lookup failed for #%s: %s", position_id, exc)
        return None, None, None

    @staticmethod
    def _infer_close_reason(record: SignalRecord, close_price: float | None) -> str:
        """Guess whether the stop or the target was hit, from the close price."""
        if close_price is None or record.stop_price is None or record.target_price is None:
            return "closed_by_broker"
        distance_to_stop = abs(close_price - record.stop_price)
        distance_to_target = abs(close_price - record.target_price)
        if distance_to_stop <= distance_to_target:
            return "stop_loss"
        return "take_profit"
