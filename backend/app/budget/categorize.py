"""Draw-down pipeline: spending out of the right category.

Order is deliberate (docs/06):

    1. deterministic rules      free, instant, predictable
    2. a prior user decision    they already told us once
    3. the AI provider          only the genuinely novel cases
    4. the review queue         anything still uncertain

The model never writes to the ledger. It proposes; validated deterministic
code disposes.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.budget import rules
from app.budget.ledger import Entry, post, transaction_is_fully_allocated
from app.db.models import ReviewItem, Transaction

log = logging.getLogger("nnf.budget.categorize")


def signed_amount(txn: Transaction) -> int:
    """Outflow positive, inflow negative. Spending draws a category down."""
    return txn.outflow_cents - txn.inflow_cents


def queue_for_review(
    db: Session,
    user_id: uuid.UUID,
    txn: Transaction,
    reason: str,
    suggestions: list[dict] | None = None,
) -> ReviewItem:
    """Park a transaction for a human decision.

    Idempotent: a transaction already awaiting review is not queued twice,
    since sync and webhooks can both reach this path.
    """
    existing = db.scalars(
        select(ReviewItem).where(
            ReviewItem.transaction_id == txn.id, ReviewItem.resolved_at.is_(None)
        )
    ).first()
    if existing is not None:
        if suggestions:
            existing.suggestions = {"options": suggestions}
        return existing

    item = ReviewItem(
        user_id=user_id,
        transaction_id=txn.id,
        reason=reason,
        suggestions={"options": suggestions} if suggestions else None,
    )
    db.add(item)
    db.flush()
    return item


def categorise(db: Session, user_id: uuid.UUID, txn: Transaction) -> str:
    """Categorise one transaction. Returns what happened.

    Outcomes: rule | prior_decision | review | already_allocated | skipped
    """
    if txn.removed_at is not None:
        return "skipped"

    if transaction_is_fully_allocated(db, txn):
        return "already_allocated"

    amount = signed_amount(txn)
    if amount == 0:
        return "skipped"

    match = rules.first_match(db, user_id, txn)
    if match is not None:
        post(
            db, user_id,
            [Entry(match.category_id, -amount, "auto_categorise", 1.0, match.reason)],
            transaction_id=txn.id,
        )
        return "rule"

    match = rules.prior_decision(db, user_id, txn)
    if match is not None:
        post(
            db, user_id,
            [Entry(match.category_id, -amount, "auto_categorise", 0.9, match.reason)],
            transaction_id=txn.id,
        )
        return "prior_decision"

    # No AI here yet (phase 5). Until then, uncertainty goes to the human,
    # which is exactly what NullProvider does anyway (ADR 0003).
    queue_for_review(db, user_id, txn, "unknown_merchant")
    return "review"


def categorise_batch(db: Session, user_id: uuid.UUID, txns: list[Transaction]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for txn in txns:
        outcome = categorise(db, user_id, txn)
        counts[outcome] = counts.get(outcome, 0) + 1
    db.flush()
    log.info("categorised %d transactions: %s", len(txns), counts)
    return counts


def resolve_review(
    db: Session,
    user_id: uuid.UUID,
    review: ReviewItem,
    splits: list[tuple[uuid.UUID, int]],
) -> None:
    """Apply a human decision, possibly splitting across categories.

    `splits` is [(category_id, amount_cents)] and must account for the whole
    transaction -- a partial split would leave the ledger disagreeing with
    the bank.
    """
    from datetime import UTC, datetime

    txn = db.get(Transaction, review.transaction_id)
    if txn is None:
        raise ValueError("review item references a missing transaction")

    amount = signed_amount(txn)
    total = sum(a for _, a in splits)
    if total != amount:
        raise ValueError(
            f"splits total {total} cents but the transaction is {amount}; "
            "every cent must be accounted for"
        )

    post(
        db, user_id,
        [Entry(cid, -amt, "manual", None, "resolved from review") for cid, amt in splits],
        transaction_id=txn.id,
    )
    review.resolved_at = datetime.now(UTC)
    review.resolution = {"splits": [[str(c), a] for c, a in splits]}
    db.flush()
