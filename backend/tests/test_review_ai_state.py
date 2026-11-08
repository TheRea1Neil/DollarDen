"""The review queue must say whether the assistant is still on it.

Every open item looked identical, so there was no way to tell "still
working" from "looked and gave up" from "never saw it". Two of those
resolve themselves and two never will, and the difference decides
whether you wait or act.

The model's own reasoning is the half worth surfacing: for a real
Amazon charge it said the merchant "sells everything from groceries to
electronics to cloud services", which explains the hand-back far
better than any status label.
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select

from app.db.models import (Account, AIJob, PlaidItem, ReviewItem,
                           Transaction, User)
from app.db.session import SessionLocal
from tests.conftest import requires_db

PW = "review-ai-state-passphrase"


def _auth(client):
    email = f"ras-{uuid.uuid4().hex[:10]}@x.com"
    r = client.post("/api/auth/register", json={"email": email, "password": PW})
    assert r.status_code == 201, r.text
    with SessionLocal() as db:
        uid = db.scalars(select(User.id).where(User.email == email)).one()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}, uid


def _queued(db, uid, *, name="AMAZON MKTPLACE PMTS") -> Transaction:
    item = PlaidItem(user_id=uid, plaid_item_id=f"it-{uuid.uuid4().hex[:8]}",
                     access_token_encrypted=b"x")
    db.add(item); db.flush()
    acct = Account(user_id=uid, item_id=item.id,
                   plaid_account_id=f"a-{uuid.uuid4().hex[:8]}",
                   name="Card", type="credit", is_tracked=True)
    db.add(acct); db.flush()
    t = Transaction(user_id=uid, account_id=acct.id,
                    plaid_transaction_id=f"t-{uuid.uuid4().hex[:8]}",
                    outflow_cents=751, inflow_cents=0, date=date.today(),
                    name=name)
    db.add(t); db.flush()
    db.add(ReviewItem(user_id=uid, transaction_id=t.id, reason="unmatched"))
    db.flush()
    return t


def _job(db, uid, txn, *, status, result=None, error=None,
         defer_reason=None, not_before=None):
    db.add(AIJob(user_id=uid, kind="auto_categorise", status=status,
                 request={"transaction_id": str(txn.id)}, result=result,
                 error=error, defer_reason=defer_reason, not_before=not_before))
    db.flush()


@requires_db
class TestReviewAiState:
    def test_an_unsure_item_says_so_and_gives_the_reason(self, client) -> None:
        with SessionLocal() as db:
            h_uid = None
            t = None
        h, uid = _auth(client)
        with SessionLocal() as db:
            t = _queued(db, uid)
            _job(db, uid, t, status="done", result={
                "outcome": "unsure", "applied": False,
                "suggestion": {"why": "Amazon sells everything from groceries "
                                      "to cloud services."}})
            db.commit()

        row = client.get("/api/budget/review", headers=h).json()[0]
        assert row["ai_state"] == "unsure"
        assert "groceries" in row["ai_reason"], "the reasoning is the useful part"

    def test_a_queued_job_reads_as_still_working(self, client) -> None:
        h, uid = _auth(client)
        with SessionLocal() as db:
            t = _queued(db, uid)
            _job(db, uid, t, status="queued")
            db.commit()
        assert client.get("/api/budget/review",
                          headers=h).json()[0]["ai_state"] == "working"

    def test_a_deferred_job_reports_when_it_will_retry(self, client) -> None:
        h, uid = _auth(client)
        later = datetime.now(UTC) + timedelta(hours=1)
        with SessionLocal() as db:
            t = _queued(db, uid)
            _job(db, uid, t, status="deferred",
                 defer_reason="The graphics card is busy (79%).",
                 not_before=later)
            db.commit()
        row = client.get("/api/budget/review", headers=h).json()[0]
        assert row["ai_state"] == "deferred"
        assert "79%" in row["ai_reason"]
        assert row["ai_retry_at"] is not None, "say when, or it reads as stuck"

    def test_a_suggestion_names_the_category_it_leans_to(self, client) -> None:
        h, uid = _auth(client)
        with SessionLocal() as db:
            t = _queued(db, uid)
            _job(db, uid, t, status="done", result={
                "outcome": "suggested", "applied": False,
                "suggestion": {"category_name": "Food & Groceries",
                               "why": "probably a grocery run"}})
            db.commit()
        row = client.get("/api/budget/review", headers=h).json()[0]
        assert row["ai_state"] == "suggested"
        assert row["ai_suggested_category"] == "Food & Groceries"

    def test_an_item_with_no_job_is_honest_about_it(self, client) -> None:
        """A stranded item must not look like one still being worked on."""
        h, uid = _auth(client)
        with SessionLocal() as db:
            _queued(db, uid)
            db.commit()
        assert client.get("/api/budget/review",
                          headers=h).json()[0]["ai_state"] == "none"

    def test_a_failed_job_is_not_mistaken_for_pending(self, client) -> None:
        h, uid = _auth(client)
        with SessionLocal() as db:
            t = _queued(db, uid)
            _job(db, uid, t, status="failed", error="The AI computer is asleep.")
            db.commit()
        row = client.get("/api/budget/review", headers=h).json()[0]
        assert row["ai_state"] == "failed"
        assert "asleep" in row["ai_reason"]

    def test_the_newest_attempt_wins(self, client) -> None:
        h, uid = _auth(client)
        with SessionLocal() as db:
            t = _queued(db, uid)
            _job(db, uid, t, status="failed", error="old failure")
            db.flush()
            _job(db, uid, t, status="queued")
            db.commit()
        assert client.get("/api/budget/review",
                          headers=h).json()[0]["ai_state"] == "working"
