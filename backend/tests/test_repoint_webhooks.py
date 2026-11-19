"""Moving the server must not cost Plaid connections.

Plaid fixes an Item's webhook URL at link time. So adding a
certificate, changing domain or moving host orphans every connection
made before the move: new ones get the new URL, old ones keep calling
the old address, and nothing anywhere reports an error. Transactions
stop arriving in real time for the accounts you have had longest,
which is the hardest kind of failure to spot.

The only other remedy is re-linking, and on the Trial plan each link
spends one of a small LIFETIME allowance that removing an Item does not
return. So this endpoint is what keeps "I set up HTTPS afterwards" from
being a permanent cost.
"""
from __future__ import annotations

import uuid

import pytest

from app.core.security import encrypt_token
from app.db.models import PlaidItem, User
from app.db.session import SessionLocal
from tests.conftest import requires_db


def _user_with_items(db, n: int = 2) -> User:
    u = User(email=f"rw-{uuid.uuid4().hex[:8]}@x.com", password_hash="x")
    db.add(u); db.flush()
    for i in range(n):
        db.add(PlaidItem(
            user_id=u.id,
            plaid_item_id=f"it-{uuid.uuid4().hex[:10]}",
            institution_name=f"Bank {i}",
            access_token_encrypted=encrypt_token(f"access-{i}"),
        ))
    db.flush()
    return u


class _FakeClient:
    """Records what it was asked to repoint, and can fail on cue."""

    def __init__(self, fail_on: set[str] | None = None,
                 url: str = "https://new.example.org/plaid/webhook/sandbox") -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail_on = fail_on or set()
        self.url = url

    def webhook_url(self) -> str:
        """The real client owns this, environment path and all.

        Re-pointing has to target exactly where a fresh link would
        register, or old Items end up somewhere new Items never go.
        """
        return self.url

    async def update_item_webhook(self, access_token: str, webhook: str):
        self.calls.append((access_token, webhook))
        if access_token in self.fail_on:
            raise RuntimeError("ITEM_LOGIN_REQUIRED")
        return {"item": {"webhook": webhook}}


@requires_db
class TestRepointingEveryConnection:
    @pytest.mark.asyncio
    async def test_every_item_is_repointed_to_the_new_url(self) -> None:
        from app.api.routes.plaid import repoint_webhooks
        from app.core.config import Settings

        with SessionLocal() as db:
            u = _user_with_items(db, 3)
            client = _FakeClient()
            s = Settings(plaid_webhook_url="https://new.example.org/plaid/webhook",
                         allow_production=False)

            out = await repoint_webhooks(user=u, db=db, client=client, s=s)

            assert len(client.calls) == 3, "every connection, not just new ones"
            assert {w for _, w in client.calls} == {
                "https://new.example.org/plaid/webhook/sandbox"}, \
                "the environment path must match where new links go"
            assert len(out["updated"]) == 3
            assert out["failed"] == []

    @pytest.mark.asyncio
    async def test_one_broken_item_does_not_stop_the_others(self) -> None:
        """A connection needing re-auth is the one you most need told about.

        It must not also prevent the healthy connections being fixed.
        """
        from app.api.routes.plaid import repoint_webhooks
        from app.core.config import Settings

        with SessionLocal() as db:
            u = _user_with_items(db, 3)
            client = _FakeClient(fail_on={"access-1"})
            s = Settings(plaid_webhook_url="https://new.example.org/plaid/webhook",
                         allow_production=False)

            out = await repoint_webhooks(user=u, db=db, client=client, s=s)

            assert len(client.calls) == 3, "kept going after the failure"
            assert len(out["updated"]) == 2
            assert len(out["failed"]) == 1
            assert "Bank 1" in out["failed"]

    @pytest.mark.asyncio
    async def test_it_refuses_when_no_webhook_url_is_configured(self) -> None:
        """Repointing at an empty string would break what still works."""
        from fastapi import HTTPException

        from app.api.routes.plaid import repoint_webhooks
        from app.core.config import Settings

        with SessionLocal() as db:
            u = _user_with_items(db, 1)
            client = _FakeClient(url="")
            s = Settings(plaid_webhook_url="", allow_production=False)

            with pytest.raises(HTTPException) as got:
                await repoint_webhooks(user=u, db=db, client=client, s=s)
            assert got.value.status_code == 400
            assert client.calls == [], "nothing touched when there is no target"
