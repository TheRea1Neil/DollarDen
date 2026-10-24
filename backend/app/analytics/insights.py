"""The cards on the insights screen.

Each one is a computed fact with its numbers already filled in. The
model's only job later is to read them out in plain English, which it
can do without arithmetic and therefore without inventing anything.

Choosing WHICH facts is the real design work here. The test applied to
each card was: does it tell you something you could not have guessed,
and could you act on it? That rules out most of what finance apps show.
A pie chart of a month you already lived through is decoration. These
are the ones that survived:

  flow          -- did more come in than went out
  by_type       -- where it actually went, against last period
  movers        -- what CHANGED, which is the part you did not know
  recurring     -- the charges you have forgotten you agreed to
  unusual       -- this month against this category's own history
  envelopes     -- funded versus spent, the one no generic app can do
  pace          -- how the month ends if nothing changes
  top_merchants -- a single place quietly taking a large share
  untyped       -- how much of the total is unlabelled, so every other
                   card can be trusted or discounted accordingly
"""
from __future__ import annotations

import statistics
import uuid
from datetime import date
from typing import Any

from sqlalchemy import Date, cast, func, select
from sqlalchemy.orm import Session

from app.analytics import queries as Q
from app.analytics import recurring as R
from app.analytics.period import Period, month_period
from app.db.models import Allocation, Category, Paycheck, Transaction

# A change smaller than this is noise, not a story.
MOVER_FLOOR_CENTS = 2_000
# How far above its own median a type must sit to be called unusual.
UNUSUAL_RATIO = 1.5


def _effective_date():
    """When an allocation happened, for date filtering.

    Spending carries its transaction's date. Funding an envelope has no
    transaction behind it, so it falls back to when the row was
    written -- which for a paycheck split is the moment it was applied.
    """
    return func.coalesce(Transaction.date, cast(Allocation.created_at, Date))


def _card(kind: str, title: str, **rest: Any) -> dict[str, Any]:
    return {"kind": kind, "title": title, **rest}


def _pct(now: int, before: int) -> float | None:
    if not before:
        return None
    return round((now - before) / before * 100, 1)


def build(
    db: Session, user_id: uuid.UUID, *, period: Period, today: date
) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    base = Q.SpendQuery(period=period)

    spend = Q.run(db, user_id, base)
    if spend.total_cents == 0 and spend.count == 0:
        return [
            _card(
                "empty",
                "Nothing to report yet",
                detail=(
                    "No categorised spending in "
                    f"{period.label}. Insights appear once transactions "
                    "are filed into categories."
                ),
            )
        ]

    cards.append(_flow(db, user_id, period, spend))
    cards.append(_by_type(db, user_id, period))
    cards.extend(_movers(db, user_id, period))
    cards.extend(_recurring(db, user_id, today))
    cards.extend(_unusual(db, user_id, period, today))
    cards.extend(_envelopes(db, user_id, period))
    cards.extend(_pace(db, user_id, period, today, spend))
    cards.extend(_top_merchants(db, user_id, period, spend))
    cards.extend(_untyped(db, user_id, period, spend))
    return cards


# --------------------------------------------------------------------------- #
def _flow(db: Session, user_id: uuid.UUID, period: Period, spend: Q.QueryResult):
    income = Q.run(db, user_id, Q.SpendQuery(period=period, metric="income"))
    net = income.total_cents - spend.total_cents
    return _card(
        "flow",
        f"In and out, {period.label}",
        in_cents=income.total_cents,
        out_cents=spend.total_cents,
        net_cents=net,
        previous_out_cents=spend.previous_total_cents,
        change_pct=_pct(spend.total_cents, spend.previous_total_cents or 0),
        txn_count=spend.count,
    )


def _by_type(db: Session, user_id: uuid.UUID, period: Period):
    res = Q.run(db, user_id, Q.SpendQuery(period=period, group_by="expense_type", limit=12))
    prev = Q.run(
        db,
        user_id,
        Q.SpendQuery(
            period=period.previous(), group_by="expense_type", limit=50,
            compare_to_previous=False,
        ),
    )
    before = {g.label: g.cents for g in prev.groups}
    return _card(
        "by_type",
        f"Where it went, {period.label}",
        total_cents=res.total_cents,
        groups=[
            {
                **g.as_dict(),
                "previous_cents": before.get(g.label, 0),
                "share_pct": round(g.cents / res.total_cents * 100, 1)
                if res.total_cents
                else 0.0,
            }
            for g in res.groups
        ],
    )


def _movers(db: Session, user_id: uuid.UUID, period: Period):
    """What changed, which is the part the user does not already know."""
    now = Q.run(db, user_id, Q.SpendQuery(period=period, group_by="expense_type", limit=50))
    was = Q.run(
        db,
        user_id,
        Q.SpendQuery(
            period=period.previous(), group_by="expense_type", limit=50,
            compare_to_previous=False,
        ),
    )
    a = {g.label: g.cents for g in now.groups}
    b = {g.label: g.cents for g in was.groups}

    deltas = [
        (label, a.get(label, 0) - b.get(label, 0))
        for label in set(a) | set(b)
    ]
    deltas = [d for d in deltas if abs(d[1]) >= MOVER_FLOOR_CENTS]
    if not deltas:
        return []
    deltas.sort(key=lambda d: -abs(d[1]))
    return [
        _card(
            "movers",
            f"Biggest changes from {period.previous().label}",
            items=[
                {
                    "label": label,
                    "delta_cents": delta,
                    "now_cents": a.get(label, 0),
                    "previous_cents": b.get(label, 0),
                }
                for label, delta in deltas[:5]
            ],
        )
    ]


def _recurring(db: Session, user_id: uuid.UUID, today: date):
    items = R.detect(db, user_id, today=today)
    live = [r for r in items if not r.likely_ended]
    if not live:
        return []
    return [
        _card(
            "recurring",
            "Repeating charges",
            monthly_cents=R.monthly_commitment_cents(items),
            annual_cents=sum(r.annual_cents for r in live),
            count=len(live),
            items=[r.as_dict() for r in items[:15]],
            ended=[r.as_dict() for r in items if r.likely_ended][:5],
        )
    ]


def _unusual(db: Session, user_id: uuid.UUID, period: Period, today: date):
    """A type measured against its own history, not against other types.

    "You spent $180 on fast food" means nothing without knowing that
    $70 is your usual. The comparison has to be personal or it is just
    a number with an opinion attached.
    """
    months: list[Period] = []
    p = month_period(today.year, today.month).previous()
    for _ in range(6):
        months.append(p)
        p = p.previous()

    history: dict[str, list[int]] = {}
    for m in months:
        res = Q.run(
            db, user_id,
            Q.SpendQuery(period=m, group_by="expense_type", limit=50, compare_to_previous=False),
        )
        for g in res.groups:
            history.setdefault(g.label, []).append(g.cents)

    current = Q.run(
        db, user_id,
        Q.SpendQuery(period=period, group_by="expense_type", limit=50, compare_to_previous=False),
    )

    flags = []
    for g in current.groups:
        past = history.get(g.label, [])
        # Three months of history before calling anything unusual: two
        # points make every month either a record high or a record low.
        if len(past) < 3:
            continue
        med = statistics.median(past)
        if med <= 0 or g.cents < MOVER_FLOOR_CENTS:
            continue
        if g.cents >= med * UNUSUAL_RATIO:
            flags.append(
                {
                    "label": g.label,
                    "cents": g.cents,
                    "typical_cents": int(med),
                    "ratio": round(g.cents / med, 2),
                    "months_of_history": len(past),
                }
            )
    if not flags:
        return []
    flags.sort(key=lambda f: -f["ratio"])
    return [_card("unusual", "Higher than usual", items=flags[:5])]


def _envelopes(db: Session, user_id: uuid.UUID, period: Period):
    """Funded against spent, per envelope.

    This is the card no generic finance app can produce, because it
    needs a budget that says in advance what each pot was meant to get.
    An envelope that is always overspent is a plan that is wrong, not a
    month that went badly.
    """
    rows = db.execute(
        select(
            Category.id,
            Category.name,
            Category.balance_cents,
            Category.target_cents,
            func.coalesce(
                func.sum(
                    func.greatest(Allocation.amount_cents, 0)
                ),
                0,
            ).label("funded"),
            func.coalesce(
                func.sum(
                    func.least(Allocation.amount_cents, 0)
                ),
                0,
            ).label("spent"),
        )
        .join(Allocation, Allocation.category_id == Category.id)
        .outerjoin(Transaction, Transaction.id == Allocation.transaction_id)
        .where(
            Category.user_id == user_id,
            Category.archived_at.is_(None),
            Category.is_unallocated.is_(False),
            _effective_date() >= period.start,
            _effective_date() <= period.end,
        )
        .group_by(Category.id, Category.name, Category.balance_cents, Category.target_cents)
    ).all()

    items = []
    for r in rows:
        funded, spent = int(r.funded), abs(int(r.spent))
        if funded == 0 and spent == 0:
            continue
        items.append(
            {
                "category_id": str(r.id),
                "name": r.name,
                "funded_cents": funded,
                "spent_cents": spent,
                "balance_cents": int(r.balance_cents),
                "target_cents": int(r.target_cents) if r.target_cents else None,
                "overspent": spent > funded and funded > 0,
                "used_pct": round(spent / funded * 100, 1) if funded else None,
            }
        )
    if not items:
        return []
    items.sort(key=lambda i: -(i["used_pct"] or 0))
    return [_card("envelopes", f"Funded against spent, {period.label}", items=items[:12])]


def _pace(db, user_id, period: Period, today: date, spend: Q.QueryResult):
    """Only meaningful mid-period: a finished month has no projection."""
    if not (period.start <= today <= period.end):
        return []
    elapsed = (today - period.start).days + 1
    if elapsed < 5 or elapsed >= period.days:
        return []
    projected = int(round(spend.total_cents / elapsed * period.days))
    return [
        _card(
            "pace",
            f"{elapsed} of {period.days} days in",
            spent_cents=spend.total_cents,
            projected_cents=projected,
            previous_total_cents=spend.previous_total_cents,
            days_elapsed=elapsed,
            days_total=period.days,
        )
    ]


def _top_merchants(db, user_id, period: Period, spend: Q.QueryResult):
    res = Q.run(
        db, user_id,
        Q.SpendQuery(period=period, group_by="merchant", limit=8, compare_to_previous=False),
    )
    if not res.groups:
        return []
    return [
        _card(
            "top_merchants",
            f"Most spent with, {period.label}",
            items=[
                {
                    **g.as_dict(),
                    "share_pct": round(g.cents / spend.total_cents * 100, 1)
                    if spend.total_cents
                    else 0.0,
                }
                for g in res.groups
            ],
        )
    ]


def _untyped(db, user_id, period: Period, spend: Q.QueryResult):
    """How much of the total has no expense type.

    Shown because every other card is only as good as this number is
    small. Hiding it would let the insights screen look authoritative
    while describing a third of the money.
    """
    res = Q.run(
        db, user_id,
        Q.SpendQuery(period=period, group_by="expense_type", limit=50, compare_to_previous=False),
    )
    untyped = next((g for g in res.groups if g.label == Q.UNTYPED_LABEL), None)
    if not untyped or not spend.total_cents:
        return []
    share = round(untyped.cents / spend.total_cents * 100, 1)
    if share < 5:
        return []
    return [
        _card(
            "untyped",
            "Not yet labelled",
            cents=untyped.cents,
            count=untyped.count,
            share_pct=share,
        )
    ]
