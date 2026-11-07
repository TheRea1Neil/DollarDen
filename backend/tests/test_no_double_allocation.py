"""One charge, one allocation, whoever gets there first.

Observed on a real Mo Bettahs charge. A webhook synced it, the AI
categoriser queued, and 18 seconds later the charge was also filed by
hand from the app. Both wrote: the envelope was drawn down $11.81
twice for one $11.81 purchase, and both entries look perfectly
ordinary in history, so nothing surfaces it.

The AI checks the review is open BEFORE calling the model, and that
call takes seconds -- easily long enough for a person to tap the
charge in the meantime.
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import select

from app.budget.categorize import resolve_review
from app.budget.ledger import Entry, post
from app.db.models import (Account, Allocation, Category, PlaidItem,
                           ReviewItem, Transaction, User)
from app.db.session import SessionLocal
from tests.conftest import requires_db


def _seed(db):
    u = User(email=f"dd-{uuid.uuid4().hex[:8]}@x.com", password_hash="x")
    db.add(u); db.flush()
    item = PlaidItem(user_id=u.id, plaid_item_id=f"it-{uuid.uuid4().hex[:8]}",
                     access_token_encrypted=b"x")
    db.add(item); db.flush()
    acct = Account(user_id=u.id, item_id=item.id, plaid_account_id="a1",
                   name="Card", type="credit", is_tracked=True)
    db.add(acct); db.flush()
    cat = Category(user_id=u.id, name="Food", kind="spending")
    db.add(cat); db.flush()
    txn = Transaction(user_id=u.id, account_id=acct.id,
                      plaid_transaction_id=f"t-{uuid.uuid4().hex[:8]}",
                      outflow_cents=1181, inflow_cents=0, date=date.today(),
                      name="PAR MO BETTAHS")
    db.add(txn); db.flush()
    rv = ReviewItem(user_id=u.id, transaction_id=txn.id, reason="unmatched")
    db.add(rv); db.flush()
    return u, txn, cat, rv


@requires_db
class TestNoDoubleAllocation:
    def test_a_manual_resolve_refuses_an_already_filed_charge(self) -> None:
        with SessionLocal() as db:
            u, txn, cat, rv = _seed(db)
            # The AI got there first.
            post(db, u.id, [Entry(cat.id, -1181, "ai_categorise", 1.0, "AI")],
                 transaction_id=txn.id)
            db.flush()

            with pytest.raises(ValueError, match="already been filed"):
                resolve_review(db, u.id, rv, [(cat.id, 1181)])

            total = sum(a.amount_cents for a in db.scalars(
                select(Allocation).where(Allocation.transaction_id == txn.id)).all())
            assert total == -1181, "the envelope must move once for one charge"

    def test_a_manual_resolve_still_works_when_nothing_filed_it(self) -> None:
        with SessionLocal() as db:
            u, txn, cat, rv = _seed(db)
            resolve_review(db, u.id, rv, [(cat.id, 1181)])
            total = sum(a.amount_cents for a in db.scalars(
                select(Allocation).where(Allocation.transaction_id == txn.id)).all())
            assert total == -1181
            assert rv.resolved_at is not None

    def test_a_partly_allocated_charge_is_also_refused(self) -> None:
        """This path always writes the FULL amount, so adding to an
        existing allocation overshoots rather than completing it."""
        with SessionLocal() as db:
            u, txn, cat, rv = _seed(db)
            post(db, u.id, [Entry(cat.id, -500, "manual", None, "part")],
                 transaction_id=txn.id)
            db.flush()
            with pytest.raises(ValueError, match="already been filed"):
                resolve_review(db, u.id, rv, [(cat.id, 1181)])

    def test_a_reversed_charge_can_be_filed_again(self) -> None:
        """Reversal nets to zero, so the charge is genuinely unfiled."""
        with SessionLocal() as db:
            u, txn, cat, rv = _seed(db)
            post(db, u.id, [Entry(cat.id, -1181, "ai_categorise", 1.0, "AI")],
                 transaction_id=txn.id)
            db.flush()
            post(db, u.id, [Entry(cat.id, 1181, "correction", None, "undo")],
                 transaction_id=txn.id)
            db.flush()

            resolve_review(db, u.id, rv, [(cat.id, 1181)])
            total = sum(a.amount_cents for a in db.scalars(
                select(Allocation).where(Allocation.transaction_id == txn.id)).all())
            assert total == -1181
