"""Tests for the paycheck split engine.

The worked example mirrors the real plan in docs/06:
  tithe 10% of gross, $350 rent, $150 health insurance,
  +$50 car insurance only if it is under $450,
  refill Food to $300, refill Short-Term Savings to $2000,
  remainder to the New Car Fund.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.budget.split import (
    RuleType,
    SplitPlanError,
    SplitRule,
    apply_split,
)

TITHING = "cat-tithing"
RENT = "cat-rent"
HEALTH = "cat-health"
CAR_INS = "cat-car-insurance"
FOOD = "cat-food"
SAVINGS = "cat-savings"
NEWCAR = "cat-newcar"


def real_plan() -> list[SplitRule]:
    return [
        SplitRule(TITHING, 1, RuleType.PERCENT, {"rate": "0.10", "of": "gross"}),
        SplitRule(RENT, 2, RuleType.FIXED, {"amount_cents": 350_00}),
        SplitRule(HEALTH, 3, RuleType.FIXED, {"amount_cents": 150_00}),
        SplitRule(
            CAR_INS,
            4,
            RuleType.CONDITIONAL,
            {
                "when": {"field": "balance", "op": "<", "value_cents": 450_00},
                "then": {"rule_type": "fixed", "params": {"amount_cents": 50_00}},
            },
        ),
        SplitRule(FOOD, 5, RuleType.REFILL_TO, {"target_cents": 300_00}),
        SplitRule(SAVINGS, 6, RuleType.REFILL_TO, {"target_cents": 2000_00}),
        SplitRule(NEWCAR, 7, RuleType.REMAINDER),
    ]


def test_worked_example_from_docs() -> None:
    """A $2000 paycheck with Food at $88, savings full, car insurance low."""
    balances = {FOOD: 88_00, SAVINGS: 2000_00, CAR_INS: 400_00}
    r = apply_split(2000_00, real_plan(), balances)

    got = {a.category_id: a.amount_cents for a in r.allocations}
    assert got[TITHING] == 200_00          # 10% of gross, first
    assert got[RENT] == 350_00
    assert got[HEALTH] == 150_00
    assert got[CAR_INS] == 50_00           # 400 < 450, so it fires
    assert got[FOOD] == 212_00             # 300 - 88
    assert SAVINGS not in got              # already at target, contributes 0
    assert got[NEWCAR] == 1038_00          # the rest

    assert r.total_cents == 2000_00
    assert r.remaining_cents == 0
    assert r.unfunded == []


def test_conditional_does_not_fire_when_predicate_false() -> None:
    balances = {FOOD: 300_00, SAVINGS: 2000_00, CAR_INS: 500_00}  # 500 >= 450
    r = apply_split(1000_00, real_plan(), balances)
    got = {a.category_id: a.amount_cents for a in r.allocations}
    assert CAR_INS not in got
    assert got[NEWCAR] == 1000_00 - 100_00 - 350_00 - 150_00


def test_refill_contributes_nothing_when_already_at_target() -> None:
    balances = {FOOD: 300_00, SAVINGS: 2000_00, CAR_INS: 999_00}
    r = apply_split(1000_00, real_plan(), balances)
    got = {a.category_id: a.amount_cents for a in r.allocations}
    assert FOOD not in got
    assert SAVINGS not in got


def test_small_paycheck_reports_unfunded_rules_instead_of_silently_underfunding() -> None:
    """$400 cannot satisfy tithe + rent + health. Later rules must be flagged."""
    balances = {FOOD: 0, SAVINGS: 0, CAR_INS: 0}
    r = apply_split(400_00, real_plan(), balances)

    got = {a.category_id: a.amount_cents for a in r.allocations}
    assert got[TITHING] == 40_00
    assert got[RENT] == 350_00
    assert got[HEALTH] == 10_00            # clamped: only $10 left

    unfunded = dict(r.unfunded)
    assert unfunded[HEALTH] == 140_00      # wanted 150, got 10
    assert CAR_INS in unfunded and FOOD in unfunded
    assert r.total_cents == 400_00         # still exactly the gross
    assert r.remaining_cents == 0


def test_percent_of_remaining_differs_from_percent_of_gross() -> None:
    rules = [
        SplitRule(RENT, 1, RuleType.FIXED, {"amount_cents": 500_00}),
        SplitRule(TITHING, 2, RuleType.PERCENT, {"rate": "0.10", "of": "remaining"}),
        SplitRule(NEWCAR, 3, RuleType.REMAINDER),
    ]
    r = apply_split(1000_00, rules, {})
    got = {a.category_id: a.amount_cents for a in r.allocations}
    assert got[TITHING] == 50_00           # 10% of the 500 left, not of 1000


def test_rounding_is_half_up_and_never_loses_a_cent() -> None:
    """10% of $1234.57 = $123.457 -> 123.46, and the remainder absorbs the rest."""
    rules = [
        SplitRule(TITHING, 1, RuleType.PERCENT, {"rate": "0.10", "of": "gross"}),
        SplitRule(NEWCAR, 2, RuleType.REMAINDER),
    ]
    r = apply_split(1234_57, rules, {})
    got = {a.category_id: a.amount_cents for a in r.allocations}
    assert got[TITHING] == 123_46
    assert r.total_cents == 1234_57


def test_plan_without_remainder_is_rejected() -> None:
    rules = [SplitRule(RENT, 1, RuleType.FIXED, {"amount_cents": 100_00})]
    with pytest.raises(SplitPlanError, match="remainder"):
        apply_split(1000_00, rules, {})


def test_remainder_must_be_last() -> None:
    rules = [
        SplitRule(NEWCAR, 1, RuleType.REMAINDER),
        SplitRule(RENT, 2, RuleType.FIXED, {"amount_cents": 100_00}),
    ]
    with pytest.raises(SplitPlanError, match="last"):
        apply_split(1000_00, rules, {})


def test_two_remainder_rules_rejected() -> None:
    rules = [
        SplitRule(FOOD, 1, RuleType.REMAINDER),
        SplitRule(NEWCAR, 2, RuleType.REMAINDER),
    ]
    with pytest.raises(SplitPlanError):
        apply_split(1000_00, rules, {})


def test_zero_paycheck_allocates_nothing() -> None:
    r = apply_split(0, real_plan(), {FOOD: 0, SAVINGS: 0, CAR_INS: 0})
    assert r.allocations == []
    assert r.total_cents == 0


def test_disabled_rules_are_skipped() -> None:
    rules = [
        SplitRule(RENT, 1, RuleType.FIXED, {"amount_cents": 100_00}, enabled=False),
        SplitRule(NEWCAR, 2, RuleType.REMAINDER),
    ]
    r = apply_split(500_00, rules, {})
    got = {a.category_id: a.amount_cents for a in r.allocations}
    assert RENT not in got
    assert got[NEWCAR] == 500_00


# --------------------------------------------------------------------------- #
# Property-based: the invariants must hold for ANY plan and ANY paycheck
# --------------------------------------------------------------------------- #
@given(
    gross=st.integers(min_value=0, max_value=50_000_00),
    rent=st.integers(min_value=0, max_value=5_000_00),
    rate=st.decimals(min_value=Decimal("0"), max_value=Decimal("1"), places=2),
    food_balance=st.integers(min_value=0, max_value=1_000_00),
    food_target=st.integers(min_value=0, max_value=1_000_00),
)
def test_allocations_always_sum_to_gross(
    gross: int, rent: int, rate: Decimal, food_balance: int, food_target: int
) -> None:
    rules = [
        SplitRule(TITHING, 1, RuleType.PERCENT, {"rate": str(rate), "of": "gross"}),
        SplitRule(RENT, 2, RuleType.FIXED, {"amount_cents": rent}),
        SplitRule(FOOD, 3, RuleType.REFILL_TO, {"target_cents": food_target}),
        SplitRule(NEWCAR, 4, RuleType.REMAINDER),
    ]
    r = apply_split(gross, rules, {FOOD: food_balance})

    # The core guarantee: every cent is accounted for, none invented.
    assert r.total_cents == gross
    assert r.remaining_cents == 0
    assert all(a.amount_cents > 0 for a in r.allocations)
    assert sum(a.amount_cents for a in r.allocations) <= gross


@given(gross=st.integers(min_value=0, max_value=100_000_00))
def test_remainder_only_plan_allocates_everything(gross: int) -> None:
    r = apply_split(gross, [SplitRule(NEWCAR, 1, RuleType.REMAINDER)], {})
    assert r.total_cents == gross


class TestMalformedPredicate:
    """An unevaluable predicate is an invalid plan, not a runtime surprise."""

    @staticmethod
    def _plan(when: dict):
        return [
            SplitRule("c", 1, RuleType.CONDITIONAL,
                      {"when": when,
                       "then": {"rule_type": "fixed",
                                "params": {"amount_cents": 5000}}}),
            SplitRule("r", 2, RuleType.REMAINDER, {}),
        ]

    @pytest.mark.parametrize("when,missing", [
        ({"field": "balance", "op": "<"}, "value_cents"),
        ({"field": "balance", "value_cents": 45000}, "op"),
        ({}, "value_cents"),
    ])
    def test_incomplete_predicate_is_a_plan_error(self, when, missing) -> None:
        with pytest.raises(SplitPlanError, match=missing):
            apply_split(200000, self._plan(when), {"c": 0, "r": 0})

    def test_junk_threshold_is_a_plan_error(self) -> None:
        with pytest.raises(SplitPlanError, match="whole number"):
            apply_split(200000,
                        self._plan({"op": "<", "value_cents": "a lot"}),
                        {"c": 0, "r": 0})

    def test_a_complete_predicate_still_works(self) -> None:
        res = apply_split(200000,
                          self._plan({"field": "balance", "op": "<",
                                      "value_cents": 45000}),
                          {"c": 0, "r": 0})
        assert res.total_cents == 200000
