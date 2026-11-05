"""A processing paycheck must be visible but not splittable.

Observed on the real account: an ACH payroll credit arrived pending,
and because candidates() excluded pending deposits the one event the
whole budget is built around was invisible for a day. It also landed
in the SPENDING review queue, inviting it to be filed into a single
category.

Splitting it while pending is still refused, and that is the important
half. A paycheck's allocations are tied to the paycheck, not the
transaction, so the pending->posted machinery -- which re-points
allocations by transaction -- would not follow them. The posted copy
could then be split a second time, and unlike a double-filed charge
that corrupts every envelope at once.
"""
from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select

from app.budget.categorize import categorise
from app.budget.paycheck import MIN_PAYCHECK_CENTS, candidates
from app.db.models import Account, PlaidItem, ReviewItem, Transaction, User
from app.db.session import SessionLocal
from tests.conftest import requires_db

PW = "pending-paycheck-passphrase"


def _auth(client):
    email = f"pp-{uuid.uuid4().hex[:10]}@x.com"
    r = client.post("/api/auth/register", json={"email": email, "password": PW})
    assert r.status_code == 201, r.text
    with SessionLocal() as db:
        uid = db.scalars(select(User.id).where(User.email == email)).one()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}, uid


def _deposit(db, uid, *, cents: int, pending: bool) -> Transaction:
    item = PlaidItem(user_id=uid, plaid_item_id=f"it-{uuid.uuid4().hex[:8]}",
                     access_token_encrypted=b"x")
    db.add(item); db.flush()
    acct = Account(user_id=uid, item_id=item.id,
                   plaid_account_id=f"ac-{uuid.uuid4().hex[:8]}",
                   name="Checking", type="depository", is_tracked=True)
    db.add(acct); db.flush()
    t = Transaction(user_id=uid, account_id=acct.id,
                    plaid_transaction_id=f"t-{uuid.uuid4().hex[:8]}",
                    outflow_cents=0, inflow_cents=cents, date=date.today(),
                    name="ACH CREDIT ELEVATED EMP CR", pending=pending)
    db.add(t); db.flush()
    return t


@requires_db
class TestPendingPaycheck:
    def test_a_processing_deposit_is_offered_and_flagged(self, client) -> None:
        h, uid = _auth(client)
        with SessionLocal() as db:
            _deposit(db, uid, cents=1676_53, pending=True)
            db.commit()

        out = client.get("/api/budget/paychecks/candidates", headers=h).json()
        assert len(out) == 1, "a processing payroll credit must not be hidden"
        assert out[0]["pending"] is True
        assert out[0]["inflow_cents"] == 1676_53

    def test_a_processing_deposit_can_be_split(self, client) -> None:
        """Allowed, because _move_paycheck carries it to the posted copy."""
        h, uid = _auth(client)
        with SessionLocal() as db:
            t = _deposit(db, uid, cents=1676_53, pending=True)
            db.commit()
            txn_id = str(t.id)

        r = client.post("/api/budget/paychecks",
                        json={"transaction_id": txn_id, "apply": False}, headers=h)
        assert r.status_code in (200, 201), r.text

    def test_a_posted_deposit_is_splittable(self, client) -> None:
        h, uid = _auth(client)
        with SessionLocal() as db:
            t = _deposit(db, uid, cents=1676_53, pending=False)
            db.commit()
            txn_id = str(t.id)

        r = client.post("/api/budget/paychecks",
                        json={"transaction_id": txn_id, "apply": False}, headers=h)
        assert r.status_code in (201, 200), r.text

    def test_income_stays_out_of_the_spending_queue(self) -> None:
        with SessionLocal() as db:
            u = User(email=f"iq-{uuid.uuid4().hex[:8]}@x.com", password_hash="x")
            db.add(u); db.flush()
            t = _deposit(db, u.id, cents=MIN_PAYCHECK_CENTS + 1, pending=False)
            outcome = categorise(db, u.id, t)
            assert outcome == "income"
            assert not db.scalars(
                select(ReviewItem).where(ReviewItem.transaction_id == t.id)).all()

    def test_a_small_refund_still_goes_to_review(self) -> None:
        """Only amounts big enough to be income are diverted."""
        with SessionLocal() as db:
            u = User(email=f"sr-{uuid.uuid4().hex[:8]}@x.com", password_hash="x")
            db.add(u); db.flush()
            t = _deposit(db, u.id, cents=MIN_PAYCHECK_CENTS - 1, pending=False)
            assert categorise(db, u.id, t) == "review"
