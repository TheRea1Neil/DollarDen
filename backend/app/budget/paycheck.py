"""Paycheck detection and application.

Wires the pure split engine (app/budget/split.py) to real transactions and
the ledger.
"""
from __future__ import annotations

import logging
import statistics
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.budget.ledger import Entry, post
from app.budget.split import RuleType
from app.budget.split import SplitRule as EngineRule
from app.budget.split import apply_split
from app.db.models import Allocation, Category, Paycheck, SplitPlan, SplitRule, Transaction

log = logging.getLogger("nnf.budget.paycheck")

# A deposit smaller than this is not worth proposing as a paycheck.
MIN_PAYCHECK_CENTS = 100_00
DEFAULT_CADENCE_DAYS = 14


@dataclass
class ApplyResult:
    paycheck_id: uuid.UUID
    allocated_cents: int
    unfunded: list[tuple[str, int]]
    allocations: int


def _engine_rules(db: Session, plan: SplitPlan) -> list[EngineRule]:
    rows = db.scalars(
        select(SplitRule).where(SplitRule.plan_id == plan.id).order_by(SplitRule.order_index)
    ).all()
    return [
        EngineRule(
            category_id=str(r.category_id),
            order_index=r.order_index,
            rule_type=RuleType(r.rule_type),
            params=r.params or {},
            enabled=r.enabled,
        )
        for r in rows
    ]


def apply_plan(
    db: Session, user_id: uuid.UUID, paycheck: Paycheck, plan: SplitPlan
) -> ApplyResult:
    """Split a paycheck across categories and post the allocations.

    Re-applying an already-applied paycheck is refused rather than
    double-allocating: webhooks and manual retries can both land here.
    """
    if paycheck.applied_at is not None:
        raise ValueError("this paycheck has already been applied")

    balances = {
        str(c.id): c.balance_cents
        for c in db.scalars(
            select(Category).where(Category.user_id == user_id, Category.archived_at.is_(None))
        ).all()
    }

    result = apply_split(paycheck.gross_cents, _engine_rules(db, plan), balances)

    entries = [
        Entry(uuid.UUID(a.category_id), a.amount_cents, "paycheck_rule",
              None, f"{a.rule_type} from plan {plan.name} v{plan.version}")
        for a in result.allocations
    ]
    post(db, user_id, entries, paycheck_id=paycheck.id)

    paycheck.split_plan_id = plan.id
    paycheck.applied_at = datetime.now(UTC)
    db.flush()

    if result.unfunded:
        # Surfaced to the caller so the user can be told which rules went
        # short. Silently underfunding is the failure mode to avoid.
        log.info("paycheck %s left %d rules unfunded", paycheck.id, len(result.unfunded))

    return ApplyResult(
        paycheck_id=paycheck.id,
        allocated_cents=result.total_cents,
        unfunded=result.unfunded,
        allocations=len(result.allocations),
    )


def unapply(db: Session, user_id: uuid.UUID, paycheck: Paycheck) -> int:
    """Reverse a paycheck by posting opposing entries. History is kept."""
    from app.budget.ledger import reverse

    allocs = list(
        db.scalars(select(Allocation).where(Allocation.paycheck_id == paycheck.id)).all()
    )
    live = [a for a in allocs if a.source == "paycheck_rule"]
    if not live:
        return 0
    reverse(db, user_id, live, f"undo paycheck {paycheck.id}")
    paycheck.applied_at = None
    db.flush()
    return len(live)


def candidates(db: Session, user_id: uuid.UUID, lookback_days: int = 60) -> list[Transaction]:
    """Deposits that might be paychecks.

    Proposals only. An unrecognised large deposit -- a tax refund, say -- must
    never be split automatically as though it were income.
    """
    since = date.today() - timedelta(days=lookback_days)
    return list(
        db.scalars(
            select(Transaction)
            .where(
                Transaction.user_id == user_id,
                Transaction.inflow_cents >= MIN_PAYCHECK_CENTS,
                Transaction.date >= since,
                Transaction.removed_at.is_(None),
                Transaction.pending.is_(False),
            )
            .order_by(Transaction.date.desc())
        ).all()
    )


def predict_next(db: Session, user_id: uuid.UUID) -> dict[str, object]:
    """Estimate the next payday from observed cadence.

    Returned as a RANGE. A single date implies precision this cannot have,
    and the alert it feeds ("Food is low and payday is ~6 days out") is only
    useful if the uncertainty is honest.
    """
    rows = db.scalars(
        select(Paycheck)
        .where(Paycheck.user_id == user_id)
        .order_by(Paycheck.received_on.desc())
        .limit(8)
    ).all()

    if len(rows) < 2:
        return {"known": False, "reason": "need at least two confirmed paychecks"}

    dates = [p.received_on for p in rows]
    gaps = [(dates[i] - dates[i + 1]).days for i in range(len(dates) - 1)]
    gaps = [g for g in gaps if 0 < g <= 45]
    if not gaps:
        return {"known": False, "reason": "no usable cadence in recent paychecks"}

    median = int(statistics.median(gaps))
    spread = int(statistics.pstdev(gaps)) if len(gaps) > 1 else 1
    spread = max(1, min(spread, 7))
    expected = dates[0] + timedelta(days=median)

    return {
        "known": True,
        "cadence_days": median,
        "earliest": (expected - timedelta(days=spread)).isoformat(),
        "expected": expected.isoformat(),
        "latest": (expected + timedelta(days=spread)).isoformat(),
        "based_on": len(gaps) + 1,
    }
