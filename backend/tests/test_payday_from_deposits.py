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


@requires_db
class TestAOneOffDepositDoesNotBecomeACadence:
    """The live regression, reproduced from real data.

    Three recorded paychecks -- payroll on the 5th and the 20th, plus a
    $196 mobile cheque split on the 18th -- gave gaps [2, 13], and
    `median([2, 13])` is 7. The app announced payday a week after the
    last one while eight perfectly regular payroll credits sat in the
    same account saying the 5th and the 20th.
    """

    def test_the_exact_live_case_predicts_from_payroll_not_the_oddity(
            self) -> None:
        with SessionLocal() as db:
            u, acct = _seed(db)
            for on in [date(2026, 11, 20), date(2026, 11, 18),
                       date(2026, 11, 5)]:
                db.add(Paycheck(user_id=u.id, gross_cents=100000,
                                received_on=on))
            # The payroll the bank actually paid: the 5th and the 20th.
            for on in [date(2026, 11, 20), date(2026, 11, 5),
                       date(2026, 10, 20), date(2026, 10, 5),
                       date(2026, 9, 18), date(2026, 9, 4),
                       date(2026, 8, 20), date(2026, 8, 5)]:
                _deposit(db, u, acct, on)
            db.flush()

            out = predict_next(db, u.id)
            assert out["known"] is True
            assert out["source"] == "deposits", "8 gaps beat 2"
            assert out["cadence_days"] == 15
            assert out["expected"] == date(2026, 12, 5).isoformat(), (
                "the 5th, not 11-27")

    def test_a_two_day_gap_is_never_a_cadence(self) -> None:
        """Nothing legitimate pays faster than weekly."""
        with SessionLocal() as db:
            u, acct = _seed(db)
            for on in [date(2026, 11, 20), date(2026, 11, 18)]:
                db.add(Paycheck(user_id=u.id, gross_cents=100000,
                                received_on=on))
            db.flush()
            out = predict_next(db, u.id)
            assert out["known"] is False, "two days apart is not a schedule"

    def test_more_gaps_wins_even_though_paychecks_are_confirmed(self) -> None:
        with SessionLocal() as db:
            u, acct = _seed(db)
            # Two paychecks -> one gap, and it disagrees with the payroll.
            for on in [date(2026, 11, 12), date(2026, 11, 5)]:
                db.add(Paycheck(user_id=u.id, gross_cents=100000,
                                received_on=on))
            for on in [date(2026, 11, 5), date(2026, 10, 20),
                       date(2026, 10, 5), date(2026, 9, 18)]:
                _deposit(db, u, acct, on)
            db.flush()

            out = predict_next(db, u.id)
            assert out["source"] == "deposits"
            assert 14 <= out["cadence_days"] <= 17

    def test_paychecks_still_win_on_equal_evidence(self) -> None:
        """So splitting paychecks remains a way to correct a bad guess."""
        with SessionLocal() as db:
            u, acct = _seed(db)
            for on in [date(2026, 11, 13), date(2026, 11, 6),
                       date(2026, 10, 30)]:
                db.add(Paycheck(user_id=u.id, gross_cents=100000,
                                received_on=on))
            for on in [date(2026, 11, 5), date(2026, 10, 20),
                       date(2026, 10, 5)]:
                _deposit(db, u, acct, on)
            db.flush()

            out = predict_next(db, u.id)
            assert out["source"] == "paychecks", "2 gaps each; paychecks win"
            assert out["cadence_days"] == 7

    def test_it_reports_how_much_evidence_is_under_the_median(self) -> None:
        with SessionLocal() as db:
            u, acct = _seed(db)
            for on in [date(2026, 11, 5), date(2026, 10, 20),
                       date(2026, 10, 5), date(2026, 9, 18)]:
                _deposit(db, u, acct, on)
            db.flush()
            out = predict_next(db, u.id)
            assert out["gaps_used"] == 3, "four paydays, three intervals"
