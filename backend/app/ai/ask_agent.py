"""Answering a question by letting the model fetch what it needs.

The receipt loop proved the shape: give the model tools, do the
arithmetic in Python, cap the loop, and it stops failing at the thing
it is bad at. This applies the same treatment to questions.

WHAT IT REPLACES
----------------
A fixed pipeline: route the question once, compile one query, run it,
phrase the answer. That worked for "how much did I spend on fast food"
and broke everywhere else.

  - Routing forced ONE path. "Am I spending more on food than I
    budgeted?" needs both a figure and the budget; the pipeline could
    fetch one or the other, never both.
  - One shot at the filter. "How much on groceries" compiled to
    merchant="groceries", matched nothing, and answered a confident
    $0.00 while the envelope held $92.25. An agent that sees zero rows
    can try the envelope name instead.
  - No calculator on the advice path, where the model was left doing
    mental arithmetic over balances -- the exact thing measured to be
    unreliable (ADR 0006).

WHAT STAYS THE SAME
-------------------
Every figure is still computed in SQL. The tools wrap functions that
already exist and are already tested; the model chooses which to call
and with what, never what the answer is. That is ADR 0009 unchanged --
the model picks the filter, the database produces the number.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.analytics import queries as Q
from app.analytics import recurring as R
from app.analytics.period import PeriodError, resolve
from app.db.models import Category, User

log = logging.getLogger("nnf.ai.ask")

# Enough for a genuinely multi-step question -- look up the budget,
# run two queries, compare -- without letting a confused model spin.
MAX_TOOL_CALLS = 8


def _money(cents: int | None) -> str:
    return "—" if cents is None else f"${cents / 100:,.2f}"


def strip_markdown(text: str) -> str:
    """Flatten markdown to plain text.

    No longer used on the answer -- the app renders markdown now -- but
    kept because any other caller that wants the text without markup
    needs it, and because the rules were worked out against real model
    output.
    """
    import re as _re

    out = _re.sub(r"\*\*(.+?)\*\*", r"\1", text)          # **bold**
    out = _re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])",
                  r"\1", out)                               # *italic*
    out = _re.sub(r"^\s{0,3}#{1,6}\s+", "", out, flags=_re.M)  # # heading
    out = _re.sub(r"^\s{0,3}[*+]\s+", "- ", out, flags=_re.M)  # * bullet
    out = _re.sub(r"`([^`]+)`", r"\1", out)                   # `code`
    return out.strip()


def tools_for(type_names: list[str], cat_names: list[str]) -> list[dict[str, Any]]:
    """Tool definitions, with the user's real names as enums.

    Names are constrained rather than free text so the model cannot ask
    for an envelope that does not exist -- the failure that produced a
    silent $0.00.
    """
    spending_params: dict[str, Any] = {
        "metric": {"type": "string",
                   "enum": ["spend", "income", "net", "count", "average"]},
        "period": {"type": "string",
                   "description": "this_month, last_month, last_30_days, "
                                  "last_3_months, this_year, all_time, ..."},
        "group_by": {"type": "string",
                     "enum": ["none", "expense_type", "category", "merchant",
                              "month", "week"]},
        "merchant": {"type": "string",
                     "description": "part of a shop name, or empty"},
    }
    if cat_names:
        spending_params["categories"] = {
            "type": "array", "items": {"type": "string", "enum": cat_names},
            "description": "envelopes to limit to"}
    if type_names:
        spending_params["expense_types"] = {
            "type": "array", "items": {"type": "string", "enum": type_names}}

    return [
        {"type": "function", "function": {
            "name": "spending",
            "description": ("Add up their transactions. Returns the total, how "
                            "many, the same figure for the previous period, and "
                            "a breakdown if grouped. This does the arithmetic — "
                            "never add anything up yourself."),
            "parameters": {"type": "object", "properties": spending_params,
                           "required": ["metric", "period"]}}},
        {"type": "function", "function": {
            "name": "budget",
            "description": ("Their envelopes: balances, targets, what each is "
                            "for, how each is funded, and their bank and card "
                            "balances. Use this for questions about the budget "
                            "itself rather than about amounts spent."),
            "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {
            "name": "transactions",
            "description": ("The individual charges behind a figure, when the "
                            "total alone does not answer the question."),
            "parameters": {"type": "object", "properties": {
                "period": {"type": "string"},
                "merchant": {"type": "string"},
                **({"categories": spending_params["categories"]}
                   if cat_names else {})},
                "required": ["period"]}}},
        {"type": "function", "function": {
            "name": "recurring_charges",
            "description": "Charges that repeat: subscriptions and standing bills.",
            "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {
            "name": "answer",
            "description": ("Give the final answer. Every amount in it must have "
                            "come from a tool result above."),
            "parameters": {"type": "object", "properties": {
                "text": {"type": "string"}}, "required": ["text"]}}},
    ]


PROMPT = """Answer a question about someone's own budget.

Today is {today}.

Look things up before answering. You have tools for their spending,
their envelopes, the individual charges, and their repeating bills.

  - `spending` does all arithmetic. NEVER add, average or compare
    figures yourself — ask for them.
  - If a lookup comes back empty, that is information: try a different
    filter before concluding they spent nothing. A shop name that
    matches nothing is not the same as no spending.
  - For questions about whether their budget is set up well, call
    `budget` first and answer from what is actually there.

ANSWERING A QUESTION ABOUT THE BUDGET ITSELF
  Do not list their envelopes back to them — they can already see
  those. Say something they do not already know.

  Asked what is MISSING, work out which ordinary costs have nowhere to
  go. Look at what their envelopes cover and name the gaps you can
  justify, and say what happens today without each one — which
  existing envelope is silently absorbing that spending. Two
  well-argued gaps beat six generic ones.

  Asked whether an amount is sensible, compare it to what they
  actually spend: fetch the figure before judging it.

  Never answer "it depends on your goals". That is true of everything
  and helps no one.

When you have what you need, call `answer`. Every amount in it must
have come from a tool result. If something is not available, say so
rather than estimating.

Be brief: under 200 words, plain language, specific to this budget.

Light markdown is fine and is rendered: **bold** for an envelope name
or a figure worth noticing, and "- " bullets for a short list. Do not
use headings — the answer is too short to need them.

At most {max_calls} tool calls.

The question:
  {question}"""


@dataclass
class AskResult:
    answer: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: int = 0
    model: str = ""
    hit_limit: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            # Shown in the app so the figures can be audited: which
            # lookups ran and what each returned. An answer you cannot
            # check is a claim, not information.
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "model": self.model,
            "hit_limit": self.hit_limit,
        }


def _run_spending(db: Session, user: User, args: dict[str, Any],
                  today: date, cats: list[Category],
                  types: list[Any]) -> dict[str, Any]:
    """Execute one spending lookup. All arithmetic happens here."""
    from app.analytics import taxonomy

    try:
        period = resolve(str(args.get("period") or "this_month"), today=today)
    except (PeriodError, ValueError) as exc:
        return {"error": f"unknown period: {exc}"}

    by_cat = {c.name.lower(): c for c in cats}
    by_type = {taxonomy.slugify(t.name): t for t in types}
    want_cats, want_types, missed = [], [], []
    for n in args.get("categories") or []:
        c = by_cat.get(str(n).strip().lower())
        (want_cats.append(c.id) if c else missed.append(str(n)))
    for n in args.get("expense_types") or []:
        t = by_type.get(taxonomy.slugify(str(n)))
        (want_types.append(t.id) if t else missed.append(str(n)))

    group = str(args.get("group_by") or "none")
    merchant = (str(args.get("merchant") or "").strip() or None)
    q = Q.SpendQuery(
        period=period, metric=str(args.get("metric") or "spend"),  # type: ignore[arg-type]
        group_by=None if group == "none" else group,  # type: ignore[arg-type]
        category_ids=tuple(want_cats), expense_type_ids=tuple(want_types),
        merchant_contains=merchant, limit=12,
    )
    try:
        res = Q.run(db, user.id, q)
    except Q.QueryError as exc:
        return {"error": str(exc)}

    out: dict[str, Any] = {
        "period": period.label,
        "metric": q.metric,
        "total": _money(res.total_cents),
        "transactions": res.count,
    }
    if res.previous_total_cents is not None:
        out["previous_period"] = res.previous_period.label if res.previous_period else None
        out["previous_total"] = _money(res.previous_total_cents)
    if res.groups:
        out["breakdown"] = [
            {"name": g.label, "amount": _money(g.cents), "count": g.count}
            for g in res.groups
        ]
    if missed:
        out["not_recognised"] = missed
    if res.count == 0:
        # Said plainly so the model tries again rather than reporting a
        # confident zero -- the groceries/$0.00 failure.
        out["note"] = ("Nothing matched. This is not necessarily zero spending; "
                       "the filter may be wrong. Try an envelope name, or no "
                       "merchant filter.")
    return out


async def run(
    db: Session, user: User, provider: Any, question: str, *, today: date,
) -> AskResult:
    """Let the model look things up until it can answer."""
    from app.ai import budget_context
    from app.analytics import taxonomy

    cats = [c for c in db.query(Category).filter(
        Category.user_id == user.id, Category.archived_at.is_(None)).all()]
    types = taxonomy.active(db, user.id)
    tools = tools_for([t.name for t in types], [c.name for c in cats])

    messages: list[dict[str, Any]] = [{
        "role": "user",
        "content": PROMPT.format(today=today.isoformat(), question=question,
                                 max_calls=MAX_TOOL_CALLS),
    }]

    steps: list[dict[str, Any]] = []
    calls = 0
    final = ""
    model_name = ""

    while calls < MAX_TOOL_CALLS:
        res = await provider.chat("", messages=messages, tools=tools,
                                  think=False, num_ctx=16384, num_predict=4096)
        model_name = res.model
        if not res.tool_calls:
            # It answered in prose instead of calling `answer`. Take it
            # rather than failing -- the content is what matters.
            final = (res.content or "").strip()
            break

        messages.append({
            "role": "assistant", "content": res.content,
            "tool_calls": [{"function": {"name": c["name"],
                                         "arguments": c["arguments"]}}
                           for c in res.tool_calls],
        })

        done = False
        for call in res.tool_calls:
            calls += 1
            name, args = call.get("name"), (call.get("arguments") or {})

            if name == "answer":
                final = str(args.get("text") or "").strip()
                done = True
                payload: dict[str, Any] = {"ok": True}
            elif name == "spending":
                payload = _run_spending(db, user, args, today, cats, types)
            elif name == "budget":
                payload = {"budget": budget_context.build(db, user)}
            elif name == "transactions":
                try:
                    period = resolve(str(args.get("period") or "this_month"),
                                     today=today)
                    by_cat = {c.name.lower(): c for c in cats}
                    ids = [by_cat[str(n).lower()].id
                           for n in (args.get("categories") or [])
                           if str(n).lower() in by_cat]
                    rows = Q.transactions_for(db, user.id, Q.SpendQuery(
                        period=period, category_ids=tuple(ids),
                        merchant_contains=(str(args.get("merchant") or "").strip()
                                           or None),
                        compare_to_previous=False), limit=25)
                    payload = {"transactions": rows}
                except (PeriodError, ValueError) as exc:
                    payload = {"error": str(exc)}
            elif name == "recurring_charges":
                found = R.detect(db, user.id, today=today)
                payload = {
                    "monthly_total": _money(R.monthly_commitment_cents(found)),
                    "charges": [
                        {"merchant": r.merchant, "every": r.cadence,
                         "amount": _money(r.typical_cents)}
                        for r in found if not r.likely_ended][:12],
                }
            else:
                payload = {"error": f"unknown tool {name}"}

            if name != "answer":
                steps.append({"tool": name, "arguments": args, "result": payload})
            messages.append({"role": "tool", "tool_name": name,
                             "content": json.dumps(payload, default=str)})
            if done:
                break
        if done:
            break

    hit_limit = calls >= MAX_TOOL_CALLS and not final
    if not final:
        final = ("I could not work that out from your data. Try asking it a "
                 "different way, or more specifically.")
    return AskResult(answer=final, steps=steps, tool_calls=calls,
                     model=model_name, hit_limit=hit_limit)
