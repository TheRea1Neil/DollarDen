"""Turning "last month" into two dates.

Split out and kept pure because it is the half of a natural-language
question most likely to be wrong in a way nobody notices. "This week"
is ambiguous (which day starts it), "last month" is ambiguous in
January, and an off-by-one at a month boundary moves real money between
two answers. All of it is testable without a database or a model.

Weeks start Monday. Ranges are INCLUSIVE at both ends, because that is
how people read "1--31 October" and the alternative invites a bug
every time someone forgets which convention this file chose.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta


class PeriodError(ValueError):
    """A period description that cannot be resolved to two dates."""


@dataclass(frozen=True)
class Period:
    start: date
    end: date  # inclusive
    label: str

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise PeriodError(f"period ends before it starts: {self.start}..{self.end}")

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def previous(self) -> "Period":
        """The comparable stretch immediately before this one.

        Month-aligned periods step back a calendar month rather than by
        their own length: the month before a 31-day October is a 30-day
        September, not "the 31 days before 1 October", which would
        straddle two months and compare nothing meaningful.
        """
        if _is_whole_month(self):
            y, m = self.start.year, self.start.month
            py, pm = (y - 1, 12) if m == 1 else (y, m - 1)
            return month_period(py, pm)
        shift = timedelta(days=self.days)
        return Period(self.start - shift, self.end - shift, f"previous {self.days} days")

    def as_dict(self) -> dict[str, str]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat(), "label": self.label}


def _is_whole_month(p: Period) -> bool:
    last = calendar.monthrange(p.start.year, p.start.month)[1]
    return (
        p.start.day == 1
        and p.end.day == last
        and p.start.month == p.end.month
        and p.start.year == p.end.year
    )


def month_period(year: int, month: int) -> Period:
    last = calendar.monthrange(year, month)[1]
    return Period(
        date(year, month, 1),
        date(year, month, last),
        f"{calendar.month_name[month]} {year}",
    )


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


# Accepted shorthand. Anything not here is rejected rather than guessed:
# a question answered for the wrong fortnight is worse than one the app
# admits it did not understand.
_RELATIVE = {
    "today",
    "yesterday",
    "this_week",
    "last_week",
    "this_month",
    "last_month",
    "this_year",
    "last_year",
    "all_time",
}

_LAST_N = re.compile(r"^last_(\d{1,3})_(day|days|week|weeks|month|months)$")


def resolve(spec: str | dict, *, today: date) -> Period:
    """Resolve a period spec against a fixed `today`.

    `today` is passed in, never read from the clock, so a question asked
    at 23:59 and the test that covers it agree about what day it is.
    """
    if isinstance(spec, dict):
        start, end = spec.get("start"), spec.get("end")
        if not start or not end:
            raise PeriodError("an explicit period needs both start and end")
        s, e = date.fromisoformat(str(start)), date.fromisoformat(str(end))
        return Period(s, e, f"{s.isoformat()} to {e.isoformat()}")

    key = str(spec or "").strip().lower().replace(" ", "_").replace("-", "_")

    if key in ("", "this_month"):
        return month_period(today.year, today.month)
    if key == "today":
        return Period(today, today, "today")
    if key == "yesterday":
        d = today - timedelta(days=1)
        return Period(d, d, "yesterday")
    if key == "this_week":
        return Period(week_start(today), today, "this week")
    if key == "last_week":
        s = week_start(today) - timedelta(days=7)
        return Period(s, s + timedelta(days=6), "last week")
    if key == "last_month":
        y, m = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        return month_period(y, m)
    if key == "this_year":
        return Period(date(today.year, 1, 1), today, str(today.year))
    if key == "last_year":
        y = today.year - 1
        return Period(date(y, 1, 1), date(y, 12, 31), str(y))
    if key == "all_time":
        # Far enough back to predate any real account, and still a real
        # date so the SQL stays a plain BETWEEN.
        return Period(date(2000, 1, 1), today, "all time")

    m = _LAST_N.match(key)
    if m:
        n, unit = int(m.group(1)), m.group(2).rstrip("s")
        if n < 1:
            raise PeriodError("a period must cover at least one day")
        if unit == "day":
            # "last 30 days" includes today, so it is 30 days total.
            return Period(today - timedelta(days=n - 1), today, f"last {n} days")
        if unit == "week":
            return Period(today - timedelta(days=n * 7 - 1), today, f"last {n} weeks")
        # Whole calendar months, ending with the one just finished. "The
        # last 3 months" asked on 24 October means Jul--Sep: including a
        # part-built October makes every comparison look like a decline.
        y, mo = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        last = month_period(y, mo)
        first = last
        for _ in range(n - 1):
            py, pm = (first.start.year - 1, 12) if first.start.month == 1 else (
                first.start.year,
                first.start.month - 1,
            )
            first = month_period(py, pm)
        return Period(first.start, last.end, f"last {n} months")

    raise PeriodError(f"unrecognised period: {spec!r}")


KNOWN_PERIODS = sorted(_RELATIVE) + ["last_N_days", "last_N_weeks", "last_N_months"]
