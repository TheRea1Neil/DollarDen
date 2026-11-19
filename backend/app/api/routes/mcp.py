"""MCP endpoints, so an assistant can use this server directly.

Two of them, on purpose:

    POST /api/mcp/budget   any account — the budget and its data
    POST /api/mcp/admin    owners only — running the instance

Not one endpoint with role-gated tools, because the separation is the
point. Someone can hand a general-purpose assistant the budget
endpoint and nothing else, and know it cannot change the server's
Plaid credentials no matter what it decides to try. Least privilege is
worth a second route.

Authentication is the same bearer token the app uses, so there is no
second credential to manage or leak. A token that works in the app
works here and expires the same way.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.api.routes.admin import owner
from app.db.models import User
from app.db.session import get_db
from app.mcp import admin_tools, budget_tools
from app.mcp.protocol import PARSE_ERROR, handle

log = logging.getLogger("nnf.mcp")

router = APIRouter(prefix="/mcp", tags=["mcp"])

VERSION = "0.1.0"


async def _serve(request: Request, tools: dict[str, Any],
                 name: str) -> Response:
    """One JSON-RPC exchange.

    Notifications get 202 with an empty body, which is what the spec
    asks for and what stops a client waiting for a reply that is never
    coming.
    """
    try:
        message = await request.json()
    except Exception:  # noqa: BLE001
        # Built with the serialiser rather than by hand: the hand-written
        # version had one brace too many, and an error response that is
        # itself malformed is a miserable thing to debug.
        return Response(
            content=_dump({"jsonrpc": "2.0", "id": None,
                           "error": {"code": PARSE_ERROR,
                                     "message": "Body was not JSON"}}),
            media_type="application/json", status_code=400)

    if isinstance(message, list):
        # Batches are permitted by JSON-RPC. Answer each, drop the
        # notifications, and return nothing at all if they were all
        # notifications.
        out = [r for r in
               [await handle(m, tools, server_name=name, version=VERSION)
                for m in message] if r is not None]
        if not out:
            return Response(status_code=202)
        return Response(content=_dump(out), media_type="application/json")

    reply = await handle(message, tools, server_name=name, version=VERSION)
    if reply is None:
        return Response(status_code=202)
    return Response(content=_dump(reply), media_type="application/json")


def _dump(value: Any) -> str:
    import json

    return json.dumps(value, default=str)


@router.post("/budget", include_in_schema=False)
async def budget_mcp(request: Request, user: User = Depends(current_user),
                     db: Session = Depends(get_db)) -> Response:
    """Tools for this account's own budget."""
    return await _serve(request, budget_tools.build(db, user),
                        "no-nonsense-finance budget")


@router.post("/admin", include_in_schema=False)
async def admin_mcp(request: Request, user: User = Depends(owner),
                    db: Session = Depends(get_db)) -> Response:
    """Tools for running the instance. Owner only."""
    return await _serve(request, admin_tools.build(db, user),
                        "no-nonsense-finance admin")
