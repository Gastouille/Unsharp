"""Trading sessions and the 'trade when the market is active' timing filter.

Two responsibilities:

* :class:`SessionClock` - is the market inside one of my configured windows,
  and when does the next one open?  Windows are timezone-aware and may cross
  midnight (useful for the Asian session or index futures).
* :class:`TimingFilter` - the Unsharp method favours the open and the periodic
  30/60-minute pivots.  This filter allows entries during the first N minutes
  of a session and inside a tolerance window around each clock pivot.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Sequence

from ..config import SessionWindow, TimingConfig, parse_time_window


@dataclass(slots=True)
class SessionStatus:
    """Where we stand relative to the configured sessions."""

    is_open: bool
    session: SessionWindow | None
    #: Local time inside the session timezone (None when no session is active).
    local_time: datetime | None
    #: Minutes since the session opened (None when closed).
    minutes_since_open: float | None
    #: Minutes until the session closes (None when closed).
    minutes_to_close: float | None
    #: Next opening moment in UTC (None when one is already open).
    next_open: datetime | None


class SessionClock:
    """Answers session questions for a list of :class:`SessionWindow`."""

    def __init__(self, sessions: Sequence[SessionWindow]) -> None:
        if not sessions:
            raise ValueError("At least one session window is required")
        self.sessions = list(sessions)

    # ------------------------------------------------------------------ #
    def status(self, moment: datetime | None = None) -> SessionStatus:
        moment = _as_utc(moment)
        for session in self.sessions:
            local = moment.astimezone(session.tzinfo)
            start_dt = self._session_start_for(session, local)
            if start_dt is None:
                continue
            end_dt = self._end_from_start(session, start_dt)
            if start_dt <= local < end_dt:
                return SessionStatus(
                    is_open=True,
                    session=session,
                    local_time=local,
                    minutes_since_open=(local - start_dt).total_seconds() / 60.0,
                    minutes_to_close=(end_dt - local).total_seconds() / 60.0,
                    next_open=None,
                )
        return SessionStatus(
            is_open=False,
            session=None,
            local_time=None,
            minutes_since_open=None,
            minutes_to_close=None,
            next_open=self.next_open(moment),
        )

    def is_open(self, moment: datetime | None = None) -> bool:
        return self.status(moment).is_open

    def active_session(self, moment: datetime | None = None) -> SessionWindow | None:
        return self.status(moment).session

    # ------------------------------------------------------------------ #
    def next_open(self, moment: datetime | None = None) -> datetime | None:
        """UTC timestamp of the next session opening (searching 14 days ahead)."""
        moment = _as_utc(moment)
        best: datetime | None = None
        for session in self.sessions:
            local_now = moment.astimezone(session.tzinfo)
            for day_offset in range(0, 15):
                day = (local_now + timedelta(days=day_offset)).date()
                if day.weekday() not in session.weekdays:
                    continue
                start_local = datetime.combine(day, session.start_time, tzinfo=session.tzinfo)
                if start_local <= local_now:
                    continue
                start_utc = start_local.astimezone(timezone.utc)
                if best is None or start_utc < best:
                    best = start_utc
                break
        return best

    def session_end(self, session: SessionWindow, moment: datetime | None = None) -> datetime | None:
        """UTC timestamp at which the currently running ``session`` closes."""
        moment = _as_utc(moment)
        local = moment.astimezone(session.tzinfo)
        start_dt = self._session_start_for(session, local)
        if start_dt is None:
            return None
        return self._end_from_start(session, start_dt).astimezone(timezone.utc)

    # ------------------------------------------------------------------ #
    def _session_start_for(self, session: SessionWindow, local: datetime) -> datetime | None:
        """Opening datetime of the session instance containing ``local``, if any.

        Handles windows that cross midnight by also testing the previous day.
        """
        candidates: list[date] = [local.date()]
        if session.crosses_midnight:
            candidates.append(local.date() - timedelta(days=1))
        for day in candidates:
            if day.weekday() not in session.weekdays:
                continue
            start_dt = datetime.combine(day, session.start_time, tzinfo=session.tzinfo)
            end_dt = self._end_from_start(session, start_dt)
            if start_dt <= local < end_dt:
                return start_dt
        return None

    @staticmethod
    def _end_from_start(session: SessionWindow, start_dt: datetime) -> datetime:
        end_day = start_dt.date() + (timedelta(days=1) if session.crosses_midnight else timedelta())
        return datetime.combine(end_day, session.end_time, tzinfo=session.tzinfo)


class TimingFilter:
    """Restricts entries to high-activity moments."""

    def __init__(self, config: TimingConfig) -> None:
        self.config = config
        self._blackouts: list[tuple[time, time]] = [
            parse_time_window(window, "timing.blackout_windows")
            for window in config.blackout_windows
        ]

    def allows(self, status: SessionStatus) -> tuple[bool, str]:
        """``(allowed, reason)`` for the moment described by ``status``."""
        cfg = self.config
        if not status.is_open or status.session is None:
            return False, "outside_session"
        if not cfg.enabled:
            return True, "timing_filter_disabled"

        local = status.local_time
        assert local is not None

        # Blackout windows always win.
        for start, end in self._blackouts:
            if _within(local.time(), start, end):
                return False, "blackout_window"

        # A session may impose its own "opening only" restriction.
        session_window = status.session.opening_window_minutes
        if session_window > 0 and (status.minutes_since_open or 0) > session_window:
            return False, "outside_session_opening_window"

        # The market open is always a valid moment.
        if cfg.opening_minutes > 0 and (status.minutes_since_open or 0) <= cfg.opening_minutes:
            return True, "opening_window"

        # Otherwise: near a clock pivot (every 30 or 60 minutes).
        if cfg.pivot_period_minutes > 0:
            distance = self.minutes_to_nearest_pivot(local, cfg.pivot_period_minutes)
            if distance <= max(0, cfg.pivot_window_minutes):
                return True, "pivot_window"
            return False, "between_pivots"

        return True, "no_pivot_restriction"

    @staticmethod
    def minutes_to_nearest_pivot(local: datetime, period_minutes: int) -> float:
        """Distance in minutes to the closest :00 / :30 (or N-minute) boundary."""
        if period_minutes <= 0:
            return 0.0
        minutes = local.minute + local.second / 60.0
        offset = minutes % period_minutes
        return min(offset, period_minutes - offset)


def _within(moment: time, start: time, end: time) -> bool:
    """Time-of-day containment, tolerant of windows crossing midnight."""
    if start <= end:
        return start <= moment < end
    return moment >= start or moment < end


def _as_utc(moment: datetime | None) -> datetime:
    if moment is None:
        return datetime.now(timezone.utc)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def next_candle_boundary(
    moment: datetime, period_minutes: int, offset_seconds: float = 0.0
) -> datetime:
    """Next candle-close moment (plus ``offset_seconds`` of server lag)."""
    moment = _as_utc(moment)
    period = timedelta(minutes=period_minutes)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    elapsed = (moment - epoch) // period
    boundary = epoch + (elapsed + 1) * period
    return boundary + timedelta(seconds=offset_seconds)
