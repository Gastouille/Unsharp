"""The Unsharp Candles detector: Lead -> Confirmation -> Execution.

Reading of the method implemented here
--------------------------------------
1. **Lead Candle** - a large directional candle that pushes into a key level,
   sweeping stops and scaring retail out of the way.  Large = big body relative
   to both the ATR and the recent average body, with little wick (a *clean*
   directional push).

2. **Confirmation zone** - one or more candles that fail to extend the Lead.
   Price chops around the level with small bodies and *rejection wicks on the
   opposite side*: someone is absorbing the flow and defending the level.

3. **Execution Candle** - a decisive candle back in the opposite direction that
   closes beyond the confirmation zone.  This is the entry trigger.

All three must print on or very near a key level; a Lead that lands "in thin
air" is discarded, as the method requires.

The detector is pure: candles in, :class:`UnsharpSetup` out.  No I/O, no state,
which makes it trivially unit-testable and reusable by the backtester.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..config import UnsharpConfig
from ..models import Candle, Direction, Level, RejectedSetup, UnsharpSetup

#: How far down the pipeline a rejection happened.  When several confirmation
#: zone lengths are tried, the *most advanced* rejection is the informative one:
#: "no key level nearby" tells you far more than "this candle is not a Lead".
_REJECTION_STAGE: dict[str, int] = {
    "execution_wrong_direction": 0,
    "execution_body_too_small": 0,
    "execution_too_wicky": 0,
    "execution_close_not_decisive": 0,
    # "wrong direction" only means "this candle was never a candidate".
    "lead_wrong_direction": 0,
    "lead_body_too_small_vs_atr": 1,
    "lead_body_too_small_vs_average": 1,
    "lead_too_wicky": 1,
    "empty_confirmation_zone": 2,
    "confirmation_extends_lead": 2,
    "confirmation_bodies_too_large": 2,
    "confirmation_zone_too_wide": 2,
    "confirmation_no_rejection_wick": 2,
    "execution_does_not_break_zone": 3,
    "no_key_level_nearby": 4,
}


def _stage(reason: str) -> int:
    return _REJECTION_STAGE.get(reason, 0)
from .indicators import atr as compute_atr
from .indicators import average_body
from .levels import LevelDetector


@dataclass(slots=True)
class DetectionResult:
    """Outcome of one detector pass on one symbol."""

    setup: UnsharpSetup | None = None
    rejections: list[RejectedSetup] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.rejections is None:
            self.rejections = []

    @property
    def found(self) -> bool:
        return self.setup is not None


class UnsharpDetector:
    """Scans a candle series for a completed Unsharp sequence.

    The sequence is always evaluated with the **last closed candle** as the
    Execution Candle, so the detector never repaints: a signal emitted at bar
    *t* stays valid forever.
    """

    def __init__(self, config: UnsharpConfig) -> None:
        self.config = config

    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #
    def detect(
        self,
        symbol: str,
        candles: Sequence[Candle],
        levels: Sequence[Level],
        atr_value: float | None = None,
    ) -> DetectionResult:
        """Look for a setup ending on ``candles[-1]``.

        Both directions are tested; if both somehow match, the one with the
        larger Lead body (the more significant sweep) wins.
        """
        cfg = self.config
        result = DetectionResult()

        min_needed = cfg.max_confirmation_candles + max(cfg.body_average_window, cfg.atr_period) + 2
        if len(candles) < cfg.min_confirmation_candles + 3:
            result.rejections.append(
                RejectedSetup(symbol, _last_ts(candles), "not_enough_candles")
            )
            return result

        atr_value = atr_value if atr_value and atr_value > 0 else compute_atr(candles, cfg.atr_period)
        if atr_value <= 0:
            result.rejections.append(
                RejectedSetup(symbol, _last_ts(candles), "atr_unavailable")
            )
            return result

        execution = candles[-1]
        candidates: list[UnsharpSetup] = []

        for direction in (Direction.LONG, Direction.SHORT):
            setup, rejection = self._try_direction(
                symbol, direction, candles, levels, atr_value, execution,
                warm=len(candles) >= min_needed,
            )
            if setup is not None:
                candidates.append(setup)
            elif rejection is not None:
                result.rejections.append(rejection)

        if candidates:
            candidates.sort(key=lambda s: s.lead.body, reverse=True)
            result.setup = candidates[0]
        return result

    # ------------------------------------------------------------------ #
    # Per-direction scan
    # ------------------------------------------------------------------ #
    def _try_direction(
        self,
        symbol: str,
        direction: Direction,
        candles: Sequence[Candle],
        levels: Sequence[Level],
        atr_value: float,
        execution: Candle,
        warm: bool,
    ) -> tuple[UnsharpSetup | None, RejectedSetup | None]:
        """Test one direction.  ``direction`` is the direction we would *trade*.

        For a LONG, the Lead Candle is bearish (a sell-off into support) and the
        Execution Candle is bullish.
        """
        cfg = self.config
        lead_direction = direction.opposite

        # --- 3. Execution Candle -------------------------------------------
        ok, why, exec_metrics = self._check_execution(execution, direction, atr_value)
        if not ok:
            return None, RejectedSetup(symbol, execution.timestamp, why, direction, exec_metrics)

        # Keep the most advanced rejection across the candidate zone lengths.
        best_rejection: RejectedSetup | None = None

        def remember(rejection: RejectedSetup) -> None:
            nonlocal best_rejection
            if best_rejection is None or _stage(rejection.reason) >= _stage(best_rejection.reason):
                best_rejection = rejection

        # Try every admissible confirmation-zone length, shortest first: the
        # tightest zone gives the tightest stop and the best R/R.
        for zone_size in range(cfg.min_confirmation_candles, cfg.max_confirmation_candles + 1):
            lead_index = len(candles) - 2 - zone_size
            if lead_index < 1:
                break
            lead = candles[lead_index]
            zone = list(candles[lead_index + 1: len(candles) - 1])
            if len(zone) != zone_size:
                break

            history = candles[: lead_index + 1]
            body_reference = average_body(history, cfg.body_average_window) if warm else 0.0

            # --- 1. Lead Candle -------------------------------------------
            ok, why, lead_metrics = self._check_lead(lead, lead_direction, atr_value, body_reference)
            if not ok:
                remember(RejectedSetup(
                    symbol, execution.timestamp, why, direction,
                    {"zone_size": zone_size, **lead_metrics},
                ))
                continue

            # --- 2. Confirmation zone --------------------------------------
            ok, why, zone_metrics = self._check_confirmation(zone, lead, direction, atr_value)
            if not ok:
                remember(RejectedSetup(
                    symbol, execution.timestamp, why, direction,
                    {"zone_size": zone_size, **zone_metrics},
                ))
                continue

            # Execution must close beyond the zone (the pivot is confirmed).
            zone_extreme_against = (
                min(c.low for c in zone) if direction is Direction.LONG
                else max(c.high for c in zone)
            )
            zone_extreme_with = (
                max(c.high for c in zone) if direction is Direction.LONG
                else min(c.low for c in zone)
            )
            if cfg.execution_must_break_zone:
                breakout = (execution.close - zone_extreme_with) * direction.sign
                required = cfg.execution_min_breakout_atr * atr_value
                if breakout < required:
                    remember(RejectedSetup(
                        symbol, execution.timestamp, "execution_does_not_break_zone", direction,
                        {"zone_size": zone_size, "breakout_atr": breakout / atr_value},
                    ))
                    continue

            # --- 4. Level anchoring ----------------------------------------
            # The stop reference is the extreme the level is supposed to defend.
            stop_reference = zone_extreme_against
            if cfg.stop_reference == "lead_and_zone":
                lead_extreme = lead.low if direction is Direction.LONG else lead.high
                stop_reference = (
                    min(stop_reference, lead_extreme) if direction is Direction.LONG
                    else max(stop_reference, lead_extreme)
                )

            tolerance = cfg.level_tolerance_atr * atr_value
            level = LevelDetector.nearest(
                levels, zone_extreme_against, tolerance,
                side="below" if direction is Direction.LONG else "above",
            )
            if level is None:
                # Second chance: a level sitting anywhere inside the zone still
                # counts as "the candles printed on a level".
                level = LevelDetector.nearest(levels, zone_extreme_against, tolerance, side="any")
            if level is None:
                if cfg.require_level:
                    remember(RejectedSetup(
                        symbol, execution.timestamp, "no_key_level_nearby", direction,
                        {"zone_size": zone_size, "zone_extreme": zone_extreme_against},
                    ))
                    continue
                level = Level(price=zone_extreme_against, type=_fallback_level_type(direction))

            metrics = {
                "atr": atr_value,
                "zone_size": float(zone_size),
                "level_distance_atr": level.distance_to(zone_extreme_against) / atr_value,
                **lead_metrics,
                **zone_metrics,
                **exec_metrics,
            }
            setup = UnsharpSetup(
                symbol=symbol,
                direction=direction,
                lead=lead,
                confirmation=zone,
                execution=execution,
                level=level,
                atr=atr_value,
                zone_extreme=stop_reference,
                metrics=metrics,
            )
            return setup, None

        return None, best_rejection

    # ------------------------------------------------------------------ #
    # Individual candle checks
    # ------------------------------------------------------------------ #
    def _check_lead(
        self,
        lead: Candle,
        lead_direction: Direction,
        atr_value: float,
        body_reference: float,
    ) -> tuple[bool, str, dict[str, float]]:
        """A big, clean, directional candle."""
        cfg = self.config
        metrics = {
            "lead_body_atr": lead.body / atr_value if atr_value else 0.0,
            "lead_body_to_range": lead.body / lead.range if lead.range else 0.0,
        }
        if body_reference > 0:
            metrics["lead_body_ratio"] = lead.body / body_reference

        if lead.direction is not lead_direction:
            return False, "lead_wrong_direction", metrics
        if metrics["lead_body_atr"] < cfg.lead_min_body_atr:
            return False, "lead_body_too_small_vs_atr", metrics
        if body_reference > 0 and metrics.get("lead_body_ratio", 0.0) < cfg.lead_min_body_ratio:
            return False, "lead_body_too_small_vs_average", metrics
        if metrics["lead_body_to_range"] < cfg.lead_min_body_to_range:
            return False, "lead_too_wicky", metrics
        return True, "", metrics

    def _check_confirmation(
        self,
        zone: Sequence[Candle],
        lead: Candle,
        direction: Direction,
        atr_value: float,
    ) -> tuple[bool, str, dict[str, float]]:
        """Price refuses to extend; small bodies and rejection wicks on a level."""
        cfg = self.config
        if not zone:
            return False, "empty_confirmation_zone", {}

        zone_high = max(c.high for c in zone)
        zone_low = min(c.low for c in zone)
        zone_range = zone_high - zone_low
        mean_body = sum(c.body for c in zone) / len(zone)
        wick_ratios = [c.wick_ratio(direction) for c in zone]
        wick_hits = sum(1 for r in wick_ratios if r >= cfg.confirmation_min_wick_ratio)

        metrics = {
            "zone_mean_body_vs_lead": mean_body / lead.body if lead.body else 0.0,
            "zone_range_vs_lead_range": zone_range / lead.range if lead.range else 0.0,
            "zone_max_wick_ratio": max(wick_ratios) if wick_ratios else 0.0,
            "zone_wick_share": wick_hits / len(zone),
        }

        # (a) The Lead is not extended: no candle pushes meaningfully further.
        lead_extreme = lead.low if direction is Direction.LONG else lead.high
        overshoot = (
            (lead_extreme - zone_low) if direction is Direction.LONG
            else (zone_high - lead_extreme)
        )
        metrics["zone_overshoot_atr"] = overshoot / atr_value if atr_value else 0.0
        if metrics["zone_overshoot_atr"] > cfg.confirmation_max_overshoot_atr:
            return False, "confirmation_extends_lead", metrics

        # (b) Small bodies: the market is chewing, not trending.
        if metrics["zone_mean_body_vs_lead"] > cfg.confirmation_max_body_ratio:
            return False, "confirmation_bodies_too_large", metrics

        # (c) The whole zone stays compact relative to the Lead.
        if metrics["zone_range_vs_lead_range"] > cfg.confirmation_max_zone_to_lead_range:
            return False, "confirmation_zone_too_wide", metrics

        # (d) Rejection wicks on the side the big player is defending.
        if metrics["zone_wick_share"] < cfg.confirmation_min_wick_share:
            return False, "confirmation_no_rejection_wick", metrics

        return True, "", metrics

    def _check_execution(
        self,
        execution: Candle,
        direction: Direction,
        atr_value: float,
    ) -> tuple[bool, str, dict[str, float]]:
        """A decisive candle back in the trade direction."""
        cfg = self.config
        close_position = (
            execution.close_position if direction is Direction.LONG
            else 1.0 - execution.close_position
        )
        metrics = {
            "execution_body_atr": execution.body / atr_value if atr_value else 0.0,
            "execution_body_to_range": execution.body / execution.range if execution.range else 0.0,
            "execution_close_position": close_position,
        }
        if execution.direction is not direction:
            return False, "execution_wrong_direction", metrics
        if metrics["execution_body_atr"] < cfg.execution_min_body_atr:
            return False, "execution_body_too_small", metrics
        if metrics["execution_body_to_range"] < cfg.execution_min_body_to_range:
            return False, "execution_too_wicky", metrics
        if close_position < cfg.execution_min_close_position:
            return False, "execution_close_not_decisive", metrics
        return True, "", metrics


def _fallback_level_type(direction: Direction):
    from ..models import LevelType

    return LevelType.RECENT_LOW if direction is Direction.LONG else LevelType.RECENT_HIGH


def _last_ts(candles: Sequence[Candle]):
    from datetime import datetime, timezone

    return candles[-1].timestamp if candles else datetime.now(timezone.utc)
