"""Re-itemising a transaction that already filed.

Itemising used to be reachable only from the review queue, so a charge
that auto-filed correctly could never be split later -- which is exactly
the Target case: one $105.30 charge that really is groceries, health,
stationery and toys.
"""
from __future__ import annotations

import uuid
from datetime import date

from fastapi.testclient import TestClient

from app.budget.ledger import Entry, post
from app.db.models import Account, Category, PlaidItem, Transaction, User
from app.db.session import SessionLocal
from sqlalchemy import select
from tests.conftest import requires_db

PW = "refile-passphrase-99"


def _auth(client: TestClient) -> dict[str, str]:
    r = client.post("/api/auth/register",
                    json={"email": f"rf-{uuid.uuid4().hex[:10]}@x.com", "password": PW})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _cat(client, h, name) -> dict:
    r = client.post("/api/budget/categories",
                    json={"name": name, "kind": "spending"}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def _filed_txn(cents: int, category_id: str, treatment: str = "spend") -> str:
    """A transaction already filed wholly against one category."""
    with SessionLocal() as db:
        uid = db.scalars(select(User.id).order_by(User.created_at.desc())).first()
        item = PlaidItem(user_id=uid, plaid_item_id=f"it-{uuid.uuid4().hex[:10]}",
                         access_token_encrypted=b"x")
        db.add(item); db.flush()
        acct = Account(user_id=uid, item_id=item.id,
                       plaid_account_id=f"ac-{uuid.uuid4().hex[:10]}", name="C")
        db.add(acct); db.flush()
        t = Transaction(user_id=uid, account_id=acct.id,
                        plaid_transaction_id=f"tx-{uuid.uuid4().hex[:14]}",
                        outflow_cents=cents, date=date.today(), name="TARGET",
                        merchant_name="Target", treatment=treatment)
        db.add(t); db.flush()
        if treatment == "spend":
            post(db, uid, [Entry(uuid.UUID(category_id), -cents, "auto_categorise")],
                 transaction_id=t.id)
        db.commit()
        return str(t.id)


def _balance(client, h, cat_id) -> int:
    rows = client.get("/api/budget/categories", headers=h).json()
    return next(c["balance_cents"] for c in rows if c["id"] == cat_id)


@requires_db
class TestRefile:
    def test_a_filed_charge_can_be_split_later(self, client: TestClient):
        h = _auth(client)
        groceries = _cat(client, h, "Food & Groceries")
        health = _cat(client, h, "Health")
        toys = _cat(client, h, "Kids & Toys")

        # The whole $105.30 lands in Groceries, as Plaid would file it.
        txn = _filed_txn(10_530, groceries["id"])
        assert _balance(client, h, groceries["id"]) == -10_530

        r = client.post(f"/api/budget/transactions/{txn}/refile", headers=h, json={
            "splits": [
                {"category_id": groceries["id"], "amount_cents": 3_909},
                {"category_id": health["id"], "amount_cents": 2_400},
                {"category_id": toys["id"], "amount_cents": 4_221},
            ]})
        assert r.status_code == 200, r.text
        assert r.json()["reversed_cents"] == 10_530
        assert r.json()["splits"] == 3

        # The original single allocation is undone and the split applied.
        assert _balance(client, h, groceries["id"]) == -3_909
        assert _balance(client, h, health["id"]) == -2_400
        assert _balance(client, h, toys["id"]) == -4_221

    def test_splits_that_do_not_match_the_charge_are_refused(self, client: TestClient):
        """Otherwise the budget quietly disagrees with the bank."""
        h = _auth(client)
        g = _cat(client, h, "Food & Groceries")
        txn = _filed_txn(10_530, g["id"])
        r = client.post(f"/api/budget/transactions/{txn}/refile", headers=h, json={
            "splits": [{"category_id": g["id"], "amount_cents": 9_000}]})
        assert r.status_code == 422
        assert "$90.00" in r.json()["detail"] and "$105.30" in r.json()["detail"]

    def test_refiling_twice_still_balances(self, client: TestClient):
        """The second refile must reverse the FIRST split, not the original."""
        h = _auth(client)
        g = _cat(client, h, "Food & Groceries")
        hl = _cat(client, h, "Health")
        txn = _filed_txn(10_000, g["id"])

        client.post(f"/api/budget/transactions/{txn}/refile", headers=h, json={
            "splits": [{"category_id": g["id"], "amount_cents": 6_000},
                       {"category_id": hl["id"], "amount_cents": 4_000}]})
        client.post(f"/api/budget/transactions/{txn}/refile", headers=h, json={
            "splits": [{"category_id": g["id"], "amount_cents": 2_500},
                       {"category_id": hl["id"], "amount_cents": 7_500}]})

        assert _balance(client, h, g["id"]) == -2_500
        assert _balance(client, h, hl["id"]) == -7_500

    def test_history_is_kept_not_edited(self, client: TestClient):
        """Append-only: you can see what it was, that it was undone, and what it became."""
        h = _auth(client)
        g = _cat(client, h, "Food & Groceries")
        txn = _filed_txn(5_000, g["id"])
        client.post(f"/api/budget/transactions/{txn}/refile", headers=h, json={
            "splits": [{"category_id": g["id"], "amount_cents": 5_000}]})

        from app.db.models import Allocation
        with SessionLocal() as db:
            rows = db.scalars(select(Allocation).where(
                Allocation.transaction_id == uuid.UUID(txn))).all()
            sources = sorted(a.source for a in rows)
        assert sources == ["auto_categorise", "correction", "receipt_split"]

    def test_a_transfer_cannot_be_split(self, client: TestClient):
        h = _auth(client)
        g = _cat(client, h, "Food & Groceries")
        txn = _filed_txn(5_000, g["id"], treatment="transfer")
        r = client.post(f"/api/budget/transactions/{txn}/refile", headers=h, json={
            "splits": [{"category_id": g["id"], "amount_cents": 5_000}]})
        assert r.status_code == 409

    def test_empty_splits_refused(self, client: TestClient):
        h = _auth(client)
        g = _cat(client, h, "Food & Groceries")
        txn = _filed_txn(5_000, g["id"])
        assert client.post(f"/api/budget/transactions/{txn}/refile", headers=h,
                           json={"splits": []}).status_code == 422

    def test_another_users_transaction_is_not_found(self, client: TestClient):
        a = _auth(client)
        ga = _cat(client, a, "Food & Groceries")
        txn = _filed_txn(5_000, ga["id"])
        b = _auth(client)
        gb = _cat(client, b, "Food & Groceries")
        r = client.post(f"/api/budget/transactions/{txn}/refile", headers=b, json={
            "splits": [{"category_id": gb["id"], "amount_cents": 5_000}]})
        assert r.status_code == 404

    def test_refiling_from_review_closes_the_review_item(self, client: TestClient):
        """Otherwise a charge just filed reappears in the queue."""
        from app.budget.categorize import queue_for_review
        from app.db.models import ReviewItem

        h = _auth(client)
        g = _cat(client, h, "Food & Groceries")
        hl = _cat(client, h, "Health")
        txn = _filed_txn(10_000, g["id"])

        with SessionLocal() as db:
            uid = db.scalars(select(User.id).order_by(User.created_at.desc())).first()
            t = db.get(Transaction, uuid.UUID(txn))
            queue_for_review(db, uid, t, "unknown_merchant")
            db.commit()
        assert len(client.get("/api/budget/review", headers=h).json()) == 1

        r = client.post(f"/api/budget/transactions/{txn}/refile", headers=h, json={
            "splits": [{"category_id": g["id"], "amount_cents": 6_000},
                       {"category_id": hl["id"], "amount_cents": 4_000}]})
        assert r.status_code == 200, r.text
        assert r.json()["resolved_review_id"] is not None
        assert client.get("/api/budget/review", headers=h).json() == []

    def test_refiling_an_unreviewed_charge_reports_no_review(self, client: TestClient):
        h = _auth(client)
        g = _cat(client, h, "Food & Groceries")
        txn = _filed_txn(5_000, g["id"])
        r = client.post(f"/api/budget/transactions/{txn}/refile", headers=h, json={
            "splits": [{"category_id": g["id"], "amount_cents": 5_000}]})
        assert r.json()["resolved_review_id"] is None
