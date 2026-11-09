"""Predict payday from deposits when there are not enough paychecks.

Waiting for someone to split TWO paychecks meant no prediction at all
for the first month of use -- while the bank had already handed over
six perfectly regular payroll credits. On the real account: deposits
on the 5th and 20th, gaps of 14-17 days, and one recorded paycheck.

Recorded paychecks still win when there are enough: those are income
the user has confirmed and acted on.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta

from app.budget.paycheck import predict_next
from app.db.models import Account, Paycheck, PlaidItem, Transaction, User
from app.db.session import SessionLocal
from tests.conftest import requires_db


def _seed(db) -> tuple[User, Account]:
    u = User(email=f"pp-{uuid.uuid4().hex[:8]}@x.com", password_hash="x")
    db.add(u); db.flush()
    item = PlaidItem(user_id=u.id, plaid_item_id=f"it-{uuid.uuid4().hex[:8]}",
                     access_token_encrypted=b"x")
    db.add(item); db.flush()
    acct = Account(user_id=u.id, item_id=item.id,
                   plaid_account_id=f"a-{uuid.uuid4().hex[:8]}",
                   name="Checking", type="depository", is_tracked=True)
    db.add(acct); db.flush()
    return u, acct


def _deposit(db, u, acct, on: date, cents: int = 165691, *,
             treatment: str = "ignored", pfc: str = "INCOME") -> Transaction:
    t = Transaction(user_id=u.id, account_id=acct.id,
                    plaid_transaction_id=f"t-{uuid.uuid4().hex[:10]}",
                    outflow_cents=0, inflow_cents=cents, date=on,
                    name="ACH CREDIT PAYROLL", pfc_primary=pfc,
                    treatment=treatment)
    db.add(t); db.flush()
    return t


@requires_db
class TestPaydayFromDeposits:
    def test_it_predicts_from_deposits_with_no_paychecks_at_all(self) -> None:
        with SessionLocal() as db:
            u, acct = _seed(db)
            # Neil's real pattern: the 5th and the 20th.
            for on in [date(2026, 11, 5), date(2026, 10, 20),
                       date(2026, 10, 5), date(2026, 9, 18)]:
                _deposit(db, u, acct, on)
            db.flush()

            out = predict_next(db, u.id)
            assert out["known"] is True
            assert out["source"] == "deposits"
            assert 14 <= out["cadence_days"] <= 17
            assert out["expected"] > date(2026, 11, 5).isoformat()

    def test_dismissed_history_still_counts(self) -> None:
        """Imported deposits get marked `ignored` so they stop cluttering
        the review queue. That says nothing about whether payday
        happened."""
        with SessionLocal() as db:
            u, acct = _seed(db)
            for on in [date(2026, 11, 5), date(2026, 10, 20), date(2026, 10, 5)]:
                _deposit(db, u, acct, on, treatment="ignored")
            db.flush()
            assert predict_next(db, u.id)["known"] is True

    def test_recorded_paychecks_win_when_there_are_enough(self) -> None:
        with SessionLocal() as db:
            u, acct = _seed(db)
            for on in [date(2026, 11, 6), date(2026, 10, 30)]:
                db.add(Paycheck(user_id=u.id, gross_cents=100000,
                                received_on=on))
            for on in [date(2026, 11, 5), date(2026, 10, 20)]:
                _deposit(db, u, acct, on)
            db.flush()

            out = predict_next(db, u.id)
            assert out["source"] == "paychecks"
            assert out["cadence_days"] == 7, "weekly paychecks, not the deposits"

    def test_two_rows_for_one_payday_do_not_look_like_a_zero_gap(self) -> None:
        """A pending deposit and its posted twin share a date. Counting
        both would drag the median cadence toward zero."""
        with SessionLocal() as db:
            u, acct = _seed(db)
            _deposit(db, u, acct, date(2026, 11, 5))
            _deposit(db, u, acct, date(2026, 11, 5))  # the twin
            _deposit(db, u, acct, date(2026, 10, 20))
            _deposit(db, u, acct, date(2026, 10, 5))
            db.flush()

            out = predict_next(db, u.id)
            assert out["known"] is True
            assert out["cadence_days"] >= 14, "a duplicate must not halve it"

    def test_non_income_deposits_are_ignored(self) -> None:
        """A tax refund or a transfer in is not payday."""
        with SessionLocal() as db:
            u, acct = _seed(db)
            for on in [date(2026, 11, 5), date(2026, 10, 20), date(2026, 10, 5)]:
                _deposit(db, u, acct, on, pfc="TRANSFER_IN")
            db.flush()
            assert predict_next(db, u.id)["known"] is False

    def test_a_single_deposit_is_not_a_pattern(self) -> None:
        with SessionLocal() as db:
            u, acct = _seed(db)
            _deposit(db, u, acct, date(2026, 11, 5))
            db.flush()
            out = predict_next(db, u.id)
            assert out["known"] is False
            assert "two paydays" in out["reason"]
