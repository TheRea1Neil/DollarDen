"""Moving money between envelopes, and rebalancing the whole budget.

Both are allocation changes, never balance edits: a balance written
directly would leave the other envelope silently wrong and the ledger
unable to say where the money went.
"""
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from tests.conftest import requires_db

PW = "move-money-passphrase-99"


def _auth(client: TestClient) -> dict[str, str]:
    r = client.post("/api/auth/register", json={
        "email": f"mm-{uuid.uuid4().hex[:10]}@x.com", "password": PW})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _cat(client, h, name) -> dict:
    r = client.post("/api/budget/categories",
                    json={"name": name, "kind": "spending"}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def _fund(client, h, pairs):
    """Put money in without a paycheck, the way a fresh budget starts."""
    from app.budget.ledger import Entry, post
    from app.db.models import User
    from app.db.session import SessionLocal
    from sqlalchemy import select
    with SessionLocal() as db:
        uid = db.scalars(select(User.id).order_by(User.created_at.desc())).first()
        post(db, uid, [Entry(uuid.UUID(cid), cents, "opening_balance")
                       for cid, cents in pairs])
        db.commit()


def _bal(client, h, cid) -> int:
    rows = client.get("/api/budget/categories", headers=h).json()
    return next(c["balance_cents"] for c in rows if c["id"] == cid)


@requires_db
class TestTransfer:
    def test_moving_money_leaves_the_total_unchanged(self, client: TestClient):
        h = _auth(client)
        a, b = _cat(client, h, "Groceries"), _cat(client, h, "Discretionary")
        _fund(client, h, [(a["id"], 10_000), (b["id"], 5_000)])

        r = client.post("/api/budget/transfer", headers=h, json={
            "from_category_id": b["id"], "to_category_id": a["id"],
            "amount_cents": 2_000})
        assert r.status_code == 200, r.text

        assert _bal(client, h, a["id"]) == 12_000
        assert _bal(client, h, b["id"]) == 3_000
        assert _bal(client, h, a["id"]) + _bal(client, h, b["id"]) == 15_000

    def test_the_move_is_two_entries_not_an_edit(self, client: TestClient):
        """So the ledger can still say where the money went."""
        from app.db.models import Allocation, User
        from app.db.session import SessionLocal
        from sqlalchemy import select

        h = _auth(client)
        a, b = _cat(client, h, "Groceries"), _cat(client, h, "Discretionary")
        _fund(client, h, [(a["id"], 10_000), (b["id"], 5_000)])
        client.post("/api/budget/transfer", headers=h, json={
            "from_category_id": b["id"], "to_category_id": a["id"],
            "amount_cents": 2_000})

        with SessionLocal() as db:
            uid = db.scalars(select(User.id).order_by(User.created_at.desc())).first()
            moves = db.scalars(select(Allocation).where(
                Allocation.user_id == uid, Allocation.source == "manual")).all()
        assert sorted(m.amount_cents for m in moves) == [-2_000, 2_000]
        assert all("moved from" in (m.note or "") for m in moves)

    def test_moving_to_the_same_category_is_refused(self, client: TestClient):
        h = _auth(client)
        a = _cat(client, h, "Groceries")
        assert client.post("/api/budget/transfer", headers=h, json={
            "from_category_id": a["id"], "to_category_id": a["id"],
            "amount_cents": 100}).status_code == 422

    def test_a_negative_amount_is_refused(self, client: TestClient):
        h = _auth(client)
        a, b = _cat(client, h, "A"), _cat(client, h, "B")
        assert client.post("/api/budget/transfer", headers=h, json={
            "from_category_id": a["id"], "to_category_id": b["id"],
            "amount_cents": -500}).status_code == 422

    def test_another_users_category_is_not_found(self, client: TestClient):
        ha, hb = _auth(client), _auth(client)
        theirs = _cat(client, ha, "Theirs")
        mine = _cat(client, hb, "Mine")
        assert client.post("/api/budget/transfer", headers=hb, json={
            "from_category_id": theirs["id"], "to_category_id": mine["id"],
            "amount_cents": 100}).status_code == 404


@requires_db
class TestReallocate:
    def test_targets_are_reached_not_added(self, client: TestClient):
        """"Groceries should hold $400" — not "add $400"."""
        h = _auth(client)
        a, b = _cat(client, h, "Groceries"), _cat(client, h, "Discretionary")
        _fund(client, h, [(a["id"], 10_000), (b["id"], 5_000)])

        r = client.post("/api/budget/reallocate", headers=h, json={
            "allocations": [{"category_id": a["id"], "amount_cents": 4_000},
                            {"category_id": b["id"], "amount_cents": 11_000}],
            "allow_over_allocation": True})
        assert r.status_code == 200, r.text
        assert _bal(client, h, a["id"]) == 4_000
        assert _bal(client, h, b["id"]) == 11_000

    def test_an_envelope_can_be_emptied(self, client: TestClient):
        h = _auth(client)
        a = _cat(client, h, "Groceries")
        _fund(client, h, [(a["id"], 10_000)])
        r = client.post("/api/budget/reallocate", headers=h, json={
            "allocations": [{"category_id": a["id"], "amount_cents": 0}],
            "allow_over_allocation": True})
        assert r.status_code == 200, r.text
        assert _bal(client, h, a["id"]) == 0

    def test_categories_left_out_are_untouched(self, client: TestClient):
        h = _auth(client)
        a, b = _cat(client, h, "Groceries"), _cat(client, h, "Rent")
        _fund(client, h, [(a["id"], 10_000), (b["id"], 90_000)])
        client.post("/api/budget/reallocate", headers=h, json={
            "allocations": [{"category_id": a["id"], "amount_cents": 1_000}],
            "allow_over_allocation": True})
        assert _bal(client, h, b["id"]) == 90_000

    def test_over_allocating_is_refused_with_the_real_numbers(self, client):
        """The guard that keeps the budget honest about what exists."""
        h = _auth(client)
        a = _cat(client, h, "Groceries")
        r = client.post("/api/budget/reallocate", headers=h, json={
            "allocations": [{"category_id": a["id"], "amount_cents": 500_000}]})
        assert r.status_code == 422
        d = r.json()["detail"]
        assert "$5,000.00" in d and "in the bank" in d and "owed on cards" in d

    def test_no_change_is_not_an_error(self, client: TestClient):
        h = _auth(client)
        a = _cat(client, h, "Groceries")
        _fund(client, h, [(a["id"], 10_000)])
        r = client.post("/api/budget/reallocate", headers=h, json={
            "allocations": [{"category_id": a["id"], "amount_cents": 10_000}],
            "allow_over_allocation": True})
        assert r.status_code == 200 and r.json()["changed"] == 0

    def test_an_empty_request_is_refused(self, client: TestClient):
        h = _auth(client)
        assert client.post("/api/budget/reallocate", headers=h,
                           json={"allocations": []}).status_code == 422
