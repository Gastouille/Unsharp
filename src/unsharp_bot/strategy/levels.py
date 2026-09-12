"""Key-level detection.

The Unsharp method only takes a setup when the three candles print *on a
level*.  This module turns a candle history into a ranked map of price zones:

* fractal swing highs / lows (structure),
* recent extremes and session extremes (liquidity pools),
* previous-day high / low / close (institutional reference prices),
* consolidation zones (repeatedly tested prices),
* optional psychological round numbers.

Levels within ``cluster_atr * ATR`` of each other are merged; the number of
merged candidates becomes the ``touches`` count, which drives ranking.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from ..config import LevelsConfig
from ..models import Candle, Level, LevelType
from .indicators import atr as compute_atr
from .indicators import is_swing_high, is_swing_low


@dataclass(slots=True)
class _Candidate:
    """Intermediate level candidate before clustering."""

    price: float
    type: LevelType
    timestamp: datetime | None = None


class LevelDetector:
    """Builds and caches the level map of one symbol."""

    def __init__(self, config: LevelsConfig) -> None:
        self.config = config

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def detect(
        self,
        candles: Sequence[Candle],
        atr_value: float | None = None,
        daily_candles: Sequence[Candle] | None = None,
        session_candles: Sequence[Candle] | None = None,
    ) -> list[Level]:
        """Return the ranked level map for ``candles`` (oldest first)."""
        if len(candles) < 3:
            return []

        window = candles[-self.config.scan_window:] if self.config.scan_window > 0 else list(candles)
        atr_value = atr_value if atr_value and atr_value > 0 else compute_atr(window)
        if atr_value <= 0:
            # Degenerate series (flat prices): fall back to a tiny epsilon so the
            # clustering maths stays defined.
            atr_value = max(1e-9, abs(window[-1].close) * 1e-5)

        candidates: list[_Candidate] = []
        candidates += self._swings(window)
        if self.config.include_recent_extremes:
            candidates += self._recent_extremes(window)
        if self.config.include_session_extremes and session_candles:
            candidates += self._session_extremes(session_candles)
        if self.config.include_previous_day and daily_candles:
            candidates += self._previous_day(daily_candles)
        if self.config.include_round_numbers:
            candidates += self._round_numbers(window)

        levels = self._cluster(candidates, atr_value)
        levels = [lv for lv in levels if lv.touches >= self.config.min_touches]
        levels.sort(key=lambda lv: lv.strength, reverse=True)
        if self.config.max_levels > 0:
            levels = levels[: self.config.max_levels]
        levels.sort(key=lambda lv: lv.price)
        return levels

    @staticmethod
    def nearest(
        levels: Sequence[Level],
        price: float,
        tolerance: float,
        side: str = "any",
    ) -> Level | None:
        """Closest level to ``price`` within ``tolerance``.

        ``side`` filters by position relative to ``price``:
        ``"below"`` (support), ``"above"`` (resistance) or ``"any"``.
        """
        best: Level | None = None
        best_distance = math.inf
        for level in levels:
            if side == "below" and level.price > price + tolerance:
                continue
            if side == "above" and level.price < price - tolerance:
                continue
            distance = level.distance_to(price)
            if distance > tolerance:
                continue
            # Ties broken by strength: a previous-day low beats a random swing.
            if distance < best_distance or (
                math.isclose(distance, best_distance) and best and level.strength > best.strength
            ):
                best, best_distance = level, distance
        return best

    @staticmethod
    def levels_beyond(
        levels: Sequence[Level],
        price: float,
        direction_sign: int,
        min_gap: float = 0.0,
    ) -> list[Level]:
        """Levels strictly ahead of ``price`` in the trade direction, nearest first."""
        ahead = []
        for level in levels:
            # Use the near edge of the zone: a conservative target.
            edge = level.low if direction_sign > 0 else level.high
            delta = (edge - price) * direction_sign
            if delta > min_gap:
                ahead.append((delta, level))
        ahead.sort(key=lambda item: item[0])
        return [level for _, level in ahead]

    # ------------------------------------------------------------------ #
    # Candidate generators
    # ------------------------------------------------------------------ #
    def _swings(self, candles: Sequence[Candle]) -> list[_Candidate]:
        lookback = max(1, self.config.swing_lookback)
        out: list[_Candidate] = []
        for index in range(lookback, len(candles) - lookback):
            candle = candles[index]
            if is_swing_high(candles, index, lookback):
                out.append(_Candidate(candle.high, LevelType.SWING_HIGH, candle.timestamp))
            if is_swing_low(candles, index, lookback):
                out.append(_Candidate(candle.low, LevelType.SWING_LOW, candle.timestamp))
        return out

    def _recent_extremes(self, candles: Sequence[Candle]) -> list[_Candidate]:
        """Highest high / lowest low of the scan window: the obvious liquidity."""
        highest = max(candles, key=lambda c: c.high)
        lowest = min(candles, key=lambda c: c.low)
        return [
            _Candidate(highest.high, LevelType.RECENT_HIGH, highest.timestamp),
            _Candidate(lowest.low, LevelType.RECENT_LOW, lowest.timestamp),
        ]

    def _session_extremes(self, candles: Sequence[Candle]) -> list[_Candidate]:
        if not candles:
            return []
        highest = max(candles, key=lambda c: c.high)
        lowest = min(candles, key=lambda c: c.low)
        return [
            _Candidate(highest.high, LevelType.SESSION_HIGH, highest.timestamp),
            _Candidate(lowest.low, LevelType.SESSION_LOW, lowest.timestamp),
        ]

    def _previous_day(self, daily: Sequence[Candle]) -> list[_Candidate]:
        """Previous-day high / low / close from daily bars (last *closed* day)."""
        if len(daily) < 2:
            return []
        previous = daily[-2]
        return [
            _Candidate(previous.high, LevelType.PREVIOUS_DAY_HIGH, previous.timestamp),
            _Candidate(previous.low, LevelType.PREVIOUS_DAY_LOW, previous.timestamp),
            _Candidate(previous.close, LevelType.PREVIOUS_DAY_CLOSE, previous.timestamp),
        ]

    def _round_numbers(self, candles: Sequence[Candle]) -> list[_Candidate]:
        """Psychological levels spanning the visible price range."""
        low = min(c.low for c in candles)
        high = max(c.high for c in candles)
        step = self.config.round_number_step
        if step <= 0:
            # Auto: one order of magnitude below the visible amplitude.
            span = max(high - low, 1e-9)
            step = 10.0 ** math.floor(math.log10(span))
            if step <= 0:
                return []
        out: list[_Candidate] = []
        start = math.floor(low / step) * step
        price = start
        guard = 0
        while price <= high and guard < 500:
            if price >= low:
                out.append(_Candidate(round(price, 10), LevelType.ROUND_NUMBER, None))
            price += step
            guard += 1
        return out

    # ------------------------------------------------------------------ #
    # Clustering
    # ------------------------------------------------------------------ #
    def _cluster(self, candidates: Sequence[_Candidate], atr_value: float) -> list[Level]:
        """Merge nearby candidates into zones, counting touches."""
        if not candidates:
            return []
        threshold = max(self.config.cluster_atr * atr_value, 0.0)
        width = max(self.config.zone_width_atr * atr_value, 0.0)

        ordered = sorted(candidates, key=lambda c: c.price)
        clusters: list[list[_Candidate]] = [[ordered[0]]]
        for candidate in ordered[1:]:
            anchor = clusters[-1][0].price
            if abs(candidate.price - anchor) <= threshold:
                clusters[-1].append(candidate)
            else:
                clusters.append([candidate])

        levels: list[Level] = []
        for cluster in clusters:
            prices = [c.price for c in cluster]
            price = sum(prices) / len(prices)
            # A cluster inherits the type of its strongest member.
            best_type = max(
                cluster,
                key=lambda c: Level(c.price, c.type).strength,
            ).type
            # Repeatedly tested prices become explicit consolidation zones.
            level_type = (
                LevelType.CONSOLIDATION
                if len(cluster) >= 3 and best_type in (LevelType.SWING_HIGH, LevelType.SWING_LOW)
                else best_type
            )
            stamps = [c.timestamp for c in cluster if c.timestamp is not None]
            levels.append(
                Level(
                    price=price,
                    type=level_type,
                    touches=len(cluster),
                    width=max(width, (max(prices) - min(prices)) / 2.0),
                    last_touch=max(stamps) if stamps else None,
                )
            )
        return levels
