"""Reconciling the budget against what is actually in the bank.

The invariant
-------------
    allocated  ==  cash  -  card_debt

Why it holds continuously:

    start            cash 1000, debt   0, allocated 1000   (1000 - 0 = 1000)
    $42 on the card  cash 1000, debt  42, allocated  958   (1000 - 42 = 958)
    pay the card     cash  958, debt   0, allocated  958   ( 958 - 0 = 958)

A card purchase draws its category down before the cash leaves, so the two
sides diverge by exactly the card balance and converge again when the card
is paid. Anything left over after that is money with no purpose assigned,
and belongs in the unallocated sink.

This is the budgeting equivalent of reconciling a bank statement: if the
numbers disagree by anything other than card debt, something is wrong and
the user should be told rather than shown a comfortable-looking total.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.budget.ledger import Entry, post
from app.db.models import Account, Category


@dataclass(frozen=True)
class Reconciliation:
    cash_cents: int
    card_debt_cents: int
    allocated_cents: int

    @property
    def available_cents(self) -> int:
        """Money that is actually yours: cash minus what you owe on cards."""
        return self.cash_cents - self.card_debt_cents

    @property
    def unallocated_cents(self) -> int:
        """Available money not yet given a purpose. Should normally be 0."""
        return self.available_cents - self.allocated_cents

    @property
    def balanced(self) -> bool:
        return self.unallocated_cents == 0

    def as_dict(self) -> dict[str, object]:
        return {
            "cash_cents": self.cash_cents,
            "card_debt_cents": self.card_debt_cents,
            "available_cents": self.available_cents,
            "allocated_cents": self.allocated_cents,
            "unallocated_cents": self.unallocated_cents,
            "balanced": self.balanced,
            "explanation": (
                "Allocated should equal cash minus card debt. Card purchases "
                "draw their category down before the cash leaves, so the two "
                "sides differ by the card balance until it is paid."
            ),
        }


def compute(db: Session, user_id: uuid.UUID) -> Reconciliation:
    cash = db.scalar(
        select(func.coalesce(func.sum(Account.current_cents), 0)).where(
            Account.user_id == user_id,
            Account.type == "depository",
            Account.is_tracked.is_(True),
        )
    )
    debt = db.scalar(
        select(func.coalesce(func.sum(Account.current_cents), 0)).where(
            Account.user_id == user_id,
            Account.type == "credit",
            Account.is_tracked.is_(True),
        )
    )
    allocated = db.scalar(
        select(func.coalesce(func.sum(Category.balance_cents), 0)).where(
            Category.user_id == user_id, Category.archived_at.is_(None)
        )
    )
    return Reconciliation(int(cash or 0), int(debt or 0), int(allocated or 0))


def unallocated_category(db: Session, user_id: uuid.UUID) -> Category | None:
    return db.scalars(
        select(Category).where(
            Category.user_id == user_id,
            Category.is_unallocated.is_(True),
            Category.archived_at.is_(None),
        )
    ).first()


def ensure_unallocated(db: Session, user_id: uuid.UUID) -> Category:
    """Get the sink, creating it if the user has none."""
    existing = unallocated_category(db, user_id)
    if existing is not None:
        return existing

    cat = Category(
        user_id=user_id,
        name="Unallocated",
        kind="savings_bucket",
        description="Money that has not been given a purpose yet.",
        is_unallocated=True,
        sort_order=9999,  # keep it last
    )
    db.add(cat)
    db.flush()
    return cat


def sweep_to_unallocated(db: Session, user_id: uuid.UUID) -> int:
    """Move any unallocated remainder into the sink, so the budget balances.

    Returns the amount moved. Works in both directions: if the budget is
    over-allocated -- more assigned than actually exists, which happens when
    spending outruns income -- this posts a negative entry, leaving the sink
    negative rather than hiding the shortfall.
    """
    rec = compute(db, user_id)
    delta = rec.unallocated_cents
    if delta == 0:
        return 0

    sink = ensure_unallocated(db, user_id)
    post(
        db,
        user_id,
        [Entry(sink.id, delta, "opening_balance", None, "reconciled to account balances")],
    )
    return delta
