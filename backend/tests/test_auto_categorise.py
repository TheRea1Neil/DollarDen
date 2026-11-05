"""The model files what it is sure of, and queues the rest.

The risk to guard is not a missed suggestion -- it is a confident
wrong one. A bad auto-file moves real money inside an envelope and
closes the review item, so the user has no reason to go looking for
it. Only `high` confidence files anything; everything else keeps the
suggestion and still asks.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.ai.tasks import run_auto_categorise
from app.db.models import (Account, Allocation, Category, PlaidItem,
                           ReviewItem, Transaction, User)
from app.db.session import SessionLocal
from tests.conftest import requires_db


class FakeProvider:
    """Returns one canned suggestion, shaped like the real schema."""

    def __init__(self, category: str | None, confidence: str = "high") -> None:
        self.category = category
        self.confidence = confidence
        self.calls = 0

    async def chat(self, prompt, schema=None, **kw):
        import json

        self.calls += 1

        class R:
            content = json.dumps({
                "category": self.category or "UNSURE",
                "confidence": self.confidence,
                "why": "looks like groceries",
                "matched_phrase": "",
            })
        return R()


def _seed(db, *, cat_name="Food And Groceries"):
    u = User(email=f"ac-{uuid.uuid4().hex[:8]}@x.com", password_hash="x")
    db.add(u); db.flush()
    item = PlaidItem(user_id=u.id, plaid_item_id=f"it-{uuid.uuid4().hex[:8]}",
                     access_token_encrypted=b"x")
    db.add(item); db.flush()
    acct = Account(user_id=u.id, item_id=item.id, plaid_account_id="a1",
                   name="Checking", type="depository", is_tracked=True)
    db.add(acct); db.flush()
    cat = Category(user_id=u.id, name=cat_name, kind="spending")
    db.add(cat); db.flush()
    txn = Transaction(user_id=u.id, account_id=acct.id,
                      plaid_transaction_id=f"t-{uuid.uuid4().hex[:8]}",
                      outflow_cents=42_17, inflow_cents=0,
                      date=__import__("datetime").date.today(),
                      name="SMITHS FOOD", merchant_name="Smiths")
    db.add(txn); db.flush()
    rv = ReviewItem(user_id=u.id, transaction_id=txn.id, reason="unmatched")
    db.add(rv); db.flush()
    return u, txn, cat, rv


@requires_db
class TestAutoCategorise:
    @pytest.mark.asyncio
    async def test_high_confidence_files_it_and_closes_the_review(self) -> None:
        with SessionLocal() as db:
            u, txn, cat, rv = _seed(db)
            db.commit()
            out = await run_auto_categorise(
                db, u, FakeProvider("Food And Groceries", "high"),
                {"transaction_id": str(txn.id)})

            assert out["outcome"] == "applied"
            allocs = db.scalars(
                select(Allocation).where(Allocation.transaction_id == txn.id)).all()
            assert len(allocs) == 1
            assert allocs[0].amount_cents == -42_17
            # History must be able to say the AI decided this one.
            assert allocs[0].source == "ai_categorise"
            db.refresh(rv)
            assert rv.resolved_at is not None
            assert rv.resolution["method"] == "ai"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("confidence", ["medium", "low"])
    async def test_anything_short_of_high_only_suggests(self, confidence) -> None:
        with SessionLocal() as db:
            u, txn, cat, rv = _seed(db)
            db.commit()
            out = await run_auto_categorise(
                db, u, FakeProvider("Food And Groceries", confidence),
                {"transaction_id": str(txn.id)})

            assert out["applied"] is False
            assert out["outcome"] == "suggested"
            assert not db.scalars(
                select(Allocation).where(Allocation.transaction_id == txn.id)).all()
            db.refresh(rv)
            assert rv.resolved_at is None, "it must still be the user's call"
            assert rv.suggestions["ai"]["category_name"] == "Food And Groceries"

    @pytest.mark.asyncio
    async def test_a_declining_model_leaves_a_plain_review(self) -> None:
        with SessionLocal() as db:
            u, txn, cat, rv = _seed(db)
            db.commit()
            out = await run_auto_categorise(
                db, u, FakeProvider(None), {"transaction_id": str(txn.id)})
            assert out["outcome"] == "unsure"
            assert out["applied"] is False
            db.refresh(rv)
            assert rv.resolved_at is None

    @pytest.mark.asyncio
    async def test_it_does_not_overwrite_a_decision_made_while_it_waited(self) -> None:
        """A job can sit deferred for an hour; the user may have filed it."""
        with SessionLocal() as db:
            u, txn, cat, rv = _seed(db)
            from datetime import UTC, datetime
            rv.resolved_at = datetime.now(UTC)
            db.commit()

            provider = FakeProvider("Food And Groceries", "high")
            out = await run_auto_categorise(
                db, u, provider, {"transaction_id": str(txn.id)})

            assert out["outcome"] == "already_handled"
            assert provider.calls == 0, "must not even ask the model"
            assert not db.scalars(
                select(Allocation).where(Allocation.transaction_id == txn.id)).all()

    @pytest.mark.asyncio
    async def test_an_excluded_charge_is_never_filed(self) -> None:
        with SessionLocal() as db:
            u, txn, cat, rv = _seed(db)
            txn.treatment = "ignored"
            db.commit()
            out = await run_auto_categorise(
                db, u, FakeProvider("Food And Groceries", "high"),
                {"transaction_id": str(txn.id)})
            assert out["outcome"] == "not_budgeted"
            assert out["applied"] is False
