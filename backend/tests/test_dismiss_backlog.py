"""Bulk-dismissing imported history.

Connecting a real bank imported three months of charges -- 167 review
items nobody intended to budget. This clears them without losing the
record and without touching anything already filed, so the scoping has
to be exact: a bulk write on real financial data gets one chance.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta

from sqlalchemy import select

from app.db.models import Account, PlaidItem, ReviewItem, Transaction, User
from app.db.session import SessionLocal
from tests.conftest import requires_db

PW = "dismiss-backlog-passphrase-1"


def _auth(client) -> tuple[dict[str, str], uuid.UUID]:
    email = f"db-{uuid.uuid4().hex[:10]}@x.com"
    r = client.post("/api/auth/register", json={"email": email, "password": PW})
    assert r.status_code == 201, r.text
    with SessionLocal() as db:
        uid = db.scalars(select(User.id).where(User.email == email)).one()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}, uid


def _account(db, uid) -> Account:
    item = PlaidItem(user_id=uid, plaid_item_id=f"it-{uuid.uuid4().hex[:10]}",
                     access_token_encrypted=b"x", institution_name="Test Bank")
    db.add(item); db.flush()
    acct = Account(user_id=uid, item_id=item.id,
                   plaid_account_id=f"ac-{uuid.uuid4().hex[:10]}",
                   name="Checking", is_tracked=True)
    db.add(acct); db.flush()
    return acct


def _reviewable(db, uid, acct, *, days_ago: int, out: int) -> Transaction:
    t = Transaction(user_id=uid, account_id=acct.id,
                    plaid_transaction_id=f"tx-{uuid.uuid4().hex[:14]}",
                    outflow_cents=out, inflow_cents=0,
                    date=date.today() - timedelta(days=days_ago), name="Shop")
    db.add(t); db.flush()
    db.add(ReviewItem(user_id=uid, transaction_id=t.id, reason="unmatched"))
    db.flush()
    return t


@requires_db
class TestDismissBefore:
    def test_dry_run_counts_without_changing_anything(self, client) -> None:
        h, uid = _auth(client)
        with SessionLocal() as db:
            acct = _account(db, uid)
            _reviewable(db, uid, acct, days_ago=40, out=1000)
            _reviewable(db, uid, acct, days_ago=35, out=2500)
            db.commit()

        cutoff = (date.today() - timedelta(days=30)).isoformat()
        r = client.post("/api/budget/review/dismiss-before",
                        json={"before": cutoff}, headers=h)
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["dismissed"] == 2
        assert out["outflow_cents"] == 3500
        assert out["dry_run"] is True

        # Nothing may have moved.
        assert len(client.get("/api/budget/review", headers=h).json()) == 2

    def test_it_only_touches_items_before_the_cutoff(self, client) -> None:
        h, uid = _auth(client)
        with SessionLocal() as db:
            acct = _account(db, uid)
            _reviewable(db, uid, acct, days_ago=40, out=1000)   # dismissed
            keep = _reviewable(db, uid, acct, days_ago=5, out=9900)  # kept
            db.commit()
            keep_id = keep.id

        cutoff = (date.today() - timedelta(days=30)).isoformat()
        r = client.post("/api/budget/review/dismiss-before",
                        json={"before": cutoff, "dry_run": False}, headers=h)
        assert r.json()["dismissed"] == 1

        left = client.get("/api/budget/review", headers=h).json()
        assert len(left) == 1
        assert left[0]["transaction_id"] == str(keep_id)

    def test_dismissed_rows_survive_as_history(self, client) -> None:
        """Ignored, not deleted -- the record has to stay."""
        h, uid = _auth(client)
        with SessionLocal() as db:
            acct = _account(db, uid)
            t = _reviewable(db, uid, acct, days_ago=40, out=1234)
            db.commit()
            txn_id = t.id

        client.post("/api/budget/review/dismiss-before",
                    json={"before": (date.today() - timedelta(days=30)).isoformat(),
                          "dry_run": False}, headers=h)

        with SessionLocal() as db:
            txn = db.get(Transaction, txn_id)
            assert txn is not None, "the transaction must not be deleted"
            assert txn.treatment == "ignored"
            rv = db.scalars(
                select(ReviewItem).where(ReviewItem.transaction_id == txn_id)
            ).one()
            assert rv.resolved_at is not None
            assert rv.resolution["excluded_as"] == "ignored"
            # The audit trail says HOW it was closed, not just that it was.
            assert rv.resolution["bulk"] == "dismiss-before"

    def test_balances_do_not_move(self, client) -> None:
        h, uid = _auth(client)
        with SessionLocal() as db:
            acct = _account(db, uid)
            _reviewable(db, uid, acct, days_ago=40, out=50_00)
            db.commit()
        def allocated() -> int:
            return client.get("/api/budget/summary",
                              headers=h).json()["reconciliation"]["allocated_cents"]

        before = allocated()

        client.post("/api/budget/review/dismiss-before",
                    json={"before": (date.today() - timedelta(days=30)).isoformat(),
                          "dry_run": False}, headers=h)

        assert allocated() == before, "dismissing unfiled history must not move money"

    def test_another_users_backlog_is_untouched(self, client) -> None:
        h_a, uid_a = _auth(client)
        h_b, uid_b = _auth(client)
        with SessionLocal() as db:
            for uid in (uid_a, uid_b):
                acct = _account(db, uid)
                _reviewable(db, uid, acct, days_ago=40, out=1000)
            db.commit()

        client.post("/api/budget/review/dismiss-before",
                    json={"before": (date.today() - timedelta(days=30)).isoformat(),
                          "dry_run": False}, headers=h_a)

        assert len(client.get("/api/budget/review", headers=h_b).json()) == 1

    def test_nothing_to_do_is_not_an_error(self, client) -> None:
        h, _ = _auth(client)
        r = client.post("/api/budget/review/dismiss-before",
                        json={"before": "2020-01-01", "dry_run": False}, headers=h)
        assert r.status_code == 200
        assert r.json() == {"dismissed": 0, "outflow_cents": 0, "oldest": None,
                            "newest": None, "dry_run": False}
