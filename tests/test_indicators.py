"""Indicator maths."""

from __future__ import annotations

from datetime import timedelta

import pytest

from unsharp_bot.models import Candle, Direction
from unsharp_bot.strategy.indicators import (
    atr,
    average_body,
    is_swing_high,
    is_swing_low,
    true_range,
)

from .conftest import START, CandleBuilder


def test_candle_geometry():
    candle = Candle(START, open=100.0, high=103.0, low=98.0, close=102.0)
    assert candle.body == pytest.approx(2.0)
    assert candle.range == pytest.approx(5.0)
    assert candle.upper_wick == pytest.approx(1.0)
    assert candle.lower_wick == pytest.approx(2.0)
    assert candle.is_bullish and not candle.is_bearish
    assert candle.close_position == pytest.approx(0.8)
    # A long rejection is read on the lower wick.
    assert candle.wick_ratio(Direction.LONG) == pytest.approx(0.4)
    assert candle.wick_ratio(Direction.SHORT) == pytest.approx(0.2)


def test_true_range_uses_previous_close():
    previous = Candle(START, 100, 101, 99, 100)
    gap_up = Candle(START + timedelta(minutes=5), 105, 106, 104, 105)
    # Gap: the true range spans from the previous close to the new high.
    assert true_range(gap_up, previous) == pytest.approx(6.0)
    assert true_range(gap_up, None) == pytest.approx(2.0)


def test_atr_on_constant_range_series():
    builder = CandleBuilder()
    for _ in range(30):
        builder.add(100, 101, 99, 100)
    assert atr(builder.build(), 14) == pytest.approx(2.0)


def test_atr_returns_zero_without_history():
    assert atr([], 14) == 0.0
    assert atr([Candle(START, 1, 1, 1, 1)], 14) == 0.0


def test_average_body():
    builder = CandleBuilder().add(100, 101, 99, 101).add(100, 101, 99, 99)
    assert average_body(builder.build(), 10) == pytest.approx(1.0)


def test_swing_detection():
    builder = CandleBuilder()
    for high, low in [(10, 8), (11, 9), (15, 13), (11, 9), (10, 8)]:
        builder.add(low, high, low, high)
    candles = builder.build()
    assert is_swing_high(candles, 2, 2)
    assert not is_swing_high(candles, 1, 2)
    # Out-of-range indices never qualify.
    assert not is_swing_high(candles, 0, 2)
    assert not is_swing_low(candles, 4, 2)
