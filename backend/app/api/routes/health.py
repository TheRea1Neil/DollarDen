"""Health and readiness endpoints. No authentication, no sensitive data."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.session import get_db

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
def ready(db: Session = Depends(get_db), s: Settings = Depends(get_settings)) -> dict[str, object]:
    """Readiness: can we actually reach the database?"""
    try:
        db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False

    return {
        "status": "ok" if db_ok else "degraded",
        "database": db_ok,
        "plaid_env": s.plaid_env,
        "ai_provider": s.ai_provider,
    }
