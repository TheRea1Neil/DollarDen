"""Tools for running the instance.

These call the admin API's own functions rather than reimplementing
them, which is the reason that API was built as an API with a UI on
top. Every rule — who may do what, which settings exist, what counts
as a valid value, what is refused — lives in one place and is enforced
identically whether a person clicks a button or an assistant calls a
tool.

Owner only. The transport refuses anyone else before reaching here.

One deliberate omission: there is no tool for creating accounts or
setting passwords. Both exist in the API, and both are things a
confused assistant should not be able to do on someone's behalf to a
server holding their bank connections. Helping someone through setup
does not require being able to mint credentials.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.db.models import User
from app.mcp.protocol import Tool

NO_ARGS: dict[str, Any] = {"type": "object", "properties": {}}


def build(db: Session, user: User) -> dict[str, Tool]:
    """The admin toolset, bound to an owner's session."""

    async def health() -> dict[str, Any]:
        from app.api.routes.admin import instance_health
        from app.core.config import get_settings

        return await instance_health(_=user, db=db, s=get_settings())

    def accounts() -> dict[str, Any]:
        from app.api.routes.admin import list_users

        rows = list_users(_=user, db=db)
        return {"accounts": [r.model_dump() for r in rows]}

    def settings_now() -> dict[str, Any]:
        from app.api.routes.admin import read_settings

        rows = read_settings(_=user, db=db)
        return {
            "settings": [r.model_dump() for r in rows],
            "note": "Secrets are never returned, only whether one is set.",
        }

    def change_settings(values: dict[str, Any]) -> dict[str, Any]:
        from app.api.routes.admin import SettingsIn, write_settings

        if not isinstance(values, dict) or not values:
            raise ValueError(
                "Pass a map of setting name to value, e.g. "
                '{"plaid_sync_interval_minutes": 30}. '
                "Call admin_settings first to see what exists.")
        try:
            out = write_settings(SettingsIn(values=values), who=user, db=db)
        except Exception as exc:  # noqa: BLE001
            # The API already refuses unknown keys, bootstrap secrets
            # and values that would not load; surface its reason.
            detail = getattr(exc, "detail", None) or str(exc)
            raise ValueError(str(detail)) from exc
        return {**out, "note": "Takes effect immediately; no restart."}

    def developer_docs(topic: str | None = None) -> dict[str, Any]:
        """The contracts someone's own hardware has to honour.

        Here so an assistant helping wire up a relay or a smart plug
        can read the exact expected shape instead of guessing, which
        is the difference between helping and inventing.
        """
        from app import devdocs as dd

        if not topic:
            return dd.index()
        doc = dd.get(topic)
        if doc is None:
            raise ValueError(
                f"No topic called {topic!r}. There is: "
                f"{', '.join(sorted(dd.TOPICS))}")
        return doc

    async def repoint_webhooks() -> dict[str, Any]:
        from app.api.routes.plaid import repoint_webhooks as repoint
        from app.core.config import get_settings
        from app.plaid.client import PlaidClient

        s = get_settings()
        return await repoint(user=user, db=db,
                             client=PlaidClient.for_user(s, user), s=s)

    return {t.name: t for t in [
        Tool("admin_health",
             "How this instance is doing: Plaid environment and connections, "
             "when the bank last reached it, whether the scheduled pull is "
             "on, whether the AI is reachable, and any stuck jobs.",
             NO_ARGS, health),
        Tool("admin_accounts",
             "The accounts on this server, with their roles and which Plaid "
             "environment each belongs to.",
             NO_ARGS, accounts),
        Tool("admin_settings",
             "Every setting that can be changed here, its current value, and "
             "whether it comes from the database or the environment.",
             NO_ARGS, settings_now),
        Tool("admin_change_settings",
             "Change one or more settings. Rejected if a value would not "
             "load, so a bad change leaves the server as it was. Call "
             "admin_settings first to see the names and valid values.",
             {"type": "object", "properties": {
                 "values": {"type": "object",
                            "description": 'map of name to value, e.g. '
                                           '{"plaid_sync_interval_minutes": 30}'},
             }, "required": ["values"]}, change_settings),
        Tool("admin_developer_docs",
             "How to build the optional pieces this server talks to: a "
             "service that wakes the AI machine, and one that reports the "
             "graphics card's state. Returns the exact endpoints, status "
             "codes, rules and a worked example. Call with no argument to "
             "list the topics.",
             {"type": "object", "properties": {
                 "topic": {"type": "string",
                           "enum": ["wake-contract", "gpu-probe"]},
             }}, developer_docs),
        Tool("admin_repoint_webhooks",
             "Point every existing bank connection at this server's current "
             "webhook address. Use after moving the server or adding HTTPS. "
             "Costs nothing — unlike re-linking, which spends a connection.",
             NO_ARGS, repoint_webhooks),
    ]}
