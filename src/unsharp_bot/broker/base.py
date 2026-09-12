"""Broker port (hexagonal architecture).

The engine only ever talks to this interface.  Swapping XTB for another broker,
or the raw WebSocket client for the ``XTBApi`` PyPI package, means writing one
new adapter and changing ``broker.name`` in the config - no strategy code moves.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable

from ..models import AccountState, Candle, Direction, Position, SymbolSpec


class BrokerError(RuntimeError):
    """Any broker-side failure (network, protocol or business rejection)."""

    def __init__(self, message: str, code: str = "", retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class BrokerConnectionError(BrokerError):
    """Transport-level failure: the socket is gone, a reconnect is needed."""

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message, code, retryable=True)


@dataclass(slots=True)
class OrderRequest:
    """A market order with optional attached stop-loss / take-profit."""

    symbol: str
    direction: Direction
    volume: float
    stop_loss: float | None = None
    take_profit: float | None = None
    comment: str = ""
    #: Max price deviation accepted by the broker, in price units.
    max_deviation: float | None = None


@dataclass(slots=True)
class OrderResult:
    """Broker acknowledgement of an order."""

    accepted: bool
    order_id: int | None = None
    position_id: int | None = None
    price: float | None = None
    message: str = ""
    raw: dict | None = None


@dataclass(slots=True)
class Quote:
    """Best bid/ask for a symbol."""

    symbol: str
    bid: float
    ask: float
    timestamp: datetime

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    def price_for(self, direction: Direction) -> float:
        """Price actually paid when *entering* in ``direction``."""
        return self.ask if direction is Direction.LONG else self.bid

    def exit_price_for(self, direction: Direction) -> float:
        """Price received when *closing* a position held in ``direction``."""
        return self.bid if direction is Direction.LONG else self.ask


class Broker(ABC):
    """Minimal capability set the Unsharp engine needs from a broker."""

    # -- connection --------------------------------------------------------- #
    @abstractmethod
    def connect(self) -> None:
        """Open the transport and authenticate."""

    @abstractmethod
    def disconnect(self) -> None:
        """Log out and close the transport (must be idempotent)."""

    @property
    @abstractmethod
    def connected(self) -> bool:
        ...

    def ensure_connected(self) -> None:
        """Reconnect if the link dropped.  Default: reconnect when needed."""
        if not self.connected:
            self.connect()

    # -- reference data ----------------------------------------------------- #
    @abstractmethod
    def get_all_symbols(self) -> list[SymbolSpec]:
        """Every tradable instrument exposed by the broker."""

    @abstractmethod
    def get_symbol(self, symbol: str) -> SymbolSpec:
        """Specification of one instrument."""

    # -- market data -------------------------------------------------------- #
    @abstractmethod
    def get_candles(
        self,
        symbol: str,
        period_minutes: int,
        count: int | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Candle]:
        """Closed OHLCV bars, oldest first.  The forming bar is excluded."""

    @abstractmethod
    def get_quote(self, symbol: str) -> Quote | None:
        """Latest bid/ask, or ``None`` when unavailable."""

    def subscribe_prices(
        self,
        symbols: Iterable[str],
        callback: Callable[[Quote], None] | None = None,
    ) -> None:
        """Optional real-time price streaming.  No-op by default."""
        return None

    # -- account ------------------------------------------------------------ #
    @abstractmethod
    def get_account_state(self) -> AccountState:
        """Balance / equity / margin snapshot."""

    def get_margin_for(self, symbol: str, volume: float) -> float | None:
        """Exact margin required for ``volume`` lots, when the broker exposes it."""
        return None

    # -- trading ------------------------------------------------------------ #
    @abstractmethod
    def open_trade(self, request: OrderRequest) -> OrderResult:
        """Send a market order."""

    @abstractmethod
    def close_trade(self, position: Position, volume: float | None = None) -> OrderResult:
        """Close (or partially close) an open position."""

    @abstractmethod
    def get_trades(self, opened_only: bool = True) -> list[Position]:
        """Currently open positions."""

    def modify_trade(
        self,
        position: Position,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> OrderResult:
        """Move the SL/TP of an open position.  Optional."""
        raise BrokerError("modify_trade is not supported by this broker")

    def get_server_time(self) -> datetime | None:
        """Broker clock, used to align candle boundaries.  Optional."""
        return None
