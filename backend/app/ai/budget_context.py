"""Everything the assistant needs to talk ABOUT the budget.

Distinct from app/analytics/queries.py, which answers "how much did I
spend on fast food". That is a data question and must be computed in
SQL (ADR 0009). This is for the other kind -- "what am I missing?",
"should Gas really be a refill?", "is $150 enough for Discretionary?"
-- where there is no figure to look up and the model needs the shape
of the budget rather than a total.

The two must not be confused. A question with a numeric answer handed
to this path gets a plausible invented number; a question about design
handed to the SQL path gets a filter and no advice.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.budget.reconcile import compute as compute_reconciliation
from app.db.models import Account, Category, CategoryRule, SplitPlan, SplitRule, User


def _money(cents: int | None) -> str:
    return "—" if cents is None else f"${cents / 100:,.2f}"


def describe_rule(rule_type: str | None, params: dict[str, Any] | None) -> str:
    """One funding rule, in the words a person would use.

    Written out rather than dumped as JSON: the model is being asked to
    reason about whether a rule is sensible, and `{"rate": "0.10"}` is
    harder to reason about than "10% of each paycheck, taken first".
    """
    p = params or {}
    match rule_type:
        case "percent":
            rate = p.get("rate", "?")
            try:
                pct = f"{float(rate) * 100:g}%"
            except (TypeError, ValueError):
                pct = str(rate)
            return f"{pct} of each paycheck"
        case "fixed":
            return f"{_money(p.get('amount_cents'))} from each paycheck"
        case "refill_to":
            cap = p.get("max_contribution_cents")
            base = f"topped back up to {_money(p.get('target_cents'))} each paycheck"
            return base + (f", but never more than {_money(cap)} at a time" if cap else "")
        case "conditional":
            when = p.get("when") or {}
            then = p.get("then") or {}
            inner = describe_rule(then.get("rule_type"), then.get("params"))
            op = {"<": "below", ">": "above", "<=": "at or below",
                  ">=": "at or above"}.get(str(when.get("op")), str(when.get("op")))
            return (f"{inner}, but only while its balance is {op} "
                    f"{_money(when.get('value_cents'))}")
        case "remainder":
            return "whatever is left over after every other rule"
        case None:
            return "not funded automatically — only ever filled by hand"
        case _:
            return str(rule_type)


def build(db: Session, user: User) -> str:
    """The whole budget, written for a reader rather than a parser."""
    cats = list(db.scalars(
        select(Category)
        .where(Category.user_id == user.id, Category.archived_at.is_(None))
        .order_by(Category.sort_order, Category.name)
    ).all())

    plan = db.scalars(
        select(SplitPlan).where(SplitPlan.user_id == user.id,
                                SplitPlan.is_active.is_(True))
    ).first()
    rules: dict[uuid.UUID, SplitRule] = {}
    if plan is not None:
        for r in db.scalars(
            select(SplitRule).where(SplitRule.plan_id == plan.id,
                                    SplitRule.enabled.is_(True))
            .order_by(SplitRule.order_index)
        ).all():
            rules[r.category_id] = r

    matchers: dict[uuid.UUID, list[str]] = {}
    for cr in db.scalars(
        select(CategoryRule).where(CategoryRule.user_id == user.id,
                                   CategoryRule.enabled.is_(True))
    ).all():
        needle = (cr.matcher or {}).get("merchant_contains")
        if needle:
            matchers.setdefault(cr.category_id, []).append(str(needle))

    out: list[str] = []
    out.append(
        "HOW THIS BUDGET WORKS\n"
        "  Money is divided into envelopes. Each paycheck is split between\n"
        "  them by ordered rules, in the order listed below, and the last\n"
        "  rule takes whatever is left. Spending is then drawn out of\n"
        "  whichever envelope it belongs to, so an envelope's balance is\n"
        "  what is actually still available to spend on that thing -- not\n"
        "  a forecast or a limit.\n"
        "  Kinds: `spending` is day to day, `obligation` is a bill that\n"
        "  must be paid, `savings_bucket` is money being accumulated."
    )

    out.append("\nENVELOPES, in funding order")
    if not cats:
        out.append("  (none yet — the budget is empty)")
    for c in cats:
        r = rules.get(c.id)
        line = [f"\n  {c.name}  [{c.kind}]"]
        line.append(f"    balance      {_money(c.balance_cents)}")
        if c.target_cents:
            line.append(f"    target       {_money(c.target_cents)}")
        if c.alert_at_cents:
            line.append(f"    warn below   {_money(c.alert_at_cents)}")
        line.append(f"    funded by    "
                    f"{describe_rule(r.rule_type if r else None, r.params if r else None)}")
        if c.description:
            line.append(f"    what it is for   {c.description}")
        if c.spend_criteria:
            line.append(f"    what gets charged here   {c.spend_criteria}")
        if matchers.get(c.id):
            line.append(f"    auto-files merchants containing   "
                        f"{', '.join(sorted(matchers[c.id])[:6])}")
        out.append("\n".join(line))

    rec = compute_reconciliation(db, user.id)
    out.append(
        f"\nACCOUNTS\n"
        f"  in the bank        {_money(rec.cash_cents)}\n"
        f"  owed on cards      {_money(rec.card_debt_cents)}\n"
        f"  actually available {_money(rec.available_cents)}\n"
        f"  allocated to envelopes {_money(rec.allocated_cents)}\n"
        f"  not yet allocated  {_money(rec.unallocated_cents)}"
    )

    accounts = list(db.scalars(
        select(Account).where(Account.user_id == user.id)
    ).all())
    tracked = [a for a in accounts if getattr(a, "is_tracked", True)]
    if tracked:
        out.append("  accounts in the budget: " + ", ".join(
            f"{a.name} ({a.subtype or a.type})" for a in tracked[:10]))
    return "\n".join(out)
