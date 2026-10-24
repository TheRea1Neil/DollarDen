"""The deterministic half of analytics.

Every figure the app reports is produced here, in SQL, from the
allocation ledger. The model never adds anything up -- it chooses a
filter and later puts the result into a sentence. That is the same
division ADR 0006 drew around paycheck splitting, for the same reason:
a plausible wrong number is the most expensive thing this app can
produce, and summing is precisely what a language model is worst at.

WHAT COUNTS AS SPEND
--------------------
A negative allocation that is attached to a transaction.

Negative, because a category losing money is money going out. Attached
to a transaction, because moving $50 from Discretionary to Groceries
also writes a negative allocation and is not spending -- it is the same
dollars in a different envelope. Counting envelope-to-envelope moves
would inflate every total on the insights screen, and the user would
have no way to see why.

The invariant that rests on this: real spending always has a
Transaction row behind it. Cash spending entered by hand must create
one too, or it will be invisible here.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from sqlalchemy import Date, String, case, cast, func, select
from sqlalchemy.orm import Session

from app.analytics.period import Period
from app.db.models import Allocation, Category, ExpenseType, Paycheck, Transaction

Metric = Literal["spend", "income", "net", "count", "average"]
GroupBy = Literal["expense_type", "category", "merchant", "month", "week", "day"]

METRICS: tuple[str, ...] = ("spend", "income", "net", "count", "average")
GROUP_BYS: tuple[str, ...] = ("expense_type", "category", "merchant", "month", "week", "day")

UNTYPED_LABEL = "Untyped"


class QueryError(ValueError):
    """A query spec that cannot be executed as written."""


@dataclass(frozen=True)
class SpendQuery:
    period: Period
    metric: Metric = "spend"
    group_by: GroupBy | None = None
    expense_type_ids: tuple[uuid.UUID, ...] = ()
    category_ids: tuple[uuid.UUID, ...] = ()
    merchant_contains: str | None = None
    min_cents: int | None = None
    limit: int = 20
    compare_to_previous: bool = True


@dataclass(frozen=True)
class Group:
    label: str
    cents: int
    count: int
    key: str | None = None  # id or bucket, for drilling in

    def as_dict(self) -> dict[str, Any]:
        return {"label": self.label, "cents": self.cents, "count": self.count, "key": self.key}


@dataclass
class QueryResult:
    metric: str
    period: Period
    total_cents: int
    count: int
    groups: list[Group] = field(default_factory=list)
    previous_total_cents: int | None = None
    previous_period: Period | None = None
    filters: dict[str, Any] = field(default_factory=dict)

    @property
    def delta_cents(self) -> int | None:
        if self.previous_total_cents is None:
            return None
        return self.total_cents - self.previous_total_cents

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "period": self.period.as_dict(),
            "total_cents": self.total_cents,
            "count": self.count,
            "groups": [g.as_dict() for g in self.groups],
            "previous_total_cents": self.previous_total_cents,
            "previous_period": self.previous_period.as_dict() if self.previous_period else None,
            "delta_cents": self.delta_cents,
            "filters": self.filters,
        }


# --------------------------------------------------------------------------- #
# Building blocks
# --------------------------------------------------------------------------- #
def _merchant():
    return func.coalesce(Transaction.merchant_name, Transaction.name)


def _spend_select(user_id: uuid.UUID, q: SpendQuery):
    """Allocations that represent money leaving the budget, filtered."""
    stmt = (
        select(Allocation)
        .join(Transaction, Transaction.id == Allocation.transaction_id)
        .where(
            Allocation.user_id == user_id,
            Allocation.amount_cents < 0,
            Transaction.removed_at.is_(None),
            Transaction.date >= q.period.start,
            Transaction.date <= q.period.end,
        )
    )
    return _apply_filters(stmt, q)


def _inflow_select(user_id: uuid.UUID, q: SpendQuery):
    """Positive allocations tied to a transaction: refunds and deposits.

    Paycheck funding is excluded by the transaction join -- funding an
    envelope moves money that already arrived, so counting it as income
    would double-count every payday.
    """
    stmt = (
        select(Allocation)
        .join(Transaction, Transaction.id == Allocation.transaction_id)
        .where(
            Allocation.user_id == user_id,
            Allocation.amount_cents > 0,
            Transaction.removed_at.is_(None),
            Transaction.date >= q.period.start,
            Transaction.date <= q.period.end,
        )
    )
    return _apply_filters(stmt, q)


def _apply_filters(stmt, q: SpendQuery):
    if q.expense_type_ids:
        stmt = stmt.where(Allocation.expense_type_id.in_(q.expense_type_ids))
    if q.category_ids:
        stmt = stmt.where(Allocation.category_id.in_(q.category_ids))
    if q.merchant_contains:
        stmt = stmt.where(_merchant().ilike(f"%{q.merchant_contains}%"))
    if q.min_cents is not None:
        stmt = stmt.where(func.abs(Allocation.amount_cents) >= q.min_cents)
    return stmt


def _is_narrowed(q: "SpendQuery") -> bool:
    """Whether this query is restricted to part of the budget.

    Used to decide whether whole-budget income (a paycheck) belongs in
    the answer. It does not belong in a figure about one envelope, one
    kind of spending or one shop.
    """
    return bool(
        q.expense_type_ids or q.category_ids or q.merchant_contains or q.min_cents
    )


def _totals(db: Session, stmt) -> tuple[int, int]:
    """Sum of magnitudes, and how many distinct transactions produced it.

    Distinct transactions, not allocation rows: an itemised receipt split
    across four envelopes is one trip to the shop, and reporting it as
    four would make "14 visits" meaningless.
    """
    sub = stmt.subquery()
    row = db.execute(
        select(
            func.coalesce(func.sum(func.abs(sub.c.amount_cents)), 0),
            func.count(func.distinct(sub.c.transaction_id)),
        )
    ).one()
    return int(row[0]), int(row[1])


# --------------------------------------------------------------------------- #
# Grouping
# --------------------------------------------------------------------------- #
def _group_expr(group_by: GroupBy):
    """(label, key) SQL expressions for one grouping."""
    if group_by == "expense_type":
        return func.coalesce(ExpenseType.name, UNTYPED_LABEL), cast(ExpenseType.id, String)
    if group_by == "category":
        return Category.name, cast(Category.id, String)
    if group_by == "merchant":
        return _merchant(), _merchant()
    if group_by == "month":
        lbl = func.to_char(Transaction.date, "YYYY-MM")
        return lbl, lbl
    if group_by == "week":
        lbl = func.to_char(func.date_trunc("week", cast(Transaction.date, Date)), "YYYY-MM-DD")
        return lbl, lbl
    if group_by == "day":
        lbl = func.to_char(Transaction.date, "YYYY-MM-DD")
        return lbl, lbl
    raise QueryError(f"cannot group by {group_by!r}")


def _grouped(db: Session, user_id: uuid.UUID, q: SpendQuery, inflow: bool) -> list[Group]:
    base = _inflow_select(user_id, q) if inflow else _spend_select(user_id, q)
    label, key = _group_expr(q.group_by)  # type: ignore[arg-type]

    stmt = base.with_only_columns(
        label.label("label"),
        key.label("key"),
        func.sum(func.abs(Allocation.amount_cents)).label("cents"),
        func.count(func.distinct(Allocation.transaction_id)).label("n"),
    )
    if q.group_by == "expense_type":
        # Outer, so allocations with no type still appear as "Untyped"
        # rather than silently vanishing from a total the user can see
        # elsewhere.
        stmt = stmt.outerjoin(ExpenseType, ExpenseType.id == Allocation.expense_type_id)
    elif q.group_by == "category":
        stmt = stmt.join(Category, Category.id == Allocation.category_id)

    rows = db.execute(
        stmt.group_by(label, key).order_by(func.sum(func.abs(Allocation.amount_cents)).desc())
        .limit(q.limit)
    ).all()

    groups = [Group(str(r.label), int(r.cents), int(r.n), str(r.key) if r.key else None)
              for r in rows]
    if q.group_by in ("month", "week", "day"):
        # Time reads left to right, not biggest first.
        groups.sort(key=lambda g: g.label)
    return groups


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def run(db: Session, user_id: uuid.UUID, q: SpendQuery) -> QueryResult:
    if q.metric not in METRICS:
        raise QueryError(f"unknown metric: {q.metric!r}")
    if q.group_by is not None and q.group_by not in GROUP_BYS:
        raise QueryError(f"unknown grouping: {q.group_by!r}")

    spend_cents, spend_n = _totals(db, _spend_select(user_id, q))
    in_cents, in_n = (0, 0)
    if q.metric in ("income", "net"):
        in_cents, in_n = _totals(db, _inflow_select(user_id, q))
        # A paycheck is income even when none of it has been allocated
        # yet, so income comes from the paycheck table as well as from
        # positive allocations.
        #
        # This counts for `net` too. Adding it to `income` alone left
        # the two disagreeing on the same screen: on real data income
        # read +$5,509.02 while net read -$2,076.13, because net saw
        # only refunds. Caught by running this against the live ledger;
        # the test fixtures had no paychecks.
        #
        # Only when nothing narrows the query, though. A paycheck
        # belongs to no category and no kind of spending, so adding the
        # full amount to "net for Groceries" would invent income that
        # envelope never received.
        if not _is_narrowed(q):
            in_cents += _paycheck_total(db, user_id, q.period)

    if q.metric == "spend":
        total, n = spend_cents, spend_n
    elif q.metric == "income":
        total, n = in_cents, in_n
    elif q.metric == "net":
        total, n = in_cents - spend_cents, spend_n + in_n
    elif q.metric == "count":
        total, n = spend_n, spend_n
    else:  # average
        n = spend_n
        total = round(spend_cents / n) if n else 0

    res = QueryResult(
        metric=q.metric,
        period=q.period,
        total_cents=total,
        count=n,
        filters={
            "expense_type_ids": [str(i) for i in q.expense_type_ids],
            "category_ids": [str(i) for i in q.category_ids],
            "merchant_contains": q.merchant_contains,
            "min_cents": q.min_cents,
            "group_by": q.group_by,
        },
    )

    if q.group_by:
        res.groups = _grouped(db, user_id, q, inflow=(q.metric == "income"))

    if q.compare_to_previous:
        prev = q.period.previous()
        pq = SpendQuery(**{**q.__dict__, "period": prev, "compare_to_previous": False,
                           "group_by": None})
        res.previous_period = prev
        res.previous_total_cents = run(db, user_id, pq).total_cents

    return res


def _paycheck_total(db: Session, user_id: uuid.UUID, period: Period) -> int:
    return int(
        db.scalar(
            select(func.coalesce(func.sum(Paycheck.gross_cents), 0)).where(
                Paycheck.user_id == user_id,
                Paycheck.received_on >= period.start,
                Paycheck.received_on <= period.end,
            )
        )
        or 0
    )


# --------------------------------------------------------------------------- #
# Supporting listings
# --------------------------------------------------------------------------- #
def transactions_for(
    db: Session, user_id: uuid.UUID, q: SpendQuery, limit: int = 100
) -> list[dict[str, Any]]:
    """The rows behind a number, so a total can always be opened up.

    An insight the user cannot audit is a claim, not information.
    """
    stmt = (
        _spend_select(user_id, q)
        .with_only_columns(
            Transaction.id,
            Transaction.date,
            _merchant().label("merchant"),
            Allocation.amount_cents,
            Category.name.label("category"),
            func.coalesce(ExpenseType.name, UNTYPED_LABEL).label("expense_type"),
        )
        .join(Category, Category.id == Allocation.category_id)
        .outerjoin(ExpenseType, ExpenseType.id == Allocation.expense_type_id)
        .order_by(Transaction.date.desc(), func.abs(Allocation.amount_cents).desc())
        .limit(limit)
    )
    return [
        {
            "transaction_id": str(r.id),
            "date": r.date.isoformat(),
            "merchant": r.merchant,
            "amount_cents": abs(int(r.amount_cents)),
            "category": r.category,
            "expense_type": r.expense_type,
        }
        for r in db.execute(stmt).all()
    ]
