"""Shared FastAPI dependencies."""
from __future__ import annotations

import uuid

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.security import decode_access_token
from app.db.models import User
from app.db.session import get_db

_bearer = HTTPBearer(auto_error=False)

_UNAUTH = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    if creds is None or not creds.credentials:
        raise _UNAUTH
    try:
        payload = decode_access_token(creds.credentials)
    except jwt.PyJWTError:
        raise _UNAUTH from None

    if payload.get("typ") != "access":
        raise _UNAUTH

    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError):
        raise _UNAUTH from None

    user = db.get(User, user_id)
    if user is None:
        raise _UNAUTH
    return user
