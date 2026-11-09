"""A misfiled charge must be correctable, refunds included.

Real case: a Paramount refund was auto-filed to "AI Fund" instead of
"Media & Phone". The only correction path in the app was Itemise --
the full receipt-splitting flow -- and it was offered for OUTFLOWS
only, so a refund in the wrong envelope could not be fixed at all.

A refund is not a lesser case: it credits an envelope, so one in the
wrong place overstates that envelope and understates the right one.
"""
from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select

from app.db.models import (Account, Allocation, Category, PlaidItem,
                           Transaction, User)
from app.db.session import SessionLocal
from tests.conftest import requires_db

PW = "refile-refund-passphrase"


def _auth(client):
    email = f"rr-{uuid.uuid4().hex[:10]}@x.com"
    r = client.post("/api/auth/register", json={"email": email, "password": PW})
    assert r.status_code == 201, r.text
    with SessionLocal() as db:
        uid = db.scalars(select(User.id).where(User.email == email)).one()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}, uid


def _cat(client, h, name) -> str:
    r = client.post("/api/budget/categories",
                    json={"name": name, "kind": "spending"}, headers=h)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _refund(db, uid, cents: int) -> Transaction:
    item = PlaidItem(user_id=uid, plaid_item_id=f"it-{uuid.uuid4().hex[:8]}",
                     access_token_encrypted=b"x")
    db.add(item); db.flush()
    acct = Account(user_id=uid, item_id=item.id,
                   plaid_account_id=f"a-{uuid.uuid4().hex[:8]}",
                   name="Card", type="credit", is_tracked=True)
    db.add(acct); db.flush()
    t = Transaction(user_id=uid, account_id=acct.id,
                    plaid_transaction_id=f"t-{uuid.uuid4().hex[:8]}",
                    outflow_cents=0, inflow_cents=cents, date=date.today(),
                    name="GOOGLE Paramount")
    db.add(t); db.flush()
    return t


@requires_db
class TestRefileRefund:
    def test_a_refund_can_be_moved_to_the_right_envelope(self, client) -> None:
        h, uid = _auth(client)
        wrong = _cat(client, h, "AI Fund")
        right = _cat(client, h, "Media & Phone")

        with SessionLocal() as db:
            t = _refund(db, uid, 1514)
            db.commit()
            txn_id = str(t.id)

        # Filed to the wrong envelope first, as the AI did.
        from app.budget.ledger import Entry, post
        with SessionLocal() as db:
            post(db, uuid.UUID(str(uid)),
                 [Entry(uuid.UUID(wrong), 1514, "ai_categorise", 1.0, "AI")],
                 transaction_id=uuid.UUID(txn_id))
            db.commit()

        # signed_amount for an inflow is NEGATIVE, so the split must be
        # too -- that sign is the thing most likely to be got wrong.
        r = client.post(f"/api/budget/transactions/{txn_id}/refile",
                        json={"splits": [{"category_id": right,
                                          "amount_cents": -1514}]}, headers=h)
        assert r.status_code == 200, r.text

        with SessionLocal() as db:
            by_cat: dict[str, int] = {}
            for a in db.scalars(select(Allocation).where(
                    Allocation.transaction_id == uuid.UUID(txn_id))).all():
                name = db.get(Category, a.category_id).name
                by_cat[name] = by_cat.get(name, 0) + a.amount_cents

        assert by_cat["AI Fund"] == 0, "the wrong envelope must be put back"
        assert by_cat["Media & Phone"] == 1514, "the refund lands where it belongs"

    def test_the_old_entries_are_reversed_not_deleted(self, client) -> None:
        """Append-only: history must still show where it went first."""
        h, uid = _auth(client)
        wrong = _cat(client, h, "AI Fund")
        right = _cat(client, h, "Media & Phone")
        with SessionLocal() as db:
            t = _refund(db, uid, 1514)
            db.commit()
            txn_id = str(t.id)

        from app.budget.ledger import Entry, post
        with SessionLocal() as db:
            post(db, uuid.UUID(str(uid)),
                 [Entry(uuid.UUID(wrong), 1514, "ai_categorise", 1.0, "AI")],
                 transaction_id=uuid.UUID(txn_id))
            db.commit()

        client.post(f"/api/budget/transactions/{txn_id}/refile",
                    json={"splits": [{"category_id": right,
                                      "amount_cents": -1514}]}, headers=h)

        with SessionLocal() as db:
            rows = db.scalars(select(Allocation).where(
                Allocation.transaction_id == uuid.UUID(txn_id))).all()
        assert len(rows) >= 3, (
            "original + reversal + new; editing in place would lose the "
            "record of what it was"
        )

    def test_a_split_with_the_wrong_sign_is_refused(self, client) -> None:
        """+1514 on an inflow would double the credit instead of moving it."""
        h, uid = _auth(client)
        right = _cat(client, h, "Media & Phone")
        with SessionLocal() as db:
            t = _refund(db, uid, 1514)
            db.commit()
            txn_id = str(t.id)

        r = client.post(f"/api/budget/transactions/{txn_id}/refile",
                        json={"splits": [{"category_id": right,
                                          "amount_cents": 1514}]}, headers=h)
        assert r.status_code == 422
