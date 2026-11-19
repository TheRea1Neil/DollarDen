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
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from app.ai import gpu
from app.ai.provider import AIHostStalled, AIUnavailable, build_provider
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
    # Suggest AND apply, queued automatically as transactions arrive.
    "auto_categorise",
    "receipt_parse",
    "receipt_scan",
    "categorise_items",
    "summary",
    "advise",
    # Analytics. `ask` compiles a question into a query and phrases the
    # result; it never computes the figures (see tasks.run_ask).
    "classify_expense_type",
    "ask",
    "narrate_insights",
}

# A job still `running` after this long is assumed dead -- almost always a
# restart mid-flight -- and is failed so it stops blocking the queue.
STALE_AFTER_SECONDS = 900

# Hosts that took a request and then went silent, and when it is worth
# approaching them again.
#
# Keyed by base URL, not global: one person's own ollama box going quiet
# must not hold up jobs bound for a different host.
#
# In memory on purpose. This is a "do not walk into the wall again for a
# bit" note, not a fact about the budget, and a container restart is
# exactly the moment it should be forgotten -- the host may well have
# been fixed in the meantime.
_stalled_until: dict[str, datetime] = {}


def _host_key(provider: Any) -> str | None:
    return getattr(provider, "base_url", None)


def _note_stall(provider: Any, settings: Settings) -> None:
    key = _host_key(provider)
    if key is not None:
        _stalled_until[key] = datetime.now(UTC) + timedelta(
            seconds=settings.ai_stall_backoff_seconds)


def _clear_stall(provider: Any) -> None:
    key = _host_key(provider)
    if key is not None:
        _stalled_until.pop(key, None)


def _stall_hold_remaining(provider: Any) -> timedelta | None:
    """How much longer to leave a silent host alone, if at all."""
    key = _host_key(provider)
    if key is None:
        return None
    until = _stalled_until.get(key)
    if until is None:
        return None
    remaining = until - datetime.now(UTC)
    if remaining.total_seconds() <= 0:
        _stalled_until.pop(key, None)
        return None
    return remaining


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

        # A host that stopped answering is left alone for a while.
        #
        # The queue is serial and a wedged model process does not
        # recover on its own, so without this every remaining job pays
        # the full request timeout in turn: one stalled deck turned a
        # handful of queued transactions into an hour of nothing.
        #
        # Deferred rather than failed -- the work is still wanted, just
        # not now -- and `attempts` is deliberately NOT incremented,
        # because the out-of-patience escape below exists for a busy
        # GPU, and forcing a job onto a host that is not answering at
        # all achieves nothing but another timeout.
        hold = _stall_hold_remaining(provider)
        if hold is not None and job.attempts >= settings.ai_stall_max_holds:
            # Held long enough. A host this quiet is not recovering by
            # itself, and waiting indefinitely is the worst outcome: the
            # transaction shows "waiting for the AI" forever instead of
            # reaching the review queue, which is the designed fallback
            # and needs no AI at all (ADR 0003).
            job.status = "failed"
            job.error = (
                "The AI computer has been switched on but not answering "
                "for a while, so this was left for you to sort out by "
                "hand. It may need restarting."
            )
            job.completed_at = datetime.now(UTC)
            db.commit()
            log.warning("job %s given up after %d holds on a silent host",
                        job_id, job.attempts)
            return
        if hold is not None:
            job.attempts += 1
            minutes = max(1, int(hold.total_seconds() // 60))
            job.status = "deferred"
            job.defer_reason = (
                "The AI computer is switched on but stopped answering. "
                f"Leaving it alone for about {minutes} more minute"
                f"{'' if minutes == 1 else 's'} before trying again — "
                "it may need restarting."
            )
            job.not_before = datetime.now(UTC) + hold
            db.commit()
            log.info("job %s held: host stalled for another %.0fs",
                     job_id, hold.total_seconds())
            return

        # Gate: run immediately when there is room, wait when there is
        # not. The normal path is straight through -- see app/ai/gpu.py.
        #
        # Skipped entirely for a provider with no machine of ours
        # behind it. A busy graphics card and a sleeping PC are reasons
        # to wait; a web service is neither, and deferring a hosted
        # model until 08:00 would be a baffling way to behave.
        verdict = (
            await gpu.can_run_now(settings) if provider.uses_local_hardware
            else gpu.Verdict(ok=True)
        )
        if not verdict.ok and job.attempts < settings.ai_max_defer_attempts:
            job.status = "deferred"
            job.attempts += 1
            job.defer_reason = verdict.reason
            job.not_before = datetime.now(UTC) + timedelta(
                minutes=settings.ai_defer_minutes)
            db.commit()
            log.info("job %s deferred (%s), attempt %d, retry after %s",
                     job_id, verdict.code, job.attempts, job.not_before)
            return
        if not verdict.ok:
            # Out of patience. Running and stuttering a frame beats a
            # transaction that is never categorised.
            log.warning("job %s has deferred %d times; running anyway (%s)",
                        job_id, job.attempts, verdict.code)

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
            _clear_stall(provider)
        except AIHostStalled as exc:
            # Powered on, took the request, never answered. Recorded so
            # the rest of the queue waits instead of repeating it. This
            # job still fails, which is what makes it the detector: its
            # own fallback (human review, for a categorisation) is the
            # right outcome either way.
            _note_stall(provider, settings)
            job.status = "failed"
            job.error = str(exc)
            log.warning("job %s stalled; holding %s for %ds: %s",
                        job_id, _host_key(provider),
                        settings.ai_stall_backoff_seconds, exc)
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
    """Next job due now: anything queued, or deferred past its wait.

    Deferred work is drained by the same loop rather than a separate
    scheduler -- the worker already polls, so a job whose `not_before`
    has passed is simply eligible again.
    """
    now = datetime.now(UTC)
    with SessionLocal() as db:
        row = db.scalars(
            select(AIJob)
            .where(
                or_(
                    AIJob.status == "queued",
                    and_(AIJob.status == "deferred",
                         AIJob.not_before.isnot(None),
                         AIJob.not_before <= now),
                )
            )
            # Oldest first, so a job that has already waited is not
            # overtaken by one that just arrived.
            .order_by(AIJob.created_at)
            .limit(1)
        ).first()
        if row is None:
            return None
        claimed = db.execute(
            update(AIJob)
            .where(AIJob.id == row.id, AIJob.status.in_(("queued", "deferred")))
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
