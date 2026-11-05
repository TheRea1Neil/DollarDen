"""A pending charge that posts must keep its categorisation.

Hit on the real account. Rent was filed while pending; when the posted
copy arrived, _move_allocations re-pointed the allocations onto it --
except the posted row had been db.add()ed and never flushed, so its
.id was still None (Transaction.id is a Python-side uuid4 default
applied at flush). Every allocation was set to transaction_id NULL.

Two consequences, both bad. An allocation with neither transaction nor
paycheck reads as an envelope transfer, so the spend disappeared from
analytics. And the posted charge came back as an unreviewed item, so
filing it again would have counted the same money twice.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select

from app.budget.ledger import Entry, post
from app.db.models import Account, Allocation, Category, PlaidItem, Transaction, User
from app.db.session import SessionLocal
from app.plaid.sync import SyncStats, _upsert
from tests.conftest import requires_db


def _seed(db):
    u = User(email=f"pp-{uuid.uuid4().hex[:8]}@x.com", password_hash="x")
    db.add(u); db.flush()
    item = PlaidItem(user_id=u.id, plaid_item_id=f"it-{uuid.uuid4().hex[:8]}",
                     access_token_encrypted=b"x")
    db.add(item); db.flush()
    acct = Account(user_id=u.id, item_id=item.id, plaid_account_id="acc-1",
                   name="Checking", type="depository", is_tracked=True)
    db.add(acct); db.flush()
    cat = Category(user_id=u.id, name="Rent", kind="obligation")
    db.add(cat); db.flush()
    return u, item, acct, cat


def _raw(txn_id: str, *, amount: float, pending: bool, replaces: str | None = None):
    raw = {"transaction_id": txn_id, "account_id": "acc-1", "amount": amount,
           "date": "2026-11-03", "name": "Mountain View Ma rent",
           "pending": pending, "iso_currency_code": "USD"}
    if replaces:
        raw["pending_transaction_id"] = replaces
    return raw


@requires_db
class TestPendingPromotion:
    def test_categorisation_survives_the_pending_to_posted_swap(self) -> None:
        with SessionLocal() as db:
            u, item, acct, cat = _seed(db)
            accounts = {"acc-1": acct}
            stats = SyncStats()

            pending = _upsert(db, item, accounts, _raw("pend-1", amount=551.95,
                                                       pending=True), stats)
            db.flush()
            post(db, u.id, [Entry(cat.id, -551_95, "manual", None, "filed")],
                 transaction_id=pending.id)
            db.flush()

            posted = _upsert(db, item, accounts,
                             _raw("post-1", amount=551.95, pending=False,
                                  replaces="pend-1"), stats)
            db.flush()

            allocs = db.scalars(
                select(Allocation).where(Allocation.user_id == u.id)
            ).all()
            assert len(allocs) == 1
            a = allocs[0]
            assert a.transaction_id is not None, (
                "a NULL transaction_id makes this read as an envelope "
                "transfer, so the spending disappears from analytics"
            )
            assert a.transaction_id == posted.id, "must follow the posted copy"
            assert stats.pending_resolved == 1

    def test_the_pending_row_is_retired_not_duplicated(self) -> None:
        with SessionLocal() as db:
            u, item, acct, cat = _seed(db)
            accounts = {"acc-1": acct}
            stats = SyncStats()
            pending = _upsert(db, item, accounts,
                              _raw("pend-2", amount=10.00, pending=True), stats)
            db.flush()
            posted = _upsert(db, item, accounts,
                             _raw("post-2", amount=10.00, pending=False,
                                  replaces="pend-2"), stats)
            db.flush()

            assert pending.removed_at is not None
            assert pending.pending_replaced_by == "post-2"
            assert posted.removed_at is None

    def test_an_amount_change_is_reconciled_not_lost(self) -> None:
        """A tip added between pending and posted is the common case."""
        with SessionLocal() as db:
            u, item, acct, cat = _seed(db)
            accounts = {"acc-1": acct}
            stats = SyncStats()
            pending = _upsert(db, item, accounts,
                              _raw("pend-3", amount=20.00, pending=True), stats)
            db.flush()
            post(db, u.id, [Entry(cat.id, -20_00, "manual", None, "filed")],
                 transaction_id=pending.id)
            db.flush()

            posted = _upsert(db, item, accounts,
                             _raw("post-3", amount=25.00, pending=False,
                                  replaces="pend-3"), stats)
            db.flush()

            total = sum(
                a.amount_cents
                for a in db.scalars(
                    select(Allocation).where(Allocation.transaction_id == posted.id)
                ).all()
            )
            assert total == -25_00, "the envelope must end up down the real amount"
