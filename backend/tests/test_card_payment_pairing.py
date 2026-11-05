"""Paying a credit card is not spending, in either arrival order.

Hit on the real account. "PAYMENT TO ACCT #9620 ON 11/05 VIA WEB" left
checking for $124.17 and was filed as spending, so the budget asked
which category it belonged to -- after the purchases it settles had
already drawn those categories down. Counting it again double-counts
every card purchase.

treatment.classify cannot catch it from the description, and should
not try: LOAN_PAYMENTS also covers mortgage and car payments, which
ARE real expenses. The pair is the signal -- the same amount arriving
on the user's own card, which Plaid labels reliably.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta

from sqlalchemy import select

from app.budget.categorize import categorise
from app.budget.ledger import Entry, post
from app.db.models import (Account, Allocation, Category, PlaidItem,
                           ReviewItem, Transaction, User)
from app.db.session import SessionLocal
from tests.conftest import requires_db


def _seed(db):
    u = User(email=f"cp-{uuid.uuid4().hex[:8]}@x.com", password_hash="x")
    db.add(u); db.flush()
    item = PlaidItem(user_id=u.id, plaid_item_id=f"it-{uuid.uuid4().hex[:8]}",
                     access_token_encrypted=b"x")
    db.add(item); db.flush()
    chk = Account(user_id=u.id, item_id=item.id, plaid_account_id="chk",
                  name="Checking", type="depository", is_tracked=True)
    card = Account(user_id=u.id, item_id=item.id, plaid_account_id="card",
                   name="Visa", type="credit", is_tracked=True)
    db.add_all([chk, card]); db.flush()
    cat = Category(user_id=u.id, name="Food", kind="spending")
    db.add(cat); db.flush()
    return u, chk, card, cat


def _txn(db, u, acct, *, out=0, inflow=0, name="X", pfc=None,
         treatment="spend", days=0):
    t = Transaction(user_id=u.id, account_id=acct.id,
                    plaid_transaction_id=f"t-{uuid.uuid4().hex[:10]}",
                    outflow_cents=out, inflow_cents=inflow,
                    date=date.today() - timedelta(days=days), name=name,
                    pfc_primary=pfc, treatment=treatment)
    db.add(t); db.flush()
    return t


@requires_db
class TestCardPaymentPairing:
    def test_the_checking_leg_is_excluded_when_the_card_leg_exists(self) -> None:
        with SessionLocal() as db:
            u, chk, card, _ = _seed(db)
            _txn(db, u, card, inflow=12417, name="PENDING PAYMENT",
                 pfc="LOAN_PAYMENTS", treatment="card_payment")
            leaving = _txn(db, u, chk, out=12417,
                           name="PAYMENT TO ACCT #9620 ON 11/05 VIA WEB",
                           pfc="LOAN_PAYMENTS")

            assert categorise(db, u.id, leaving) == "not_budgeted"
            assert leaving.treatment == "card_payment"
            assert not db.scalars(
                select(ReviewItem).where(
                    ReviewItem.transaction_id == leaving.id)).all()

    def test_it_corrects_the_first_leg_when_the_second_arrives_later(self) -> None:
        """The two sides can arrive days apart, in either order."""
        with SessionLocal() as db:
            u, chk, card, cat = _seed(db)
            leaving = _txn(db, u, chk, out=12417,
                           name="PAYMENT TO ACCT #9620", pfc="LOAN_PAYMENTS")
            # Filed as spending before its twin showed up.
            post(db, u.id, [Entry(cat.id, -12417, "manual", None, "filed")],
                 transaction_id=leaving.id)
            db.add(ReviewItem(user_id=u.id, transaction_id=leaving.id,
                              reason="unmatched"))
            db.flush()

            arriving = _txn(db, u, card, inflow=12417, name="PENDING PAYMENT",
                            pfc="LOAN_PAYMENTS", treatment="card_payment")
            assert categorise(db, u.id, arriving) == "not_budgeted"

            db.refresh(leaving)
            assert leaving.treatment == "card_payment"
            net = sum(a.amount_cents for a in db.scalars(
                select(Allocation).where(
                    Allocation.transaction_id == leaving.id)).all())
            assert net == 0, "the category must be put back, not left drawn down"
            rv = db.scalars(select(ReviewItem).where(
                ReviewItem.transaction_id == leaving.id)).one()
            assert rv.resolved_at is not None

    def test_a_mortgage_payment_is_still_a_real_expense(self) -> None:
        """LOAN_PAYMENTS alone must not exclude anything -- the whole
        reason the description check exists."""
        with SessionLocal() as db:
            u, chk, card, _ = _seed(db)
            mortgage = _txn(db, u, chk, out=155000,
                            name="Mountain View Ma DES:rent", pfc="LOAN_PAYMENTS")
            assert categorise(db, u.id, mortgage) == "review"
            assert mortgage.treatment == "spend"

    def test_a_refund_of_a_coincidental_amount_is_not_a_card_payment(self) -> None:
        with SessionLocal() as db:
            u, chk, card, _ = _seed(db)
            # A refund onto the card, NOT a payment: no LOAN_PAYMENTS,
            # not already marked.
            _txn(db, u, card, inflow=4200, name="AMAZON REFUND",
                 pfc="GENERAL_MERCHANDISE")
            spend = _txn(db, u, chk, out=4200, name="SOME SHOP",
                         pfc="GENERAL_MERCHANDISE")
            assert categorise(db, u.id, spend) == "review"
            assert spend.treatment == "spend"

    def test_an_unmatched_amount_is_left_alone(self) -> None:
        with SessionLocal() as db:
            u, chk, card, _ = _seed(db)
            _txn(db, u, card, inflow=9999, name="PENDING PAYMENT",
                 pfc="LOAN_PAYMENTS", treatment="card_payment")
            other = _txn(db, u, chk, out=12417, name="PAYMENT TO ACCT #9620",
                         pfc="LOAN_PAYMENTS")
            assert categorise(db, u.id, other) == "review"
