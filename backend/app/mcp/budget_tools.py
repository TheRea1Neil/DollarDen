"""Tools for someone's own budget.

Every one of these calls the same function the app calls. None of them
re-implements a rule, a filter or a sum — that was the point of
building the admin surface as an API with a UI on top rather than the
other way round, and it applies doubly here. A tool that computed its
own totals would drift from the screens, and the drift would show up
as an assistant confidently quoting a figure the app does not agree
with.

Read-only, deliberately, for now. Reading a budget is useful and
safe; moving money is neither reversible by the model nor obviously
wanted, and the append-only ledger means a careless write is
corrected rather than undone. Writes can be added once there is a
reason beyond "it would be possible".
"""
from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics import queries as Q
from app.analytics.period import PeriodError, resolve
from app.db.models import Category, ReviewItem, User
from app.mcp.protocol import Tool

NO_ARGS: dict[str, Any] = {"type": "object", "properties": {}}


def _categories(db: Session, user: User) -> list[Category]:
    return list(db.scalars(
        select(Category).where(Category.user_id == user.id,
                               Category.archived_at.is_(None))
        .order_by(Category.sort_order, Category.name)).all())


def _money(cents: int | None) -> str:
    return "—" if cents is None else f"${cents / 100:,.2f}"


def build(db: Session, user: User) -> dict[str, Tool]:
    """The budget toolset, bound to one account's data."""

    def envelopes() -> dict[str, Any]:
        cats = _categories(db, user)
        return {
            "envelopes": [
                {"name": c.name, "kind": c.kind,
                 "balance": _money(c.balance_cents),
                 "balance_cents": c.balance_cents,
                 "what_it_is_for": c.description}
                for c in cats if not c.is_unallocated
            ],
            "unallocated": next(
                (_money(c.balance_cents) for c in cats if c.is_unallocated), None),
            "note": "An envelope's balance is what is actually left to spend "
                    "on that thing. It is not a forecast or a limit.",
        }

    def spending(period: str = "this_month", group_by: str | None = None,
                 category: str | None = None, merchant: str | None = None,
                 metric: str = "spend") -> dict[str, Any]:
        try:
            resolved = resolve(period, today=date.today())
        except (PeriodError, ValueError) as exc:
            raise ValueError(
                f"{exc}. Try this_month, last_month, last_30_days, "
                f"last_3_months, this_year or all_time.") from exc

        ids: tuple = ()
        if category:
            by_name = {c.name.lower(): c for c in _categories(db, user)}
            match = by_name.get(category.lower())
            if match is None:
                raise ValueError(
                    f"No envelope called {category!r}. "
                    f"There is: {', '.join(sorted(by_name))}")
            ids = (match.id,)

        result = Q.run(db, user.id, Q.SpendQuery(
            period=resolved, metric=metric,  # type: ignore[arg-type]
            group_by=group_by,  # type: ignore[arg-type]
            category_ids=ids,
            merchant_contains=merchant or None,
        ))
        return {
            "period": period,
            "total": _money(result.total_cents),
            "total_cents": result.total_cents,
            "transactions": result.count,
            "previous_period_total": _money(result.previous_total_cents)
            if result.previous_total_cents is not None else None,
            "change_cents": result.delta_cents,
            "breakdown": [g.as_dict() for g in (result.groups or [])],
        }

    def transactions(period: str = "this_month", merchant: str | None = None,
                     limit: int = 25) -> dict[str, Any]:
        try:
            resolved = resolve(period, today=date.today())
        except (PeriodError, ValueError) as exc:
            raise ValueError(str(exc)) from exc
        rows = Q.transactions_for(db, user.id, Q.SpendQuery(
            period=resolved,
            merchant_contains=merchant or None,
            compare_to_previous=False), limit=min(int(limit), 100))
        return {"period": period, "transactions": rows}

    def payday() -> dict[str, Any]:
        # The same function the Ask assistant uses, so both agree.
        from app.ai.ask_agent import _run_payday

        return _run_payday(db, user, date.today(), _categories(db, user))

    def calculate(expression: str) -> dict[str, Any]:
        """Arithmetic, done exactly rather than recalled."""
        from app.ai.ask_agent import _run_calculate

        out = _run_calculate({"expression": expression})
        if "error" in out:
            raise ValueError(out["error"])
        return out

    def needs_attention() -> dict[str, Any]:
        items = db.scalars(
            select(ReviewItem).where(ReviewItem.user_id == user.id,
                                     ReviewItem.resolved_at.is_(None))).all()
        return {
            "waiting_for_you": len(items),
            "note": "Transactions the app could not file on its own. They "
                    "are visible in the app under Transactions → Review.",
        }

    return {t.name: t for t in [
        Tool("budget_envelopes",
             "The envelopes in this budget: what each holds now, what it is "
             "for, and how much money is still undivided. Use this for "
             "questions about the budget itself rather than about amounts "
             "spent.",
             NO_ARGS, envelopes),
        Tool("budget_spending",
             "Add up transactions over a period, optionally grouped or "
             "filtered. This does the arithmetic — never total transactions "
             "yourself.",
             {"type": "object", "properties": {
                 "period": {"type": "string",
                            "description": "this_month, last_month, "
                                           "last_30_days, last_3_months, "
                                           "this_year, all_time"},
                 "group_by": {"type": "string",
                              "enum": ["expense_type", "category", "merchant",
                                       "month", "week"]},
                 "category": {"type": "string", "description": "envelope name"},
                 "merchant": {"type": "string", "description": "part of a shop name"},
                 "metric": {"type": "string",
                            "enum": ["spend", "income", "net", "count", "average"]},
             }}, spending),
        Tool("budget_transactions",
             "The individual charges behind a figure, when a total alone "
             "does not answer the question.",
             {"type": "object", "properties": {
                 "period": {"type": "string"},
                 "merchant": {"type": "string"},
                 "limit": {"type": "integer"},
             }}, transactions),
        Tool("budget_payday",
             "Today's date, and when the next payday is expected with how "
             "many days away it is. Pair it with budget_calculate to work "
             "out how far money has to stretch.",
             NO_ARGS, payday),
        Tool("budget_calculate",
             "Work out a sum exactly: a balance spread over days, a rate, a "
             "difference. Give a plain expression such as '162.56 / 3'. Use "
             "this for every calculation rather than doing it yourself.",
             {"type": "object",
              "properties": {"expression": {"type": "string"}},
              "required": ["expression"]}, calculate),
        Tool("budget_needs_attention",
             "How many transactions are waiting for a human decision.",
             NO_ARGS, needs_attention),
    ]}
