"""Period resolution. No database, no model -- pure date arithmetic.

These are the cases that produce a wrong answer which LOOKS right: an
answer for September when October was asked for reads perfectly well.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.analytics.period import Period, PeriodError, month_period, resolve


def R(spec, today=date(2026, 10, 24)):
    return resolve(spec, today=today)


class TestRelative:
    def test_this_month_is_the_whole_calendar_month(self):
        p = R("this_month")
        assert (p.start, p.end) == (date(2026, 10, 1), date(2026, 10, 31))

    def test_last_month(self):
        p = R("last_month")
        assert (p.start, p.end) == (date(2026, 9, 1), date(2026, 9, 30))

    def test_last_month_in_january_crosses_the_year(self):
        p = R("last_month", today=date(2026, 1, 8))
        assert (p.start, p.end) == (date(2025, 12, 1), date(2025, 12, 31))

    def test_this_week_starts_monday_and_ends_today(self):
        # 24 Oct 2026 is a Saturday.
        p = R("this_week")
        assert (p.start, p.end) == (date(2026, 10, 19), date(2026, 10, 24))

    def test_last_week_is_a_full_seven_days(self):
        p = R("last_week")
        assert (p.start, p.end) == (date(2026, 10, 12), date(2026, 10, 18))
        assert p.days == 7

    def test_last_30_days_includes_today(self):
        p = R("last_30_days")
        assert p.days == 30
        assert p.end == date(2026, 10, 24)

    def test_last_3_months_excludes_the_part_built_current_month(self):
        """Including a half-finished October makes every trend look like a fall."""
        p = R("last_3_months")
        assert (p.start, p.end) == (date(2026, 7, 1), date(2026, 9, 30))

    def test_leap_day_is_inside_february(self):
        p = R("this_month", today=date(2028, 2, 10))
        assert p.end == date(2028, 2, 29)

    def test_unknown_period_is_refused_not_guessed(self):
        with pytest.raises(PeriodError):
            R("since_the_dog_died")


class TestPrevious:
    def test_a_whole_month_steps_back_a_calendar_month(self):
        """Not 31 days back, which would straddle September and August."""
        prev = month_period(2026, 10).previous()
        assert (prev.start, prev.end) == (date(2026, 9, 1), date(2026, 9, 30))

    def test_january_steps_back_into_december(self):
        prev = month_period(2026, 1).previous()
        assert (prev.start, prev.end) == (date(2025, 12, 1), date(2025, 12, 31))

    def test_an_arbitrary_range_steps_back_its_own_length(self):
        p = Period(date(2026, 10, 10), date(2026, 10, 16), "a week")
        prev = p.previous()
        assert (prev.start, prev.end) == (date(2026, 10, 3), date(2026, 10, 9))
        assert prev.days == p.days


class TestExplicit:
    def test_custom_range(self):
        p = R({"start": "2026-03-01", "end": "2026-06-30"})
        assert (p.start, p.end) == (date(2026, 3, 1), date(2026, 6, 30))

    def test_a_backwards_range_is_refused(self):
        with pytest.raises(PeriodError):
            R({"start": "2026-06-30", "end": "2026-03-01"})

    def test_half_a_range_is_refused(self):
        with pytest.raises(PeriodError):
            R({"start": "2026-03-01"})
