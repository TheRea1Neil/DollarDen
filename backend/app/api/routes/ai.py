"""AI endpoints: goals, provider settings, and the job queue."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.jobs import KINDS, enqueue, provider_for
from app.api.deps import current_user
from app.core.config import Settings, get_settings
from app.db.models import AIJob, User, UserGoal
from app.db.session import get_db

router = APIRouter(prefix="/ai", tags=["ai"])


# --------------------------------------------------------------------------- #
# Goals
# --------------------------------------------------------------------------- #
class GoalsIn(BaseModel):
    body: str = Field(max_length=4000)


class GoalsOut(BaseModel):
    body: str
    updated_at: str | None


@router.get("/goals", response_model=GoalsOut)
def get_goals(user: User = Depends(current_user), db: Session = Depends(get_db)) -> GoalsOut:
    row = db.scalars(select(UserGoal).where(UserGoal.user_id == user.id)).first()
    return GoalsOut(
        body=row.body if row else "",
        updated_at=row.updated_at.isoformat() if row else None,
    )


@router.put("/goals", response_model=GoalsOut)
def set_goals(
    body: GoalsIn, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> GoalsOut:
    """What the user is trying to achieve, in their own words.

    Fed verbatim into summaries and advice. The difference between generic
    advice and useful advice is almost entirely this context.
    """
    row = db.scalars(select(UserGoal).where(UserGoal.user_id == user.id)).first()
    if row is None:
        row = UserGoal(user_id=user.id, body=body.body)
        db.add(row)
    else:
        row.body = body.body
        row.updated_at = datetime.now(UTC)
    db.commit()
    return GoalsOut(body=row.body, updated_at=row.updated_at.isoformat())


# --------------------------------------------------------------------------- #
# Provider settings
# --------------------------------------------------------------------------- #
class AISettingsIn(BaseModel):
    provider: str | None = None  # deck | custom | none
    base_url: str | None = None

    @field_validator("provider")
    @classmethod
    def _known(cls, v: str | None) -> str | None:
        if v is not None and v not in {"deck", "custom", "none"}:
            raise ValueError("provider must be deck, custom or none")
        return v


class AISettingsOut(BaseModel):
    provider: str
    base_url: str | None
    is_default: bool
    reachable: bool | None = None


@router.get("/settings", response_model=AISettingsOut)
def get_ai_settings(
    user: User = Depends(current_user), s: Settings = Depends(get_settings)
) -> AISettingsOut:
    return AISettingsOut(
        provider=user.ai_provider or s.ai_provider,
        base_url=user.ai_base_url,
        is_default=user.ai_provider is None,
    )


@router.put("/settings", response_model=AISettingsOut)
async def set_ai_settings(
    body: AISettingsIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    s: Settings = Depends(get_settings),
) -> AISettingsOut:
    """Choose where inference runs.

    `custom` points at the user's own Ollama -- their laptop, say -- which
    is checked for reachability now rather than failing on first use.
    """
    if body.provider == "custom" and not (body.base_url or user.ai_base_url):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "A custom provider needs a base_url, e.g. http://192.168.1.50:11434",
        )

    if body.provider is not None:
        user.ai_provider = body.provider
    if body.base_url is not None:
        user.ai_base_url = body.base_url.strip().rstrip("/") or None
    db.commit()

    reachable = None
    if (user.ai_provider or s.ai_provider) == "custom" and user.ai_base_url:
        from app.ai.provider import OllamaProvider

        reachable = await OllamaProvider(user.ai_base_url, s).available()

    return AISettingsOut(
        provider=user.ai_provider or s.ai_provider,
        base_url=user.ai_base_url,
        is_default=user.ai_provider is None,
        reachable=reachable,
    )


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #
class JobIn(BaseModel):
    kind: str
    request: dict = Field(default_factory=dict)

    @field_validator("kind")
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in KINDS:
            raise ValueError(f"kind must be one of {sorted(KINDS)}")
        return v


class JobOut(BaseModel):
    id: str
    kind: str
    status: str  # queued | waking | running | deferred | done | failed
    result: dict | None
    error: str | None
    created_at: str
    completed_at: str | None
    # Set while a job is waiting for the GPU or for morning,
    # so the app can say WHY rather than showing a stalled spinner.
    defer_reason: str | None = None
    retry_at: str | None = None
    attempts: int = 0

    @property
    def finished(self) -> bool:
        return self.status in ("done", "failed")


def _job_out(j: AIJob) -> JobOut:
    return JobOut(
        id=str(j.id), kind=j.kind, status=j.status, result=j.result, error=j.error,
        defer_reason=j.defer_reason,
        retry_at=j.not_before.isoformat() if j.not_before else None,
        attempts=j.attempts,
        created_at=j.created_at.isoformat(),
        completed_at=j.completed_at.isoformat() if j.completed_at else None,
    )


@router.post("/jobs", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
def create_job(
    body: JobIn, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> JobOut:
    """Queue a job. Returns immediately: poll the id for the result.

    202 rather than 201 on purpose -- the work has been accepted, not done.
    The AI host may be asleep and take ~10s to wake.
    """
    job = enqueue(db, user.id, body.kind, body.request)
    db.commit()
    return _job_out(job)


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(
    job_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> JobOut:
    try:
        pk = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found") from None
    job = db.scalars(
        select(AIJob).where(AIJob.id == pk, AIJob.user_id == user.id)
    ).first()
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return _job_out(job)


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(
    limit: int = 20, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> list[JobOut]:
    rows = db.scalars(
        select(AIJob)
        .where(AIJob.user_id == user.id)
        .order_by(AIJob.created_at.desc())
        .limit(min(limit, 100))
    ).all()
    return [_job_out(j) for j in rows]


@router.get("/status")
async def ai_status(
    user: User = Depends(current_user), s: Settings = Depends(get_settings)
) -> dict[str, object]:
    """Is inference available right now?

    Deliberately does NOT wake a sleeping host: this is called to render a
    settings screen, and waking a PC to draw a status dot would be rude.
    """
    from app.ai import gpu

    provider = provider_for(user, s)
    name = type(provider).__name__
    available = await provider.available()

    # Probe health is surfaced so a probe that an OS update removed is
    # visible in the app, not only in the logs. The gate FAILS OPEN when
    # it cannot be reached, so a missing probe costs the gaming
    # protection and nothing else -- but the user should still be told.
    state = await gpu.probe(s)
    quiet = gpu.in_quiet_hours(s)
    probe_configured = bool(s.gpu_probe_url)
    return {
        "provider": user.ai_provider or s.ai_provider,
        "implementation": name,
        "available_now": available,
        "quiet_hours": quiet,
        "quiet_window": (f"{s.ai_quiet_start_hour:02d}:00-"
                         f"{s.ai_quiet_end_hour:02d}:00 {s.ai_quiet_timezone}"),
        "gpu": {
            **state.as_dict(),
            "configured": probe_configured,
            "needs_vram_mb": s.ai_min_vram_mb,
            # Only a real problem when a probe IS configured and the
            # host is otherwise up: unreachable-while-asleep is normal.
            "degraded": probe_configured and available and not state.reachable,
        },
        "note": (
            "The AI host sleeps when idle. 'available_now: false' usually "
            "means asleep, not broken — the first job wakes it, taking about "
            "ten seconds."
            if name == "DeckProvider" and not available
            else None
        ),
    }
