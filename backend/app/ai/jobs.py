"""AI job queue.

Every AI call is a queued job with a persisted result, never a blocking
request. The inference host sleeps by default and takes roughly ten seconds
to wake, so nothing in the UI can afford to wait on it inline.

Jobs live in the database rather than memory, so a restart mid-job leaves a
visible `running` row to reconcile instead of losing the request silently.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.ai.provider import AIUnavailable, build_provider
from app.core.config import Settings, get_settings
from app.db.models import AIJob, User
from app.db.session import SessionLocal

log = logging.getLogger("nnf.ai.jobs")

# Only kinds with a `run_<kind>` handler in app.ai.tasks belong here.
# "categorise" and "receipt_parse" lived here with no handler: enqueuing
# either returned 202 and then failed every time, which is strictly worse
# than refusing it up front. Neither is built yet -- unknown merchants go
# to the human review queue (app/budget/categorize.py), and receipts are
# phase 5d -- so each kind comes back when its handler does. The guard in
# tests/test_ai.py keeps this honest.
KINDS = {
    "design_categories",
    "design_category",
    "refine_category",
    "describe_category",
    "suggest_category",
    "summary",
    "advise",
}

# A job still `running` after this long is assumed dead -- almost always a
# restart mid-flight -- and is failed so it stops blocking the queue.
STALE_AFTER_SECONDS = 900


def enqueue(db: Session, user_id: uuid.UUID, kind: str, request: dict[str, Any]) -> AIJob:
    if kind not in KINDS:
        raise ValueError(f"unknown job kind: {kind}")
    job = AIJob(user_id=user_id, kind=kind, status="queued", request=request)
    db.add(job)
    db.flush()
    log.info("queued %s job %s for user %s", kind, job.id, user_id)
    return job


def provider_for(user: User, settings: Settings):
    """Build the provider this user chose, falling back to the server default."""
    choice = user.ai_provider or settings.ai_provider
    if choice == "none":
        from app.ai.provider import NullProvider

        return NullProvider()
    if choice == "custom" and user.ai_base_url:
        from app.ai.provider import OllamaProvider

        return OllamaProvider(user.ai_base_url, settings)
    return build_provider(settings)


async def run_job(job_id: uuid.UUID) -> None:
    """Execute one job, recording the outcome either way.

    Opens its own session: this runs detached from the request that queued
    it, and must not share that request's transaction.
    """
    from app.ai import tasks

    with SessionLocal() as db:
        job = db.get(AIJob, job_id)
        if job is None or job.status not in ("queued", "waking"):
            return
        user = db.get(User, job.user_id)
        if user is None:
            return

        settings = get_settings()
        provider = provider_for(user, settings)

        job.status = "running"
        db.commit()

        try:
            handler = getattr(tasks, f"run_{job.kind}", None)
            if handler is None:
                raise ValueError(f"no handler for job kind {job.kind}")

            result = await handler(db, user, provider, job.request or {})
            job.result = result
            job.status = "done"
            job.provider = type(provider).__name__
        except AIUnavailable as exc:
            # Expected: the host is asleep, offline, or mid-game. Not a bug,
            # and the message should say so plainly.
            job.status = "failed"
            job.error = str(exc)
            log.info("job %s unavailable: %s", job_id, exc)
        except Exception as exc:  # noqa: BLE001
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            log.exception("job %s failed", job_id)
        finally:
            job.completed_at = datetime.now(UTC)
            db.commit()


async def worker_loop(poll_seconds: float = 3.0) -> None:
    """Background loop that drains the queue.

    One container runs this, so claiming is simple -- but the claim is still
    done with a conditional UPDATE so a second instance could never run the
    same job twice.
    """
    log.info("AI job worker started")
    while True:
        try:
            job_id = _claim_next()
            if job_id is None:
                _fail_stale_jobs()
                await asyncio.sleep(poll_seconds)
                continue
            await run_job(job_id)
        except asyncio.CancelledError:
            log.info("AI job worker stopping")
            raise
        except Exception:  # noqa: BLE001
            # The worker must never die: a crash here would silently stop
            # every future job.
            log.exception("worker loop error")
            await asyncio.sleep(poll_seconds)


def _claim_next() -> uuid.UUID | None:
    with SessionLocal() as db:
        row = db.scalars(
            select(AIJob).where(AIJob.status == "queued").order_by(AIJob.created_at).limit(1)
        ).first()
        if row is None:
            return None
        claimed = db.execute(
            update(AIJob)
            .where(AIJob.id == row.id, AIJob.status == "queued")
            .values(status="waking")
            .returning(AIJob.id)
        ).first()
        db.commit()
        return claimed[0] if claimed else None


def _fail_stale_jobs() -> None:
    """Fail jobs stuck `running`, which means a restart killed them."""
    cutoff = datetime.now(UTC).timestamp() - STALE_AFTER_SECONDS
    with SessionLocal() as db:
        stale = db.scalars(select(AIJob).where(AIJob.status.in_(("running", "waking")))).all()
        changed = False
        for job in stale:
            if job.created_at.timestamp() < cutoff:
                job.status = "failed"
                job.error = "Timed out — the server restarted or the AI host never answered."
                job.completed_at = datetime.now(UTC)
                changed = True
        if changed:
            db.commit()
