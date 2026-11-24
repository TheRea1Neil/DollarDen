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


@requires_db
class TestNewChargesActuallyReachTheQueue:
    """The queueing guard, which is a separate failure from the job.

    Every test above runs `run_auto_categorise` directly, so they all
    passed while nothing on the instance was queueing it. A rename
    folded the provider `deck` into `managed` at the settings edge and
    this guard still allow-listed `deck`, so it returned 0 before
    reading a transaction -- for three weeks, suite green, every charge
    arriving as "not looked at automatically".

    So: parametrise over the provider values, and assert on the count
    the sync reports rather than on the guard's internals.
    """

    @pytest.mark.parametrize("provider", ["managed", "custom", "openai"])
    def test_a_configured_provider_queues_one_job_per_open_review(
            self, provider, monkeypatch) -> None:
        from app.core import config as cfg
        from app.db.models import AIJob
        from app.plaid.sync import _queue_ai_categorisation

        with SessionLocal() as db:
            u, txn, cat, rv = _seed(db)
            db.commit()

            monkeypatch.setattr(cfg.get_settings(), "ai_provider", provider,
                                raising=False)
            queued = _queue_ai_categorisation(db, u.id, [txn])
            db.commit()

            assert queued == 1, f"{provider} is a model; it must queue"
            jobs = db.scalars(select(AIJob).where(
                AIJob.user_id == u.id,
                AIJob.kind == "auto_categorise")).all()
            assert len(jobs) == 1
            assert jobs[0].request["transaction_id"] == str(txn.id)

    def test_no_provider_queues_nothing(self, monkeypatch) -> None:
        """ADR 0003: with the model off, queueing would only pile up errors."""
        from app.core import config as cfg
        from app.plaid.sync import _queue_ai_categorisation

        with SessionLocal() as db:
            u, txn, cat, rv = _seed(db)
            db.commit()

            monkeypatch.setattr(cfg.get_settings(), "ai_provider", "none",
                                raising=False)
            assert _queue_ai_categorisation(db, u.id, [txn]) == 0

    def test_a_users_own_host_queues_even_with_the_instance_off(
            self, monkeypatch) -> None:
        from app.core import config as cfg
        from app.plaid.sync import _queue_ai_categorisation

        with SessionLocal() as db:
            u, txn, cat, rv = _seed(db)
            u.ai_base_url = "http://my-own-box:11434"
            db.commit()

            monkeypatch.setattr(cfg.get_settings(), "ai_provider", "none",
                                raising=False)
            assert _queue_ai_categorisation(db, u.id, [txn]) == 1

    def test_every_provider_the_config_allows_is_covered_here(self) -> None:
        """Fails when a provider is added, which is the point.

        The regression was a value the guard had never heard of. A new
        one must either be listed above or deliberately excluded.
        """
        import typing

        from app.core.config import Settings

        allowed = set(typing.get_args(
            Settings.model_fields["ai_provider"].annotation))
        covered = {"managed", "custom", "openai", "none"}
        # `deck` is the pre-rename spelling; the edge folds it, so it can
        # never reach this guard and needs no case of its own.
        assert allowed - {"deck"} == covered, (
            f"provider(s) {allowed - {'deck'} - covered} added with no "
            "queueing test -- add a case above")
