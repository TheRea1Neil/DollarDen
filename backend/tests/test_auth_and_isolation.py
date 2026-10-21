"""Auth flow plus the cross-user isolation guarantee.

docs/07 calls user-A-reads-user-B the most likely serious bug in a two-user
finance app, so it gets explicit tests rather than trust.
"""
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from tests.conftest import requires_db

PW = "correct-horse-battery-staple"


def _register(client: TestClient, email: str) -> dict:
    r = client.post("/api/auth/register", json={"email": email, "password": PW})
    assert r.status_code == 201, r.text
    return r.json()


def _auth(tokens: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


@requires_db
def test_register_login_me_roundtrip(client: TestClient) -> None:
    email = f"a-{uuid.uuid4().hex[:8]}@example.com"
    tokens = _register(client, email)
    assert tokens["access_token"] and tokens["refresh_token"]

    me = client.get("/api/auth/me", headers=_auth(tokens))
    assert me.status_code == 200
    assert me.json()["email"] == email

    login = client.post("/api/auth/login", json={"email": email, "password": PW})
    assert login.status_code == 200


@requires_db
def test_duplicate_email_rejected(client: TestClient) -> None:
    email = f"dup-{uuid.uuid4().hex[:8]}@example.com"
    _register(client, email)
    r = client.post("/api/auth/register", json={"email": email, "password": PW})
    assert r.status_code == 409


@requires_db
def test_wrong_password_rejected(client: TestClient) -> None:
    email = f"wp-{uuid.uuid4().hex[:8]}@example.com"
    _register(client, email)
    r = client.post("/api/auth/login", json={"email": email, "password": "wrong-password-here"})
    assert r.status_code == 401


@requires_db
def test_login_error_does_not_reveal_whether_account_exists(client: TestClient) -> None:
    email = f"ex-{uuid.uuid4().hex[:8]}@example.com"
    _register(client, email)
    known = client.post("/api/auth/login", json={"email": email, "password": "bad-password-xx"})
    unknown = client.post(
        "/api/auth/login",
        json={"email": f"nope-{uuid.uuid4().hex[:8]}@example.com", "password": "bad-password-xx"},
    )
    assert known.status_code == unknown.status_code == 401
    assert known.json()["detail"] == unknown.json()["detail"]


@requires_db
def test_no_token_is_unauthorised(client: TestClient) -> None:
    assert client.get("/api/auth/me").status_code in (401, 403)


@requires_db
def test_garbage_token_is_unauthorised(client: TestClient) -> None:
    r = client.get("/api/auth/me", headers={"Authorization": "Bearer not.a.jwt"})
    assert r.status_code == 401


@requires_db
def test_refresh_rotates_and_old_token_is_single_use(client: TestClient) -> None:
    email = f"rt-{uuid.uuid4().hex[:8]}@example.com"
    tokens = _register(client, email)
    old = tokens["refresh_token"]

    first = client.post("/api/auth/refresh", json={"refresh_token": old})
    assert first.status_code == 200
    assert first.json()["refresh_token"] != old

    replay = client.post("/api/auth/refresh", json={"refresh_token": old})
    assert replay.status_code == 401, "a refresh token must not be reusable"


@requires_db
def test_logout_revokes_refresh_token(client: TestClient) -> None:
    email = f"lo-{uuid.uuid4().hex[:8]}@example.com"
    tokens = _register(client, email)
    assert client.post("/api/auth/logout", json={"refresh_token": tokens["refresh_token"]}).status_code == 204
    after = client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert after.status_code == 401


@requires_db
def test_two_users_get_distinct_identities(client: TestClient) -> None:
    """Foundation of isolation: A's token must never resolve to B."""
    a = _register(client, f"iso-a-{uuid.uuid4().hex[:8]}@example.com")
    b = _register(client, f"iso-b-{uuid.uuid4().hex[:8]}@example.com")

    me_a = client.get("/api/auth/me", headers=_auth(a)).json()
    me_b = client.get("/api/auth/me", headers=_auth(b)).json()

    assert me_a["id"] != me_b["id"]
    assert me_a["email"] != me_b["email"]


@requires_db
def test_repository_refuses_cross_user_access() -> None:
    """The structural guarantee: a scoped repo cannot see another user's rows."""
    from app.core.security import hash_password
    from app.db.models import Category, User
    from app.db.repository import UserScopedRepository
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        ua = User(email=f"ra-{uuid.uuid4().hex[:8]}@x.com", password_hash=hash_password(PW))
        ub = User(email=f"rb-{uuid.uuid4().hex[:8]}@x.com", password_hash=hash_password(PW))
        db.add_all([ua, ub])
        db.flush()

        repo_a = UserScopedRepository(db, Category, ua.id)
        repo_b = UserScopedRepository(db, Category, ub.id)

        cat_a = repo_a.add(Category(name="A Food", kind="spending"))
        db.flush()

        # B must not see it, and must not be able to fetch it by id.
        assert repo_b.all() == []
        assert repo_b.get(cat_a.id) is None
        assert repo_a.get(cat_a.id) is not None

        db.rollback()


def test_repository_rejects_non_user_scoped_models() -> None:
    """A model without user_id must not be wrapped in a scoped repo by mistake.

    The guard is a hasattr check, so a plain class exercises it without
    needing a real ORM model.
    """
    import pytest

    from app.db.repository import UserScopedRepository

    class NotScoped:  # deliberately has no user_id
        id = None

    with pytest.raises(TypeError, match="not user-scoped"):
        UserScopedRepository(None, NotScoped, uuid.uuid4())  # type: ignore[arg-type]
