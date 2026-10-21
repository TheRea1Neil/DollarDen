"""Off-box redundancy for Plaid access tokens.

Why this exists
---------------
Losing an access_token means re-linking the bank, which consumes one of a
finite number of connection credits. The database on the Pi is a single point
of failure, so the token needs to exist somewhere else.

Why the phone only ever receives CIPHERTEXT
-------------------------------------------
The exported blob is the exact `access_token_encrypted` column value -- still
sealed with TOKEN_ENCRYPTION_KEY, which never leaves the server. The phone
stores an opaque byte string it cannot open, so a lost or compromised handset
does not leak bank access. Recovery needs both halves:

    phone blob  +  TOKEN_ENCRYPTION_KEY (password manager)  =  restore

That deliberately preserves the property documented in docs/07: the app never
holds a usable Plaid token.

Restoring
---------
POST /backup/tokens to a freshly built backend configured with the same
encryption key. Each blob is decrypted and then VERIFIED AGAINST PLAID before
anything is written, so a restore cannot quietly populate the database with
tokens that no longer work.
"""
from __future__ import annotations

import base64
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.core.config import Settings, get_settings
from app.core.security import decrypt_token, encrypt_token, key_fingerprint
from app.db.models import PlaidItem, User
from app.db.session import get_db
from app.plaid.client import PlaidClient, PlaidError
from app.plaid.sync import refresh_accounts

log = logging.getLogger("nnf.api.backup")

router = APIRouter(prefix="/backup", tags=["backup"])


class TokenBlob(BaseModel):
    """One encrypted token, safe to store on a device."""

    plaid_item_id: str
    institution_name: str | None = None
    # base64 of the Fernet ciphertext. Not decryptable without the server key.
    blob_b64: str
    key_fingerprint: str
    exported_at: str


class BackupOut(BaseModel):
    key_fingerprint: str
    count: int
    items: list[TokenBlob]
    note: str = (
        "These blobs are encrypted with a key held only by the server. Store "
        "them in the OS keychain. They are useless without TOKEN_ENCRYPTION_KEY, "
        "which must be backed up separately."
    )


@router.get("/tokens", response_model=BackupOut)
def export_tokens(
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> BackupOut:
    """Export this user's encrypted token blobs for on-device redundancy."""
    items = db.scalars(select(PlaidItem).where(PlaidItem.user_id == user.id)).all()
    now = datetime.now(UTC).isoformat()
    fp = key_fingerprint()

    blobs = [
        TokenBlob(
            plaid_item_id=i.plaid_item_id,
            institution_name=i.institution_name,
            blob_b64=base64.b64encode(i.access_token_encrypted).decode(),
            key_fingerprint=fp,
            exported_at=now,
        )
        for i in items
    ]
    log.info("exported %d encrypted token blobs for user %s", len(blobs), user.id)
    return BackupOut(key_fingerprint=fp, count=len(blobs), items=blobs)


class RestoreIn(BaseModel):
    items: list[TokenBlob] = Field(min_length=1, max_length=50)
    # Writing over a healthy Item is almost always a mistake, so it is opt-in.
    overwrite_existing: bool = False


class RestoreResult(BaseModel):
    plaid_item_id: str
    outcome: str      # restored | already_present | wrong_key | invalid_token | error
    detail: str | None = None


class RestoreOut(BaseModel):
    restored: int
    results: list[RestoreResult]


@router.post("/tokens", response_model=RestoreOut)
async def restore_tokens(
    body: RestoreIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    s: Settings = Depends(get_settings),
) -> RestoreOut:
    """Rebuild Items from encrypted blobs after a catastrophic data loss.

    Every blob is decrypted and checked against Plaid before being written,
    so a restore either produces working connections or reports exactly which
    ones failed and why.
    """
    current_fp = key_fingerprint()
    client = PlaidClient(s)
    results: list[RestoreResult] = []
    restored = 0

    for blob in body.items:
        if blob.key_fingerprint != current_fp:
            # Decryption would fail anyway; saying so plainly points at the
            # real problem (wrong key loaded) rather than "corrupt backup".
            results.append(
                RestoreResult(
                    plaid_item_id=blob.plaid_item_id,
                    outcome="wrong_key",
                    detail=(
                        f"blob was sealed with key {blob.key_fingerprint}, server "
                        f"holds {current_fp}. Load the original TOKEN_ENCRYPTION_KEY."
                    ),
                )
            )
            continue

        existing = db.scalars(
            select(PlaidItem).where(PlaidItem.plaid_item_id == blob.plaid_item_id)
        ).first()
        if existing is not None and not body.overwrite_existing:
            results.append(
                RestoreResult(
                    plaid_item_id=blob.plaid_item_id,
                    outcome="already_present",
                    detail="pass overwrite_existing=true to replace it",
                )
            )
            continue
        if existing is not None and existing.user_id != user.id:
            raise HTTPException(status.HTTP_409_CONFLICT, "Item belongs to another user")

        try:
            raw = base64.b64decode(blob.blob_b64)
            access_token = decrypt_token(raw)
        except Exception as exc:
            results.append(
                RestoreResult(plaid_item_id=blob.plaid_item_id, outcome="error",
                              detail=f"could not decrypt: {exc}")
            )
            continue

        # Prove the token still works before trusting it.
        try:
            meta = await client.get_item(access_token)
        except PlaidError as exc:
            results.append(
                RestoreResult(plaid_item_id=blob.plaid_item_id, outcome="invalid_token",
                              detail=f"Plaid rejected it: {exc.error_code}")
            )
            continue

        item = existing or PlaidItem(
            user_id=user.id,
            plaid_item_id=blob.plaid_item_id,
            access_token_encrypted=b"",
        )
        item.access_token_encrypted = encrypt_token(access_token)
        item.institution_id = (meta.get("item") or {}).get("institution_id")
        if blob.institution_name:
            item.institution_name = blob.institution_name
        item.status = "healthy"
        # Deliberately NOT restoring sync_cursor: a null cursor makes the next
        # sync re-pull full history, which is the safe direction. A stale
        # cursor would silently skip everything that happened in between.
        item.sync_cursor = None
        if existing is None:
            db.add(item)

        db.flush()

        # Deleting an Item cascades to its accounts, so a restore has to
        # repopulate them. Without this every synced transaction is skipped
        # as "unknown account" -- silently, and with the cursor advancing.
        try:
            n_accounts = await refresh_accounts(db, client, item, access_token)
        except Exception as exc:
            log.warning("restored %s but could not fetch accounts: %s", blob.plaid_item_id, exc)
            n_accounts = 0

        restored += 1
        results.append(
            RestoreResult(
                plaid_item_id=blob.plaid_item_id,
                outcome="restored",
                detail=f"{n_accounts} accounts refreshed",
            )
        )

    db.commit()
    log.info("restore: %d of %d blobs restored for user %s", restored, len(body.items), user.id)
    return RestoreOut(restored=restored, results=results)
