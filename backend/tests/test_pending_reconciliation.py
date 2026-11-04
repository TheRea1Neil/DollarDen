"""A pending charge is not an unallocated dollar.

Hit on the real account. A $30 insurance charge on a credit card was
filed correctly, so the category went down $30 -- but the card balance
had not risen yet, because the charge was still pending. The invariant
`allocated == cash - card_debt` was therefore out by exactly $30, and
the app reported it as "you still need to allocate $30", sending the
user hunting for a transaction they had already categorised.

The gap is real and self-correcting. What was wrong was calling it the
user's problem.
"""
from __future__ import annotations

from app.budget.reconcile import Reconciliation


def _r(cash: int, debt: int, allocated: int, pending: int = 0) -> Reconciliation:
    return Reconciliation(cash, debt, allocated, pending)


def test_a_balanced_budget_is_still_balanced():
    r = _r(cash=100_00, debt=0, allocated=100_00)
    assert r.balanced
    assert not r.explained_by_pending
    assert "not settled yet" not in r.as_dict()["explanation"]


def test_the_real_case_is_recognised_as_pending():
    # cash 8707.67, card 93.17, allocated 8584.50 -> 30.00 adrift,
    # and exactly 30.00 of pending spend explains it.
    r = _r(cash=870767, debt=9317, allocated=858450, pending=3000)
    assert r.unallocated_cents == 3000
    assert not r.balanced
    assert r.explained_by_pending, "the gap is the pending charge, not a gap"
    assert "30.00" in r.as_dict()["explanation"]
    assert "nothing to do" in r.as_dict()["explanation"]


def test_a_gap_larger_than_pending_is_still_the_users_to_close():
    """Do not explain away money that genuinely has no home."""
    r = _r(cash=100_00, debt=0, allocated=50_00, pending=10_00)
    assert r.unallocated_cents == 50_00
    assert not r.explained_by_pending
    assert "nothing to do" not in r.as_dict()["explanation"]


def test_pending_does_not_mask_an_over_allocation_it_cannot_explain():
    r = _r(cash=100_00, debt=0, allocated=200_00, pending=5_00)
    assert r.unallocated_cents == -100_00
    assert not r.explained_by_pending


def test_over_allocated_within_pending_is_explained():
    r = _r(cash=100_00, debt=0, allocated=103_00, pending=5_00)
    assert r.unallocated_cents == -3_00
    assert r.explained_by_pending


def test_pending_is_reported_even_when_balanced():
    r = _r(cash=100_00, debt=0, allocated=100_00, pending=7_50)
    d = r.as_dict()
    assert d["pending_cents"] == 750
    assert d["explained_by_pending"] is False
