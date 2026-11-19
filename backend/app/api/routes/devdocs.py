"""Serve the developer docs this server keeps about itself.

Unauthenticated. There is nothing install-specific in them — the same
text on every server — and an assistant helping somebody wire up a
relay needs to read the contract before there is an account to
authenticate as.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status

from app import devdocs

router = APIRouter(prefix="/docs", tags=["docs"])


@router.get("")
def list_topics() -> dict[str, Any]:
    """What this server can tell you about building things that talk to it."""
    return devdocs.index()


@router.get("/{topic}")
def read_topic(topic: str) -> dict[str, Any]:
    doc = devdocs.get(topic)
    if doc is None:
        known = ", ".join(sorted(devdocs.TOPICS))
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"No such topic. There is: {known}")
    return doc
