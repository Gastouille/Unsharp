"""XTB adapter: maps xAPI payloads onto the domain models of :mod:`unsharp_bot.models`."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from ..config import BrokerConfig
from ..models import AccountState, Candle, Direction, Position, SymbolSpec
from .base import (
    Broker,
    BrokerConnectionError,
    BrokerError,
    OrderRequest,
    OrderResult,
    Quote,
)
from .xtb_client import (
    CMD_BUY,
    CMD_SELL,
    STATUS_ACCEPTED,
    STATUS_ERROR,
    STATUS_PENDING,
    STATUS_REJECTED,
    TYPE_CLOSE,
    TYPE_MODIFY,
    TYPE_OPEN,
    XtbClient,
    XtbStreamClient,
    parse_rate_info,
    to_utc,
)

LOGGER = logging.getLogger(__name__)

#: Instruments whose bars XTB serves with a different period id are not special
#: cased: we simply pass the period through (already validated in the config).


class XtbBroker(Broker):
    """:class:`~unsharp_bot.broker.base.Broker` implementation for XTB xStation5."""

    def __init__(self, config: BrokerConfig) -> None:
        self.config = config
        self.client = XtbClient(
            url=config.main_endpoint,
            user_id=config.user_id,
            password=config.password,
            app_name=config.app_name,
            min_request_interval=config.min_request_interval,
            request_timeout=config.request_timeout,
            ping_interval=config.ping_interval,
            reconnect_max_attempts=config.reconnect_max_attempts,
            reconnect_base_delay=config.reconnect_base_delay,
            reconnect_max_delay=config.reconnect_max_delay,
        )
        self.stream: XtbStreamClient | None = None
        self._symbol_cache: dict[str, SymbolSpec] = {}
        self._quotes: dict[str, Quote] = {}
        self._quotes_lock = threading.Lock()
        self._tick_callback: Callable[[Quote], None] | None = None
        self._position_callback: Callable[[Position], None] | None = None
        self._account_cache: AccountState | None = None

    # ------------------------------------------------------------------ #
    # Connection
    # ------------------------------------------------------------------ #
    @property
    def connected(self) -> bool:
        return self.client.connected

    def connect(self) -> None:
        self.client.connect()
        if self.config.use_streaming and self.client.stream_session_id:
            self._start_streaming()

    def _start_streaming(self) -> None:
        try:
            self.stream = XtbStreamClient(
                url=self.config.stream_endpoint,
                stream_session_id=self.client.stream_session_id or "",
                on_tick=self._handle_tick,
                on_trade=self._handle_trade,
                on_balance=self._handle_balance,
                reconnect_base_delay=self.config.reconnect_base_delay,
                reconnect_max_delay=self.config.reconnect_max_delay,
            )
            self.stream.start()
            self.stream.subscribe_balance()
            self.stream.subscribe_trades()
        except Exception as exc:  # streaming is a nice-to-have, never fatal
            LOGGER.warning("Streaming socket unavailable, continuing without it: %s", exc)
            self.stream = None

    def disconnect(self) -> None:
        if self.stream is not None:
            self.stream.stop()
            self.stream = None
        self.client.disconnect()

    def ensure_connected(self) -> None:
        if not self.client.connected:
            LOGGER.warning("Main socket lost, reconnecting")
            self.client.reconnect()
            if self.config.use_streaming and self.client.stream_session_id:
                if self.stream is not None:
                    self.stream.stop()
                self._start_streaming()

    # ------------------------------------------------------------------ #
    # Reference data
    # ------------------------------------------------------------------ #
    def get_all_symbols(self) -> list[SymbolSpec]:
        records = self.client.command("getAllSymbols") or []
        specs = [self._to_symbol_spec(record) for record in records]
        for spec in specs:
            self._symbol_cache[spec.symbol] = spec
        LOGGER.info("Loaded %d tradable symbols from XTB", len(specs))
        return specs

    def get_symbol(self, symbol: str) -> SymbolSpec:
        record = self.client.command("getSymbol", {"symbol": symbol})
        if not record:
            raise BrokerError(f"Unknown symbol: {symbol}")
        spec = self._to_symbol_spec(record)
        self._symbol_cache[spec.symbol] = spec
        return spec

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        """Cached :meth:`get_symbol`."""
        cached = self._symbol_cache.get(symbol)
        if cached is not None:
            return cached
        return self.get_symbol(symbol)

    @staticmethod
    def _to_symbol_spec(record: dict[str, Any]) -> SymbolSpec:
        precision = int(record.get("precision") or record.get("digits") or 5)
        tick_size = float(record.get("tickSize") or 0.0) or 10.0 ** (-precision)
        return SymbolSpec(
            symbol=str(record.get("symbol", "")),
            description=str(record.get("description", "")),
            category=str(record.get("categoryName", "")),
            currency=str(record.get("currency", "")),
            currency_profit=str(record.get("currencyProfit", "")),
            contract_size=float(record.get("contractSize") or 1.0),
            lot_min=float(record.get("lotMin") or 0.01),
            lot_max=float(record.get("lotMax") or 100.0),
            lot_step=float(record.get("lotStep") or 0.01),
            tick_size=tick_size,
            tick_value=float(record.get("tickValue") or 0.0),
            precision=precision,
            leverage=float(record.get("leverage") or 0.0),
            spread_raw=float(record.get("spreadRaw") or 0.0),
            stops_level=float(record.get("stopsLevel") or 0.0),
            time_string=str(record.get("timeString", "")),
            trading_enabled=bool(record.get("quoteId") is not None)
            if "quoteId" in record
            else True,
        )

    # ------------------------------------------------------------------ #
    # Market data
    # ------------------------------------------------------------------ #
    def get_candles(
        self,
        symbol: str,
        period_minutes: int,
        count: int | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Candle]:
        """Closed bars, oldest first.

        ``getChartLastRequest`` is used when only a count is given (it is the
        cheaper call); ``getChartRangeRequest`` when an explicit window is asked
        for, which is what the backtester needs.
        """
        if start is None and count is None:
            count = 200

        if end is not None or (start is not None and count is None):
            payload = {
                "info": {
                    "symbol": symbol,
                    "period": int(period_minutes),
                    "start": _epoch_ms(start) if start else 0,
                    "end": _epoch_ms(end or datetime.now(timezone.utc)),
                    "ticks": 0,
                }
            }
            data = self.client.command("getChartRangeRequest", payload)
        else:
            if start is None:
                # Ask for a bit more than needed: XTB caps history per period.
                lookback = timedelta(minutes=period_minutes * (count or 200) * 3)
                start = datetime.now(timezone.utc) - lookback
            payload = {
                "info": {
                    "symbol": symbol,
                    "period": int(period_minutes),
                    "start": _epoch_ms(start),
                }
            }
            data = self.client.command("getChartLastRequest", payload)

        if not data:
            return []
        digits = int(data.get("digits", 5))
        candles: list[Candle] = []
        for record in data.get("rateInfos", []):
            parsed = parse_rate_info(record, digits)
            candles.append(
                Candle(
                    timestamp=datetime.fromtimestamp(parsed["timestamp"], tz=timezone.utc),
                    open=parsed["open"],
                    high=parsed["high"],
                    low=parsed["low"],
                    close=parsed["close"],
                    volume=parsed["volume"],
                )
            )
        candles.sort(key=lambda c: c.timestamp)
        candles = drop_forming_candle(candles, period_minutes)
        if count is not None and len(candles) > count:
            candles = candles[-count:]
        return candles

    def get_quote(self, symbol: str) -> Quote | None:
        """Prefer the streamed quote; fall back to a ``getSymbol`` snapshot."""
        with self._quotes_lock:
            quote = self._quotes.get(symbol)
        if quote is not None and (datetime.now(timezone.utc) - quote.timestamp).total_seconds() < 30:
            return quote
        try:
            record = self.client.command("getSymbol", {"symbol": symbol}) or {}
        except BrokerError as exc:
            LOGGER.warning("Quote unavailable for %s: %s", symbol, exc)
            return None
        bid, ask = record.get("bid"), record.get("ask")
        if bid is None or ask is None:
            return None
        quote = Quote(
            symbol=symbol,
            bid=float(bid),
            ask=float(ask),
            timestamp=datetime.now(timezone.utc),
        )
        with self._quotes_lock:
            self._quotes[symbol] = quote
        return quote

    def subscribe_prices(
        self,
        symbols: Iterable[str],
        callback: Callable[[Quote], None] | None = None,
    ) -> None:
        self._tick_callback = callback
        if self.stream is None:
            return
        try:
            self.stream.subscribe_prices(list(symbols))
        except Exception as exc:  # pragma: no cover
            LOGGER.warning("Price subscription failed: %s", exc)

    def on_position_update(self, callback: Callable[[Position], None] | None) -> None:
        """Register a callback fired on every streamed trade update."""
        self._position_callback = callback

    # ------------------------------------------------------------------ #
    # Account
    # ------------------------------------------------------------------ #
    def get_account_state(self) -> AccountState:
        data = self.client.command("getMarginLevel") or {}
        state = AccountState(
            balance=float(data.get("balance", 0.0)),
            equity=float(data.get("equity", 0.0)),
            margin_used=float(data.get("margin", 0.0)),
            margin_free=float(data.get("margin_free", 0.0)),
            currency=str(data.get("currency", "")),
            margin_level=float(data.get("margin_level", 0.0) or 0.0),
            credit=float(data.get("credit", 0.0) or 0.0),
        )
        self._account_cache = state
        return state

    def get_account_status(self) -> dict[str, Any]:
        """``getCurrentUserData``: leverage, currency, account group."""
        return self.client.command("getCurrentUserData") or {}

    def get_margin_for(self, symbol: str, volume: float) -> float | None:
        try:
            data = self.client.command(
                "getMarginTrade", {"symbol": symbol, "volume": float(volume)}
            )
        except BrokerError as exc:
            LOGGER.debug("getMarginTrade failed for %s %.2f: %s", symbol, volume, exc)
            return None
        if not data:
            return None
        margin = data.get("margin")
        return float(margin) if margin is not None else None

    def get_profit_calculation(
        self,
        symbol: str,
        direction: Direction,
        volume: float,
        open_price: float,
        close_price: float,
    ) -> float | None:
        """Broker-side P&L simulation, already converted to the account currency."""
        try:
            data = self.client.command(
                "getProfitCalculation",
                {
                    "symbol": symbol,
                    "cmd": CMD_BUY if direction is Direction.LONG else CMD_SELL,
                    "volume": float(volume),
                    "openPrice": float(open_price),
                    "closePrice": float(close_price),
                },
            )
        except BrokerError as exc:
            LOGGER.debug("getProfitCalculation failed for %s: %s", symbol, exc)
            return None
        if not data:
            return None
        profit = data.get("profit")
        return float(profit) if profit is not None else None

    def get_server_time(self) -> datetime | None:
        try:
            data = self.client.command("getServerTime") or {}
        except BrokerError:
            return None
        timestamp = data.get("time")
        return to_utc(float(timestamp)) if timestamp else None

    # ------------------------------------------------------------------ #
    # Trading
    # ------------------------------------------------------------------ #
    def open_trade(self, request: OrderRequest) -> OrderResult:
        """Market order with an attached SL/TP, retried with exponential backoff."""
        spec = self.symbol_spec(request.symbol)
        quote = self.get_quote(request.symbol)
        if quote is None:
            return OrderResult(False, message="no_quote_available")

        price = quote.price_for(request.direction)
        trade_info: dict[str, Any] = {
            "cmd": CMD_BUY if request.direction is Direction.LONG else CMD_SELL,
            "type": TYPE_OPEN,
            "symbol": request.symbol,
            "volume": float(request.volume),
            "price": spec.round_price(price),
            "sl": spec.round_price(request.stop_loss) if request.stop_loss else 0.0,
            "tp": spec.round_price(request.take_profit) if request.take_profit else 0.0,
            "offset": 0,
            "order": 0,
            "expiration": 0,
            "customComment": request.comment[:64],
        }
        return self._send_transaction(trade_info, "open")

    def close_trade(self, position: Position, volume: float | None = None) -> OrderResult:
        spec = self.symbol_spec(position.symbol)
        quote = self.get_quote(position.symbol)
        price = quote.exit_price_for(position.direction) if quote else position.open_price
        trade_info = {
            # On a close, cmd must mirror the *original* direction.
            "cmd": CMD_BUY if position.direction is Direction.LONG else CMD_SELL,
            "type": TYPE_CLOSE,
            "symbol": position.symbol,
            "volume": float(volume if volume is not None else position.volume),
            "price": spec.round_price(price),
            "order": int(position.position_id),
            "offset": 0,
            "expiration": 0,
            "customComment": "unsharp-close",
        }
        return self._send_transaction(trade_info, "close")

    def modify_trade(
        self,
        position: Position,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> OrderResult:
        spec = self.symbol_spec(position.symbol)
        trade_info = {
            "cmd": CMD_BUY if position.direction is Direction.LONG else CMD_SELL,
            "type": TYPE_MODIFY,
            "symbol": position.symbol,
            "volume": float(position.volume),
            "order": int(position.position_id),
            "sl": spec.round_price(stop_loss) if stop_loss else (position.stop_loss or 0.0),
            "tp": spec.round_price(take_profit) if take_profit else (position.take_profit or 0.0),
            "price": spec.round_price(position.open_price),
            "offset": 0,
            "expiration": 0,
            "customComment": "unsharp-modify",
        }
        return self._send_transaction(trade_info, "modify")

    def _send_transaction(self, trade_info: dict[str, Any], label: str) -> OrderResult:
        """``tradeTransaction`` + ``tradeTransactionStatus``, with retries."""
        attempts = max(1, self.config.order_retry_attempts)
        delay = self.config.order_retry_base_delay
        last_message = ""

        for attempt in range(1, attempts + 1):
            try:
                self.ensure_connected()
                data = self.client.command(
                    "tradeTransaction", {"tradeTransInfo": trade_info}
                ) or {}
                order_id = data.get("order")
                if order_id is None:
                    last_message = f"no order id returned for {label}"
                    raise BrokerError(last_message, retryable=True)

                status = self._await_transaction_status(int(order_id))
                request_status = status.get("requestStatus")
                if request_status == STATUS_ACCEPTED:
                    LOGGER.info(
                        "Order %s accepted: %s %s vol=%s sl=%s tp=%s (order=%s)",
                        label, trade_info["symbol"],
                        "BUY" if trade_info["cmd"] == CMD_BUY else "SELL",
                        trade_info["volume"], trade_info.get("sl"), trade_info.get("tp"),
                        order_id,
                    )
                    return OrderResult(
                        accepted=True,
                        order_id=int(order_id),
                        position_id=int(order_id),
                        price=float(status.get("ask") or status.get("bid") or trade_info["price"]),
                        message=str(status.get("message") or ""),
                        raw=status,
                    )
                last_message = (
                    f"{label} rejected (requestStatus={request_status}): "
                    f"{status.get('message') or 'no message'}"
                )
                LOGGER.warning(last_message)
                if request_status in (STATUS_REJECTED, STATUS_ERROR):
                    # A business rejection will not fix itself: stop retrying.
                    return OrderResult(False, order_id=int(order_id), message=last_message, raw=status)
            except BrokerConnectionError as exc:
                last_message = str(exc)
                LOGGER.warning("Transport failure on %s (attempt %d/%d): %s",
                               label, attempt, attempts, exc)
            except BrokerError as exc:
                last_message = str(exc)
                LOGGER.warning("Broker error on %s (attempt %d/%d): %s",
                               label, attempt, attempts, exc)
                if not exc.retryable:
                    return OrderResult(False, message=last_message)

            if attempt < attempts:
                time.sleep(delay)
                delay *= 2  # exponential backoff

        return OrderResult(False, message=last_message or f"{label} failed")

    def _await_transaction_status(
        self, order_id: int, timeout: float = 15.0, poll_interval: float = 0.5
    ) -> dict[str, Any]:
        """Poll ``tradeTransactionStatus`` until the broker settles the order."""
        deadline = time.monotonic() + timeout
        status: dict[str, Any] = {}
        while time.monotonic() < deadline:
            status = self.client.command("tradeTransactionStatus", {"order": int(order_id)}) or {}
            if status.get("requestStatus") != STATUS_PENDING:
                return status
            time.sleep(poll_interval)
        return status or {"requestStatus": STATUS_PENDING, "message": "status timeout"}

    def get_trades(self, opened_only: bool = True) -> list[Position]:
        records = self.client.command("getTrades", {"openedOnly": bool(opened_only)}) or []
        return [self._to_position(record) for record in records]

    def get_trades_history(self, start: datetime, end: datetime | None = None) -> list[Position]:
        records = self.client.command(
            "getTradesHistory",
            {"start": _epoch_ms(start), "end": _epoch_ms(end or datetime.now(timezone.utc))},
        ) or []
        return [self._to_position(record) for record in records]

    @staticmethod
    def _to_position(record: dict[str, Any]) -> Position:
        cmd = int(record.get("cmd", 0))
        direction = Direction.LONG if cmd == CMD_BUY else Direction.SHORT
        open_time = record.get("open_time")
        close_time = record.get("close_time")
        return Position(
            position_id=int(record.get("position") or record.get("order") or 0),
            symbol=str(record.get("symbol", "")),
            direction=direction,
            volume=float(record.get("volume", 0.0)),
            open_price=float(record.get("open_price", 0.0)),
            open_time=to_utc(float(open_time)) if open_time else None,
            stop_loss=float(record["sl"]) if record.get("sl") else None,
            take_profit=float(record["tp"]) if record.get("tp") else None,
            profit=float(record.get("profit") or 0.0),
            commission=float(record.get("commission") or 0.0),
            swap=float(record.get("storage") or 0.0),
            close_price=float(record["close_price"]) if record.get("close_price") else None,
            close_time=to_utc(float(close_time)) if close_time else None,
            closed=bool(record.get("closed", False)),
            comment=str(record.get("customComment") or record.get("comment") or ""),
            order_id=int(record.get("order") or 0) or None,
        )

    # ------------------------------------------------------------------ #
    # Streaming callbacks
    # ------------------------------------------------------------------ #
    def _handle_tick(self, data: dict[str, Any]) -> None:
        symbol = data.get("symbol")
        bid, ask = data.get("bid"), data.get("ask")
        if not symbol or bid is None or ask is None:
            return
        timestamp = data.get("timestamp")
        quote = Quote(
            symbol=str(symbol),
            bid=float(bid),
            ask=float(ask),
            timestamp=to_utc(float(timestamp)) if timestamp else datetime.now(timezone.utc),
        )
        with self._quotes_lock:
            self._quotes[quote.symbol] = quote
        if self._tick_callback is not None:
            self._tick_callback(quote)

    def _handle_trade(self, data: dict[str, Any]) -> None:
        if self._position_callback is None:
            return
        try:
            self._position_callback(self._to_position(data))
        except Exception as exc:  # pragma: no cover
            LOGGER.exception("Position callback failed: %s", exc)

    def _handle_balance(self, data: dict[str, Any]) -> None:
        self._account_cache = AccountState(
            balance=float(data.get("balance", 0.0)),
            equity=float(data.get("equity", 0.0)),
            margin_used=float(data.get("margin", 0.0)),
            margin_free=float(data.get("marginFree", 0.0)),
            margin_level=float(data.get("marginLevel", 0.0) or 0.0),
            credit=float(data.get("credit", 0.0) or 0.0),
        )


def _epoch_ms(moment: datetime) -> int:
    """Datetime -> xAPI epoch milliseconds (UTC)."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)


def drop_forming_candle(candles: list[Candle], period_minutes: int) -> list[Candle]:
    """Remove the still-forming last bar so the strategy never repaints.

    A bar is considered closed once ``open_time + period`` is in the past.
    """
    if not candles:
        return candles
    period = timedelta(minutes=period_minutes)
    now = datetime.now(timezone.utc)
    return [c for c in candles if c.timestamp + period <= now]
