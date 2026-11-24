"""Running the instance: accounts, configuration, and whether it is well.

An API with a UI on top, not a UI with logic inside it. The portal in
`app/web/` calls exactly these endpoints and holds no rules of its
own, so that an assistant driving the same instance over MCP later can
do everything a person can without any of it being reimplemented.

Owner-only throughout, except `GET /admin/state`, which has to answer
before anyone can sign in.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.core.config import Settings, get_settings, refresh_settings
from app.core.instance_settings import (CHOICES, EDITABLE, is_secret,
                                        load_overrides, save, validate)
from app.core.security import hash_password
from app.db.models import AIJob, PlaidItem, User
from app.db.session import get_db

log = logging.getLogger("dollarden.admin")

router = APIRouter(prefix="/admin", tags=["admin"])


def owner(user: User = Depends(current_user)) -> User:
    """Only an owner may administer the instance."""
    if user.role != "owner":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only the owner of this server can change its settings.",
        )
    return user


# --------------------------------------------------------------------- #
# state: the one unauthenticated endpoint
# --------------------------------------------------------------------- #


class StateOut(BaseModel):
    configured: bool
    accepts_signups: bool
    version: str


@router.get("/state", response_model=StateOut)
def state(db: Session = Depends(get_db),
          s: Settings = Depends(get_settings)) -> StateOut:
    """Does this server need setting up?

    Unauthenticated by necessity: the setup wizard and the app both
    need an answer before any account exists to authenticate as.

    It says only whether there are accounts and whether sign-ups are
    open — no names, no counts, nothing about the budget. "Configured"
    is deliberately "are there any accounts at all", never "is there an
    owner": an instance upgraded from before roles has accounts and no
    owner, and must not be claimable by whoever reaches it first.
    """
    any_user = db.scalar(select(func.count()).select_from(User)) or 0
    return StateOut(
        configured=any_user > 0,
        accepts_signups=any_user == 0 or s.allow_open_registration,
        version=s.app_name,
    )


# --------------------------------------------------------------------- #
# accounts
# --------------------------------------------------------------------- #


class UserOut(BaseModel):
    id: str
    email: str
    display_name: str | None
    role: str
    timezone: str
    created_at: str
    plaid_env: str


class NewUser(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12)
    display_name: str | None = None
    timezone: str = "UTC"
    role: str = "member"
    # Sandbox unless asked otherwise. Guessing wrong this way costs
    # nothing; guessing the other way spends a bank connection that is
    # never given back.
    plaid_env: str = "sandbox"


def _user_out(u: User) -> UserOut:
    return UserOut(
        id=str(u.id), email=u.email, display_name=u.display_name,
        role=u.role, timezone=u.timezone,
        created_at=u.created_at.isoformat(),
        plaid_env=u.plaid_env,
    )


@router.get("/users", response_model=list[UserOut])
def list_users(_: User = Depends(owner),
               db: Session = Depends(get_db)) -> list[UserOut]:
    rows = db.scalars(select(User).order_by(User.created_at)).all()
    return [_user_out(u) for u in rows]


@router.post("/users", response_model=UserOut,
             status_code=status.HTTP_201_CREATED)
def create_user(body: NewUser, _: User = Depends(owner),
                db: Session = Depends(get_db)) -> UserOut:
    """Make an account for someone in the household.

    This is the only way in once an instance is set up, which is what
    lets public registration stay closed.
    """
    if body.role not in ("owner", "member"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Role must be owner or member.")
    if body.plaid_env not in ("sandbox", "production"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Environment must be sandbox or production.")
    if db.scalars(select(User).where(User.email == body.email.lower())).first():
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "An account with that email already exists.")
    u = User(
        email=str(body.email).lower(),
        password_hash=hash_password(body.password),
        display_name=body.display_name,
        timezone=body.timezone,
        role=body.role,
        plaid_env=body.plaid_env,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    log.info("owner created account %s (%s, %s)", u.email, u.role, u.plaid_env)
    return _user_out(u)


# --------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------- #


class SettingOut(BaseModel):
    key: str
    kind: str
    value: Any = None
    is_set: bool
    from_database: bool
    # Present when only certain values are valid, so the portal can
    # offer a menu rather than inviting a typo.
    choices: list[str] | None = None


@router.get("/settings", response_model=list[SettingOut])
def read_settings(_: User = Depends(owner),
                  db: Session = Depends(get_db)) -> list[SettingOut]:
    """Every editable setting, where it came from, and its value.

    Secrets are never returned, only whether one is set. There is no
    reason for a browser to hold a Plaid secret in order to replace it,
    and plenty of reasons it should not.
    """
    # Read inside the handler rather than through Depends: this is the
    # one endpoint that must reflect a change made moments ago, and a
    # dependency is resolved before the handler and can be served from
    # a cache this request itself just invalidated.
    live = get_settings().model_dump()
    stored = load_overrides(db)
    out: list[SettingOut] = []
    for key, kind in EDITABLE.items():
        value = live.get(key)
        secret = is_secret(key)
        out.append(SettingOut(
            key=key,
            kind=kind,
            value=None if secret else value,
            is_set=bool(value) if secret else value is not None,
            from_database=key in stored,
            choices=CHOICES.get(key),
        ))
    return out


class SettingsIn(BaseModel):
    values: dict[str, Any]


@router.put("/settings")
def write_settings(body: SettingsIn, who: User = Depends(owner),
                   db: Session = Depends(get_db)) -> dict[str, Any]:
    """Save settings, then prove they load.

    The write is rolled back if the result would not produce a valid
    configuration. Otherwise a mistyped number is accepted cheerfully,
    ignored by the loader, and the server carries on with old values
    while the portal shows the new ones.
    """
    unknown = sorted(set(body.values) - set(EDITABLE))
    if unknown:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Not settings that can be changed here: {', '.join(unknown)}",
        )

    try:
        checked = validate(body.values)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    changed = save(db, checked, by=who.email)
    try:
        candidate = load_overrides(db)
        Settings(**{**Settings().model_dump(), **candidate})
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Those settings would not load: {exc}",
        ) from exc

    db.commit()
    refresh_settings()
    log.info("settings changed by %s: %s", who.email, changed)
    return {"changed": changed}


# --------------------------------------------------------------------- #
# health
# --------------------------------------------------------------------- #


@router.get("/health")
async def instance_health(_: User = Depends(owner),
                          db: Session = Depends(get_db),
                          s: Settings = Depends(get_settings)) -> dict[str, Any]:
    """What an owner needs to know at a glance, and nothing else.

    Each figure answers a question someone actually asks when the app
    looks wrong: is the bank talking to us, is the AI reachable, is
    anything stuck.
    """
    now = datetime.now(UTC)

    last_hook = db.scalar(select(func.max(PlaidItem.last_webhook_at)))
    items = db.scalar(select(func.count()).select_from(PlaidItem)) or 0
    healthy = db.scalar(
        select(func.count()).select_from(PlaidItem)
        .where(PlaidItem.status == "healthy")) or 0

    jobs: dict[str, int] = {}
    for st, n in db.execute(
        select(AIJob.status, func.count()).group_by(AIJob.status)
    ).all():
        jobs[str(st)] = int(n)

    ai_ok: bool | None = None
    if s.ai_provider != "none":
        try:
            from app.ai.provider import build_provider

            ai_ok = await build_provider(s).available()
        except Exception:  # noqa: BLE001
            ai_ok = False

    return {
        "now": now.isoformat(),
        "plaid": {
            "environment": s.plaid_env,
            "connections": items,
            "healthy": healthy,
            "last_webhook_at": last_hook.isoformat() if last_hook else None,
            "webhook_url": s.plaid_webhook_url or None,
            "scheduled_pull_minutes": s.plaid_sync_interval_minutes
            if s.plaid_sync_enabled else None,
        },
        "ai": {
            "provider": s.ai_provider,
            "reachable": ai_ok,
            "model": s.ai_model_text,
        },
        "jobs": jobs,
        "accounts": db.scalar(select(func.count()).select_from(User)) or 0,
    }
