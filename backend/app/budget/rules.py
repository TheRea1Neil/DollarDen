"""Deterministic transaction categorisation.

Runs BEFORE any model is consulted. Most spending is repeat merchants, so
rules are faster, free, and predictable -- the model only needs to see the
genuinely novel cases (docs/06).
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Allocation, CategoryRule, Transaction


@dataclass(frozen=True)
class Match:
    category_id: uuid.UUID
    reason: str


def _text_of(txn: Transaction) -> str:
    return f"{txn.merchant_name or ''} {txn.name or ''}".lower()


def matches(matcher: dict[str, Any], txn: Transaction) -> bool:
    """Evaluate one matcher against a transaction.

    Every present condition must hold (AND). An empty matcher matches
    nothing -- a rule that fires on everything is always a mistake.
    """
    if not matcher:
        return False

    text = _text_of(txn)

    if (needle := matcher.get("merchant_contains")) is not None:
        if str(needle).lower() not in text:
            return False

    if (pattern := matcher.get("merchant_regex")) is not None:
        try:
            if not re.search(str(pattern), text, re.IGNORECASE):
                return False
        except re.error:
            return False   # a malformed rule must not match everything

    if (want := matcher.get("pfc_detailed")) is not None:
        if (txn.pfc_detailed or "") != want:
            return False

    if (want := matcher.get("pfc_primary")) is not None:
        if (txn.pfc_primary or "") != want:
            return False

    if (acct := matcher.get("account_id")) is not None:
        if str(txn.account_id) != str(acct):
            return False

    if (bounds := matcher.get("amount_between")) is not None:
        lo, hi = int(bounds[0]), int(bounds[1])
        if not (lo <= txn.outflow_cents <= hi):
            return False

    if (direction := matcher.get("direction")) is not None:
        is_out = txn.outflow_cents > 0
        if direction == "outflow" and not is_out:
            return False
        if direction == "inflow" and is_out:
            return False

    return True


def first_match(db: Session, user_id: uuid.UUID, txn: Transaction) -> Match | None:
    """Lowest priority number wins; ties broken by creation order."""
    rules = db.scalars(
        select(CategoryRule)
        .where(CategoryRule.user_id == user_id, CategoryRule.enabled.is_(True))
        .order_by(CategoryRule.priority)
    ).all()

    for rule in rules:
        if matches(rule.matcher or {}, txn):
            return Match(rule.category_id, f"rule:{rule.id}")
    return None


def prior_decision(db: Session, user_id: uuid.UUID, txn: Transaction) -> Match | None:
    """Reuse what the user chose last time for the same merchant.

    Only single-category manual decisions are reused. A transaction the user
    deliberately split across categories says nothing reliable about how the
    next one should be split.
    """
    merchant = (txn.merchant_name or txn.name or "").strip()
    if not merchant:
        return None

    rows = db.execute(
        select(Allocation.transaction_id, Allocation.category_id)
        .join(Transaction, Transaction.id == Allocation.transaction_id)
        .where(
            Allocation.user_id == user_id,
            Allocation.source.in_(("manual", "auto_categorise")),
            Transaction.id != txn.id,
            Transaction.removed_at.is_(None),
            func_coalesce_merchant() == merchant,
        )
        .order_by(Allocation.created_at.desc())
        .limit(10)
    ).all()

    by_txn: dict[uuid.UUID, set[uuid.UUID]] = {}
    for txn_id, cat_id in rows:
        by_txn.setdefault(txn_id, set()).add(cat_id)

    for cats in by_txn.values():
        if len(cats) == 1:
            return Match(next(iter(cats)), "prior_decision")
    return None


def func_coalesce_merchant():
    from sqlalchemy import func

    return func.coalesce(Transaction.merchant_name, Transaction.name)
