"""Who may create an account on a self-hosted instance.

Registration used to be open to anyone who could reach the server,
rate limited and nothing more. For one person's budget on one Pi that
was merely untidy. Published, it is a hole with a price attached: an
instance on a public URL means a stranger creating an account and
spending the owner's Plaid connections, which are finite, lifetime,
and not returned when the account is deleted.

So it is closed, with exactly two ways through: the first account on
an empty instance, which becomes the owner, and an owner making one.
"""
from __future__ import annotations

import uuid

from sqlalchemy import delete

from app.core.config import Settings, get_settings
from app.db.models import User
from app.db.session import SessionLocal
from tests.conftest import requires_db


def _body(**over):
    b = {"email": f"reg-{uuid.uuid4().hex[:8]}@example.com",
         "password": "a-long-enough-password", "timezone": "UTC"}
    b.update(over)
    return b


@requires_db
class TestTheFirstAccountClaimsTheInstance:
    def test_an_empty_instance_accepts_one_account_and_makes_it_owner(
            self, client) -> None:
        with SessionLocal() as db:
            db.execute(delete(User))
            db.commit()

        get_settings.cache_clear()
        body = _body()
        r = client.post("/api/auth/register", json=body)
        assert r.status_code == 201, r.text

        with SessionLocal() as db:
            made = db.query(User).filter(User.email == body["email"]).first()
            assert made is not None
            assert made.role == "owner", "whoever sets the server up owns it"

    def test_a_second_account_is_refused_once_closed(self, client) -> None:
        """The hole: anyone who finds the URL making themselves an account."""
        with SessionLocal() as db:
            db.execute(delete(User))
            db.commit()
        get_settings.cache_clear()

        assert client.post("/api/auth/register", json=_body()).status_code == 201

        import app.api.routes.auth as auth_mod

        real = auth_mod.get_settings
        auth_mod.get_settings = lambda: Settings(
            allow_open_registration=False, allow_production=False)
        try:
            r = client.post("/api/auth/register", json=_body())
        finally:
            auth_mod.get_settings = real

        assert r.status_code == 403
        assert "does not accept new sign-ups" in r.text
        assert "Ask whoever runs" in r.text, "say what to do instead"


@requires_db
class TestAnInstanceThatPredatesRoles:
    def test_existing_accounts_mean_nobody_can_claim_ownership(
            self, client) -> None:
        """The land grab, and why the gate is "no accounts" not "no owner".

        A server upgraded from before roles existed has accounts and no
        owner. If the wizard keyed off "no owner yet", the first
        stranger to reach it would become the owner of someone's live
        budget server. Ownership there is named from the machine
        itself, with `python -m app.ownership`.
        """
        import app.api.routes.auth as auth_mod

        with SessionLocal() as db:
            db.execute(delete(User))
            db.add(User(email=f"old-{uuid.uuid4().hex[:8]}@example.com",
                        password_hash="x", role="member"))
            db.commit()

        real = auth_mod.get_settings
        auth_mod.get_settings = lambda: Settings(
            allow_open_registration=False, allow_production=False)
        try:
            r = client.post("/api/auth/register", json=_body())
        finally:
            auth_mod.get_settings = real

        assert r.status_code == 403, "an unowned instance is not up for grabs"


class TestTheDefault:
    def test_registration_is_closed_unless_opened(self) -> None:
        """The DECLARED default, not an instantiated one.

        Settings reads the environment, and this suite deliberately
        sets ALLOW_OPEN_REGISTRATION=true because it creates users
        constantly. Asserting on an instance here would therefore test
        the test harness and pass whatever the shipped default became.
        """
        field = Settings.model_fields["allow_open_registration"]
        assert field.default is False, "closed unless someone opens it"
