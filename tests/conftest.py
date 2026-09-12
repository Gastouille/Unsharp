"""Shared fixtures and synthetic candle builders."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from unsharp_bot.models import Candle, SymbolSpec

START = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)  # a Monday


class CandleBuilder:
    """Fluent builder producing a deterministic candle series."""

    def __init__(self, start: datetime = START, period_minutes: int = 5) -> None:
        self.start = start
        self.period = timedelta(minutes=period_minutes)
        self.candles: list[Candle] = []

    def add(self, open_: float, high: float, low: float, close: float, volume: float = 100.0):
        timestamp = self.start + self.period * len(self.candles)
        self.candles.append(Candle(timestamp, open_, high, low, close, volume))
        return self

    def oscillate(self, count: int, centre: float, amplitude: float, wick: float = 0.35):
        """Background noise: a sine wave that creates repeatable swing levels."""
        for index in range(count):
            mid = centre + amplitude * math.sin(index / 2.2)
            close = mid + (0.12 if index % 2 else -0.12)
            self.add(mid, max(mid, close) + wick, min(mid, close) - wick, close)
        return self

    def flat(self, count: int, price: float, wick: float = 0.2):
        for _ in range(count):
            self.add(price, price + wick, price - wick, price)
        return self

    def build(self) -> list[Candle]:
        return list(self.candles)


def long_setup_series() -> list[Candle]:
    """Background + a textbook LONG Unsharp sequence on support near 98.0."""
    builder = CandleBuilder()
    builder.oscillate(40, centre=99.6, amplitude=1.4)
    for _ in range(6):  # repeated tests of the 98.0 support
        builder.add(98.8, 99.0, 98.05, 98.7)
        builder.add(98.7, 99.4, 98.6, 99.3)
    builder.add(100.0, 100.1, 97.95, 98.15)   # Lead: big bearish sweep
    builder.add(98.15, 98.35, 97.92, 98.25)   # Confirmation 1: lower wick
    builder.add(98.25, 98.40, 97.98, 98.30)   # Confirmation 2: lower wick
    builder.add(98.30, 99.10, 98.28, 99.05)   # Execution: decisive bullish
    return builder.build()


def short_setup_series() -> list[Candle]:
    """Mirror image: a SHORT Unsharp sequence on resistance near 102.0."""
    builder = CandleBuilder()
    builder.oscillate(40, centre=100.4, amplitude=1.4)
    for _ in range(6):
        builder.add(101.2, 101.95, 101.0, 101.3)
        builder.add(101.3, 101.4, 100.6, 100.7)
    builder.add(100.0, 102.05, 99.9, 101.85)  # Lead: big bullish sweep
    builder.add(101.85, 102.08, 101.65, 101.75)  # Confirmation 1: upper wick
    builder.add(101.75, 102.02, 101.60, 101.70)  # Confirmation 2: upper wick
    builder.add(101.70, 101.72, 100.90, 100.95)  # Execution: decisive bearish
    return builder.build()


@pytest.fixture
def index_spec() -> SymbolSpec:
    """A CFD index-like instrument (US500 shaped)."""
    return SymbolSpec(
        symbol="US500",
        contract_size=1.0,
        lot_min=0.01,
        lot_max=100.0,
        lot_step=0.01,
        tick_size=0.1,
        tick_value=0.1,
        precision=1,
        leverage=5.0,          # 5% margin => 20:1
        currency="USD",
        currency_profit="USD",
    )


@pytest.fixture
def forex_spec() -> SymbolSpec:
    """A forex instrument (EURUSD shaped)."""
    return SymbolSpec(
        symbol="EURUSD",
        contract_size=100_000.0,
        lot_min=0.01,
        lot_max=100.0,
        lot_step=0.01,
        tick_size=0.00001,
        tick_value=1.0,        # 1 unit of account currency per pipette per lot
        precision=5,
        leverage=3.33,
        currency="EUR",
        currency_profit="USD",
    )
