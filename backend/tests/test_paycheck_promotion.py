"""A paycheck split while pending must not be splittable again.

Splitting a pending deposit is allowed so the budget reflects money
the bank has accepted but not posted -- on a real account, `available`
included a $1,656.91 payroll credit a full day before `current` did.

That is only safe because the paycheck follows its posted copy. A
paycheck's allocations hang off the PAYCHECK, not the transaction, so
the machinery that re-points spending allocations does not touch them:
without this the posted deposit would look unrecorded and could be
split a second time, which spoils every envelope at once rather than
one.
"""
from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select

from app.budget.paycheck import candidates
from app.db.models import Account, Allocation, Category, Paycheck, PlaidItem, Transaction, User
from app.db.session import SessionLocal
from app.plaid.sync import SyncStats, _upsert
from tests.conftest import requires_db


def _seed(db):
    u = User(email=f"pm-{uuid.uuid4().hex[:8]}@x.com", password_hash="x")
    db.add(u); db.flush()
    item = PlaidItem(user_id=u.id, plaid_item_id=f"it-{uuid.uuid4().hex[:8]}",
                     access_token_encrypted=b"x")
    db.add(item); db.flush()
    acct = Account(user_id=u.id, item_id=item.id, plaid_account_id="acc-1",
                   name="Checking", type="depository", is_tracked=True)
    db.add(acct); db.flush()
    return u, item, acct


def _raw(txn_id, *, amount, pending, replaces=None):
    # Plaid sends a deposit as a negative amount.
    raw = {"transaction_id": txn_id, "account_id": "acc-1", "amount": -amount,
           "date": date.today().isoformat(), "name": "ACH CREDIT PAYROLL",
           "pending": pending, "iso_currency_code": "USD"}
    if replaces:
        raw["pending_transaction_id"] = replaces
    return raw


@requires_db
class TestPaycheckPromotion:
    def test_the_paycheck_follows_the_posted_deposit(self) -> None:
        with SessionLocal() as db:
            u, item, acct = _seed(db)
            accounts = {"acc-1": acct}
            stats = SyncStats()

            pending = _upsert(db, item, accounts,
                              _raw("pend-1", amount=1656.91, pending=True), stats)
            db.flush()
            pc = Paycheck(user_id=u.id, transaction_id=pending.id,
                          gross_cents=165691, received_on=date.today())
            db.add(pc); db.flush()
            pc_id = pc.id

            posted = _upsert(db, item, accounts,
                             _raw("post-1", amount=1656.91, pending=False,
                                  replaces="pend-1"), stats)
            db.flush()

            db.expire_all()
            moved = db.get(Paycheck, pc_id)
            assert moved.transaction_id == posted.id, (
                "left on the pending row, the posted deposit looks "
                "unrecorded and can be split a second time"
            )

    def test_the_posted_deposit_is_not_offered_again(self) -> None:
        with SessionLocal() as db:
            u, item, acct = _seed(db)
            accounts = {"acc-1": acct}
            stats = SyncStats()
            pending = _upsert(db, item, accounts,
                              _raw("pend-2", amount=2000.00, pending=True), stats)
            db.flush()
            db.add(Paycheck(user_id=u.id, transaction_id=pending.id,
                            gross_cents=200000, received_on=date.today()))
            db.flush()
            _upsert(db, item, accounts,
                    _raw("post-2", amount=2000.00, pending=False,
                         replaces="pend-2"), stats)
            db.commit()

            offered = candidates(db, u.id)
            recorded = {
                p.transaction_id
                for p in db.scalars(
                    select(Paycheck).where(Paycheck.user_id == u.id)).all()
            }
            # It may still be listed, but it must be marked recorded --
            # that flag is what stops a second split.
            for t in offered:
                assert t.id in recorded, "an already-split deposit offered as new"

    def test_an_amount_change_lands_in_unallocated(self) -> None:
        """Not spread back over the split: that would rewrite a human
        decision silently. Unallocated is where money with no purpose
        is supposed to sit, and it is visible there."""
        with SessionLocal() as db:
            u, item, acct = _seed(db)
            accounts = {"acc-1": acct}
            stats = SyncStats()
            pending = _upsert(db, item, accounts,
                              _raw("pend-3", amount=1000.00, pending=True), stats)
            db.flush()
            pc = Paycheck(user_id=u.id, transaction_id=pending.id,
                          gross_cents=100000, received_on=date.today())
            db.add(pc); db.flush()
            pc_id = pc.id

            _upsert(db, item, accounts,
                    _raw("post-3", amount=1043.09, pending=False,
                         replaces="pend-3"), stats)
            db.flush()

            db.expire_all()
            assert db.get(Paycheck, pc_id).gross_cents == 104309

            sink = db.scalars(
                select(Category).where(Category.user_id == u.id,
                                       Category.is_unallocated.is_(True))).one()
            total = sum(
                a.amount_cents for a in db.scalars(
                    select(Allocation).where(Allocation.category_id == sink.id)).all())
            assert total == 4309, "the extra must be visible, not absorbed"

    def test_an_unsplit_pending_deposit_promotes_quietly(self) -> None:
        with SessionLocal() as db:
            u, item, acct = _seed(db)
            accounts = {"acc-1": acct}
            stats = SyncStats()
            _upsert(db, item, accounts,
                    _raw("pend-4", amount=500.00, pending=True), stats)
            db.flush()
            _upsert(db, item, accounts,
                    _raw("post-4", amount=500.00, pending=False,
                         replaces="pend-4"), stats)
            db.flush()
            assert not db.scalars(
                select(Paycheck).where(Paycheck.user_id == u.id)).all()
