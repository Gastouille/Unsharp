"""Small, dependency-free indicator toolbox.

Everything works on ``list[Candle]`` so the strategy core can be unit-tested
without pandas, a broker connection or a network.
"""

from __future__ import annotations

from statistics import fmean, median
from typing import Sequence

from ..models import Candle


def true_range(current: Candle, previous: Candle | None) -> float:
    """Classic True Range: max(H-L, |H-Cprev|, |L-Cprev|)."""
    if previous is None:
        return current.range
    return max(
        current.high - current.low,
        abs(current.high - previous.close),
        abs(current.low - previous.close),
    )


def atr(candles: Sequence[Candle], period: int = 14) -> float:
    """Wilder-smoothed Average True Range over the last ``period`` candles.

    Returns 0.0 when there is not enough history, which callers treat as
    "indicators not ready yet".
    """
    if len(candles) < 2:
        return 0.0
    ranges = [true_range(candles[i], candles[i - 1]) for i in range(1, len(candles))]
    if not ranges:
        return 0.0
    period = max(2, min(period, len(ranges)))
    # Seed with a simple mean, then apply Wilder smoothing on the remainder.
    value = fmean(ranges[:period])
    for tr in ranges[period:]:
        value = (value * (period - 1) + tr) / period
    return value


def average_body(candles: Sequence[Candle], window: int = 20) -> float:
    """Mean candle body over the last ``window`` candles."""
    if not candles:
        return 0.0
    subset = candles[-window:] if window > 0 else list(candles)
    return fmean(c.body for c in subset) if subset else 0.0


def median_body(candles: Sequence[Candle], window: int = 20) -> float:
    """Median body: more robust than the mean when one huge candle skews it."""
    if not candles:
        return 0.0
    subset = candles[-window:] if window > 0 else list(candles)
    return median(c.body for c in subset) if subset else 0.0


def average_range(candles: Sequence[Candle], window: int = 20) -> float:
    if not candles:
        return 0.0
    subset = candles[-window:] if window > 0 else list(candles)
    return fmean(c.range for c in subset) if subset else 0.0


def is_swing_high(candles: Sequence[Candle], index: int, lookback: int) -> bool:
    """True when ``candles[index]`` is a fractal high over ±``lookback`` bars.

    Ties are resolved strictly on the right side so a flat top yields a single
    swing instead of one per candle.
    """
    if index - lookback < 0 or index + lookback >= len(candles):
        return False
    pivot = candles[index].high
    for offset in range(1, lookback + 1):
        if candles[index - offset].high > pivot:
            return False
        if candles[index + offset].high >= pivot:
            return False
    return True


def is_swing_low(candles: Sequence[Candle], index: int, lookback: int) -> bool:
    """True when ``candles[index]`` is a fractal low over ±``lookback`` bars."""
    if index - lookback < 0 or index + lookback >= len(candles):
        return False
    pivot = candles[index].low
    for offset in range(1, lookback + 1):
        if candles[index - offset].low < pivot:
            return False
        if candles[index + offset].low <= pivot:
            return False
    return True


def highest_high(candles: Sequence[Candle]) -> float:
    return max((c.high for c in candles), default=0.0)


def lowest_low(candles: Sequence[Candle]) -> float:
    return min((c.low for c in candles), default=0.0)
