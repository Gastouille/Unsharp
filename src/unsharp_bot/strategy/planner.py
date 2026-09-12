"""Turns a validated :class:`UnsharpSetup` into entry / stop / target prices.

Rules implemented (from the method):

* **Entry**  - close of the Execution Candle (or open of the next candle).
* **Stop**   - just beyond the wicks of the confirmation zone, plus an ATR buffer,
               and never closer than the broker's minimum stop distance.
* **Target** - the next key level in the trade direction that yields at least
               ``min_risk_reward``.  If no level qualifies, an optional synthetic
               target at exactly ``min_risk_reward`` is used.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..config import UnsharpConfig
from ..models import Direction, Level, SymbolSpec, UnsharpSetup
from .levels import LevelDetector


@dataclass(slots=True)
class TradeGeometry:
    """Price skeleton of a trade, before any sizing."""

    entry_price: float
    stop_price: float
    target_price: float
    risk_per_unit: float
    risk_reward: float
    target_level: Level | None = None
    synthetic_target: bool = False
    notes: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.notes is None:
            self.notes = []


class TradePlanner:
    """Computes the price geometry of a setup."""

    def __init__(self, config: UnsharpConfig) -> None:
        self.config = config

    def build(
        self,
        setup: UnsharpSetup,
        levels: Sequence[Level],
        spec: SymbolSpec | None = None,
        entry_override: float | None = None,
    ) -> tuple[TradeGeometry | None, str]:
        """Return ``(geometry, "")`` or ``(None, reason)`` when unusable."""
        cfg = self.config
        direction = setup.direction
        sign = direction.sign
        atr_value = setup.atr
        notes: list[str] = []

        entry = entry_override if entry_override is not None else setup.entry_reference

        # --- Stop ---------------------------------------------------------- #
        buffer = cfg.stop_buffer_atr * atr_value
        stop = setup.zone_extreme - sign * buffer

        # The stop must give the trade room to breathe.
        min_distance = cfg.min_stop_distance_atr * atr_value
        if spec is not None:
            min_distance = max(min_distance, spec.min_stop_distance)
        if (entry - stop) * sign < min_distance:
            stop = entry - sign * min_distance
            notes.append("stop_widened_to_minimum_distance")

        risk_per_unit = abs(entry - stop)
        if risk_per_unit <= 0:
            return None, "degenerate_stop_distance"

        # --- Target -------------------------------------------------------- #
        target, target_level, synthetic = self._select_target(
            entry, risk_per_unit, direction, levels, notes
        )
        if target is None:
            return None, "no_target_meeting_min_rr"

        risk_reward = (target - entry) * sign / risk_per_unit
        if risk_reward < cfg.min_risk_reward:
            return None, "risk_reward_below_minimum"

        if spec is not None:
            entry = spec.round_price(entry)
            stop = spec.round_price(stop)
            target = spec.round_price(target)
            risk_per_unit = abs(entry - stop)
            if risk_per_unit <= 0:
                return None, "degenerate_stop_distance_after_rounding"
            risk_reward = (target - entry) * sign / risk_per_unit

        return (
            TradeGeometry(
                entry_price=entry,
                stop_price=stop,
                target_price=target,
                risk_per_unit=risk_per_unit,
                risk_reward=risk_reward,
                target_level=target_level,
                synthetic_target=synthetic,
                notes=notes,
            ),
            "",
        )

    # ------------------------------------------------------------------ #
    def _select_target(
        self,
        entry: float,
        risk_per_unit: float,
        direction: Direction,
        levels: Sequence[Level],
        notes: list[str],
    ) -> tuple[float | None, Level | None, bool]:
        """Pick the first level ahead that pays at least ``min_risk_reward``."""
        cfg = self.config
        sign = direction.sign
        min_gap = cfg.min_risk_reward * risk_per_unit
        max_gap = cfg.max_target_rr * risk_per_unit

        ahead = LevelDetector.levels_beyond(levels, entry, sign, min_gap=0.0)
        for level in ahead:
            # Conservative fill assumption: aim at the near edge of the zone.
            edge = level.low if sign > 0 else level.high
            distance = (edge - entry) * sign
            if distance < min_gap:
                continue
            if distance > max_gap:
                break
            return edge, level, False

        if cfg.allow_synthetic_target:
            notes.append("synthetic_target_no_level_at_min_rr")
            return entry + sign * min_gap, None, True
        return None, None, False
