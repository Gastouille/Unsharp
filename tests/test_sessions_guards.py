"""Trading sessions, timing filter and portfolio risk guards."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

import pytest

from unsharp_bot.config import ConfigError, RiskConfig, SessionWindow, TimingConfig
from unsharp_bot.engine.clock import SessionClock, TimingFilter, next_candle_boundary
from unsharp_bot.models import AccountState, Direction
from unsharp_bot.risk.guards import RiskGuard


def utc(year, month, day, hour, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


@pytest.fixture
def ny_session() -> SessionWindow:
    return SessionWindow(
        name="us", start="09:30", end="16:00", timezone="America/New_York",
        days=["mon", "tue", "wed", "thu", "fri"],
    )


def test_session_is_open_during_us_hours(ny_session):
    clock = SessionClock([ny_session])
    # 2026-09-14 is a Monday; 09:30 New York = 13:30 UTC in September (DST).
    assert not clock.is_open(utc(2026, 9, 14, 13, 0))
    assert clock.is_open(utc(2026, 9, 14, 13, 30))
    assert clock.is_open(utc(2026, 9, 14, 19, 0))
    assert not clock.is_open(utc(2026, 9, 14, 20, 30))


def test_session_is_closed_at_the_weekend(ny_session):
    clock = SessionClock([ny_session])
    assert not clock.is_open(utc(2026, 9, 12, 15, 0))   # Saturday
    assert not clock.is_open(utc(2026, 9, 13, 15, 0))   # Sunday


def test_next_open_skips_the_weekend(ny_session):
    clock = SessionClock([ny_session])
    next_open = clock.next_open(utc(2026, 9, 12, 22, 0))  # Saturday evening
    assert next_open == utc(2026, 9, 14, 13, 30)          # Monday open


def test_session_status_reports_progress(ny_session):
    status = SessionClock([ny_session]).status(utc(2026, 9, 14, 14, 30))
    assert status.is_open
    assert status.minutes_since_open == pytest.approx(60.0)
    assert status.minutes_to_close == pytest.approx(330.0)
    assert status.session.name == "us"


def test_session_crossing_midnight():
    asia = SessionWindow(
        name="asia", start="23:00", end="06:00", timezone="UTC",
        days=["mon", "tue", "wed", "thu", "fri"],
    )
    clock = SessionClock([asia])
    assert clock.is_open(utc(2026, 9, 14, 23, 30))   # Monday night
    assert clock.is_open(utc(2026, 9, 15, 2, 0))     # spills into Tuesday
    assert not clock.is_open(utc(2026, 9, 15, 7, 0))


def test_multiple_sessions_are_all_considered():
    europe = SessionWindow(name="eu", start="09:00", end="11:00", timezone="UTC")
    us = SessionWindow(name="us", start="15:00", end="17:00", timezone="UTC")
    clock = SessionClock([europe, us])
    assert clock.active_session(utc(2026, 9, 14, 10, 0)).name == "eu"
    assert clock.active_session(utc(2026, 9, 14, 16, 0)).name == "us"
    assert clock.active_session(utc(2026, 9, 14, 13, 0)) is None


def test_timing_filter_allows_the_opening_window(ny_session):
    clock = SessionClock([ny_session])
    timing = TimingFilter(TimingConfig())  # opening 60 min, pivots every 30 min
    allowed, reason = timing.allows(clock.status(utc(2026, 9, 14, 14, 5)))
    assert allowed and reason == "opening_window"


def test_timing_filter_restricts_to_pivots(ny_session):
    clock = SessionClock([ny_session])
    timing = TimingFilter(TimingConfig())
    # 16:15 UTC = 12:15 New York: 165 min into the session, 15 min from a pivot.
    allowed, reason = timing.allows(clock.status(utc(2026, 9, 14, 16, 15)))
    assert not allowed and reason == "between_pivots"
    allowed, reason = timing.allows(clock.status(utc(2026, 9, 14, 16, 35)))
    assert allowed and reason == "pivot_window"


def test_timing_filter_blackout_window(ny_session):
    clock = SessionClock([ny_session])
    config = dataclasses.replace(TimingConfig(), blackout_windows=["12:00-13:00"])
    timing = TimingFilter(config)
    # 16:30 UTC = 12:30 New York, inside the blackout.
    allowed, reason = timing.allows(clock.status(utc(2026, 9, 14, 16, 30)))
    assert not allowed and reason == "blackout_window"


def test_timing_filter_can_be_disabled(ny_session):
    clock = SessionClock([ny_session])
    timing = TimingFilter(dataclasses.replace(TimingConfig(), enabled=False))
    allowed, _ = timing.allows(clock.status(utc(2026, 9, 14, 16, 15)))
    assert allowed


def test_timing_filter_rejects_outside_a_session(ny_session):
    clock = SessionClock([ny_session])
    timing = TimingFilter(TimingConfig())
    allowed, reason = timing.allows(clock.status(utc(2026, 9, 14, 3, 0)))
    assert not allowed and reason == "outside_session"


def test_next_candle_boundary_aligns_on_the_period():
    moment = datetime(2026, 9, 14, 13, 37, 20, tzinfo=timezone.utc)
    assert next_candle_boundary(moment, 5) == datetime(2026, 9, 14, 13, 40, tzinfo=timezone.utc)
    assert next_candle_boundary(moment, 15) == datetime(2026, 9, 14, 13, 45, tzinfo=timezone.utc)
    with_offset = next_candle_boundary(moment, 5, offset_seconds=4)
    assert with_offset.second == 4


def test_invalid_session_time_is_rejected():
    with pytest.raises(ConfigError):
        SessionWindow(name="bad", start="9h30", end="16:00", timezone="UTC")


def test_invalid_weekday_is_rejected():
    with pytest.raises(ConfigError):
        SessionWindow(name="bad", start="09:30", end="16:00", timezone="UTC", days=["funday"])


# --------------------------------------------------------------------------- #
# Risk guards
# --------------------------------------------------------------------------- #
def account(equity: float) -> AccountState:
    return AccountState(equity, equity, 0.0, equity, "EUR")


def test_guard_allows_a_normal_trade():
    guard = RiskGuard(RiskConfig())
    guard.start_day(utc(2026, 9, 14, 13).date(), 10_000)
    verdict = guard.check("US500", Direction.LONG, utc(2026, 9, 14, 14), account(10_000), 0, 0)
    assert verdict.allowed


def test_guard_caps_open_positions():
    guard = RiskGuard(dataclasses.replace(RiskConfig(), max_open_positions=2))
    guard.start_day(utc(2026, 9, 14, 13).date(), 10_000)
    verdict = guard.check("US500", Direction.LONG, utc(2026, 9, 14, 14), account(10_000), 2, 0)
    assert not verdict.allowed and verdict.reason == "max_open_positions_reached"


def test_guard_caps_positions_per_symbol():
    guard = RiskGuard(RiskConfig())
    guard.start_day(utc(2026, 9, 14, 13).date(), 10_000)
    verdict = guard.check("US500", Direction.LONG, utc(2026, 9, 14, 14), account(10_000), 1, 1)
    assert not verdict.allowed and verdict.reason == "max_positions_per_symbol_reached"


def test_guard_caps_trades_per_day():
    guard = RiskGuard(dataclasses.replace(RiskConfig(), max_trades_per_day=2))
    guard.start_day(utc(2026, 9, 14, 13).date(), 10_000)
    for _ in range(2):
        guard.register_trade("US500", utc(2026, 9, 14, 14))
    verdict = guard.check("US500", Direction.LONG, utc(2026, 9, 14, 15), account(10_000), 0, 0)
    assert not verdict.allowed and verdict.reason == "max_trades_per_day_reached"


def test_guard_halts_on_the_daily_loss_limit():
    guard = RiskGuard(dataclasses.replace(RiskConfig(), daily_loss_limit_fraction=0.05))
    guard.start_day(utc(2026, 9, 14, 13).date(), 10_000)
    verdict = guard.check("US500", Direction.LONG, utc(2026, 9, 14, 14), account(9_400), 0, 0)
    assert not verdict.allowed and verdict.reason == "daily_loss_limit_reached"
    assert guard.halted
    # The halt persists for the rest of the day, even if equity recovers.
    later = guard.check("US500", Direction.LONG, utc(2026, 9, 14, 15), account(10_100), 0, 0)
    assert not later.allowed and later.reason.startswith("day_halted")


def test_guard_cooldown_after_a_loss():
    config = dataclasses.replace(RiskConfig(), cooldown_minutes_after_loss=30)
    guard = RiskGuard(config)
    guard.start_day(utc(2026, 9, 14, 13).date(), 10_000)
    guard.register_close("US500", -120.0, utc(2026, 9, 14, 14))
    blocked = guard.check("US500", Direction.LONG, utc(2026, 9, 14, 14, 10), account(9_880), 0, 0)
    assert not blocked.allowed and blocked.reason == "cooldown_after_loss"
    # Another symbol is unaffected.
    assert guard.check("EURUSD", Direction.LONG, utc(2026, 9, 14, 14, 10), account(9_880), 0, 0).allowed
    # And the cooldown expires.
    assert guard.check("US500", Direction.LONG, utc(2026, 9, 14, 14, 45), account(9_880), 0, 0).allowed


def test_guard_resets_on_a_new_day():
    guard = RiskGuard(dataclasses.replace(RiskConfig(), max_trades_per_day=1))
    guard.start_day(utc(2026, 9, 14, 13).date(), 10_000)
    guard.register_trade("US500", utc(2026, 9, 14, 14))
    assert not guard.check("US500", Direction.LONG, utc(2026, 9, 14, 15), account(10_000), 0, 0).allowed
    # A new calendar day resets the counters.
    assert guard.check("US500", Direction.LONG, utc(2026, 9, 15, 14), account(10_000), 0, 0).allowed


def test_guard_summary_tracks_results():
    guard = RiskGuard(RiskConfig())
    guard.start_day(utc(2026, 9, 14, 13).date(), 10_000)
    guard.register_trade("US500", utc(2026, 9, 14, 14))
    guard.register_close("US500", 250.0, utc(2026, 9, 14, 15))
    guard.register_close("US500", -100.0, utc(2026, 9, 14, 16))
    summary = guard.summary()
    assert summary["trades_taken"] == 1
    assert summary["wins"] == 1 and summary["losses"] == 1
    assert summary["realised_pnl"] == pytest.approx(150.0)
