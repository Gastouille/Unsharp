"""In-process simulated broker.

Two uses:

* ``broker.name: paper`` runs the whole engine offline against a candle feed
  (CSV or synthetic), with no credentials and no network - the fastest way to
  smoke-test a configuration change.
* the backtester drives the very same class, so a strategy validated in a
  backtest and one running live go through identical sizing and order code.

Fills are intentionally pessimistic: a configurable spread and slippage are
applied, and when a candle touches both the stop and the target the **stop is
assumed to trigger first**.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable

from ..models import AccountState, Candle, Direction, Position, SymbolSpec
from .base import Broker, OrderRequest, OrderResult, Quote

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class PaperFill:
    """Record of a simulated position close, for reporting."""

    position_id: int
    symbol: str
    direction: Direction
    volume: float
    open_price: float
    close_price: float
    open_time: datetime
    close_time: datetime
    profit: float
    reason: str


@dataclass(slots=True)
class PaperBrokerConfig:
    """Simulation knobs."""

    starting_balance: float = 10_000.0
    currency: str = "EUR"
    #: Spread applied around the mid price, in price units (per symbol override).
    default_spread: float = 0.0
    #: Extra adverse slippage on entry and exit, in price units.
    slippage: float = 0.0
    #: Commission charged per lot, per side, in account currency.
    commission_per_lot: float = 0.0
    #: Conservative assumption when a bar touches both SL and TP.
    stop_first_on_ambiguous_bar: bool = True


class PaperBroker(Broker):
    """Simulated broker driven by an externally advanced clock.

    The owner of the instance is responsible for calling :meth:`feed_candle`
    (backtest) or :meth:`set_quote` (live paper trading on real quotes).
    """

    def __init__(
        self,
        specs: dict[str, SymbolSpec] | None = None,
        config: PaperBrokerConfig | None = None,
        candle_provider: Callable[[str, int, int], list[Candle]] | None = None,
    ) -> None:
        self.config = config or PaperBrokerConfig()
        self.specs: dict[str, SymbolSpec] = dict(specs or {})
        self.candle_provider = candle_provider

        self.balance = self.config.starting_balance
        self.equity = self.config.starting_balance
        self._positions: dict[int, Position] = {}
        self._quotes: dict[str, Quote] = {}
        self._history: dict[str, list[Candle]] = {}
        self._fills: list[PaperFill] = []
        self._id_counter = itertools.count(1)
        self._now: datetime = datetime.now(timezone.utc)
        self._connected = False

    # ------------------------------------------------------------------ #
    # Connection (trivial)
    # ------------------------------------------------------------------ #
    def connect(self) -> None:
        self._connected = True
        LOGGER.info("Paper broker ready (balance=%.2f %s)", self.balance, self.config.currency)

    def disconnect(self) -> None:
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------ #
    # Simulation clock and feed
    # ------------------------------------------------------------------ #
    @property
    def now(self) -> datetime:
        return self._now

    def set_now(self, moment: datetime) -> None:
        self._now = moment

    def register_symbol(self, spec: SymbolSpec) -> None:
        self.specs[spec.symbol] = spec

    def set_quote(self, symbol: str, bid: float, ask: float, timestamp: datetime | None = None) -> None:
        self._quotes[symbol] = Quote(symbol, bid, ask, timestamp or self._now)

    def feed_candle(self, symbol: str, candle: Candle) -> list[PaperFill]:
        """Append a candle, advance the clock and resolve open positions.

        Returns the positions closed by this bar (stop or target hit).
        """
        self._history.setdefault(symbol, []).append(candle)
        self._now = candle.timestamp
        spread = self._spread(symbol)
        self.set_quote(symbol, candle.close - spread / 2, candle.close + spread / 2, candle.timestamp)
        fills = self._resolve_positions(symbol, candle)
        self._mark_to_market()
        return fills

    def _spread(self, symbol: str) -> float:
        spec = self.specs.get(symbol)
        if spec is not None and spec.spread_raw > 0:
            return spec.spread_raw
        return self.config.default_spread

    # ------------------------------------------------------------------ #
    # Reference data
    # ------------------------------------------------------------------ #
    def get_all_symbols(self) -> list[SymbolSpec]:
        return list(self.specs.values())

    def get_symbol(self, symbol: str) -> SymbolSpec:
        spec = self.specs.get(symbol)
        if spec is None:
            # Neutral default so a paper run never dies on an unknown symbol.
            spec = SymbolSpec(symbol=symbol)
            self.specs[symbol] = spec
        return spec

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        return self.get_symbol(symbol)

    def get_candles(
        self,
        symbol: str,
        period_minutes: int,
        count: int | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Candle]:
        if self.candle_provider is not None:
            return self.candle_provider(symbol, period_minutes, count or 200)
        candles = self._history.get(symbol, [])
        return candles[-count:] if count else list(candles)

    def get_quote(self, symbol: str) -> Quote | None:
        return self._quotes.get(symbol)

    def subscribe_prices(
        self,
        symbols: Iterable[str],
        callback: Callable[[Quote], None] | None = None,
    ) -> None:
        return None

    # ------------------------------------------------------------------ #
    # Account
    # ------------------------------------------------------------------ #
    def get_account_state(self) -> AccountState:
        self._mark_to_market()
        margin_used = sum(self._margin_of(p) for p in self._positions.values())
        return AccountState(
            balance=self.balance,
            equity=self.equity,
            margin_used=margin_used,
            margin_free=max(0.0, self.equity - margin_used),
            currency=self.config.currency,
            margin_level=(self.equity / margin_used * 100.0) if margin_used > 0 else 0.0,
            timestamp=self._now,
        )

    def get_margin_for(self, symbol: str, volume: float) -> float | None:
        spec = self.get_symbol(symbol)
        quote = self._quotes.get(symbol)
        price = quote.mid if quote else 0.0
        if price <= 0:
            return None
        leverage_ratio = 100.0 / spec.leverage if 0 < spec.leverage <= 100 else max(spec.leverage, 1.0)
        return abs(volume) * price * (spec.contract_size or 1.0) / leverage_ratio

    def _margin_of(self, position: Position) -> float:
        margin = self.get_margin_for(position.symbol, position.volume)
        return margin or 0.0

    # ------------------------------------------------------------------ #
    # Trading
    # ------------------------------------------------------------------ #
    def open_trade(self, request: OrderRequest) -> OrderResult:
        spec = self.get_symbol(request.symbol)
        quote = self._quotes.get(request.symbol)
        if quote is None:
            return OrderResult(False, message="no_quote_available")

        price = quote.price_for(request.direction) + request.direction.sign * self.config.slippage
        price = spec.round_price(price)
        position_id = next(self._id_counter)
        position = Position(
            position_id=position_id,
            symbol=request.symbol,
            direction=request.direction,
            volume=request.volume,
            open_price=price,
            open_time=self._now,
            stop_loss=request.stop_loss,
            take_profit=request.take_profit,
            commission=-self.config.commission_per_lot * request.volume,
            comment=request.comment,
            order_id=position_id,
        )
        self._positions[position_id] = position
        LOGGER.info(
            "[paper] OPEN %s %s vol=%.4f @ %.5f sl=%s tp=%s",
            request.direction.value, request.symbol, request.volume, price,
            request.stop_loss, request.take_profit,
        )
        return OrderResult(True, order_id=position_id, position_id=position_id, price=price)

    def close_trade(self, position: Position, volume: float | None = None) -> OrderResult:
        live = self._positions.get(position.position_id)
        if live is None:
            return OrderResult(False, message="unknown_position")
        quote = self._quotes.get(live.symbol)
        price = quote.exit_price_for(live.direction) if quote else live.open_price
        fill = self._close(live, price, "manual")
        return OrderResult(True, order_id=live.position_id, position_id=live.position_id, price=fill.close_price)

    def modify_trade(
        self,
        position: Position,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> OrderResult:
        live = self._positions.get(position.position_id)
        if live is None:
            return OrderResult(False, message="unknown_position")
        if stop_loss is not None:
            live.stop_loss = stop_loss
        if take_profit is not None:
            live.take_profit = take_profit
        return OrderResult(True, order_id=live.position_id, position_id=live.position_id)

    def get_trades(self, opened_only: bool = True) -> list[Position]:
        return list(self._positions.values())

    # ------------------------------------------------------------------ #
    # Position resolution
    # ------------------------------------------------------------------ #
    def _resolve_positions(self, symbol: str, candle: Candle) -> list[PaperFill]:
        """Check every open position on ``symbol`` against the new bar."""
        fills: list[PaperFill] = []
        for position in list(self._positions.values()):
            if position.symbol != symbol:
                continue
            stop, target = position.stop_loss, position.take_profit
            hit_stop = stop is not None and (
                candle.low <= stop if position.direction is Direction.LONG
                else candle.high >= stop
            )
            hit_target = target is not None and (
                candle.high >= target if position.direction is Direction.LONG
                else candle.low <= target
            )
            if hit_stop and hit_target:
                # Ambiguous bar: assume the worst case unless told otherwise.
                if self.config.stop_first_on_ambiguous_bar:
                    hit_target = False
                else:
                    hit_stop = False
            if hit_stop:
                fills.append(self._close(position, stop, "stop_loss"))  # type: ignore[arg-type]
            elif hit_target:
                fills.append(self._close(position, target, "take_profit"))  # type: ignore[arg-type]
        return fills

    def _close(self, position: Position, price: float, reason: str) -> PaperFill:
        spec = self.get_symbol(position.symbol)
        money_per_unit = spec.money_per_price_unit_per_lot
        gross = (price - position.open_price) * position.direction.sign * position.volume * money_per_unit
        commission = position.commission - self.config.commission_per_lot * position.volume
        profit = gross + commission

        position.close_price = price
        position.close_time = self._now
        position.closed = True
        position.profit = gross
        position.commission = commission

        self.balance += profit
        self._positions.pop(position.position_id, None)

        fill = PaperFill(
            position_id=position.position_id,
            symbol=position.symbol,
            direction=position.direction,
            volume=position.volume,
            open_price=position.open_price,
            close_price=price,
            open_time=position.open_time or self._now,
            close_time=self._now,
            profit=profit,
            reason=reason,
        )
        self._fills.append(fill)
        LOGGER.info(
            "[paper] CLOSE %s %s @ %.5f (%s) pnl=%.2f",
            position.direction.value, position.symbol, price, reason, profit,
        )
        return fill

    def _mark_to_market(self) -> None:
        """Recompute equity from the open positions' floating P&L."""
        floating = 0.0
        for position in self._positions.values():
            quote = self._quotes.get(position.symbol)
            if quote is None:
                continue
            spec = self.get_symbol(position.symbol)
            price = quote.exit_price_for(position.direction)
            floating += (
                (price - position.open_price)
                * position.direction.sign
                * position.volume
                * spec.money_per_price_unit_per_lot
            )
            position.profit = (
                (price - position.open_price)
                * position.direction.sign
                * position.volume
                * spec.money_per_price_unit_per_lot
            )
        self.equity = self.balance + floating

    def force_close_all(self, reason: str = "end_of_data") -> list[PaperFill]:
        """Close every open position at the last known price (end of a replay)."""
        fills: list[PaperFill] = []
        for position in list(self._positions.values()):
            quote = self._quotes.get(position.symbol)
            price = quote.exit_price_for(position.direction) if quote else position.open_price
            fills.append(self._close(position, price, reason))
        return fills

    # ------------------------------------------------------------------ #
    @property
    def fills(self) -> list[PaperFill]:
        return list(self._fills)
