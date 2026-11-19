"""Plaid endpoints: linking, syncing, and the webhook receiver."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.core.config import Settings, get_settings
from app.core.security import decrypt_token, encrypt_token
from app.db.models import Account, PlaidItem, User
from app.db.session import SessionLocal, get_db
from app.plaid.client import PlaidClient, PlaidError
from app.plaid.sync import refresh_items, SyncIncomplete, refresh_accounts, sync_item
from app.plaid.webhook import WebhookVerificationError, verify

log = logging.getLogger("nnf.api.plaid")

router = APIRouter(prefix="/plaid", tags=["plaid"])


def _client(s: Settings = Depends(get_settings),
            user: User = Depends(current_user)) -> PlaidClient:
    """A Plaid client for the signed-in account's environment.

    Sandbox and real accounts live on the same instance, so the
    environment cannot come from the server's configuration -- it is a
    property of whose behalf we are acting on.
    """
    return PlaidClient.for_user(s, user)


def _client_for_env(s: Settings, environment: str) -> PlaidClient:
    """A client for a named environment, for callers with no user.

    The webhook is the one that matters: it arrives unauthenticated and
    its environment comes from the path it was delivered to.
    """
    return PlaidClient(s, environment=environment)


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
    platform: Literal["android", "ios"] | None = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    client: PlaidClient = Depends(_client),
) -> LinkTokenOut:
    """Mint a link_token for the Plaid Link SDK.

    With `item_id`, returns an UPDATE MODE token that re-authenticates that
    existing Item in place. That path does NOT consume a connection credit,
    and is the correct response to ITEM_LOGIN_REQUIRED. See docs/04.

    `platform` must be sent by the app: Android OAuth needs
    `android_package_name` and iOS needs `redirect_uri`, and passing the
    wrong one fails silently at the bank's login page rather than erroring.
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
        resp = await client.create_link_token(
            str(user.id), access_token=access_token, platform=platform
        )
    except PlaidError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Plaid: {exc.error_code}") from exc
    except RuntimeError as exc:
        # Misconfiguration, not a Plaid failure. 500 with the setting named
        # beats a 502 that sends the reader looking at Plaid's status page.
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc

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


class RefreshOut(BaseModel):
    synced: int
    skipped: int
    added: int
    nudged: int
    balances_as_of: str | None
    errors: list[str] = []


@router.post("/refresh", response_model=RefreshOut)
async def refresh_everything(
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    client: PlaidClient = Depends(_client),
    s: Settings = Depends(get_settings),
) -> RefreshOut:
    """Pull transactions AND balances for every connected bank, now.

    Pull-to-refresh used to re-read this server's database, which meant
    it could only ever show what the last webhook happened to deliver.
    When a webhook is missed -- rejected, dropped, or never sent -- the
    app goes quietly stale with no way for the user to correct it. A
    manual refresh must be able to reach the bank.

    Throttled per Item: a user can pull repeatedly, and
    /accounts/balance/get reaches the institution every time. Inside
    the window the cached figures are returned rather than an error,
    because the honest answer to "refresh twice in five seconds" is
    "nothing has changed".
    """
    items = list(
        db.scalars(select(PlaidItem).where(PlaidItem.user_id == user.id)).all())
    # Same code path as the background schedule, on purpose: see
    # plaid/sync.refresh_items.
    tally = await refresh_items(db, client, items, s)
    synced, skipped = tally.synced, tally.skipped
    added, nudged, errors = tally.added, tally.nudged, tally.errors

    as_of = db.scalar(
        select(func.max(Account.balance_as_of)).where(Account.user_id == user.id)
    )
    return RefreshOut(
        synced=synced, skipped=skipped, added=added, nudged=nudged,
        balances_as_of=as_of.isoformat() if as_of else None,
        errors=errors,
    )


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
        "categorised": stats.categorised,
        "status": item.status,
    }


# --------------------------------------------------------------------------- #
# Webhook
# --------------------------------------------------------------------------- #
SYNC_CODES = {"SYNC_UPDATES_AVAILABLE", "INITIAL_UPDATE", "HISTORICAL_UPDATE", "DEFAULT_UPDATE"}
REAUTH_CODES = {"ITEM_LOGIN_REQUIRED", "PENDING_EXPIRATION"}


@router.post("/rewebhook")
async def repoint_webhooks(
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    client: PlaidClient = Depends(_client),
    s: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Point every existing connection at this server's webhook URL.

    Needed because Plaid fixes an Item's webhook at link time. Move the
    server -- put a certificate in front of it, change the domain, move
    it to another box -- and every connection made before the move
    keeps calling the old address. Nothing errors; transactions just
    quietly stop arriving in real time for the oldest accounts, which
    is the hardest kind of failure to notice.

    The alternative is re-linking, and on the Trial plan that spends
    one of a small lifetime allowance which removing an Item does not
    return. So this exists to keep "add HTTPS later" free.
    """
    # The same URL a fresh link would register, environment path and
    # all -- otherwise re-pointing would send old Items somewhere new
    # Items never go.
    target = client.webhook_url()
    if not target:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "No webhook URL is configured for this server.",
        )

    items = db.scalars(select(PlaidItem).where(PlaidItem.user_id == user.id)).all()
    updated, failed = [], []
    for item in items:
        try:
            await client.update_item_webhook(
                decrypt_token(item.access_token_encrypted), target
            )
            updated.append(item.institution_name or str(item.id))
        except Exception as exc:  # noqa: BLE001
            # One bad Item must not stop the rest: a connection that
            # needs re-authenticating will fail here and is exactly the
            # one the user most needs told about.
            log.warning("could not repoint webhook for item %s: %s", item.id, exc)
            failed.append(item.institution_name or str(item.id))

    return {"webhook": target, "updated": updated, "failed": failed}


@router.post("/webhook/{environment}", include_in_schema=False)
@router.post("/webhook", include_in_schema=False)
async def webhook(
    request: Request,
    environment: str | None = None,
    s: Settings = Depends(get_settings),
) -> dict[str, str]:
    """Receive a Plaid webhook.

    Public endpoint. Every request is signature-verified before anything is
    read from it; unverified requests are rejected without side effects.

    **The environment comes from the path**, because verification needs
    it before the body can be trusted. Plaid's signing key is fetched
    from the environment's own host, so verifying a sandbox webhook
    against production keys fails -- and the only other ways to know
    which environment sent it are to read the unverified body, or to
    try both and double every key fetch. The webhook URL is registered
    per Item at link time, so the path is free and certain.

    The bare `/webhook` route remains for Items linked before this
    existed; it falls back to the instance's configured environment,
    which is exactly what those Items were created under.
    """
    env = (environment or s.plaid_env).strip().lower()
    if env not in ("sandbox", "production"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown webhook path")
    client = _client_for_env(s, env)

    body = await request.body()
    try:
        await verify(body, request.headers.get("plaid-verification"), client, env)
    except WebhookVerificationError as exc:
        # Record the rejection against the Item before refusing. A
        # webhook that silently stops being accepted looks identical to
        # one that was never sent, and container logs do not survive a
        # redeploy -- which is exactly when you go looking.
        log.warning("rejected unverified webhook: %s", exc)
        try:
            peek = json.loads(body or b"{}")
            stamp_id = peek.get("item_id")
        except Exception:  # noqa: BLE001
            stamp_id = None
        if stamp_id:
            with SessionLocal() as db:
                it = db.scalars(
                    select(PlaidItem).where(PlaidItem.plaid_item_id == stamp_id)
                ).first()
                if it is not None:
                    it.last_webhook_at = datetime.now(UTC)
                    it.last_webhook_code = f"rejected:{str(exc)[:48]}"
                    db.commit()
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

        item.last_webhook_at = datetime.now(UTC)
        item.last_webhook_code = str(wh_code or wh_type or "?")[:64]
        db.commit()

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
