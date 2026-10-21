"""Registration, login, refresh, logout."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.core.config import get_settings
from app.core.security import (
    create_access_token,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    verify_password,
)
from app.db.models import Session as SessionRow
from app.db.models import User
from app.db.session import get_db

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=256)
    display_name: str | None = Field(default=None, max_length=120)
    timezone: str = "America/Denver"


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: str
    email: str
    display_name: str | None
    timezone: str


def _issue(db: Session, user: User, user_agent: str | None) -> TokenOut:
    s = get_settings()
    raw, hashed = new_refresh_token()
    db.add(
        SessionRow(
            user_id=user.id,
            refresh_token_hash=hashed,
            expires_at=datetime.now(UTC) + timedelta(days=s.refresh_token_ttl_days),
            user_agent=(user_agent or "")[:300] or None,
        )
    )
    db.commit()
    return TokenOut(access_token=create_access_token(str(user.id)), refresh_token=raw)


@router.post("/register", response_model=TokenOut, status_code=status.HTTP_201_CREATED)
def register(body: RegisterIn, request: Request, db: Session = Depends(get_db)) -> TokenOut:
    existing = db.scalars(select(User).where(User.email == body.email.lower())).first()
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with that email already exists")

    user = User(
        email=body.email.lower(),
        password_hash=hash_password(body.password),
        display_name=body.display_name,
        timezone=body.timezone,
    )
    db.add(user)
    db.flush()
    return _issue(db, user, request.headers.get("user-agent"))


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)) -> TokenOut:
    user = db.scalars(select(User).where(User.email == body.email.lower())).first()

    # Same error and comparable work either way, so the response does not
    # reveal whether the address is registered.
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")

    return _issue(db, user, request.headers.get("user-agent"))


class RefreshIn(BaseModel):
    refresh_token: str


@router.post("/refresh", response_model=TokenOut)
def refresh(body: RefreshIn, request: Request, db: Session = Depends(get_db)) -> TokenOut:
    hashed = hash_refresh_token(body.refresh_token)
    row = db.scalars(select(SessionRow).where(SessionRow.refresh_token_hash == hashed)).first()

    now = datetime.now(UTC)
    if row is None or row.revoked_at is not None or row.expires_at <= now:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired refresh token")

    # Rotate: a refresh token is single-use.
    row.revoked_at = now
    user = db.get(User, row.user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired refresh token")

    return _issue(db, user, request.headers.get("user-agent"))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(body: RefreshIn, db: Session = Depends(get_db)) -> None:
    hashed = hash_refresh_token(body.refresh_token)
    row = db.scalars(select(SessionRow).where(SessionRow.refresh_token_hash == hashed)).first()
    if row and row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        db.commit()


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user)) -> UserOut:
    return UserOut(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        timezone=user.timezone,
    )
