"""A sync must update balances, not just transactions.

Found on the real account: the webhook and the Sync button both called
sync_item, which only drained /transactions/sync. refresh_accounts --
the only thing that writes accounts.current_cents -- ran just at link
time. So the transaction list moved and the headline figure the whole
budget is built on, cash minus card debt, stayed frozen at whatever it
was the day the bank was connected.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.db.models import Account, PlaidItem, User
from app.db.session import SessionLocal
from app.plaid.sync import sync_item
from tests.conftest import requires_db


class FakePlaid:
    """Minimal stand-in: no pages of transactions, one changed balance."""

    def __init__(self, current: float, *, fail_balances: bool = False,
                 no_live: bool = False) -> None:
        self.current = current
        self.fail_balances = fail_balances
        self.no_live = no_live
        self.accounts_calls = 0
        self.balance_calls = 0

    async def transactions_sync(self, access_token, cursor, count=100):
        return {"added": [], "modified": [], "removed": [],
                "next_cursor": "cursor-2", "has_more": False}

    async def get_balances(self, access_token):
        """Live balances. Preferred, because the cached ones go stale."""
        self.balance_calls += 1
        if self.no_live:
            raise RuntimeError("balance product unavailable")
        if self.fail_balances:
            raise RuntimeError("plaid is down")
        return self._payload()

    async def get_accounts(self, access_token):
        self.accounts_calls += 1
        if self.fail_balances:
            raise RuntimeError("plaid is down")
        return self._payload()

    def _payload(self):
        return {"accounts": [{
            "account_id": "acc-1", "name": "Checking", "official_name": None,
            "mask": "0000", "type": "depository", "subtype": "checking",
            "balances": {"current": self.current, "available": self.current,
                         "iso_currency_code": "USD"},
        }]}


def _seed(db) -> tuple[User, PlaidItem, Account]:
    u = User(email=f"bal-{uuid.uuid4().hex[:8]}@x.com", password_hash="x")
    db.add(u); db.flush()
    item = PlaidItem(user_id=u.id, plaid_item_id=f"it-{uuid.uuid4().hex[:8]}",
                     access_token_encrypted=b"x", institution_name="Bank")
    db.add(item); db.flush()
    acct = Account(user_id=u.id, item_id=item.id, plaid_account_id="acc-1",
                   name="Checking", is_tracked=True, current_cents=110_00)
    db.add(acct); db.flush()
    return u, item, acct


@requires_db
class TestSyncRefreshesBalances:
    @pytest.mark.asyncio
    async def test_balance_moves_with_the_bank(self) -> None:
        with SessionLocal() as db:
            _, item, acct = _seed(db)
            db.commit()
            acct_id = acct.id
            assert acct.current_cents == 110_00

            fake = FakePlaid(current=842.37)
            await sync_item(db, fake, item, "tok")

            db.expire_all()
            refreshed = db.get(Account, acct_id)
            assert refreshed.current_cents == 84237, (
                "the dashboard figure comes from this column; a sync that "
                "leaves it stale shows the wrong money to allocate"
            )
            assert fake.balance_calls == 1, "must ask the bank, not Plaid's cache"
            assert fake.accounts_calls == 0

    @pytest.mark.asyncio
    async def test_a_failed_balance_refresh_does_not_lose_the_cursor(self) -> None:
        """Transactions are unrecoverable; balances are not."""
        with SessionLocal() as db:
            _, item, _ = _seed(db)
            db.commit()

            fake = FakePlaid(current=1.0, fail_balances=True)
            stats = await sync_item(db, fake, item, "tok")

            db.expire_all()
            fresh = db.scalars(
                select(PlaidItem).where(PlaidItem.id == item.id)
            ).one()
            assert fresh.sync_cursor == "cursor-2", (
                "the cursor must already be committed before balances are "
                "attempted -- Plaid will not resend those transactions"
            )
            assert any("balance refresh" in e for e in stats.errors)

    @pytest.mark.asyncio
    async def test_it_can_be_switched_off(self) -> None:
        with SessionLocal() as db:
            _, item, _ = _seed(db)
            db.commit()
            fake = FakePlaid(current=99.0)
            await sync_item(db, fake, item, "tok", refresh_balances=False)
            assert fake.balance_calls == 0
            assert fake.accounts_calls == 0

    @pytest.mark.asyncio
    async def test_it_falls_back_to_cached_balances(self) -> None:
        """A stale balance beats no account record at all."""
        with SessionLocal() as db:
            _, item, acct = _seed(db)
            db.commit()
            acct_id = acct.id

            fake = FakePlaid(current=500.0, no_live=True)
            await sync_item(db, fake, item, "tok")

            db.expire_all()
            assert fake.balance_calls == 1
            assert fake.accounts_calls == 1, "should fall back, not give up"
            assert db.get(Account, acct_id).current_cents == 50000
