"""The admin surface: who may use it, and what it refuses.

This is the first part of the system where one account can act on
another's behalf and change how the server itself behaves, so the
refusals matter more than the happy paths and are tested first.

It is an API with a UI on top rather than a UI with logic inside it,
because an assistant driving the same instance over MCP later must be
able to do everything a person can without any of it being
reimplemented. That is why the rules live here and are tested here.
"""
from __future__ import annotations

import uuid

import pytest

from app.core.config import Settings, get_settings
from app.core.security import hash_password
from app.db.models import User
from app.db.session import SessionLocal
from tests.conftest import requires_db


def _make(role: str = "member") -> tuple[str, str]:
    email = f"adm-{uuid.uuid4().hex[:8]}@example.com"
    password = "a-long-enough-password"
    with SessionLocal() as db:
        db.add(User(email=email, password_hash=hash_password(password),
                    role=role, timezone="UTC"))
        db.commit()
    return email, password


def _token(client, email: str, password: str) -> str:
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _as(client, role: str = "member") -> dict[str, str]:
    email, password = _make(role)
    return {"Authorization": f"Bearer {_token(client, email, password)}"}


@requires_db
class TestOnlyTheOwnerMayAdminister:
    @pytest.mark.parametrize("method,path", [
        ("get", "/api/admin/users"),
        ("get", "/api/admin/settings"),
        ("get", "/api/admin/health"),
    ])
    def test_a_member_is_refused(self, client, method, path) -> None:
        r = getattr(client, method)(path, headers=_as(client, "member"))
        assert r.status_code == 403
        assert "owner" in r.text.lower(), "say why, not just no"

    def test_a_stranger_is_refused(self, client) -> None:
        assert client.get("/api/admin/users").status_code in (401, 403)

    def test_a_member_cannot_create_accounts(self, client) -> None:
        """Otherwise closing registration achieves nothing."""
        r = client.post("/api/admin/users", headers=_as(client, "member"),
                        json={"email": "x@example.com",
                              "password": "a-long-enough-password"})
        assert r.status_code == 403


@requires_db
class TestStateIsSafeToExposeUnauthenticated:
    """It must answer before anyone can sign in, so it must say little."""

    def test_it_needs_no_credentials(self, client) -> None:
        assert client.get("/api/admin/state").status_code == 200

    def test_it_reveals_nothing_about_who_is_here(self, client) -> None:
        _make("owner")
        body = client.get("/api/admin/state").json()
        assert set(body) == {"configured", "accepts_signups", "version"}
        assert body["configured"] is True
        text = client.get("/api/admin/state").text
        assert "@" not in text, "no addresses, no counts, nothing identifying"


@requires_db
class TestSecretsDoNotComeBackOut:
    def test_a_plaid_secret_is_never_returned(self, client) -> None:
        """A browser has no reason to hold it in order to replace it."""
        headers = _as(client, "owner")
        client.put("/api/admin/settings", headers=headers,
                   json={"values": {"plaid_secret": "super-secret-value"}})

        body = client.get("/api/admin/settings", headers=headers)
        assert "super-secret-value" not in body.text
        row = next(s for s in body.json() if s["key"] == "plaid_secret")
        assert row["value"] is None
        assert row["is_set"] is True, "but say that one exists"


@requires_db
class TestWhatCannotBeSetHere:
    @pytest.mark.parametrize("key", [
        "token_encryption_key",   # protects everything else, incl. this table
        "jwt_signing_key",
        "postgres_password",
        "postgres_host",
    ])
    def test_bootstrap_secrets_are_refused(self, client, key) -> None:
        r = client.put("/api/admin/settings", headers=_as(client, "owner"),
                       json={"values": {key: "anything"}})
        assert r.status_code == 400
        assert key in r.text

    def test_an_unknown_setting_is_refused(self, client) -> None:
        r = client.put("/api/admin/settings", headers=_as(client, "owner"),
                       json={"values": {"made_up_setting": "1"}})
        assert r.status_code == 400


@requires_db
class TestSavingSettings:
    def test_a_saved_value_takes_effect_without_a_restart(self, client) -> None:
        headers = _as(client, "owner")
        r = client.put("/api/admin/settings", headers=headers,
                       json={"values": {"plaid_sync_interval_minutes": 15}})
        assert r.status_code == 200, r.text
        assert get_settings().plaid_sync_interval_minutes == 15

    def test_emptying_a_field_falls_back_to_the_environment(self, client) -> None:
        """Clearing must mean "use the .env value", not "store a blank"."""
        headers = _as(client, "owner")
        client.put("/api/admin/settings", headers=headers,
                   json={"values": {"ai_model_text": "something-else"}})
        assert get_settings().ai_model_text == "something-else"

        client.put("/api/admin/settings", headers=headers,
                   json={"values": {"ai_model_text": ""}})
        assert get_settings().ai_model_text != "something-else"

    def test_a_value_that_would_not_load_is_rolled_back(self, client) -> None:
        """Otherwise the portal shows new values the server never adopted."""
        headers = _as(client, "owner")
        client.put("/api/admin/settings", headers=headers,
                   json={"values": {"plaid_sync_interval_minutes": 30}})
        assert get_settings().plaid_sync_interval_minutes == 30

        r = client.put("/api/admin/settings", headers=headers,
                       json={"values": {"plaid_sync_interval_minutes": "not a number"}})
        assert r.status_code == 400
        assert get_settings().plaid_sync_interval_minutes == 30, "unchanged"


@requires_db
class TestCreatingAccounts:
    def test_an_owner_can_create_a_member(self, client) -> None:
        headers = _as(client, "owner")
        email = f"new-{uuid.uuid4().hex[:8]}@example.com"
        r = client.post("/api/admin/users", headers=headers,
                        json={"email": email, "password": "a-long-enough-password"})
        assert r.status_code == 201, r.text
        assert r.json()["role"] == "member"

    def test_duplicate_addresses_are_refused(self, client) -> None:
        headers = _as(client, "owner")
        email = f"dup-{uuid.uuid4().hex[:8]}@example.com"
        body = {"email": email, "password": "a-long-enough-password"}
        assert client.post("/api/admin/users", headers=headers, json=body).status_code == 201
        assert client.post("/api/admin/users", headers=headers, json=body).status_code == 409

    def test_a_short_password_is_refused(self, client) -> None:
        r = client.post("/api/admin/users", headers=_as(client, "owner"),
                        json={"email": f"s-{uuid.uuid4().hex[:6]}@example.com",
                              "password": "short"})
        assert r.status_code == 422


@requires_db
class TestLoadingSettingsDoesNotEatItself:
    """Regression: loading settings must not re-enter loading settings.

    `get_settings()` overlays stored settings on the environment, which
    means decrypting the stored secrets, which used to mean
    `decrypt_token` reading `get_settings().token_encryption_key` -- a
    call to the function from inside itself.

    It failed twice, in two different ways, which is why the fix
    removes the cycle rather than managing it.

    Unguarded, each nested call opened another database session and
    recursed until the pool of ten was exhausted; the process then
    blocked forever on a connection that was never coming back. No
    error, no traceback, just a suite that ran for ten minutes instead
    of eighty seconds, with Postgres showing eight connections `idle in
    transaction` on the same SELECT.

    Guarded, it was worse, because it looked fine. CPython's
    `lru_cache` resolves a reentrant miss by KEEPING the inner result
    and discarding the outer one -- so the cache ended up holding
    settings with every stored value missing, while the caller that
    triggered the load got the correct object and saw nothing wrong.
    The next reader got the empty one.

    The encryption key is now passed down instead of looked up.
    """

    def test_a_stored_secret_survives_the_round_trip(self, client) -> None:
        headers = _as(client, "owner")
        r = client.put("/api/admin/settings", headers=headers,
                       json={"values": {"plaid_secret": "a-secret-to-decrypt",
                                        "plaid_client_id": "client-123"}})
        assert r.status_code == 200, r.text

        s = get_settings()
        assert s.plaid_secret == "a-secret-to-decrypt"
        assert s.plaid_client_id == "client-123"

    def test_the_value_that_gets_CACHED_is_the_complete_one(self, client) -> None:
        """The failure mode that looked fine: right answer, wrong cache.

        Reading twice is the whole test. The first read returns the
        correct object either way; it is the second, served from the
        cache, that used to come back empty.
        """
        headers = _as(client, "owner")
        client.put("/api/admin/settings", headers=headers,
                   json={"values": {"plaid_secret": "cached-correctly"}})

        first = get_settings().plaid_secret
        second = get_settings().plaid_secret
        assert first == "cached-correctly"
        assert second == first, "the cache kept a settings object missing its secrets"

    def test_an_endpoint_sees_it_too(self, client) -> None:
        """A request is served from that cache, so it must agree."""
        headers = _as(client, "owner")
        client.put("/api/admin/settings", headers=headers,
                   json={"values": {"plaid_secret": "visible-to-requests"}})

        row = next(x for x in client.get("/api/admin/settings",
                                         headers=headers).json()
                   if x["key"] == "plaid_secret")
        assert row["is_set"] is True
        assert row["value"] is None, "and still never sent to the browser"


@requires_db
class TestSettingsWithOnlyCertainValidValues:
    """The API declares the choices; the portal only renders them.

    `plaid_env` is not a free-text field in any meaningful sense: a
    typo decides whether a link attempt goes to the sandbox or spends
    one of a finite number of real bank connections. Keeping the list
    in the API rather than the page means there is one copy of it, and
    the assistant driving this over MCP later sees the same constraint
    a person does.
    """

    def test_the_plaid_environment_offers_its_two_values(self, client) -> None:
        rows = client.get("/api/admin/settings", headers=_as(client, "owner")).json()
        row = next(r for r in rows if r["key"] == "plaid_env")
        assert row["choices"] == ["sandbox", "production"]

    def test_the_ai_provider_offers_its_values(self, client) -> None:
        rows = client.get("/api/admin/settings", headers=_as(client, "owner")).json()
        row = next(r for r in rows if r["key"] == "ai_provider")
        assert row["choices"] == ["none", "managed", "custom", "openai"]

    def test_ordinary_settings_have_none(self, client) -> None:
        rows = client.get("/api/admin/settings", headers=_as(client, "owner")).json()
        row = next(r for r in rows if r["key"] == "plaid_webhook_url")
        assert row["choices"] is None

    def test_an_invalid_environment_is_still_refused(self, client) -> None:
        """The menu is a convenience; the API is the rule."""
        r = client.put("/api/admin/settings", headers=_as(client, "owner"),
                       json={"values": {"plaid_env": "pretend"}})
        assert r.status_code == 400
