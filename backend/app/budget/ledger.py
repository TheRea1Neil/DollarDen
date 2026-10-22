"""The allocation ledger.

A category's balance is the SUM OF ITS ALLOCATIONS, never an independently
maintained number. `categories.balance_cents` is a cache of that sum and is
only ever written here, so it cannot silently drift away from the entries
that are supposed to explain it.

Allocations are append-only. A mistake is corrected with a new, opposing
entry, not by editing or deleting history -- the same reason double-entry
bookkeeping works.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Allocation, Category, Transaction

log = logging.getLogger("nnf.budget.ledger")


class LedgerError(ValueError):
    """An operation that would corrupt the ledger."""


@dataclass(frozen=True)
class Entry:
    """One proposed movement into (+) or out of (-) a category."""

    category_id: uuid.UUID
    amount_cents: int
    source: str
    confidence: float | None = None
    note: str | None = None


VALID_SOURCES = {
    "paycheck_rule",
    # Set aside by hand during a paycheck review, before any rule ran.
    # Distinct from "manual" so the history can say which it was: this
    # one is tied to a paycheck and is reversed by unapplying it.
    "paycheck_manual",
    "auto_categorise",
    "manual",
    "receipt_split",
    "correction",
    "opening_balance",
}


def post(
    db: Session,
    user_id: uuid.UUID,
    entries: list[Entry],
    *,
    transaction_id: uuid.UUID | None = None,
    paycheck_id: uuid.UUID | None = None,
) -> list[Allocation]:
    """Write allocations and refresh the affected category balances.

    Validates before writing: an invalid batch leaves the ledger untouched
    rather than half-applied.
    """
    if not entries:
        return []

    for e in entries:
        if e.amount_cents == 0:
            raise LedgerError("a zero-amount allocation carries no information")
        if e.source not in VALID_SOURCES:
            raise LedgerError(f"unknown allocation source: {e.source}")

    wanted = {e.category_id for e in entries}
    categories = {
        c.id: c
        for c in db.scalars(
            select(Category).where(Category.id.in_(wanted), Category.user_id == user_id)
        ).all()
    }

    missing = wanted - set(categories)
    if missing:
        # Covers both "does not exist" and "belongs to another user": the
        # query is user-scoped, so a cross-user id is simply absent.
        raise LedgerError(f"unknown category for this user: {sorted(map(str, missing))}")

    archived = [str(c.id) for c in categories.values() if c.archived_at is not None]
    if archived:
        raise LedgerError(f"cannot allocate to an archived category: {archived}")

    created: list[Allocation] = []
    for e in entries:
        created.append(
            Allocation(
                user_id=user_id,
                category_id=e.category_id,
                transaction_id=transaction_id,
                paycheck_id=paycheck_id,
                amount_cents=e.amount_cents,
                source=e.source,
                confidence=e.confidence,
                note=e.note,
            )
        )
    db.add_all(created)
    db.flush()

    for cid in wanted:
        recompute_balance(db, user_id, cid)

    return created


def recompute_balance(db: Session, user_id: uuid.UUID, category_id: uuid.UUID) -> int:
    """Recalculate one category's cached balance from its allocations."""
    total = db.scalar(
        select(func.coalesce(func.sum(Allocation.amount_cents), 0)).where(
            Allocation.category_id == category_id, Allocation.user_id == user_id
        )
    )
    total = int(total or 0)
    cat = db.get(Category, category_id)
    if cat is not None:
        cat.balance_cents = total
    db.flush()
    return total


def reverse(
    db: Session, user_id: uuid.UUID, allocations: list[Allocation], note: str
) -> list[Allocation]:
    """Undo allocations by posting their opposites.

    History is preserved: the originals stay, and the correction explains
    itself. Deleting them would make past balances unexplainable.
    """
    entries = [
        Entry(
            category_id=a.category_id,
            amount_cents=-a.amount_cents,
            source="correction",
            note=note,
        )
        for a in allocations
    ]
    return post(
        db,
        user_id,
        entries,
        transaction_id=allocations[0].transaction_id if allocations else None,
        paycheck_id=allocations[0].paycheck_id if allocations else None,
    )


def allocations_for_transaction(db: Session, txn: Transaction) -> list[Allocation]:
    return list(
        db.scalars(select(Allocation).where(Allocation.transaction_id == txn.id)).all()
    )


def transaction_is_fully_allocated(db: Session, txn: Transaction) -> bool:
    """Does the ledger account for the whole transaction?

    Sign convention, which is easy to get backwards:

        signed_amount(txn) = outflow - inflow     (positive == spending)
        allocation posted  = -signed_amount       (spending DECREASES a category)

    so a fully-allocated transaction has allocations summing to the NEGATIVE
    of its signed amount. Comparing against the positive value makes this
    always return False, which re-categorises every transaction on every
    pass and double-counts the spend.
    """
    allocated = db.scalar(
        select(func.coalesce(func.sum(Allocation.amount_cents), 0)).where(
            Allocation.transaction_id == txn.id
        )
    )
    return int(allocated or 0) == -(txn.outflow_cents - txn.inflow_cents)


def audit_balances(db: Session, user_id: uuid.UUID) -> list[tuple[uuid.UUID, int, int]]:
    """Find categories whose cached balance disagrees with their ledger.

    Should always return empty. Exposed so a scheduled job can assert the
    invariant in production rather than only in tests.
    """
    rows = db.execute(
        select(
            Category.id,
            Category.balance_cents,
            func.coalesce(func.sum(Allocation.amount_cents), 0),
        )
        .outerjoin(Allocation, Allocation.category_id == Category.id)
        .where(Category.user_id == user_id)
        .group_by(Category.id, Category.balance_cents)
    ).all()
    return [(r[0], r[1], int(r[2])) for r in rows if r[1] != int(r[2])]
