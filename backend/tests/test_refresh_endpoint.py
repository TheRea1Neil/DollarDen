"""Pull-to-refresh must be able to reach the bank.

It used to re-read this server's database, so it could only show what
the last webhook happened to deliver. When a webhook is missed the app
goes quietly stale and the user has no way to correct it -- which is
what happened on the real account: balances a day old, and pulling
down changed nothing.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db.models import Account, PlaidItem, User
from app.db.session import SessionLocal
from tests.conftest import requires_db

PW = "refresh-endpoint-passphrase"


def _auth(client):
    email = f"rf-{uuid.uuid4().hex[:10]}@x.com"
    r = client.post("/api/auth/register", json={"email": email, "password": PW})
    assert r.status_code == 201, r.text
    with SessionLocal() as db:
        uid = db.scalars(select(User.id).where(User.email == email)).one()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}, uid


def _item(db, uid, *, synced_ago_seconds: int | None):
    it = PlaidItem(user_id=uid, plaid_item_id=f"it-{uuid.uuid4().hex[:10]}",
                   access_token_encrypted=b"x", institution_name="Bank")
    if synced_ago_seconds is not None:
        it.last_synced_at = datetime.now(UTC) - timedelta(seconds=synced_ago_seconds)
    db.add(it); db.flush()
    db.add(Account(user_id=uid, item_id=it.id,
                   plaid_account_id=f"ac-{uuid.uuid4().hex[:8]}",
                   name="Checking", type="depository", is_tracked=True,
                   current_cents=1000, balance_as_of=datetime.now(UTC)))
    db.flush()
    return it


@requires_db
class TestRefresh:
    def test_no_banks_is_not_an_error(self, client) -> None:
        h, _ = _auth(client)
        r = client.post("/api/plaid/refresh", headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["synced"] == 0

    def test_a_just_synced_item_is_skipped_not_refetched(self, client) -> None:
        """/accounts/balance/get reaches the bank; pulls must not spam it."""
        h, uid = _auth(client)
        with SessionLocal() as db:
            _item(db, uid, synced_ago_seconds=2)
            db.commit()

        out = client.post("/api/plaid/refresh", headers=h).json()
        assert out["skipped"] == 1
        assert out["synced"] == 0
        # Throttled is not an error: nothing has changed in two seconds.
        assert out["errors"] == []

    def test_a_stale_item_is_attempted(self, client) -> None:
        h, uid = _auth(client)
        with SessionLocal() as db:
            _item(db, uid, synced_ago_seconds=3600)
            db.commit()

        out = client.post("/api/plaid/refresh", headers=h).json()
        assert out["skipped"] == 0
        # The token is fake, so Plaid refuses -- the point is that it
        # TRIED, and that a failure is reported rather than raised.
        assert out["synced"] + len(out["errors"]) >= 1

    def test_a_never_synced_item_is_attempted(self, client) -> None:
        h, uid = _auth(client)
        with SessionLocal() as db:
            _item(db, uid, synced_ago_seconds=None)
            db.commit()
        out = client.post("/api/plaid/refresh", headers=h).json()
        assert out["skipped"] == 0

    def test_it_reports_when_the_balances_are_from(self, client) -> None:
        h, uid = _auth(client)
        with SessionLocal() as db:
            _item(db, uid, synced_ago_seconds=2)
            db.commit()
        out = client.post("/api/plaid/refresh", headers=h).json()
        assert out["balances_as_of"] is not None, (
            "the user needs to know how old the number is, especially "
            "when the refresh was throttled"
        )

    def test_another_users_banks_are_not_touched(self, client) -> None:
        h_a, uid_a = _auth(client)
        _, uid_b = _auth(client)
        with SessionLocal() as db:
            _item(db, uid_b, synced_ago_seconds=3600)
            db.commit()
        out = client.post("/api/plaid/refresh", headers=h_a).json()
        assert out["synced"] == 0 and out["skipped"] == 0
