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
from app.budget.treatment import Treatment
from app.db.models import Allocation, Category, Paycheck, SplitPlan, SplitRule, Transaction

log = logging.getLogger("dollarden.budget.paycheck")

# A deposit smaller than this is not worth proposing as a paycheck.
MIN_PAYCHECK_CENTS = 100_00
DEFAULT_CADENCE_DAYS = 14

# A gap has to be plausible as a PAY interval before it can be evidence
# of one. Nothing legitimate pays faster than weekly, so a gap under
# this is an artifact -- a one-off deposit split as a paycheck, a
# correction, a second job's stub -- not a cadence. The old floor was
# `> 0`, which let a 2-day artifact sit in a 2-element median and move
# the answer by half.
MIN_CADENCE_GAP_DAYS = 5
MAX_CADENCE_GAP_DAYS = 45


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
    db: Session,
    user_id: uuid.UUID,
    paycheck: Paycheck,
    plan: SplitPlan,
    *,
    rules: list[EngineRule] | None = None,
    manual: list[tuple[uuid.UUID, int]] | None = None,
) -> ApplyResult:
    """Split a paycheck across categories and post the allocations.

    Re-applying an already-applied paycheck is refused rather than
    double-allocating: webhooks and manual retries can both land here.

    `rules` overrides the plan's own rules for THIS paycheck only. A
    one-off reorder is a normal thing to want -- a cheque that includes a
    refund should not be tithed on like ordinary income -- and it must
    not quietly rewrite the plan that runs every other payday. Making a
    change permanent is a separate, deliberate act on the category.

    `manual` allocations are taken off the top, before any rule runs, and
    the rules then divide what is LEFT. That ordering is the point: money
    you have already decided about should not also be counted as income
    by a percentage rule.
    """
    if paycheck.applied_at is not None:
        raise ValueError("this paycheck has already been applied")

    balances = {
        str(c.id): c.balance_cents
        for c in db.scalars(
            select(Category).where(Category.user_id == user_id, Category.archived_at.is_(None))
        ).all()
    }

    entries: list[Entry] = []
    manual_total = 0
    for cat_id, amount in (manual or []):
        if amount <= 0:
            continue
        manual_total += amount
        entries.append(Entry(cat_id, amount, "paycheck_manual", None,
                             "set aside before the rules ran"))
        key = str(cat_id)
        balances[key] = balances.get(key, 0) + amount

    if manual_total > paycheck.gross_cents:
        raise ValueError(
            f"manual allocations (${manual_total / 100:,.2f}) exceed the "
            f"paycheck (${paycheck.gross_cents / 100:,.2f})"
        )

    remaining_gross = paycheck.gross_cents - manual_total
    engine = rules if rules is not None else _engine_rules(db, plan)
    result = apply_split(remaining_gross, engine, balances)

    entries += [
        Entry(uuid.UUID(a.category_id), a.amount_cents, "paycheck_rule",
              None, f"{a.rule_type} from plan {plan.name} v{plan.version}"
                    + (" (adjusted for this paycheck)" if rules is not None else ""))
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
        allocated_cents=result.total_cents + manual_total,
        unfunded=result.unfunded,
        allocations=len(result.allocations),
    )


def unapply(db: Session, user_id: uuid.UUID, paycheck: Paycheck) -> int:
    """Reverse a paycheck by posting opposing entries. History is kept."""
    from app.budget.ledger import reverse

    allocs = list(
        db.scalars(select(Allocation).where(Allocation.paycheck_id == paycheck.id)).all()
    )
    # Both kinds: money the rules allocated AND money set aside by hand
    # during the review. Undoing a paycheck must leave nothing behind, or
    # the hand-placed part silently survives and the balances drift.
    live = [a for a in allocs
            if a.source in ("paycheck_rule", "paycheck_manual")]
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
                # Pending deposits ARE offered. A payroll credit sits
                # pending for a day or more, and hiding it meant the
                # one event the budget is built around was invisible
                # exactly when the user went looking for it. Splitting
                # one is still refused -- see record_paycheck -- but
                # "it is on its way" is worth saying.
                # Only deposits that still count toward the budget. Once a
                # deposit has been marked ignored -- dismissed as imported
                # history, say -- or identified as a transfer between the
                # user's own accounts, proposing it as a paycheck is just
                # asking the same question again after they answered it.
                # "PAYMENT FROM CHK 4835" is a transfer, not income.
                Transaction.treatment == Treatment.SPEND.value,
            )
            .order_by(Transaction.date.desc())
        ).all()
    )


def _income_deposit_dates(db: Session, user_id: uuid.UUID,
                          limit: int = 10) -> list[date]:
    """Dates of deposits that look like pay, newest first.

    Used when there are not enough SPLIT paychecks to measure a
    cadence. The deposits are better evidence anyway -- they are what
    the bank actually did, and they exist from the first sync, whereas
    a Paycheck row only appears once someone has sat down and divided
    one up.

    Treatment is deliberately ignored. Imported history gets dismissed
    as `ignored` so it stops cluttering the review queue, and that
    dismissal says nothing about whether the deposit happened.
    """
    rows = db.scalars(
        select(Transaction)
        .where(
            Transaction.user_id == user_id,
            Transaction.inflow_cents >= MIN_PAYCHECK_CENTS,
            Transaction.outflow_cents == 0,
            Transaction.pfc_primary == "INCOME",
            Transaction.removed_at.is_(None),
        )
        .order_by(Transaction.date.desc())
        .limit(limit)
    ).all()

    # One payday can appear twice: a pending row and its posted twin,
    # or two deposits on the same day. Either way the GAP is zero and
    # would drag the median down, so collapse to distinct dates.
    seen: list[date] = []
    for t in rows:
        if t.date not in seen:
            seen.append(t.date)
    return seen


def _cadence(dates: list[date]) -> tuple[int, int, int] | None:
    """Median gap, spread, and the NUMBER OF GAPS behind them.

    The gap count is returned because a median is only as resistant as
    the sample under it: with two gaps the median is their midpoint and
    one bad entry drags it halfway. Callers need to know that before
    preferring one source of dates over another.
    """
    gaps = [(dates[i] - dates[i + 1]).days for i in range(len(dates) - 1)]
    gaps = [g for g in gaps
            if MIN_CADENCE_GAP_DAYS <= g <= MAX_CADENCE_GAP_DAYS]
    if not gaps:
        return None
    median = int(statistics.median(gaps))
    spread = int(statistics.pstdev(gaps)) if len(gaps) > 1 else 1
    return median, max(1, min(spread, 7)), len(gaps)


def predict_next(db: Session, user_id: uuid.UUID) -> dict[str, object]:
    """Estimate the next payday from observed cadence.

    Returned as a RANGE. A single date implies precision this cannot have,
    and the alert it feeds ("Food is low and payday is ~6 days out") is only
    useful if the uncertainty is honest.

    Measures BOTH recorded paychecks and income deposits, then uses
    whichever rests on more gaps -- not whichever is more "confirmed".

    Preferring recorded paychecks on a count of two was wrong. Splitting
    a single one-off deposit as a paycheck is a normal thing to do, and
    it would land in a 2-element median as a 2-day gap: three paychecks
    (two real, one mobile cheque) read as a 7-day cadence while eight
    textbook-regular payroll credits sat unread in the same account. The
    deposits are the stronger signal for CADENCE anyway -- they are what
    the bank actually did -- which is why `_income_deposit_dates` says
    so. This acts on it.

    Paychecks still win a tie, so a user whose pay the bank mislabels
    can correct the prediction by splitting paychecks.
    """
    recorded = db.scalars(
        select(Paycheck)
        .where(Paycheck.user_id == user_id)
        .order_by(Paycheck.received_on.desc())
        .limit(8)
    ).all()

    # (dates, source-name, tie-break rank) -- paychecks outrank deposits.
    sources = [
        ([p.received_on for p in recorded], "paychecks", 1),
        (_income_deposit_dates(db, user_id), "deposits", 0),
    ]

    measured = [
        (cad, dates, name, rank)
        for dates, name, rank in sources
        if len(dates) >= 2 and (cad := _cadence(dates)) is not None
    ]

    if not measured:
        if all(len(dates) < 2 for dates, _, _ in sources):
            return {"known": False,
                    "reason": "need at least two paydays to see a pattern"}
        return {"known": False, "reason": "no usable cadence in recent paydays"}

    # Most gaps wins; paychecks break the tie.
    cad, dates, source, _ = max(measured, key=lambda m: (m[0][2], m[3]))
    median, spread, gaps_used = cad
    expected = dates[0] + timedelta(days=median)

    return {
        "known": True,
        "cadence_days": median,
        "earliest": (expected - timedelta(days=spread)).isoformat(),
        "expected": expected.isoformat(),
        "latest": (expected + timedelta(days=spread)).isoformat(),
        "based_on": len(dates),
        # How many intervals the median rests on. Two paydays give one
        # gap and no outlier resistance at all; the app can soften its
        # wording when this is small.
        "gaps_used": gaps_used,
        # Which evidence this came from, so the app can say "from your
        # deposits" rather than implying these were paychecks the user
        # confirmed and split.
        "source": source,
    }
