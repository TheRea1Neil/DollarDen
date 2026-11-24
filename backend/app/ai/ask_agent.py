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

import ast
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.analytics import queries as Q
from app.analytics import recurring as R
from app.analytics.period import PeriodError, resolve
from app.db.models import Category, User

log = logging.getLogger("dollarden.ai.ask")

# Enough for a genuinely multi-step question -- look up the budget,
# run two queries, compare -- without letting a confused model spin.
MAX_TOOL_CALLS = 8

# The window the agent runs in. Prior turns are kept until this is
# nearly full rather than capped at a fixed number of exchanges: the
# limit that matters is tokens, and a count of turns says nothing about
# how long each one was. Raising num_ctx on a bigger machine then just
# buys a longer conversation with no other change.
CONTEXT_TOKENS = 16384

# Leave room for the next question, the tool schemas and the answer.
# Running the window to the brim makes ollama silently drop the OLDEST
# messages, which here are the system prompt and the earliest turns --
# so the agent quietly forgets its instructions rather than failing.
CONTEXT_HEADROOM = 4096


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
            "name": "payday",
            "description": ("Today's date, and when the next payday is "
                            "expected with how many days away that is. Pair "
                            "it with `calculate` to work out how far money "
                            "has to stretch."),
            "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {
            "name": "calculate",
            "description": ("Work out any sum exactly: a balance divided over "
                            "days, a per-day rate, a difference, a total. "
                            "Give a plain expression such as "
                            "'162.56 / 3' or '(220 - 45) / 11'. Use this for "
                            "EVERY calculation — never work one out yourself."),
            "parameters": {"type": "object", "properties": {
                "expression": {"type": "string",
                               "description": "plain arithmetic, e.g. 162.56 / 3"}},
                "required": ["expression"]}}},
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
their envelopes, the individual charges, their repeating bills, their
payday, and a calculator.

  - `spending` totals their TRANSACTIONS — what was spent, over what
    period, grouped how. Never add charges up yourself; ask for the
    total.
  - `payday` knows today's date and when the next payday is due, with
    how many days off it is. Do not count days between dates yourself.
  - `calculate` does every other sum: a balance spread over days, a
    difference, a rate, a total. Work out WHICH sum answers the
    question, then have it computed — never do it in your head. An
    allowance that is off by a third reads exactly as confident as a
    right one.
    To answer "how far does my food money go until payday": `budget`
    for the balance, `payday` for the days, `calculate` to divide.
  - A payday is a RANGE, not a date. If it matters to the answer, say
    the expected day and that it could be a little either side.
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
    # Measured, not estimated: ollama reports how many tokens it read.
    context_used: int = 0
    context_limit: int = CONTEXT_TOKENS
    # Oldest turns were dropped to make room. Worth surfacing -- the
    # assistant will have forgotten the start of the conversation.
    dropped_turns: int = 0

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
            "context_used": self.context_used,
            "context_limit": self.context_limit,
            "dropped_turns": self.dropped_turns,
        }


# Arithmetic the model is allowed to ask for, and nothing else.
#
# `eval` on a string a language model wrote is a straightforward way to
# hand it the process, so the expression is parsed and walked instead:
# numbers and these operators, no names, no attributes, no calls except
# the four below. Anything else is refused as a normal tool error, which
# the model can read and correct, rather than an exception.
_BINOPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a ** b,
}
_FUNCS = {"round": round, "abs": abs, "min": min, "max": max}


def _eval_expr(node: ast.AST) -> float:
    if isinstance(node, ast.Constant):
        # bool is an int subclass; True + 1 is not arithmetic anyone meant.
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError("only numbers can be used")
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        v = _eval_expr(node.operand)
        return v if isinstance(node.op, ast.UAdd) else -v
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        left, right = _eval_expr(node.left), _eval_expr(node.right)
        if isinstance(node.op, ast.Pow) and (abs(right) > 32 or abs(left) > 1e6):
            # 9**9**9 is a valid expression and an unbounded wait.
            raise ValueError("that power is too large to be useful")
        if isinstance(node.op, (ast.Div, ast.FloorDiv, ast.Mod)) and right == 0:
            raise ValueError("cannot divide by zero")
        return _BINOPS[type(node.op)](left, right)
    if isinstance(node, ast.Call):
        name = getattr(node.func, "id", None)
        if name not in _FUNCS or node.keywords:
            raise ValueError("only round, abs, min and max can be used")
        return _FUNCS[name](*[_eval_expr(a) for a in node.args])
    raise ValueError("that is not a plain arithmetic expression")


def _run_calculate(args: dict[str, Any]) -> dict[str, Any]:
    """Do one sum, exactly, so the model never has to.

    Separate from `spending`, which adds up TRANSACTIONS in SQL. This is
    for the arithmetic on top: how long a balance lasts, what a total
    would be, how two figures compare. The model decides what to work
    out; the result is computed rather than recalled.
    """
    expr = str(args.get("expression") or "").strip()
    if not expr:
        return {"error": "no expression given"}
    if len(expr) > 200:
        return {"error": "expression too long"}
    try:
        value = _eval_expr(ast.parse(expr, mode="eval").body)
    except ZeroDivisionError:
        return {"error": "cannot divide by zero"}
    except ValueError as exc:
        return {"error": str(exc)}
    except Exception:  # noqa: BLE001
        return {"error": "that is not a plain arithmetic expression"}

    if not math.isfinite(value):
        return {"error": "that does not come out to a usable number"}
    return {
        "expression": expr,
        # Rounded because 162.56 / 3 is 54.18666666666667, and no answer
        # about money should ever contain that.
        "result": round(value, 4),
        "as_money": _money(round(value * 100)),
    }


def _run_payday(db: Session, user: User, today: date,
                cats: list[Category]) -> dict[str, Any]:
    """Today, the next payday, and what each envelope allows per day.

    The day counts and the division are done HERE, in Python, and handed
    over as finished figures. That is ADR 0006 and 0009 applied to the
    calendar: the model decides that "until payday" is the right horizon,
    and the engine works out how long that is and what it means per day.

    Letting the model do it instead means trusting it to know how many
    days are between two dates and then to divide a balance by that --
    two things language models get quietly, plausibly wrong. A daily food
    allowance that is off by a third is worse than no answer.

    The payday itself is a RANGE (see budget/paycheck.predict_next), so
    all three dates are offered. `per_day` deliberately uses the EXPECTED
    date: budgeting to the earliest it could arrive overstates what is
    safe to spend, and the model can read the range and caveat it.
    """
    from app.budget import paycheck

    out: dict[str, Any] = {
        "today": today.isoformat(),
        "weekday": today.strftime("%A"),
    }

    pred = paycheck.predict_next(db, user.id)
    if not pred.get("known"):
        out["next_payday"] = {
            "known": False,
            # Passed through verbatim: it says WHY, and "no pattern yet"
            # is a real answer to "when do I get paid".
            "reason": pred.get("reason"),
        }
        return out

    expected = date.fromisoformat(str(pred["expected"]))
    earliest = date.fromisoformat(str(pred["earliest"]))
    latest = date.fromisoformat(str(pred["latest"]))
    days = (expected - today).days

    out["next_payday"] = {
        "known": True,
        "expected": expected.isoformat(),
        "earliest": earliest.isoformat(),
        "latest": latest.isoformat(),
        "days_until_expected": days,
        "days_until_earliest": (earliest - today).days,
        "days_until_latest": (latest - today).days,
        "paid_every_days": pred.get("cadence_days"),
        "based_on_paydays": pred.get("based_on"),
        "worked_out_from": pred.get("source"),
    }
    if days < 0:
        # Overdue is not an error -- a late deposit is exactly when
        # someone asks how far their money has to stretch. Say it
        # plainly instead of returning a negative day count to divide by.
        out["next_payday"]["overdue_by_days"] = -days

    return out


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


def _estimate_tokens(text: str) -> int:
    """Rough token count for budgeting history before the call.

    Four characters per token is the usual English approximation. It is
    only used to decide how many prior turns to send; the number
    REPORTED to the user is ollama's own count, measured after the fact.
    """
    return max(1, len(text) // 4)


async def run(
    db: Session, user: User, provider: Any, question: str, *, today: date,
    history: list[dict[str, str]] | None = None,
) -> AskResult:
    """Let the model look things up until it can answer.

    `history` is prior [{question, answer}] turns, oldest first. They are
    replayed as plain user/assistant messages -- the model needs what it
    SAID, not which lookups it ran to get there, and replaying tool calls
    would cost most of the window for no benefit.

    Without this a follow-up is meaningless: "what about last month?"
    has no referent, which is the whole reason the chat needed memory.
    """
    from app.ai import budget_context
    from app.analytics import taxonomy

    cats = [c for c in db.query(Category).filter(
        Category.user_id == user.id, Category.archived_at.is_(None)).all()]
    types = taxonomy.active(db, user.id)
    tools = tools_for([t.name for t in types], [c.name for c in cats])

    opening = PROMPT.format(today=today.isoformat(), question=question,
                            max_calls=MAX_TOOL_CALLS)

    # Fit as many prior turns as the window allows, newest first, then
    # restore chronological order. Dropping from the OLDEST end keeps
    # the turns a follow-up is most likely to refer to.
    budget = CONTEXT_TOKENS - CONTEXT_HEADROOM - _estimate_tokens(opening)
    kept: list[dict[str, str]] = []
    dropped = 0
    for turn in reversed(history or []):
        q, a = str(turn.get("question", "")), str(turn.get("answer", ""))
        cost = _estimate_tokens(q) + _estimate_tokens(a)
        if cost > budget:
            dropped += 1
            continue
        budget -= cost
        kept.append(turn)
    kept.reverse()

    messages: list[dict[str, Any]] = []
    for turn in kept:
        messages.append({"role": "user", "content": str(turn.get("question", ""))})
        messages.append({"role": "assistant", "content": str(turn.get("answer", ""))})
    messages.append({"role": "user", "content": opening})

    steps: list[dict[str, Any]] = []
    calls = 0
    final = ""
    model_name = ""
    context_used = 0

    while calls < MAX_TOOL_CALLS:
        res = await provider.chat("", messages=messages, tools=tools,
                                  think=False, num_ctx=CONTEXT_TOKENS, num_predict=4096)
        model_name = res.model
        # The last call read the most, so this ends up being the peak.
        context_used = max(context_used, res.prompt_eval_count or 0)
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
            elif name == "payday":
                payload = _run_payday(db, user, today, cats)
            elif name == "calculate":
                payload = _run_calculate(args)
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
                     model=model_name, hit_limit=hit_limit,
                     context_used=context_used, context_limit=CONTEXT_TOKENS,
                     dropped_turns=dropped)
