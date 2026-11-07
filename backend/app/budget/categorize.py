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
from datetime import UTC, datetime

from app.budget.ledger import (
    Entry,
    allocations_for_transaction,
    post,
    reverse,
    transaction_is_fully_allocated,
)
from app.budget.paycheck import MIN_PAYCHECK_CENTS
from app.budget.treatment import Treatment
from app.db.models import ReviewItem, Transaction

log = logging.getLogger("nnf.budget.categorize")


# A card payment is two transactions for one event: money leaving
# checking and the same money arriving on the card. Plaid labels the
# card side reliably; the checking side often arrives as nothing more
# than "PAYMENT TO ACCT #9620 ON 11/05 VIA WEB".
#
# treatment.classify refuses to treat LOAN_PAYMENTS on a depository
# outflow as a card payment, and it is right to: mortgage, car and
# student loan payments share that category and ARE real expenses.
# Guessing from the description means either missing card payments or
# swallowing someone's mortgage.
#
# The pair is the reliable signal. An equal amount, close in time, on
# one of the user's own cards, already identified as a payment by
# Plaid -- that is not a coincidence, and it needs no string matching.
_PAIR_WINDOW_DAYS = 5


def _card_payment_twin(
    db: Session, user_id: uuid.UUID, txn: Transaction
) -> Transaction | None:
    """The other leg of a card payment, if this transaction is one leg."""
    from datetime import timedelta

    from app.db.models import Account

    acct = db.get(Account, txn.account_id)
    if acct is None:
        return None
    on_credit = (acct.type or "").lower() == "credit"
    lo = txn.date - timedelta(days=_PAIR_WINDOW_DAYS)
    hi = txn.date + timedelta(days=_PAIR_WINDOW_DAYS)

    if not on_credit and txn.outflow_cents > 0:
        # Money left checking: look for it arriving on a card.
        want, column = txn.outflow_cents, Transaction.inflow_cents
        side = Account.type == "credit"
    elif on_credit and txn.inflow_cents > 0:
        # Money arrived on a card: look for it leaving checking.
        want, column = txn.inflow_cents, Transaction.outflow_cents
        side = Account.type != "credit"
    else:
        return None

    rows = db.scalars(
        select(Transaction)
        .join(Account, Account.id == Transaction.account_id)
        .where(
            Transaction.user_id == user_id,
            Transaction.id != txn.id,
            Transaction.removed_at.is_(None),
            Transaction.date >= lo,
            Transaction.date <= hi,
            column == want,
            side,
        )
    ).all()

    for other in rows:
        # Only pair with something Plaid or the user already agrees is a
        # card payment. Without this an ordinary refund that happens to
        # match a transfer amount would silently leave the budget.
        if other.treatment == Treatment.CARD_PAYMENT.value:
            return other
        if (other.pfc_primary or "").upper() == "LOAN_PAYMENTS":
            return other
    return None


def _settle_card_payment(db: Session, user_id: uuid.UUID, txn: Transaction) -> bool:
    """Mark both legs of a card payment, in whichever order they arrive.

    The two sides can appear in either order, and days apart, so the
    leg arriving second has to go back and correct the first.
    """
    twin = _card_payment_twin(db, user_id, txn)
    if twin is None:
        return False

    txn.treatment = Treatment.CARD_PAYMENT.value

    if twin.treatment != Treatment.CARD_PAYMENT.value:
        twin.treatment = Treatment.CARD_PAYMENT.value
        existing = allocations_for_transaction(db, twin)
        if existing:
            reverse(db, user_id, existing, "settles a credit card, not spending")
        open_review = db.scalars(
            select(ReviewItem).where(
                ReviewItem.transaction_id == twin.id,
                ReviewItem.resolved_at.is_(None),
            )
        ).first()
        if open_review is not None:
            open_review.resolved_at = datetime.now(UTC)
            open_review.resolution = {"excluded_as": Treatment.CARD_PAYMENT.value}
    db.flush()
    return True


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

    Outcomes: rule | prior_decision | review | already_allocated |
              not_budgeted | skipped
    """
    if txn.removed_at is not None:
        return "skipped"

    # Pairing runs FIRST, before the early return below. The card leg
    # of a payment usually arrives already marked card_payment, and
    # returning on that would skip the correction its twin needs --
    # which is the whole "second leg arrives later" case. Harmless on
    # anything that is not half of a payment: it finds no twin and
    # does nothing.
    if _settle_card_payment(db, user_id, txn):
        return "not_budgeted"

    # Credit card payments and internal transfers move money without
    # spending it. The purchases they settle were already allocated, so
    # counting them again would double-count every card purchase. They are
    # excluded from both allocation and the review queue -- but remain
    # visible under /budget/excluded so nothing is silently hidden.
    if not Treatment(txn.treatment).affects_budget:
        return "not_budgeted"

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

    # A deposit large enough to be income belongs to the paycheck flow,
    # which splits it across envelopes by rule. Putting it in the
    # spending queue invites filing a paycheck into a single category,
    # and it crowds out the charges that queue is actually for.
    if txn.inflow_cents >= MIN_PAYCHECK_CENTS and txn.outflow_cents == 0:
        return "income"

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

    # Refuse if anything has already been allocated against this
    # charge. The AI categoriser runs concurrently and can win the race
    # by milliseconds, and this path always writes the FULL amount --
    # so adding to an existing allocation overshoots rather than
    # completing it. Both entries look perfectly ordinary in history,
    # which is why it goes unnoticed.
    #
    # Net, not "any row": a charge whose allocations were reversed nets
    # to zero and is genuinely unfiled again.
    net = sum(a.amount_cents for a in allocations_for_transaction(db, txn))
    if net != 0:
        raise ValueError(
            "That charge has already been filed — it may have been "
            "categorised automatically while this screen was open. "
            "Refresh to see where it went."
        )

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
