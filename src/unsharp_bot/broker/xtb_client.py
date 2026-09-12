"""Raw xStation5 / xAPI WebSocket transport.

Why a hand-written client rather than the ``XTBApi`` PyPI package?

* xAPI is a thin request/response JSON protocol over a WebSocket - roughly 200
  lines of transport - and owning it means we control the two things that
  actually matter in production: the **rate limit** (XTB drops clients that send
  more than ~1 command per 200 ms) and the **reconnect/re-login** logic.
* The third-party wrappers hide the ``streamSessionId`` handshake, which we need
  to drive the streaming socket.
* ``xtb_broker.XtbBroker`` keeps the domain mapping separate, so swapping this
  transport for ``XTBApi`` later only touches one file.

Protocol notes (xAPI documentation):

* Every command is a JSON object ``{"command": ..., "arguments": {...}}``.
* Responses are ``{"status": true, "returnData": ...}`` or
  ``{"status": false, "errorCode": ..., "errorDescr": ...}``.
* ``getChartLastRequest`` returns prices as **integers scaled by 10^digits**, and
  ``high``/``low``/``close`` are **deltas from ``open``**.  This is the single
  most common source of bugs when talking to XTB, handled in :func:`parse_rate_info`.
* The main socket is closed after a few minutes of silence, hence the ping loop.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

try:  # pragma: no cover - import guard for a clearer error message
    import websocket  # type: ignore
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "The 'websocket-client' package is required for the XTB adapter. "
        "Install it with: pip install websocket-client"
    ) from exc

from .base import BrokerConnectionError, BrokerError

LOGGER = logging.getLogger(__name__)

#: Trade operation codes (TRADE_TRANS_INFO.cmd).
CMD_BUY = 0
CMD_SELL = 1
CMD_BUY_LIMIT = 2
CMD_SELL_LIMIT = 3
CMD_BUY_STOP = 4
CMD_SELL_STOP = 5
CMD_BALANCE = 6
CMD_CREDIT = 7

#: Transaction types (TRADE_TRANS_INFO.type).
TYPE_OPEN = 0
TYPE_PENDING = 1
TYPE_CLOSE = 2
TYPE_MODIFY = 3
TYPE_DELETE = 4

#: tradeTransactionStatus.requestStatus
STATUS_ERROR = 0
STATUS_PENDING = 1
STATUS_ACCEPTED = 3
STATUS_REJECTED = 4

#: Error codes that are worth retrying rather than aborting.
RETRYABLE_ERROR_CODES = frozenset({
    "BE005",   # user password changed / session lost
    "BE006",   # session expired
    "BE014",   # market closed for the instrument (transient during rollover)
    "EX000",   # internal error
    "SE199",   # internal server error
    "BE117",   # invalid session / re-login needed
})

#: Error codes meaning "log in again".
SESSION_ERROR_CODES = frozenset({"BE005", "BE006", "BE117", "EX009", "BE118"})


def parse_rate_info(record: dict[str, Any], digits: int) -> dict[str, float]:
    """Convert one xAPI ``RATE_INFO_RECORD`` into absolute float prices.

    XTB encodes the bar as::

        open   = open  / 10^digits            (absolute)
        high   = (open + high)  / 10^digits   (delta!)
        low    = (open + low)   / 10^digits   (delta!)
        close  = (open + close) / 10^digits   (delta!)
    """
    factor = 10.0 ** digits
    open_raw = float(record["open"])
    open_price = open_raw / factor
    high = (open_raw + float(record["high"])) / factor
    low = (open_raw + float(record["low"])) / factor
    close = (open_raw + float(record["close"])) / factor
    return {
        "timestamp": float(record["ctm"]) / 1000.0,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": float(record.get("vol", 0.0)),
    }


def to_utc(timestamp_ms: float) -> datetime:
    """xAPI timestamps are epoch milliseconds (UTC)."""
    return datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc)


class RateLimiter:
    """Serialises commands so we never exceed the broker's request rate."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = max(0.0, min_interval)
        self._last_call = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self.min_interval - (now - self._last_call)
            if delay > 0:
                time.sleep(delay)
            self._last_call = time.monotonic()


class XtbClient:
    """Synchronous request/response client for the xAPI **main** socket."""

    def __init__(
        self,
        url: str,
        user_id: str,
        password: str,
        app_name: str = "unsharp-bot",
        min_request_interval: float = 0.25,
        request_timeout: float = 20.0,
        ping_interval: float = 150.0,
        reconnect_max_attempts: int = 8,
        reconnect_base_delay: float = 2.0,
        reconnect_max_delay: float = 60.0,
    ) -> None:
        self.url = url
        self.user_id = user_id
        self.password = password
        self.app_name = app_name
        self.request_timeout = request_timeout
        self.ping_interval = ping_interval
        self.reconnect_max_attempts = reconnect_max_attempts
        self.reconnect_base_delay = reconnect_base_delay
        self.reconnect_max_delay = reconnect_max_delay

        self._rate_limiter = RateLimiter(min_request_interval)
        self._socket: "websocket.WebSocket | None" = None
        self._lock = threading.RLock()
        self._stream_session_id: str | None = None
        self._ping_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._logged_in = False

    # ------------------------------------------------------------------ #
    # Connection
    # ------------------------------------------------------------------ #
    @property
    def connected(self) -> bool:
        return self._socket is not None and self._logged_in

    @property
    def stream_session_id(self) -> str | None:
        return self._stream_session_id

    def connect(self) -> None:
        """Open the socket and log in, retrying with exponential backoff."""
        with self._lock:
            self._open_socket()
            self.login()
            self._start_ping_loop()

    def _open_socket(self) -> None:
        delay = self.reconnect_base_delay
        last_error: Exception | None = None
        for attempt in range(1, max(1, self.reconnect_max_attempts) + 1):
            try:
                LOGGER.debug("Opening WebSocket to %s (attempt %d)", self.url, attempt)
                self._socket = websocket.create_connection(
                    self.url, timeout=self.request_timeout
                )
                self._socket.settimeout(self.request_timeout)
                return
            except Exception as exc:  # network errors are expected here
                last_error = exc
                LOGGER.warning(
                    "WebSocket connection to %s failed (attempt %d/%d): %s",
                    self.url, attempt, self.reconnect_max_attempts, exc,
                )
                if attempt >= self.reconnect_max_attempts:
                    break
                time.sleep(delay)
                delay = min(delay * 2, self.reconnect_max_delay)
        raise BrokerConnectionError(
            f"Unable to connect to {self.url}: {last_error}"
        )

    def login(self) -> None:
        """xAPI ``login``; stores the ``streamSessionId`` for the stream socket."""
        response = self._send(
            {
                "command": "login",
                "arguments": {
                    "userId": str(self.user_id),
                    "password": self.password,
                    "appName": self.app_name,
                },
            },
            raw_response=True,
        )
        if not response.get("status"):
            raise BrokerError(
                f"XTB login refused: {response.get('errorDescr') or response}",
                code=str(response.get("errorCode", "")),
            )
        self._stream_session_id = response.get("streamSessionId")
        self._logged_in = True
        LOGGER.info("Logged in to XTB (%s)", self.url)

    def logout(self) -> None:
        if self._socket is None:
            return
        try:
            self._send({"command": "logout"}, raw_response=True, allow_reconnect=False)
        except Exception as exc:  # a failing logout must never crash a shutdown
            LOGGER.debug("Logout failed (ignored): %s", exc)
        finally:
            self._logged_in = False

    def disconnect(self) -> None:
        """Stop the ping loop, log out and close the socket.  Idempotent."""
        self._stop_event.set()
        with self._lock:
            self.logout()
            if self._socket is not None:
                try:
                    self._socket.close()
                except Exception:  # pragma: no cover
                    pass
                self._socket = None
            self._stream_session_id = None
        LOGGER.info("Disconnected from XTB main socket")

    def reconnect(self) -> None:
        """Drop everything and rebuild the session."""
        LOGGER.warning("Reconnecting to the XTB main socket...")
        with self._lock:
            if self._socket is not None:
                try:
                    self._socket.close()
                except Exception:  # pragma: no cover
                    pass
                self._socket = None
            self._logged_in = False
            self._open_socket()
            self.login()

    # ------------------------------------------------------------------ #
    # Keep-alive
    # ------------------------------------------------------------------ #
    def _start_ping_loop(self) -> None:
        if self._ping_thread and self._ping_thread.is_alive():
            return
        self._stop_event.clear()
        self._ping_thread = threading.Thread(
            target=self._ping_loop, name="xtb-ping", daemon=True
        )
        self._ping_thread.start()

    def _ping_loop(self) -> None:
        while not self._stop_event.wait(self.ping_interval):
            try:
                self.command("ping")
                LOGGER.debug("ping -> XTB main socket")
            except Exception as exc:  # pragma: no cover - background thread
                LOGGER.warning("Ping failed: %s", exc)

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #
    def command(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Send ``name`` and return ``returnData`` (raising on a broker error)."""
        payload: dict[str, Any] = {"command": name}
        if arguments:
            payload["arguments"] = arguments
        response = self._send(payload)
        return response.get("returnData")

    def _send(
        self,
        payload: dict[str, Any],
        raw_response: bool = False,
        allow_reconnect: bool = True,
    ) -> dict[str, Any]:
        """Serialise, rate-limit, send, and parse one command."""
        message = json.dumps(payload)
        command_name = payload.get("command", "?")

        with self._lock:
            if self._socket is None:
                if not allow_reconnect:
                    raise BrokerConnectionError("Socket is closed")
                self.reconnect()

            self._rate_limiter.wait()
            try:
                assert self._socket is not None
                self._socket.send(message)
                raw = self._socket.recv()
            except Exception as exc:
                LOGGER.warning("Transport error on '%s': %s", command_name, exc)
                self._logged_in = False
                if not allow_reconnect:
                    raise BrokerConnectionError(f"Transport error on '{command_name}': {exc}") from exc
                # One transparent retry after rebuilding the session.
                self.reconnect()
                self._rate_limiter.wait()
                try:
                    assert self._socket is not None
                    self._socket.send(message)
                    raw = self._socket.recv()
                except Exception as exc2:
                    raise BrokerConnectionError(
                        f"Transport error on '{command_name}' after reconnect: {exc2}"
                    ) from exc2

        try:
            response = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise BrokerError(f"Malformed XTB response for '{command_name}': {raw!r}") from exc

        if not isinstance(response, dict):
            raise BrokerError(f"Unexpected XTB response for '{command_name}': {response!r}")

        if response.get("status") is True or raw_response:
            return response

        code = str(response.get("errorCode", ""))
        description = response.get("errorDescr", "unknown error")
        # An expired session is recoverable: log in again and replay once.
        if code in SESSION_ERROR_CODES and allow_reconnect:
            LOGGER.warning("Session error %s on '%s', re-logging in", code, command_name)
            self.reconnect()
            return self._send(payload, raw_response=raw_response, allow_reconnect=False)
        raise BrokerError(
            f"XTB command '{command_name}' failed [{code}]: {description}",
            code=code,
            retryable=code in RETRYABLE_ERROR_CODES,
        )


class XtbStreamClient:
    """Asynchronous listener for the xAPI **streaming** socket.

    Used for tick prices, balance updates and trade-status notifications.  It is
    strictly optional: the engine works on closed candles pulled from the main
    socket, and treats streaming data as an accelerator (fresh quotes, instant
    position updates).
    """

    def __init__(
        self,
        url: str,
        stream_session_id: str,
        on_tick: Callable[[dict[str, Any]], None] | None = None,
        on_trade: Callable[[dict[str, Any]], None] | None = None,
        on_balance: Callable[[dict[str, Any]], None] | None = None,
        on_trade_status: Callable[[dict[str, Any]], None] | None = None,
        reconnect_base_delay: float = 2.0,
        reconnect_max_delay: float = 60.0,
    ) -> None:
        self.url = url
        self.stream_session_id = stream_session_id
        self.on_tick = on_tick
        self.on_trade = on_trade
        self.on_balance = on_balance
        self.on_trade_status = on_trade_status
        self.reconnect_base_delay = reconnect_base_delay
        self.reconnect_max_delay = reconnect_max_delay

        self._socket: "websocket.WebSocket | None" = None
        self._thread: threading.Thread | None = None
        self._keepalive_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._subscribed_symbols: set[str] = set()
        self._standing_subscriptions: list[dict[str, Any]] = []

    @property
    def connected(self) -> bool:
        return self._socket is not None

    def start(self) -> None:
        """Open the stream socket and start the reader thread."""
        self._stop_event.clear()
        self._open()
        self._thread = threading.Thread(target=self._read_loop, name="xtb-stream", daemon=True)
        self._thread.start()
        self._keepalive_thread = threading.Thread(
            target=self._keepalive_loop, name="xtb-stream-ka", daemon=True
        )
        self._keepalive_thread.start()

    def _open(self) -> None:
        with self._lock:
            self._socket = websocket.create_connection(self.url, timeout=30)
            self._socket.settimeout(30)
        LOGGER.info("Streaming socket opened (%s)", self.url)
        self._send({"command": "getKeepAlive", "streamSessionId": self.stream_session_id})
        # Replay standing subscriptions after a reconnect.
        for payload in list(self._standing_subscriptions):
            self._send(payload)
        for symbol in list(self._subscribed_symbols):
            self._send_tick_subscription(symbol)

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            if self._socket is not None:
                try:
                    self._socket.close()
                except Exception:  # pragma: no cover
                    pass
                self._socket = None
        LOGGER.info("Streaming socket closed")

    # ------------------------------------------------------------------ #
    def subscribe_prices(self, symbols: list[str], min_arrival_ms: int = 2000) -> None:
        for symbol in symbols:
            self._subscribed_symbols.add(symbol)
            self._send_tick_subscription(symbol, min_arrival_ms)

    def _send_tick_subscription(self, symbol: str, min_arrival_ms: int = 2000) -> None:
        self._send(
            {
                "command": "getTickPrices",
                "streamSessionId": self.stream_session_id,
                "symbol": symbol,
                "minArrivalTime": min_arrival_ms,
                "maxLevel": 1,
            }
        )

    def subscribe_trades(self) -> None:
        payload = {"command": "getTrades", "streamSessionId": self.stream_session_id}
        self._standing_subscriptions.append(payload)
        self._send(payload)

    def subscribe_balance(self) -> None:
        payload = {"command": "getBalance", "streamSessionId": self.stream_session_id}
        self._standing_subscriptions.append(payload)
        self._send(payload)

    def subscribe_trade_status(self) -> None:
        payload = {"command": "getTradeStatus", "streamSessionId": self.stream_session_id}
        self._standing_subscriptions.append(payload)
        self._send(payload)

    # ------------------------------------------------------------------ #
    def _send(self, payload: dict[str, Any]) -> None:
        with self._lock:
            if self._socket is None:
                raise BrokerConnectionError("Streaming socket is not open")
            try:
                self._socket.send(json.dumps(payload))
            except Exception as exc:
                raise BrokerConnectionError(f"Streaming send failed: {exc}") from exc

    def _keepalive_loop(self) -> None:
        """xAPI requires periodic traffic on the streaming socket."""
        while not self._stop_event.wait(30.0):
            try:
                self._send({"command": "ping", "streamSessionId": self.stream_session_id})
            except Exception as exc:  # pragma: no cover - background thread
                LOGGER.debug("Streaming keep-alive failed: %s", exc)

    def _read_loop(self) -> None:
        delay = self.reconnect_base_delay
        while not self._stop_event.is_set():
            try:
                with self._lock:
                    socket = self._socket
                if socket is None:
                    raise BrokerConnectionError("Streaming socket is gone")
                raw = socket.recv()
                if not raw:
                    continue
                delay = self.reconnect_base_delay
                self._dispatch(raw)
            except websocket.WebSocketTimeoutException:
                continue
            except Exception as exc:
                if self._stop_event.is_set():
                    return
                LOGGER.warning("Streaming socket error: %s - reconnecting in %.1fs", exc, delay)
                time.sleep(delay)
                delay = min(delay * 2, self.reconnect_max_delay)
                try:
                    self._open()
                except Exception as exc2:  # pragma: no cover
                    LOGGER.warning("Streaming reconnect failed: %s", exc2)

    def _dispatch(self, raw: str) -> None:
        try:
            message = json.loads(raw)
        except ValueError:
            LOGGER.debug("Unparsable streaming frame: %r", raw[:200])
            return
        command = message.get("command")
        data = message.get("data") or {}
        try:
            if command == "tickPrices" and self.on_tick:
                self.on_tick(data)
            elif command == "trade" and self.on_trade:
                self.on_trade(data)
            elif command == "balance" and self.on_balance:
                self.on_balance(data)
            elif command == "tradeStatus" and self.on_trade_status:
                self.on_trade_status(data)
            elif command == "keepAlive":
                LOGGER.debug("streaming keepAlive")
        except Exception as exc:  # pragma: no cover - a bad callback must not kill the loop
            LOGGER.exception("Streaming callback for '%s' raised: %s", command, exc)
