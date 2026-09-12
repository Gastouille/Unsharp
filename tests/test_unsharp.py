"""Detection of the Lead / Confirmation / Execution sequence."""

from __future__ import annotations

import dataclasses


from unsharp_bot.config import LevelsConfig, UnsharpConfig
from unsharp_bot.models import Direction, Level, LevelType
from unsharp_bot.strategy.indicators import atr
from unsharp_bot.strategy.levels import LevelDetector
from unsharp_bot.strategy.unsharp import UnsharpDetector

from .conftest import long_setup_series, short_setup_series


def _detect(candles, unsharp_config=None, levels_config=None):
    atr_value = atr(candles, 14)
    levels = LevelDetector(levels_config or LevelsConfig()).detect(candles, atr_value)
    detector = UnsharpDetector(unsharp_config or UnsharpConfig())
    return detector.detect("TEST", candles, levels, atr_value), levels, atr_value


def test_detects_long_setup():
    result, _, _ = _detect(long_setup_series())
    assert result.found
    setup = result.setup
    assert setup.direction is Direction.LONG
    assert setup.lead.is_bearish          # the Lead pushes against the trade
    assert setup.execution.is_bullish     # the Execution confirms the pivot
    assert len(setup.confirmation) == 2
    # The sequence prints on a real level, not in thin air.
    assert setup.level.distance_to(min(c.low for c in setup.confirmation)) == 0.0


def test_detects_short_setup():
    result, _, _ = _detect(short_setup_series())
    assert result.found
    setup = result.setup
    assert setup.direction is Direction.SHORT
    assert setup.lead.is_bullish
    assert setup.execution.is_bearish


def test_rejects_setup_without_a_level():
    """A Lead landing 'in the void' must be discarded."""
    candles = long_setup_series()
    atr_value = atr(candles, 14)
    detector = UnsharpDetector(UnsharpConfig())
    # Only far-away levels are supplied.
    far_levels = [Level(price=50.0, type=LevelType.SWING_LOW)]
    result = detector.detect("TEST", candles, far_levels, atr_value)
    assert not result.found
    assert any(r.reason == "no_key_level_nearby" for r in result.rejections)


def test_require_level_false_allows_the_setup():
    candles = long_setup_series()
    atr_value = atr(candles, 14)
    config = dataclasses.replace(UnsharpConfig(), require_level=False)
    result = UnsharpDetector(config).detect("TEST", candles, [], atr_value)
    assert result.found


def test_rejects_when_the_lead_is_too_small():
    config = dataclasses.replace(UnsharpConfig(), lead_min_body_atr=5.0)
    result, _, _ = _detect(long_setup_series(), config)
    assert not result.found
    assert any("lead_body_too_small" in r.reason for r in result.rejections)


def test_rejects_when_the_execution_does_not_reverse():
    """A weak, indecisive Execution Candle is not a trigger."""
    candles = long_setup_series()
    candles[-1] = dataclasses.replace(candles[-1], close=98.31, high=98.35)
    result, _, _ = _detect(candles)
    assert not result.found


def test_rejects_when_the_confirmation_extends_the_lead():
    """If price keeps falling after the Lead, nobody is defending the level."""
    candles = long_setup_series()
    # Push the confirmation zone far below the Lead low (97.95).
    candles[-3] = dataclasses.replace(candles[-3], low=96.50)
    candles[-2] = dataclasses.replace(candles[-2], low=96.30)
    result, _, _ = _detect(candles)
    assert not result.found


def test_rejects_when_confirmation_has_no_rejection_wick():
    candles = long_setup_series()
    # Remove the lower wicks: no visible absorption of the selling flow.
    candles[-3] = dataclasses.replace(candles[-3], low=98.15)
    candles[-2] = dataclasses.replace(candles[-2], low=98.25)
    result, _, _ = _detect(candles)
    assert not result.found
    assert any(
        r.reason in ("confirmation_no_rejection_wick", "confirmation_zone_too_wide")
        for r in result.rejections
    )


def test_min_confirmation_candles_is_enforced():
    config = dataclasses.replace(
        UnsharpConfig(), min_confirmation_candles=4, max_confirmation_candles=6
    )
    result, _, _ = _detect(long_setup_series(), config)
    # The fixture only has two confirmation candles.
    assert not result.found


def test_detector_needs_history():
    detector = UnsharpDetector(UnsharpConfig())
    result = detector.detect("TEST", [], [], 1.0)
    assert not result.found
    assert result.rejections[0].reason == "not_enough_candles"


def test_setup_is_serialisable():
    result, _, _ = _detect(long_setup_series())
    payload = result.setup.to_dict()
    assert payload["direction"] == "LONG"
    assert payload["level"]["type"]
    assert len(payload["confirmation"]) == 2
    assert "lead_body_atr" in payload["metrics"]
