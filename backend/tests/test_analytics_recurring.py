"""Subscription detection: the merchant grouping and cadence rules.

Written against the detector's own helpers where possible so the
tricky part -- deciding what counts as regular -- is testable without a
database.
"""
from __future__ import annotations

from app.analytics.recurring import _classify, normalise_merchant


class TestNormalise:
    def test_strips_a_till_number(self):
        assert normalise_merchant("SQ *BLUE BOTTLE 4821") == "blue bottle"

    def test_two_till_numbers_collapse_to_one_merchant(self):
        """Left apart, each falls under the three-charge floor and vanishes."""
        a = normalise_merchant("SQ *BLUE BOTTLE 4821")
        b = normalise_merchant("SQ *BLUE BOTTLE 9930")
        assert a == b

    def test_keeps_a_name_that_legitimately_ends_in_a_number(self):
        # Two digits or more is a store code; "7" here is part of the name.
        assert normalise_merchant("STORE 7") == "store 7"

    def test_case_and_spacing(self):
        assert normalise_merchant("  Netflix.com  ") == "netflix.com"


class TestCadence:
    def test_monthly_on_a_shifting_day_of_month(self):
        got = _classify([31, 28, 31, 30])
        assert got is not None and got[0] == "monthly"

    def test_weekly(self):
        got = _classify([7, 7, 8, 7])
        assert got is not None and got[0] == "weekly"

    def test_yearly(self):
        got = _classify([365, 366])
        assert got is not None and got[0] == "yearly"

    def test_irregular_shopping_is_not_a_subscription(self):
        """Groceries every 4 to 20 days is a habit, not a standing charge."""
        assert _classify([4, 19, 6, 20, 9]) is None

    def test_a_gap_nothing_bills_on_is_rejected(self):
        assert _classify([45, 44, 46]) is None

    def test_regularity_is_higher_for_an_exact_schedule(self):
        exact = _classify([30, 30, 30])
        loose = _classify([27, 34, 30])
        assert exact and loose
        assert exact[2] > loose[2]
