"""Plaid endpoints: linking, syncing, and the webhook receiver."""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.core.config import Settings, get_settings
from app.core.security import decrypt_token, encrypt_token
from app.db.models import Account, PlaidItem, User
from app.db.session import SessionLocal, get_db
from app.plaid.client import PlaidClient, PlaidError
from app.plaid.sync import SyncIncomplete, refresh_accounts, sync_item
from app.plaid.webhook import WebhookVerificationError, verify

log = logging.getLogger("nnf.api.plaid")

router = APIRouter(prefix="/plaid", tags=["plaid"])


def _client(s: Settings = Depends(get_settings)) -> PlaidClient:
    return PlaidClient(s)


# --------------------------------------------------------------------------- #
# Linking
# --------------------------------------------------------------------------- #
class LinkTokenOut(BaseModel):
    link_token: str
    expiration: str | None = None
    update_mode: bool = False


@router.post("/link-token", response_model=LinkTokenOut)
async def create_link_token(
    item_id: str | None = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    client: PlaidClient = Depends(_client),
) -> LinkTokenOut:
    """Mint a link_token for the Plaid Link SDK.

    With `item_id`, returns an UPDATE MODE token that re-authenticates that
    existing Item in place. That path does NOT consume a connection credit,
    and is the correct response to ITEM_LOGIN_REQUIRED. See docs/04.
    """
    access_token = None
    if item_id:
        item = db.scalars(
            select(PlaidItem).where(
                PlaidItem.id == item_id, PlaidItem.user_id == user.id
            )
        ).first()
        if item is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")
        access_token = decrypt_token(item.access_token_encrypted)

    try:
        resp = await client.create_link_token(str(user.id), access_token=access_token)
    except PlaidError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Plaid: {exc.error_code}") from exc

    return LinkTokenOut(
        link_token=resp["link_token"],
        expiration=resp.get("expiration"),
        update_mode=bool(access_token),
    )


class ExchangeIn(BaseModel):
    public_token: str


class ItemOut(BaseModel):
    id: str
    institution_name: str | None
    status: str
    accounts: int


@router.post("/exchange", response_model=ItemOut, status_code=status.HTTP_201_CREATED)
async def exchange(
    body: ExchangeIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    client: PlaidClient = Depends(_client),
) -> ItemOut:
    """Exchange a public_token for a long-lived access_token and persist it.

    The access_token never leaves the backend and is encrypted at rest.
    """
    try:
        resp = await client.exchange_public_token(body.public_token)
    except PlaidError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Plaid: {exc.error_code}") from exc

    access_token: str = resp["access_token"]
    plaid_item_id: str = resp["item_id"]

    existing = db.scalars(
        select(PlaidItem).where(PlaidItem.plaid_item_id == plaid_item_id)
    ).first()
    if existing is not None:
        if existing.user_id != user.id:
            raise HTTPException(status.HTTP_409_CONFLICT, "Item belongs to another user")
        # Re-link of a known Item: refresh the token, keep the cursor.
        existing.access_token_encrypted = encrypt_token(access_token)
        existing.status = "healthy"
        item = existing
    else:
        item = PlaidItem(
            user_id=user.id,
            plaid_item_id=plaid_item_id,
            access_token_encrypted=encrypt_token(access_token),
            status="healthy",
        )
        db.add(item)
    db.flush()

    # Institution name, best effort - never fail the link over cosmetics.
    try:
        meta = await client.get_item(access_token)
        inst_id = (meta.get("item") or {}).get("institution_id")
        if inst_id:
            item.institution_id = inst_id
            inst = await client.get_institution(inst_id)
            item.institution_name = (inst.get("institution") or {}).get("name")
    except Exception as exc:
        log.warning("could not resolve institution: %s", exc)

    count = await refresh_accounts(db, client, item, access_token)
    db.commit()

    return ItemOut(
        id=str(item.id),
        institution_name=item.institution_name,
        status=item.status,
        accounts=count,
    )




# --------------------------------------------------------------------------- #
# Items and sync
# --------------------------------------------------------------------------- #
@router.get("/items", response_model=list[ItemOut])
def list_items(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[ItemOut]:
    items = db.scalars(select(PlaidItem).where(PlaidItem.user_id == user.id)).all()
    out = []
    for i in items:
        n = len(db.scalars(select(Account).where(Account.item_id == i.id)).all())
        out.append(
            ItemOut(id=str(i.id), institution_name=i.institution_name, status=i.status, accounts=n)
        )
    return out


@router.post("/items/{item_id}/sync")
async def sync_now(
    item_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    client: PlaidClient = Depends(_client),
) -> dict[str, Any]:
    item = db.scalars(
        select(PlaidItem).where(PlaidItem.id == item_id, PlaidItem.user_id == user.id)
    ).first()
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")

    access_token = decrypt_token(item.access_token_encrypted)
    try:
        stats = await sync_item(db, client, item, access_token)
    except SyncIncomplete as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except PlaidError as exc:
        if exc.needs_reauth:
            item.status = "login_required"
            db.commit()
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "This connection needs re-authentication. Request an update-mode "
                "link token from /plaid/link-token?item_id=... - this does not "
                "consume a connection credit.",
            ) from exc
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Plaid: {exc.error_code}") from exc

    return {
        "added": stats.added,
        "modified": stats.modified,
        "removed": stats.removed,
        "pending_resolved": stats.pending_resolved,
        "status": item.status,
    }


# --------------------------------------------------------------------------- #
# Webhook
# --------------------------------------------------------------------------- #
SYNC_CODES = {"SYNC_UPDATES_AVAILABLE", "INITIAL_UPDATE", "HISTORICAL_UPDATE", "DEFAULT_UPDATE"}
REAUTH_CODES = {"ITEM_LOGIN_REQUIRED", "PENDING_EXPIRATION"}


@router.post("/webhook", include_in_schema=False)
async def webhook(
    request: Request,
    s: Settings = Depends(get_settings),
    client: PlaidClient = Depends(_client),
) -> dict[str, str]:
    """Receive a Plaid webhook.

    Public endpoint. Every request is signature-verified before anything is
    read from it; unverified requests are rejected without side effects.
    """
    body = await request.body()
    try:
        await verify(body, request.headers.get("plaid-verification"), client)
    except WebhookVerificationError as exc:
        log.warning("rejected unverified webhook: %s", exc)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid webhook signature") from exc

    payload = await request.json()
    wh_type = payload.get("webhook_type")
    wh_code = payload.get("webhook_code")
    plaid_item_id = payload.get("item_id")
    log.info("webhook %s/%s for item %s", wh_type, wh_code, plaid_item_id)

    if not plaid_item_id:
        return {"status": "ignored"}

    # Webhooks are at-least-once, so handlers must be idempotent. The sync
    # loop is: re-running it simply finds nothing new.
    with SessionLocal() as db:
        item = db.scalars(
            select(PlaidItem).where(PlaidItem.plaid_item_id == plaid_item_id)
        ).first()
        if item is None:
            log.warning("webhook for unknown item %s", plaid_item_id)
            return {"status": "unknown_item"}

        if wh_code in REAUTH_CODES:
            item.status = "login_required" if wh_code == "ITEM_LOGIN_REQUIRED" else "pending_expiration"
            db.commit()
            return {"status": "flagged_for_reauth"}

        if wh_code in SYNC_CODES:
            access_token = decrypt_token(item.access_token_encrypted)
            try:
                stats = await sync_item(db, client, item, access_token)
            except PlaidError as exc:
                if exc.needs_reauth:
                    item.status = "login_required"
                    db.commit()
                    return {"status": "needs_reauth"}
                log.error("webhook sync failed: %s", exc.error_code)
                return {"status": "sync_failed"}
            return {"status": "synced", "detail": str(stats)}

    return {"status": "ignored"}
