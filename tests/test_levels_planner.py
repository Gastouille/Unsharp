"""Key-level detection and trade geometry."""

from __future__ import annotations

import dataclasses

import pytest

from unsharp_bot.config import LevelsConfig, UnsharpConfig
from unsharp_bot.models import Level, LevelType
from unsharp_bot.strategy.indicators import atr
from unsharp_bot.strategy.levels import LevelDetector
from unsharp_bot.strategy.planner import TradePlanner
from unsharp_bot.strategy.unsharp import UnsharpDetector

from .conftest import CandleBuilder, long_setup_series


def test_levels_cluster_repeated_touches():
    candles = CandleBuilder().oscillate(60, centre=100.0, amplitude=2.0).build()
    levels = LevelDetector(LevelsConfig()).detect(candles, atr(candles, 14))
    assert levels
    # The oscillation extremes must surface as multi-touch zones.
    assert any(level.touches >= 3 for level in levels)
    assert all(isinstance(level, Level) for level in levels)


def test_previous_day_levels_are_included():
    intraday = CandleBuilder().oscillate(60, centre=100.0, amplitude=2.0).build()
    daily = CandleBuilder(period_minutes=1440).add(99, 105, 95, 101).add(101, 103, 98, 100).build()
    config = dataclasses.replace(LevelsConfig(), cluster_atr=0.05, zone_width_atr=0.0)
    levels = LevelDetector(config).detect(intraday, atr(intraday, 14), daily_candles=daily)
    types = {level.type for level in levels}
    assert LevelType.PREVIOUS_DAY_HIGH in types
    assert LevelType.PREVIOUS_DAY_LOW in types


def test_nearest_respects_the_tolerance_and_side():
    levels = [
        Level(price=95.0, type=LevelType.SWING_LOW),
        Level(price=105.0, type=LevelType.SWING_HIGH),
    ]
    assert LevelDetector.nearest(levels, 95.4, tolerance=1.0) is not None
    # Nothing within 1.0 of 100.0.
    assert LevelDetector.nearest(levels, 100.0, tolerance=1.0) is None
    # "below" keeps supports; "above" keeps resistances.
    assert LevelDetector.nearest(levels, 95.4, tolerance=1.0, side="below").price == 95.0
    assert LevelDetector.nearest(levels, 104.6, tolerance=1.0, side="above").price == 105.0


def test_nearest_side_filter_uses_the_zone_centre():
    """A wide zone can have a near edge while its centre sits on the other side."""
    wide = Level(price=105.0, type=LevelType.SWING_HIGH, width=4.0)  # spans 101..109
    # The near edge (101) is within tolerance of 100, so the distance check passes.
    assert LevelDetector.nearest([wide], 100.0, tolerance=1.5) is wide
    # But the zone is a resistance, so it is excluded when asking for a support.
    assert LevelDetector.nearest([wide], 100.0, tolerance=1.5, side="below") is None
    assert LevelDetector.nearest([wide], 100.0, tolerance=1.5, side="above") is wide


def test_nearest_prefers_the_stronger_level_on_a_tie():
    weak = Level(price=100.0, type=LevelType.ROUND_NUMBER)
    strong = Level(price=100.0, type=LevelType.PREVIOUS_DAY_LOW, touches=3)
    assert LevelDetector.nearest([weak, strong], 100.0, tolerance=1.0) is strong


def test_levels_beyond_is_ordered_by_distance():
    levels = [
        Level(price=110.0, type=LevelType.SWING_HIGH),
        Level(price=105.0, type=LevelType.SWING_HIGH),
        Level(price=95.0, type=LevelType.SWING_LOW),
    ]
    ahead = LevelDetector.levels_beyond(levels, price=100.0, direction_sign=1)
    assert [level.price for level in ahead] == [105.0, 110.0]
    behind = LevelDetector.levels_beyond(levels, price=100.0, direction_sign=-1)
    assert [level.price for level in behind] == [95.0]


def _setup_and_levels():
    candles = long_setup_series()
    atr_value = atr(candles, 14)
    levels = LevelDetector(LevelsConfig()).detect(candles, atr_value)
    result = UnsharpDetector(UnsharpConfig()).detect("TEST", candles, levels, atr_value)
    assert result.found
    return result.setup, levels


def test_planner_builds_a_coherent_long_trade(index_spec):
    setup, levels = _setup_and_levels()
    spec = dataclasses.replace(index_spec, precision=2, tick_size=0.01, tick_value=0.01)
    geometry, reason = TradePlanner(UnsharpConfig()).build(setup, levels, spec)
    assert geometry is not None, reason
    assert geometry.stop_price < geometry.entry_price < geometry.target_price
    assert geometry.risk_reward >= UnsharpConfig().min_risk_reward - 1e-9
    # The stop sits below the wicks of the confirmation zone.
    zone_low = min(candle.low for candle in setup.confirmation)
    assert geometry.stop_price < zone_low


def test_planner_refuses_when_no_target_meets_min_rr(index_spec):
    setup, levels = _setup_and_levels()
    config = dataclasses.replace(
        UnsharpConfig(), min_risk_reward=20.0, allow_synthetic_target=False
    )
    geometry, reason = TradePlanner(config).build(setup, levels, index_spec)
    assert geometry is None
    assert reason in ("no_target_meeting_min_rr", "risk_reward_below_minimum")


def test_synthetic_target_honours_min_rr(index_spec):
    setup, levels = _setup_and_levels()
    config = dataclasses.replace(UnsharpConfig(), min_risk_reward=3.0, allow_synthetic_target=True)
    spec = dataclasses.replace(index_spec, precision=4, tick_size=0.0001, tick_value=0.0001)
    geometry, reason = TradePlanner(config).build(setup, levels, spec)
    assert geometry is not None, reason
    assert geometry.risk_reward == pytest.approx(3.0, abs=0.05)


def test_stop_reference_lead_and_zone_is_wider(index_spec):
    setup, levels = _setup_and_levels()
    zone_config = UnsharpConfig()
    lead_config = dataclasses.replace(zone_config, stop_reference="lead_and_zone")

    narrow, _ = TradePlanner(zone_config).build(setup, levels, index_spec)
    # The setup carries the zone extreme computed with the default reference, so
    # rebuild it with the wider reference to compare like for like.
    from unsharp_bot.strategy.unsharp import UnsharpDetector as Detector

    candles = long_setup_series()
    atr_value = atr(candles, 14)
    result = Detector(lead_config).detect("TEST", candles, levels, atr_value)
    wide, _ = TradePlanner(lead_config).build(result.setup, levels, index_spec)
    assert wide.stop_price <= narrow.stop_price


def test_minimum_stop_distance_is_enforced(index_spec):
    setup, levels = _setup_and_levels()
    config = dataclasses.replace(UnsharpConfig(), min_stop_distance_atr=3.0)
    geometry, reason = TradePlanner(config).build(setup, levels, index_spec)
    assert geometry is not None, reason
    assert "stop_widened_to_minimum_distance" in geometry.notes
    # Widened to 3 ATR, up to the instrument's price rounding.
    assert geometry.risk_per_unit == pytest.approx(3.0 * setup.atr, abs=2 * index_spec.point)
