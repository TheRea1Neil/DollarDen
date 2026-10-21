"""Transaction sync via /transactions/sync.

Two things here are easy to get wrong and expensive to get wrong:

1. SIGN CONVENTION. Plaid returns a POSITIVE amount for money LEAVING the
   account and a negative amount for money arriving. That is the opposite of
   most people's intuition. We normalise once, here, into two non-negative
   columns (outflow_cents / inflow_cents) rather than one signed column, so a
   sign error cannot silently propagate into category balances.

2. PENDING -> POSTED. Plaid removes the pending transaction and adds a posted
   one with a DIFFERENT transaction_id. Treating that as two transactions
   double-counts the spend. We link them and move the allocation instead.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Account, PlaidItem, Transaction

log = logging.getLogger("nnf.plaid.sync")


def to_cents(amount: float | int | str | Decimal) -> int:
    """Convert a Plaid decimal amount to integer cents.

    Decimal, never float arithmetic: 19.99 * 100 == 1998.9999... in binary
    floating point, and int() of that is 1998. A cent lost per transaction
    compounds into wrong category balances.
    """
    return int((Decimal(str(amount)) * 100).quantize(Decimal("1")))


def split_amount(plaid_amount: float | int | str | Decimal) -> tuple[int, int]:
    """Plaid amount -> (outflow_cents, inflow_cents), both non-negative.

    Positive Plaid amount == money out.
    """
    cents = to_cents(plaid_amount)
    return (cents, 0) if cents >= 0 else (0, -cents)


@dataclass
class SyncStats:
    added: int = 0
    modified: int = 0
    removed: int = 0
    pending_resolved: int = 0
    skipped_unknown_account: int = 0
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"added={self.added} modified={self.modified} removed={self.removed} "
            f"pending_resolved={self.pending_resolved}"
        )


def _account_map(db: Session, item: PlaidItem) -> dict[str, Account]:
    rows = db.scalars(select(Account).where(Account.item_id == item.id)).all()
    return {a.plaid_account_id: a for a in rows}


def _upsert(db: Session, item: PlaidItem, accounts: dict[str, Account],
            raw: dict[str, Any], stats: SyncStats) -> Transaction | None:
    acct = accounts.get(raw["account_id"])
    if acct is None:
        # An account we have not stored yet (e.g. added at the bank since the
        # last /accounts/get). Skip rather than orphan the row; the next
        # account refresh picks it up.
        stats.skipped_unknown_account += 1
        return None

    out_c, in_c = split_amount(raw["amount"])
    txn = db.scalars(
        select(Transaction).where(Transaction.plaid_transaction_id == raw["transaction_id"])
    ).first()

    is_new = txn is None
    if txn is None:
        txn = Transaction(
            user_id=item.user_id,
            account_id=acct.id,
            plaid_transaction_id=raw["transaction_id"],
        )
        db.add(txn)

    txn.outflow_cents = out_c
    txn.inflow_cents = in_c
    txn.iso_currency_code = raw.get("iso_currency_code") or "USD"
    txn.date = datetime.strptime(raw["date"], "%Y-%m-%d").date()
    txn.name = raw.get("name") or ""
    txn.merchant_name = raw.get("merchant_name")
    txn.pending = bool(raw.get("pending"))
    pfc = raw.get("personal_finance_category") or {}
    txn.pfc_primary = pfc.get("primary")
    txn.pfc_detailed = pfc.get("detailed")
    txn.removed_at = None

    # Plaid tells us which pending transaction this posted one replaces.
    ref = raw.get("pending_transaction_id")
    if ref and not txn.pending:
        prior = db.scalars(
            select(Transaction).where(Transaction.plaid_transaction_id == ref)
        ).first()
        if prior is not None and prior.id != txn.id:
            _move_allocations(db, prior, txn)
            prior.pending_replaced_by = txn.plaid_transaction_id
            prior.removed_at = datetime.now(UTC)
            stats.pending_resolved += 1

    if is_new:
        stats.added += 1
    else:
        stats.modified += 1
    return txn


def _move_allocations(db: Session, old: Transaction, new: Transaction) -> None:
    """Re-point allocations from a pending transaction onto its posted twin.

    Moving rather than re-creating keeps the ledger append-only in spirit and
    avoids double-counting. If the amount changed (a tip added to a restaurant
    charge is the common case), a correction entry reconciles the difference.
    """
    from app.db.models import Allocation

    allocs = db.scalars(select(Allocation).where(Allocation.transaction_id == old.id)).all()
    if not allocs:
        return

    old_total = old.outflow_cents - old.inflow_cents
    new_total = new.outflow_cents - new.inflow_cents

    for a in allocs:
        a.transaction_id = new.id

    delta = new_total - old_total
    if delta != 0:
        # Attribute the change to the largest existing allocation.
        target = max(allocs, key=lambda a: abs(a.amount_cents))
        db.add(
            Allocation(
                user_id=new.user_id,
                category_id=target.category_id,
                transaction_id=new.id,
                amount_cents=-delta,
                source="correction",
                note=f"pending->posted amount changed by {delta} cents",
            )
        )
        log.info("pending->posted delta %s cents on %s", delta, new.plaid_transaction_id)


async def sync_item(db: Session, client: Any, item: PlaidItem,
                    access_token: str, max_pages: int = 50) -> SyncStats:
    """Drain /transactions/sync for one Item.

    The cursor is advanced in memory and committed exactly once, together with
    the data it describes. Committing the cursor before the rows would lose
    transactions permanently on a crash -- Plaid will not resend them.
    """
    stats = SyncStats()
    cursor = item.sync_cursor
    accounts = _account_map(db, item)
    pages = 0

    while pages < max_pages:
        pages += 1
        page = await client.transactions_sync(access_token, cursor)

        for raw in page.get("added", []):
            _upsert(db, item, accounts, raw, stats)
        for raw in page.get("modified", []):
            _upsert(db, item, accounts, raw, stats)

        for rem in page.get("removed", []):
            txn = db.scalars(
                select(Transaction).where(
                    Transaction.plaid_transaction_id == rem["transaction_id"]
                )
            ).first()
            if txn is not None and txn.removed_at is None:
                txn.removed_at = datetime.now(UTC)
                stats.removed += 1

        cursor = page.get("next_cursor") or cursor
        if not page.get("has_more"):
            break
    else:
        stats.errors.append(f"stopped after {max_pages} pages; more data remains")

    item.sync_cursor = cursor
    item.last_synced_at = datetime.now(UTC)
    item.status = "healthy"
    db.commit()          # rows and cursor land together

    log.info("sync %s: %s", item.plaid_item_id, stats)
    return stats
