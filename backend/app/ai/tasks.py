"""AI task handlers.

Every handler follows the same shape: gather context, ask the model for a
constrained JSON shape, then VALIDATE before returning. The model proposes;
deterministic code disposes (ADR 0003). Nothing here writes to the ledger.
"""
from __future__ import annotations

import json
import uuid
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai import receipt_text
from app.budget.reconcile import compute as compute_reconciliation
from app.db.models import Category, User, UserGoal

log = logging.getLogger("nnf.ai.tasks")

VALID_KINDS = {"spending", "savings_bucket", "obligation"}
VALID_RULE_TYPES = {"percent", "fixed", "refill_to", "conditional", "remainder"}


def _goals(db: Session, user: User) -> str:
    row = db.scalars(select(UserGoal).where(UserGoal.user_id == user.id)).first()
    return (row.body if row else "").strip()


def _describe_category(c: Category) -> str:
    """One line of context about a category.

    The description is written by the user or a previous proposal and says
    what the category is FOR. It is not used by the allocation engine --
    that is driven entirely by split rules -- but it is exactly the context
    the model needs to place a new transaction or design around it.
    """
    bits = [f"  - {c.name} ({c.kind})"]
    if c.target_cents:
        bits.append(f", target ${c.target_cents / 100:.2f}")
    if c.description:
        bits.append(f" — {c.description}")
    return "".join(bits)


def _categories(db: Session, user: User) -> list[Category]:
    return list(
        db.scalars(
            select(Category)
            .where(Category.user_id == user.id, Category.archived_at.is_(None))
            .order_by(Category.sort_order, Category.name)
        ).all()
    )


# --------------------------------------------------------------------------- #
# Designing categories and a split plan
# --------------------------------------------------------------------------- #
DESIGN_SCHEMA = {
    "type": "object",
    "properties": {
        "categories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "kind": {"type": "string",
                             "enum": ["spending", "savings_bucket", "obligation"]},
                    "description": {"type": "string"},
                    "target_cents": {"type": "integer"},
                    "alert_at_cents": {"type": "integer"},
                },
                "required": ["name", "kind", "description"],
            },
        },
        "split_rules": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category_name": {"type": "string"},
                    "order_index": {"type": "integer"},
                    "rule_type": {"type": "string",
                                  "enum": ["percent", "fixed", "refill_to",
                                           "conditional", "remainder"]},
                    # A bare {"type": "object"} gives the model no shape to
                    # fill, and it returned empty params every time. Naming
                    # the fields explicitly is what makes them appear.
                    # Every field a rule type can need MUST be declared here.
                    # A JSON-schema-constrained model cannot emit a property
                    # the schema does not name: omitting `when`/`then` made
                    # conditional rules impossible to express, and the model
                    # fell back to stuffing the threshold into target_cents
                    # on 24 out of 24 trials. No amount of retrying fixes
                    # output the schema forbids.
                    "params": {
                        "type": "object",
                        "properties": {
                            "rate": {"type": "string",
                                     "description": "percent rules: '0.10' for 10%"},
                            "of": {"type": "string", "enum": ["gross", "remaining"]},
                            "amount_cents": {"type": "integer",
                                             "description": "fixed rules, in cents"},
                            "target_cents": {"type": "integer",
                                             "description": "refill_to rules, in cents"},
                            "max_contribution_cents": {
                                "type": "integer",
                                "description": "refill_to rules, optional: never "
                                               "add more than this in one paycheck, "
                                               "so a big shortfall is filled over "
                                               "several rather than all at once",
                            },
                            "when": {
                                "type": "object",
                                "description": "conditional rules: the test to apply",
                                "properties": {
                                    "field": {"type": "string", "enum": ["balance"]},
                                    "op": {"type": "string",
                                           "enum": ["<", "<=", ">", ">=", "=="]},
                                    "value_cents": {
                                        "type": "integer",
                                        "description": "the threshold in CENTS, e.g. "
                                                       "45000 for $450. REQUIRED.",
                                    },
                                },
                                # Naming a property is not enough -- the model
                                # emitted `op` and omitted `value_cents`, and a
                                # predicate with no threshold cannot be
                                # evaluated at all. Declaring a property
                                # optional is an invitation to leave it out.
                                "required": ["op", "value_cents"],
                            },
                            "then": {
                                "type": "object",
                                "description": "conditional rules: what to do if true",
                                "properties": {
                                    "rule_type": {"type": "string",
                                                  "enum": ["fixed", "refill_to", "percent"]},
                                    "params": {
                                        "type": "object",
                                        "properties": {
                                            "amount_cents": {"type": "integer"},
                                            "target_cents": {"type": "integer"},
                                            "rate": {"type": "string"},
                                        },
                                    },
                                },
                            },
                        },
                    },
                    "why": {"type": "string"},
                },
                "required": ["category_name", "order_index", "rule_type", "params", "why"],
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["categories", "split_rules", "notes"],
}

DESIGN_PROMPT = """You design budget categories for an envelope-style budgeting app.

How this budget works:
- A category is a BALANCE, not a report line. It holds money.
- Each paycheck is split across categories by ORDERED rules.
- Rule types:
    percent     {{"rate": "0.10", "of": "gross"}}   a share, e.g. tithing
    fixed       {{"amount_cents": 35000}}            a set amount, e.g. rent
    refill_to   {{"target_cents": 30000}}            top back up to a target
    conditional {{"when": {{"field": "balance", "op": "<", "value_cents": 45000}},
                 "then": {{"rule_type": "fixed", "params": {{"amount_cents": 5000}}}}}}
    remainder   {{}}                                 everything left over
- Rules run in order. EXACTLY ONE remainder rule, and it MUST be last.
- "remainder" is a RULE TYPE, never a category name. The remainder rule
  names a real category to receive whatever is left, e.g.
  {{"category_name": "New Car Fund", "rule_type": "remainder", "params": {{}}}}
- All money is INTEGER CENTS. $350.00 is 35000.

Category kinds:
    spending        refilled each paycheck and spent down (groceries, gas)
    savings_bucket  accumulates toward something (emergency fund, new car)
    obligation      accrues toward a known payment (rent, tithing, insurance)

{existing_block}

What the user wants:
{goals}

{request_block}

Design categories and an ordered split plan that achieves this.

ORDER MATTERS ENORMOUSLY. Each rule takes its money before the next one
runs, so a rule placed too early starves everything after it. Use this
order:

  1. percent-of-gross rules (tithing) — before anything consumes the pay
  2. fixed obligations that MUST be paid (rent, insurance, utilities)
  3. refill_to for essentials you spend down (groceries, gas)
  4. conditional top-ups
  5. refill_to for savings GOALS (emergency fund, car fund) — these are
     large and will consume everything if placed before the essentials
  6. remainder, last

A large refill_to target placed above rent will take the entire paycheck
and leave rent unfunded. That is the single most common mistake here.

More rules:
- Give every spending category a sensible target_cents.
- Use refill_to targets the user actually stated. Do not copy one goal's
  target onto a different category.
- End with exactly one remainder rule pointing at a real savings category.
- EVERY rule must have filled params matching its type:
    percent   -> {{"rate": "0.10", "of": "gross"}}
    fixed     -> {{"amount_cents": 35000}}
    refill_to -> {{"target_cents": 30000}}
    remainder -> {{}}
  A rule with empty params is unusable.
- Explain each rule in one short sentence in "why".
- Prefer few, meaningful categories over many fine-grained ones.

Return JSON only."""


async def run_design_categories(
    db: Session, user: User, provider: Any, request: dict[str, Any]
) -> dict[str, Any]:
    existing = _categories(db, user)
    goals = request.get("goals") or _goals(db, user) or "(none stated)"

    if existing:
        lines = "\n".join(_describe_category(c) for c in existing)
        existing_block = (
            f"The user ALREADY has these categories. Reuse them by name where they "
            f"fit, and only add what is missing:\n{lines}"
        )
    else:
        existing_block = "The user has no categories yet. Design a full set."

    extra = (request.get("instructions") or "").strip()
    request_block = f"Additional instructions:\n{extra}" if extra else ""

    prompt = DESIGN_PROMPT.format(
        existing_block=existing_block, goals=goals, request_block=request_block
    )

    res = await provider.chat(prompt, schema=DESIGN_SCHEMA)
    proposal = _parse_json(res.content)
    return _validate_design(proposal, existing)


def _parse_json(content: str) -> dict[str, Any]:
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        # Schema-constrained output should be valid JSON; if it is not, the
        # proposal is unusable and saying so beats half-parsing it.
        raise ValueError(f"model returned unparseable JSON: {exc}") from exc


def _validate_design(
    proposal: dict[str, Any],
    existing: list[Category],
    typical_gross_cents: int = 200000,
) -> dict[str, Any]:
    """Check a proposal before a human ever sees it.

    A proposal that cannot be applied is worse than none: the user would
    accept it and hit an error. Problems are reported alongside the
    proposal rather than silently repaired, so the draft stays editable.
    """
    problems: list[str] = []   # block applying
    notices: list[str] = []    # worth saying, but harmless
    cats = proposal.get("categories") or []
    rules = proposal.get("split_rules") or []

    names: set[str] = {c.name for c in existing}
    clean_cats = []
    for c in cats:
        name = str(c.get("name", "")).strip()
        kind = c.get("kind")
        if not name:
            problems.append("a category had no name and was dropped")
            continue
        if kind not in VALID_KINDS:
            problems.append(f"'{name}' had invalid kind '{kind}', defaulted to spending")
            kind = "spending"
        if name in names:
            # Informational. Reusing an existing category is the desired
            # behaviour, not a reason to block the whole proposal.
            notices.append(f"'{name}' already exists and will be reused")
        names.add(name)
        clean_cats.append({
            "name": name,
            "kind": kind,
            "description": str(c.get("description", "")).strip(),
            "target_cents": _as_int(c.get("target_cents")),
            "alert_at_cents": _as_int(c.get("alert_at_cents")),
            "already_exists": name in {e.name for e in existing},
        })

    clean_rules = []
    for r in sorted(rules, key=lambda x: x.get("order_index", 0)):
        cname = str(r.get("category_name", "")).strip()
        rtype = r.get("rule_type")
        if rtype not in VALID_RULE_TYPES:
            problems.append(f"rule for '{cname}' had invalid type '{rtype}' and was dropped")
            continue
        if cname not in names:
            problems.append(f"rule references unknown category '{cname}' and was dropped")
            continue
        params = r.get("params") or {}
        missing = _missing_params(rtype, params)
        if missing:
            problems.append(
                f"rule {len(clean_rules) + 1} ('{cname}', {rtype}) is missing "
                f"{missing} — fill it in before applying"
            )
        clean_rules.append({
            "category_name": cname,
            "order_index": len(clean_rules) + 1,
            "rule_type": rtype,
            "params": params,
            "why": str(r.get("why", "")).strip(),
        })

    # Structural repair, not retrying.
    #
    # Measured: asking the model to fix its own output does not help -- see
    # experiments/selfcheck_strategies.py and docs/05. But some faults have
    # exactly one correct fix, and applying it in code is both faster and
    # more reliable than another 15-second round trip. Repairs are reported
    # as notices so nothing changes silently.
    clean_rules, repairs = _repair_remainders(clean_rules)
    notices.extend(repairs)

    remainders = [r for r in clean_rules if r["rule_type"] == "remainder"]
    if len(remainders) == 0:
        problems.append("no remainder rule — add one for whatever is left over")
    elif len(remainders) > 1:
        problems.append(f"{len(remainders)} remainder rules — only one is allowed")
    elif clean_rules and clean_rules[-1]["rule_type"] != "remainder":
        problems.append("the remainder rule must be last")

    # Dry-run the plan against a representative paycheck.
    #
    # Structural validation cannot catch an ORDERING mistake: a plan can be
    # perfectly well-formed and still starve rent, because a large
    # refill_to above it consumed everything. Observed exactly that -- an
    # Emergency Fund refill to $6,000 sat at position 2 and took the whole
    # paycheck. Simulating is the only way to see it.
    # Only simulate a plan that is otherwise sound. Running the engine over
    # rules with missing params raises, and a plan already known to be
    # broken has nothing useful to say about ordering anyway.
    warnings = _simulate(clean_rules, typical_gross_cents) if not problems else []

    return {
        "categories": clean_cats,
        "split_rules": clean_rules,
        "notes": str(proposal.get("notes", "")).strip(),
        "problems": problems,
        "notices": notices,
        "warnings": warnings,
        "simulated_gross_cents": typical_gross_cents,
        # Only real problems block applying. The proposal is editable either
        # way -- the user can fix what the model got wrong.
        "applicable": not problems,
    }


_REQUIRED_PARAMS = {
    "percent": ("rate",),
    "fixed": ("amount_cents",),
    "refill_to": ("target_cents",),
    "conditional": ("when", "then"),
    "remainder": (),
}


def _missing_params(rule_type: str, params: dict[str, Any]) -> str:
    """Which required params a rule lacks.

    A `percent` rule with no rate is unusable, and the engine would reject
    it at apply time -- better to say so while the draft is still editable.
    """
    needed = _REQUIRED_PARAMS.get(rule_type, ())
    absent = [k for k in needed if params.get(k) in (None, "", {})]

    # `when` existing is not enough. A predicate carrying an operator but
    # no threshold passed this check, saved fine, and then raised at the
    # moment a paycheck was recorded.
    if rule_type == "conditional" and "when" not in absent:
        when = params.get("when") or {}
        absent += [f"when.{k}" for k in ("op", "value_cents")
                   if when.get(k) in (None, "")]
    if rule_type == "conditional" and "then" not in absent:
        then = params.get("then") or {}
        if then.get("rule_type") in (None, ""):
            absent.append("then.rule_type")

    return ", ".join(absent)


def _simulate(rules: list[dict[str, Any]], gross_cents: int) -> list[str]:
    """Run the plan against a representative paycheck and report starvation.

    Returns warnings, not problems: the plan is legal, and the user may
    genuinely intend it. But being told "with a $2,000 paycheck this leaves
    Rent with nothing" is the difference between catching the mistake now
    and discovering it on payday.
    """
    from app.budget.split import RuleType
    from app.budget.split import SplitRule as EngineRule
    from app.budget.split import SplitPlanError, apply_split

    if not rules:
        return []

    engine = [
        EngineRule(
            category_id=r["category_name"],
            order_index=r["order_index"],
            rule_type=RuleType(r["rule_type"]),
            params=r["params"],
        )
        for r in rules
    ]
    try:
        # Balances start at zero: a brand-new budget is the worst case, and
        # the case the assistant is usually being asked about.
        result = apply_split(gross_cents, engine, {r["category_name"]: 0 for r in rules})
    except (SplitPlanError, KeyError, TypeError, ValueError):
        # Structural and param faults are reported separately. A simulation
        # must never be the thing that breaks validation.
        return []

    warnings: list[str] = []
    funded = {a.category_id: a.amount_cents for a in result.allocations}

    starved = [
        r["category_name"] for r in rules
        if r["rule_type"] != "remainder" and funded.get(r["category_name"], 0) == 0
    ]
    if starved:
        warnings.append(
            f"With a ${gross_cents / 100:,.0f} paycheck, these get nothing because "
            f"earlier rules use it all: {', '.join(starved)}. "
            f"Move large savings goals below your essentials."
        )

    for name, short in result.unfunded:
        warnings.append(
            f"'{name}' would be short ${short / 100:,.2f} of what its rule asks for."
        )

    return warnings


def _repair_remainders(
    rules: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Fix remainder-rule faults that have exactly one correct resolution.

    Two faults are mechanical:

    * More than one remainder rule. Only the LAST can receive what is left,
      because everything before it would consume the whole paycheck. The
      earlier ones are dropped.
    * The remainder rule is not last. It is moved to the end, which is the
      only position where it means anything.

    Both are reported, never applied silently. Anything ambiguous is left
    alone and surfaces as a problem for the user to resolve.
    """
    notes: list[str] = []
    remainder_idx = [i for i, r in enumerate(rules) if r["rule_type"] == "remainder"]

    if len(remainder_idx) > 1:
        keep = remainder_idx[-1]
        dropped = [rules[i]["category_name"] for i in remainder_idx[:-1]]
        rules = [r for i, r in enumerate(rules) if i == keep or i not in remainder_idx]
        notes.append(
            f"the model proposed {len(remainder_idx)} remainder rules; kept the "
            f"last one and dropped {', '.join(dropped)} — only one rule can "
            f"receive what is left over"
        )
        remainder_idx = [i for i, r in enumerate(rules) if r["rule_type"] == "remainder"]

    if remainder_idx and remainder_idx[-1] != len(rules) - 1:
        rule = rules.pop(remainder_idx[-1])
        rules.append(rule)
        notes.append(
            f"moved the remainder rule ('{rule['category_name']}') to last, "
            f"where it is the only thing it can mean"
        )

    for n, r in enumerate(rules, start=1):
        r["order_index"] = n
    return rules, notes


def _as_int(v: Any) -> int | None:
    """A non-negative whole number of cents, or None.

    Most money the model proposes cannot sensibly be negative — a
    target, a threshold, a paycheck amount — so a negative is treated
    as garbage rather than carried forward. Receipt LINE ITEMS are the
    exception; see `_as_signed_int`.
    """
    n = _as_signed_int(v)
    return n if n is not None and n >= 0 else None


def _as_signed_int(v: Any) -> int | None:
    """A whole number of cents that may be negative.

    Receipt lines can be: a coupon or a returned item is a negative
    line, and dropping it makes the receipt stop adding up — which is
    exactly the thing itemising is supposed to guarantee.
    """
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Spending summary, written against the user's own goals
# --------------------------------------------------------------------------- #
SUMMARY_PROMPT = """You are reviewing someone's spending for the period just ended.

Their stated goals:
{goals}

Where their money currently sits:
{balances}

Money position:
  in the bank      ${cash:.2f}
  owed on cards    ${debt:.2f}
  allocated        ${allocated:.2f}

Spending this period, largest first:
{spending}

Write a short review, at most 200 words:
1. What actually happened, in plain language and specific numbers.
2. Where the money went that they may not have noticed.
3. Two or three concrete suggestions, tied to THEIR stated goals above.

Be direct and specific. No generic advice about budgeting. If their goals
say nothing relevant, say what you would need to know. Do not invent
numbers that are not shown above."""


async def run_summary(
    db: Session, user: User, provider: Any, request: dict[str, Any]
) -> dict[str, Any]:
    from app.db.models import Allocation, Transaction

    goals = _goals(db, user) or "(the user has not written any goals yet)"
    cats = _categories(db, user)
    rec = compute_reconciliation(db, user.id)

    balances = "\n".join(
        f"  {c.name}: ${c.balance_cents / 100:.2f}"
        + (f" of ${c.target_cents / 100:.2f}" if c.target_cents else "")
        + (f" — {c.description}" if c.description else "")
        for c in cats
    ) or "  (no categories yet)"

    rows = db.execute(
        select(Category.name, Allocation.amount_cents, Transaction.name)
        .join(Category, Category.id == Allocation.category_id)
        .join(Transaction, Transaction.id == Allocation.transaction_id)
        .where(
            Allocation.user_id == user.id,
            Allocation.amount_cents < 0,
            Transaction.removed_at.is_(None),
        )
        .order_by(Allocation.amount_cents)
        .limit(25)
    ).all()
    spending = "\n".join(
        f"  ${abs(amt) / 100:.2f}  {txn[:36]}  -> {cat}" for cat, amt, txn in rows
    ) or "  (no categorised spending yet)"

    prompt = SUMMARY_PROMPT.format(
        goals=goals, balances=balances, spending=spending,
        cash=rec.cash_cents / 100, debt=rec.card_debt_cents / 100,
        allocated=rec.allocated_cents / 100,
    )
    res = await provider.chat(prompt)
    return {
        "summary": res.content.strip(),
        "model": res.model,
        "tokens_per_second": res.tokens_per_second,
    }


# --------------------------------------------------------------------------- #
# Goal-aware advice
# --------------------------------------------------------------------------- #
async def run_advise(
    db: Session, user: User, provider: Any, request: dict[str, Any]
) -> dict[str, Any]:
    question = (request.get("question") or "").strip()
    if not question:
        raise ValueError("advise requires a question")

    goals = _goals(db, user) or "(none stated)"
    cats = _categories(db, user)
    rec = compute_reconciliation(db, user.id)
    balances = "\n".join(
        f"  {c.name}: ${c.balance_cents / 100:.2f}"
        + (f" — {c.description}" if c.description else "")
        for c in cats
    )

    prompt = (
        f"Someone is asking about their own budget.\n\n"
        f"Their goals:\n{goals}\n\n"
        f"Their categories:\n{balances or '  (none yet)'}\n\n"
        f"In the bank ${rec.cash_cents / 100:.2f}, owed on cards "
        f"${rec.card_debt_cents / 100:.2f}.\n\n"
        f"Their question:\n{question}\n\n"
        f"Answer directly in under 150 words, using their actual numbers. "
        f"Do not invent figures that are not shown above."
    )
    res = await provider.chat(prompt)
    return {"answer": res.content.strip(), "model": res.model}


# --------------------------------------------------------------------------- #
# Designing ONE category
# --------------------------------------------------------------------------- #
SINGLE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "kind": {"type": "string",
                 "enum": ["spending", "savings_bucket", "obligation"]},
        "description": {"type": "string",
                        "description": "what this category is for, one sentence"},
        "spend_criteria": {
            "type": "string",
            "description": "what gets CHARGED here, as a boundary: which "
                           "purchases belong and which belong elsewhere. "
                           "e.g. 'Fuel at a pump; Uber rides are Date "
                           "Fund.' This is what decides where a charge "
                           "goes, so be specific about the edge.",
        },
        "target_cents": {"type": "integer",
                         "description": "refill target in CENTS, 30000 means $300"},
        "alert_at_cents": {"type": "integer"},
        "suggested_rule": {
            "type": "object",
            "properties": {
                "rule_type": {"type": "string",
                              "enum": ["percent", "fixed", "refill_to", "conditional"]},
                "params": {
                    "type": "object",
                    "properties": {
                        "rate": {"type": "string"},
                        "of": {"type": "string", "enum": ["gross", "remaining"]},
                        "amount_cents": {"type": "integer"},
                        "target_cents": {"type": "integer"},
                    },
                },
                "why": {"type": "string"},
            },
        },
        "matcher_hint": {
            "type": "object",
            "description": "words likely to appear on matching transactions",
            "properties": {
                "merchant_contains": {"type": "string"},
            },
        },
    },
    "required": ["name", "kind", "description"],
}

SINGLE_PROMPT = """Design ONE budget category from a description.

Category kinds:
    spending        refilled each paycheck and spent down (groceries, gas)
    savings_bucket  accumulates toward something (emergency fund, new car)
    obligation      accrues toward a known payment (rent, tithing, insurance)

All money is INTEGER CENTS. $300.00 is 30000. $2,000.00 is 200000.

The user already has these categories — do NOT duplicate one of them, and
pick a name that clearly distinguishes the new one:
{existing}

Their overall goals, for context:
{goals}

They want a category for:
{request}

Return:
- a short clear name
- the right kind
- a one-sentence description of what it is for
- target_cents if it makes sense to refill it to a set amount
- suggested_rule: how each paycheck should fund it (NOT a remainder rule —
  this is one category, not a whole plan)
- matcher_hint: a word likely to appear on transactions belonging here,
  lowercase, if there is an obvious one

Return JSON only."""


async def run_design_category(
    db: Session, user: User, provider: Any, request: dict[str, Any]
) -> dict[str, Any]:
    """Design a single category from a prose description.

    Separate from `design_categories` because adding one category should
    not mean regenerating -- and risking changes to -- an entire working
    plan the user has already tuned.
    """
    wanted = (request.get("request") or "").strip()
    if not wanted:
        raise ValueError("describe what the category is for")

    existing = _categories(db, user)
    prompt = SINGLE_PROMPT.format(
        existing="\n".join(_describe_category(c) for c in existing) or "  (none yet)",
        goals=_goals(db, user) or "(none stated)",
        request=wanted,
    )

    res = await provider.chat(prompt, schema=SINGLE_SCHEMA)
    raw = _parse_json(res.content)
    return _validate_single(raw, existing)


def _validate_single(
    raw: dict[str, Any], existing: list[Category]
) -> dict[str, Any]:
    problems: list[str] = []
    notices: list[str] = []

    name = str(raw.get("name", "")).strip()
    if not name:
        problems.append("the model did not give the category a name")

    kind = raw.get("kind")
    if kind not in VALID_KINDS:
        notices.append(f"kind '{kind}' was not valid, defaulted to spending")
        kind = "spending"

    if any(c.name.lower() == name.lower() for c in existing):
        problems.append(f"'{name}' already exists — rename it or edit the existing one")

    rule = raw.get("suggested_rule") or {}
    rule_type = rule.get("rule_type")
    if rule_type is not None:
        if rule_type not in VALID_RULE_TYPES or rule_type == "remainder":
            notices.append(f"suggested rule type '{rule_type}' was dropped")
            rule = {}
        else:
            missing = _missing_params(rule_type, rule.get("params") or {})
            if missing:
                notices.append(
                    f"the suggested rule is missing {missing}; fill it in or skip it"
                )

    return {
        "name": name,
        "kind": kind,
        "description": str(raw.get("description", "")).strip(),
        "spend_criteria": str(raw.get("spend_criteria", "")).strip() or None,
        "target_cents": _as_int(raw.get("target_cents")),
        "alert_at_cents": _as_int(raw.get("alert_at_cents")),
        "suggested_rule": rule or None,
        "matcher_hint": raw.get("matcher_hint") or None,
        "problems": problems,
        "notices": notices,
        "applicable": not problems,
    }


# --------------------------------------------------------------------------- #
# Refining ONE existing category
# --------------------------------------------------------------------------- #
REFINE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "kind": {"type": "string",
                 "enum": ["spending", "savings_bucket", "obligation"]},
        "description": {"type": "string"},
        "target_cents": {"type": "integer",
                         "description": "the full-envelope amount, in CENTS"},
        "alert_at_cents": {"type": "integer",
                           "description": "warn below this, in CENTS"},
        "funding": {
            "type": "object",
            "properties": {
                "rule_type": {"type": "string",
                              "enum": ["percent", "fixed", "refill_to",
                                       "conditional", "none"]},
                "rate": {"type": "string", "description": "percent: '0.10' is 10%"},
                "of": {"type": "string", "enum": ["gross", "remaining"]},
                "amount_cents": {"type": "integer", "description": "fixed rules"},
                "target_cents": {"type": "integer", "description": "refill_to rules"},
                "max_contribution_cents": {
                    "type": "integer",
                    "description": "refill_to: cap per paycheck, optional"},
                "when_op": {"type": "string",
                            "enum": ["<", "<=", ">", ">=", "=="],
                            "description": "conditional only"},
                "when_value_cents": {"type": "integer",
                                     "description": "conditional only: the "
                                                    "threshold in CENTS. REQUIRED "
                                                    "for conditional rules."},
                "then_amount_cents": {"type": "integer",
                                      "description": "conditional only: how much "
                                                     "when the test passes"},
            },
            "required": ["rule_type"],
        },
        "add_matchers": {
            "type": "array",
            "description": "transactions that should come out of this category",
            "items": {
                "type": "object",
                "properties": {
                    "merchant_contains": {"type": "string"},
                    "direction": {"type": "string", "enum": ["outflow", "inflow"]},
                },
                "required": ["merchant_contains"],
            },
        },
        "what_changed": {"type": "string",
                         "description": "one sentence, plain English"},
    },
    "required": ["name", "kind", "description", "funding", "what_changed"],
}

REFINE_PROMPT = """Change ONE budget category, as asked.

Here is the category as it stands:

  name:        {name}
  kind:        {kind}
  for:         {description}
  holds now:   {balance}
  full at:     {target}
  warn below:  {alert}
  funded by:   {funding}
  money comes out of it when: {matchers}

Their overall goals, for context:
{goals}

Their other categories, so you do not duplicate or contradict them:
{others}

What they want changed:
{request}

Return the category as it should be AFTER the change. Carry over anything
they did not ask to change -- do not drop the description or reset the
target because they only mentioned the funding.

All money is INTEGER CENTS. $300.00 is 30000.

Funding rule types:
    percent      a share of pay. Set rate ("0.10" = 10%) and of="gross".
    fixed        the same amount each paycheck. Set amount_cents.
    refill_to    topped back up to a target, adding only the shortfall.
                 Set target_cents. Optionally cap one paycheck's top-up
                 with max_contribution_cents.
    conditional  funded only while a threshold holds. Set when_op,
                 when_value_cents AND then_amount_cents. All three.
    none         not funded automatically; they allocate to it by hand.

For a conditional, read the direction carefully: "only while it holds less
than $450" is when_op "<" with when_value_cents 45000.

Return JSON only."""


async def run_refine_category(
    db: Session, user: User, provider: Any, request: dict[str, Any]
) -> dict[str, Any]:
    """Change one existing category from a plain-English instruction.

    Distinct from `design_category`, which invents one from nothing. This
    one starts from what is already there and must preserve the parts the
    user did not mention.
    """
    cat_id = (request.get("category_id") or "").strip()
    wanted = (request.get("request") or "").strip()
    if not cat_id:
        raise ValueError("refine_category needs a category_id")
    if not wanted:
        raise ValueError("say what should change about it")

    try:
        pk = uuid.UUID(cat_id)
    except ValueError:
        raise ValueError("category_id is not a valid id") from None

    cat = db.scalars(
        select(Category).where(Category.id == pk, Category.user_id == user.id)
    ).first()
    if cat is None:
        raise ValueError("that category does not exist")

    funding_txt = request.get("funding_summary") or "nothing — not funded automatically"
    matchers = request.get("matcher_summaries") or []
    others = [c for c in _categories(db, user) if c.id != cat.id]

    prompt = REFINE_PROMPT.format(
        name=cat.name, kind=cat.kind,
        description=cat.description or "(not written down)",
        balance=f"${cat.balance_cents / 100:,.2f}",
        target=f"${cat.target_cents / 100:,.2f}" if cat.target_cents else "no set amount",
        alert=f"${cat.alert_at_cents / 100:,.2f}" if cat.alert_at_cents else "never",
        funding=funding_txt,
        matchers="; ".join(matchers) if matchers else "nothing matches it automatically",
        goals=_goals(db, user) or "(none stated)",
        others="\n".join(_describe_category(c) for c in others) or "  (none)",
        request=wanted,
    )

    res = await provider.chat(prompt, schema=REFINE_SCHEMA)
    return _validate_refine(_parse_json(res.content), cat, others)


_FUNDING_REQUIRED = {
    "percent": ("rate",),
    "fixed": ("amount_cents",),
    "refill_to": ("target_cents",),
    "conditional": ("when_op", "when_value_cents", "then_amount_cents"),
    "none": (),
}


def _validate_refine(
    raw: dict[str, Any], cat: Category, others: list[Category]
) -> dict[str, Any]:
    problems: list[str] = []
    notices: list[str] = []

    name = str(raw.get("name", "")).strip() or cat.name
    if any(o.name.lower() == name.lower() for o in others):
        problems.append(f"'{name}' is already another category's name")

    kind = raw.get("kind")
    if kind not in VALID_KINDS:
        notices.append(f"kind '{kind}' was not valid, kept '{cat.kind}'")
        kind = cat.kind

    f = raw.get("funding") or {}
    rule_type = f.get("rule_type")
    if rule_type not in _FUNDING_REQUIRED:
        notices.append(f"funding type '{rule_type}' was not valid and was dropped")
        rule_type, f = "none", {}
    else:
        absent = [k for k in _FUNDING_REQUIRED[rule_type] if f.get(k) in (None, "")]
        if absent:
            # Same failure the allocation experiment found: an operator
            # with no threshold. Blocking, because the engine cannot run it.
            problems.append(
                f"the funding rule is missing {', '.join(absent)} — "
                f"fill it in before applying"
            )

    return {
        "name": name,
        "kind": kind,
        "description": str(raw.get("description", "")).strip() or (cat.description or ""),
        "target_cents": _as_int(raw.get("target_cents")),
        "alert_at_cents": _as_int(raw.get("alert_at_cents")),
        "funding": {"rule_type": rule_type, **{k: v for k, v in f.items()
                                               if k != "rule_type"}},
        "add_matchers": [m for m in (raw.get("add_matchers") or [])
                         if (m.get("merchant_contains") or "").strip()],
        "what_changed": str(raw.get("what_changed", "")).strip(),
        "problems": problems,
        "notices": notices,
        "applicable": not problems,
    }


# --------------------------------------------------------------------------- #
# Writing a category's description
# --------------------------------------------------------------------------- #
DESCRIBE_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {
            "type": "string",
            "description": "one sentence on what this category is FOR",
        },
        "spend_criteria": {
            "type": "string",
            "description": "one sentence on what gets CHARGED here, "
                           "stated as a boundary: what belongs and what "
                           "belongs elsewhere",
        },
    },
    "required": ["description", "spend_criteria"],
}

DESCRIBE_PROMPT = """Write two short lines about a budget category.

  name:        {name}
  kind:        {kind}
  holds now:   {balance}
  full at:     {target}
  funded by:   {funding}
{spending}

Their other categories, so the line draws a boundary rather than
overlapping one:
{others}

Two separate things, and do not mix them:

`description` — what this category is FOR. Its purpose. One sentence.

`spend_criteria` — what gets CHARGED to it. This is the one that
decides where a purchase lands, so state the EDGE: what belongs here
and what belongs somewhere else instead. Name real merchants or payees
where you can, and a threshold if one applies. For example:
  "Groceries from a shop; eating out together is Date Fund."
  "Fuel at a pump only; Uber and Lyft are Date Fund."
  "Discretionary buys over $40 that are not food."

Do NOT restate the funding rule in `spend_criteria` — "whatever is left
over goes here" says nothing about what gets charged, and putting it
there makes filing WORSE.

In the user's voice, plainly. No preamble. Return JSON only."""


async def run_describe_category(
    db: Session, user: User, provider: Any, request: dict[str, Any]
) -> dict[str, Any]:
    """Suggest a description for one category.

    Deliberately small. A description is not load-bearing -- allocation
    is entirely split rules -- but it is what the assistant reads when
    writing summaries and advice, and it is what a second person reading
    the budget needs.
    """
    cat_id = (request.get("category_id") or "").strip()
    if not cat_id:
        raise ValueError("describe_category needs a category_id")
    try:
        pk = uuid.UUID(cat_id)
    except ValueError:
        raise ValueError("category_id is not a valid id") from None

    cat = db.scalars(
        select(Category).where(Category.id == pk, Category.user_id == user.id)
    ).first()
    if cat is None:
        raise ValueError("that category does not exist")

    # What has actually landed here says more than the name does.
    merchants = request.get("recent_merchants") or []
    spending = (
        "  recently: " + ", ".join(str(m) for m in merchants[:8])
        if merchants else ""
    )
    others = [c for c in _categories(db, user) if c.id != cat.id]

    prompt = DESCRIBE_PROMPT.format(
        name=cat.name, kind=cat.kind,
        balance=f"${cat.balance_cents / 100:,.2f}",
        target=f"${cat.target_cents / 100:,.2f}" if cat.target_cents else "no set amount",
        funding=request.get("funding_summary") or "not funded automatically",
        spending=spending,
        others="\n".join(_describe_category(c) for c in others) or "  (none)",
    )
    res = await provider.chat(prompt, schema=DESCRIBE_SCHEMA)
    raw = _parse_json(res.content)
    text = str(raw.get("description", "")).strip()
    criteria = str(raw.get("spend_criteria", "")).strip()

    notices: list[str] = []
    # Funding language in the field the categoriser reads is not WRONG
    # -- measured identically -- but it means the field is not doing its
    # job, because the user has written something that cannot decide
    # where a charge goes.
    if criteria and any(
        p in criteria.lower()
        for p in ("left over", "leftover", "remainder", "whatever is left",
                  "per paycheck", "each paycheck")
    ):
        notices.append(
            "The 'what gets charged here' line talks about funding rather "
            "than spending. Rewrite it as what belongs here, or filing "
            "will get worse rather than better."
        )

    return {
        "description": text,
        "spend_criteria": criteria,
        "applicable": bool(text or criteria),
        "problems": [] if (text or criteria)
                    else ["the assistant did not write anything"],
        "notices": notices,
    }


# --------------------------------------------------------------------------- #
# Suggesting a category for one charge
# --------------------------------------------------------------------------- #
#
# Measured in experiments/categorise-arms before being built. The short
# version: picking a label is a task this model can do (83% precision,
# ~1.2s), unlike dividing a paycheck (ADR 0006). Descriptions that name a
# payee or state a boundary are what make it work; descriptions that
# restate the name make it worse.
#
# It SUGGESTS. One wrong answer in that experiment came with high
# confidence, and a wrong auto-file is money in the wrong envelope that
# nobody notices, while a suggestion costs one tap to reject.
SUGGEST_PROMPT = """Which budget category should this charge come out of?

The charge:
  {merchant}
  {amount}

The categories:
{cats}

Pick the ONE category this charge belongs to.

If the merchant name does not tell you what was actually bought — a bare
payment processor, a transfer, a generic terminal string — answer
"{unsure}". A wrong guess puts money in the wrong envelope and is worse
than asking. "{unsure}" is a good answer when the name is genuinely
uninformative.

`matched_phrase`: if the category's description is what told you this
charge belongs there, copy the exact words from it. Copy them verbatim —
do not reword. If the merchant name alone was enough and the description
played no part, use an empty string.

Return JSON only."""

UNSURE = "UNSURE"


def _suggest_schema(names: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": names + [UNSURE]},
            "confidence": {"type": "string",
                           "enum": ["high", "medium", "low"]},
            "why": {"type": "string", "description": "one short sentence"},
            "matched_phrase": {
                "type": "string",
                "description": "copy the exact words from the chosen "
                               "category's description that decided it. "
                               "Empty string only if you went on the "
                               "merchant name alone.",
            },
        },
        # REQUIRED on purpose. A property a schema leaves optional is one
        # the model leaves out -- the same failure as the conditional
        # threshold in ADR 0006. Asked for it optionally, it cited
        # nothing even when the description was the only thing that
        # could have decided the answer.
        "required": ["category", "confidence", "why", "matched_phrase"],
    }


def _existing_matchers(db: Session, user: User, merchant: str) -> list[dict]:
    """Matcher rules that already claim this charge.

    Shown with the suggestion so a rule learned by accident can be
    undone where it misfires, rather than only from deep inside the
    category's settings. A matcher silently outranks the assistant, so
    the place to notice a bad one is the moment it is wrong.
    """
    from app.budget.rules import matches
    from app.db.models import CategoryRule

    low = merchant.lower()
    out: list[dict] = []
    rows = db.execute(
        select(CategoryRule, Category)
        .join(Category, Category.id == CategoryRule.category_id)
        .where(CategoryRule.user_id == user.id, CategoryRule.enabled.is_(True))
        .order_by(CategoryRule.priority)
    ).all()
    for rule, cat in rows:
        needle = (rule.matcher or {}).get("merchant_contains")
        if needle and str(needle).lower() in low:
            out.append({
                "rule_id": str(rule.id),
                "category_id": str(cat.id),
                "category_name": cat.name,
                "matches_on": str(needle),
            })
    return out


async def run_suggest_category(
    db: Session, user: User, provider: Any, request: dict[str, Any]
) -> dict[str, Any]:
    """Suggest where one charge belongs, and say what decided it."""
    merchant = (request.get("merchant") or "").strip()
    amount = request.get("amount_cents")
    if not merchant:
        raise ValueError("suggest_category needs a merchant")

    cats = [c for c in _categories(db, user) if not c.is_unallocated]
    if not cats:
        raise ValueError("there are no categories to choose from")

    lines = []
    for c in cats:
        line = f"  - {c.name} ({c.kind})"
        # `spend_criteria` only -- the spending boundary, which is the
        # half that can decide anything. Sending the funding half too
        # measured identically rather than worse, so this is tidiness
        # and prompt economy, not a correctness fix.
        if c.spend_criteria:
            line += f": {c.spend_criteria}"
        lines.append(line)

    names = [c.name for c in cats]
    prompt = SUGGEST_PROMPT.format(
        merchant=merchant,
        amount=f"${int(amount or 0) / 100:,.2f}",
        cats="\n".join(lines),
        unsure=UNSURE,
    )
    res = await provider.chat(prompt, schema=_suggest_schema(names))
    out = _validate_suggestion(_parse_json(res.content), cats, merchant)
    out["existing_matchers"] = _existing_matchers(db, user, merchant)
    return out


# Words too common to mean anything when found in a description.
_NOISE = {
    "the", "and", "for", "inc", "llc", "ltd", "co", "com", "store",
    "payment", "purchase", "pos", "debit", "credit", "card", "ach",
    "transfer", "online", "www", "usa", "us", "trip", "ride", "sq",
}


def _mentions(merchant: str, cats: list[Category]) -> list[dict[str, str]]:
    """Which descriptions actually name something in this charge.

    Computed here rather than asked of the model. Asked to cite its
    reasoning, the model reliably quotes when the merchant appears
    verbatim in a description and reliably does NOT when the
    description merely shifted its answer -- so its own account of what
    decided it cannot be trusted. This can be: it is a string match the
    user can check.

    The point is Neil's: you should be able to see whether a
    description you wrote is doing anything.
    """
    words = {
        w.strip(".,#*'\"").lower()
        for w in merchant.replace("*", " ").replace("/", " ").split()
    }
    words = {w for w in words if len(w) > 2 and w not in _NOISE
             and not w.isdigit()}

    hits: list[dict[str, str]] = []
    for c in cats:
        # Both, here: the user wants to know whether anything they wrote
        # names this merchant, wherever they wrote it.
        for field, text in (("spend_criteria", c.spend_criteria),
                            ("description", c.description)):
            if not text:
                continue
            low = text.lower()
            found = next((w for w in sorted(words) if w in low), None)
            if found:
                hits.append({"category_name": c.name, "word": found,
                             "field": field, "description": text})
                break
    return hits


def _validate_suggestion(
    raw: dict[str, Any], cats: list[Category], merchant: str = ""
) -> dict[str, Any]:
    picked = str(raw.get("category", "")).strip()
    by_name = {c.name: c for c in cats}

    mentions = _mentions(merchant, cats) if merchant else []

    if picked == UNSURE or picked not in by_name:
        return {
            "category_id": None, "category_name": None,
            "confidence": "low",
            "why": str(raw.get("why", "")).strip(),
            "matched_phrase": None, "matched_phrase_raw": None,
            "attribution_verified": False,
            "mentions": mentions,
            "chosen_description": None,
            "unsure": True,
        }

    cat = by_name[picked]
    phrase = str(raw.get("matched_phrase", "")).strip()

    # Check the quote is real. A model asked to cite will sometimes
    # produce a plausible paraphrase instead, and an attribution the
    # user cannot find in their own description is worse than none --
    # the whole point is to show which words are doing the work.
    source = cat.spend_criteria or cat.description or ""
    quoted = bool(
        phrase and source
        and phrase.lower().strip(' ."\'') in source.lower()
    )

    # A real quote is not the same as a relevant one. The model will
    # happily quote "eating out together, films, outings" at
    # SQ *MERCHANT 4821 -- the words are genuinely in the description
    # and say nothing about the charge. Checking only that the quote
    # EXISTS dressed three wrong answers up as evidence-backed during
    # the clean-slate walkthrough.
    #
    # So a quote counts only when it also names something in the charge.
    relevant = False
    if quoted and merchant:
        words = {
            w.strip(".,#*'\"").lower()
            for w in merchant.replace("*", " ").replace("/", " ").split()
        }
        words = {w for w in words if len(w) > 2 and w not in _NOISE
                 and not w.isdigit()}
        low_phrase = phrase.lower()
        relevant = any(w in low_phrase for w in words)
    verified = quoted and relevant

    return {
        "category_id": str(cat.id),
        "category_name": cat.name,
        "confidence": str(raw.get("confidence", "medium")),
        "why": str(raw.get("why", "")).strip(),
        "matched_phrase": phrase if verified else None,
        # Kept even when it fails verification: a paraphrase is useful
        # to see while tuning, and silently dropping it hides whether
        # the model is citing at all.
        "matched_phrase_raw": phrase or None,
        "attribution_verified": verified,
        # Deterministic, and the useful half: every description that
        # names something in this charge, whether or not the model said
        # so and whether or not it is the category it picked. A
        # description that keeps turning up here against the wrong
        # charges is one worth rewording.
        "mentions": mentions,
        "chosen_description": cat.spend_criteria or cat.description or None,
        "unsure": False,
    }


# --------------------------------------------------------------------------- #
# Reading a receipt
# --------------------------------------------------------------------------- #
RECEIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "merchant": {"type": "string"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string",
                                    "description": "the line as printed"},
                    "amount_cents": {
                        "type": "integer",
                        "description": "line total in CENTS. $4.99 is 499. "
                                       "If a quantity is shown, this is the "
                                       "total for all of them, not the unit "
                                       "price.",
                    },
                    "quantity": {"type": "integer"},
                },
                "required": ["description", "amount_cents"],
            },
        },
        "subtotal_cents": {"type": "integer"},
        "tax_cents": {"type": "integer"},
        "tip_cents": {"type": "integer"},
        "total_cents": {"type": "integer"},
    },
    # Everything required: a model omits what a schema leaves optional,
    # and a receipt missing its total cannot be reconciled.
    "required": ["merchant", "items", "subtotal_cents", "tax_cents",
                 "tip_cents", "total_cents"],
}

RECEIPT_PROMPT = """Read this receipt and list what was bought.

Every amount in CENTS as a whole number: $4.99 is 499, $12.00 is 1200.

One entry per line item as printed. If a line shows a quantity, give the
TOTAL for that line, not the unit price, and put the count in quantity.

Separate out tax and tip rather than folding them into items. Use 0 when
a receipt has none. Do not invent a tip that is not printed.

Leave discounts and coupons as their own items with a NEGATIVE
amount_cents — they are part of what makes the lines add up.

Return JSON only."""

TEXT_RECEIPT_PROMPT = RECEIPT_PROMPT + "\n\nThe receipt:\n\n{text}"

# Used when the deterministic pass has already found the structure, which
# is the normal case for OCR text. The model is handed only the candidate
# item rows and told what they must add up to, instead of being asked to
# work out which lines are purchases -- the judgement it got wrong on the
# first real receipt. See app/ai/receipt_text.py.
STRUCTURED_RECEIPT_PROMPT = """Read these receipt lines and list what was bought.

Every amount in CENTS as a whole number: $4.99 is 499, $12.00 is 1200.

Each line is one purchase, printed as: description, then a product code,
then the price. Take the PRICE, never the product code — a long run of
digits with no decimal point is a barcode, not money.

{targets}
Some rows may have been joined together or split by the scan. If one
line clearly holds two purchases, return both. If a line has no price,
still return it with amount_cents 0 rather than dropping it.

Totals, tax, card and terminal lines have already been removed. Every
line below is something that was bought.

The lines:

{text}

Return JSON only."""


def _targets_line(st: Any) -> str:
    """Tell the model what the receipt says it should find."""
    bits = []
    if st.item_count is not None:
        bits.append(f"The receipt says {st.item_count} items were sold.")
    if st.subtotal_cents is not None:
        bits.append(
            f"They must add up to {st.subtotal_cents} cents "
            f"(${st.subtotal_cents / 100:,.2f}) before tax."
        )
    if st.orphan_prices:
        shown = ", ".join(str(p) for p in st.orphan_prices)
        bits.append(
            f"These prices were read with no item beside them and belong to "
            f"a line that is missing one: {shown} cents."
        )
    return ("\n".join(bits) + "\n") if bits else ""


async def run_receipt_parse(
    db: Session, user: User, provider: Any, request: dict[str, Any]
) -> dict[str, Any]:
    """Turn a receipt photo or pasted text into line items.

    Reading a receipt is the kind of work a vision model is genuinely
    good at -- transcription, not judgement. What the items MEAN is
    decided afterwards, one at a time, by the same categorise path that
    already exists and was measured.
    """
    image_b64 = (request.get("image_b64") or "").strip()
    text = (request.get("text") or "").strip()
    if not image_b64 and not text:
        raise ValueError("give the receipt as an image or as text")

    expected = _as_int(request.get("expected_cents"))

    if image_b64:
        res = await provider.chat(RECEIPT_PROMPT, images=[image_b64],
                                  schema=RECEIPT_SCHEMA)
        return _validate_receipt(_parse_json(res.content), expected_cents=expected)

    # Text path: read the structure first, deterministically.
    structure = receipt_text.extract(text)
    if structure.item_lines:
        prompt = STRUCTURED_RECEIPT_PROMPT.format(
            targets=_targets_line(structure),
            text="\n".join(structure.item_lines)[:8000],
        )
    else:
        # Nothing looked like an item -- a handwritten slip, or a layout
        # the patterns do not know. Fall back rather than return nothing.
        prompt = TEXT_RECEIPT_PROMPT.format(text=text[:8000])
        structure = None

    res = await provider.chat(prompt, schema=RECEIPT_SCHEMA)
    out = _validate_receipt(_parse_json(res.content), expected_cents=expected)

    if structure is not None:
        # The receipt's own figures beat anything inferred from the
        # items, so they overwrite rather than supplement.
        if structure.subtotal_cents is not None:
            out["subtotal_cents"] = structure.subtotal_cents
        if structure.tax_cents is not None:
            out["tax_cents"] = structure.tax_cents
        if structure.total_cents is not None:
            out["total_cents"] = structure.total_cents
        out["notices"] = (
            receipt_text.reconcile(out.get("items") or [], structure)
            + list(out.get("notices") or [])
        )
        out["structure"] = structure.as_dict()

    return out


def _validate_receipt(
    raw: dict[str, Any], expected_cents: int | None = None
) -> dict[str, Any]:
    """Check the receipt adds up, and say so when it does not.

    An OCR slip of one digit is both easy to make and easy to miss, and
    the whole point of itemising is that the parts equal the whole. The
    arithmetic is done here rather than trusted from the model.
    """
    items: list[dict[str, Any]] = []
    for it in raw.get("items") or []:
        desc = str(it.get("description", "")).strip()
        amount = _as_signed_int(it.get("amount_cents"))
        if not desc or amount is None:
            continue
        items.append({
            "description": desc,
            "amount_cents": amount,
            "quantity": _as_int(it.get("quantity")) or 1,
        })

    tax = _as_int(raw.get("tax_cents")) or 0
    tip = _as_int(raw.get("tip_cents")) or 0
    total = _as_int(raw.get("total_cents")) or 0
    items_sum = sum(i["amount_cents"] for i in items)

    problems: list[str] = []
    notices: list[str] = []

    if not items:
        problems.append("no line items could be read from this")

    # What the receipt says about itself.
    computed = items_sum + tax + tip
    if total and computed != total:
        notices.append(
            f"The lines add up to ${computed / 100:,.2f} but the receipt "
            f"says ${total / 100:,.2f}. Check the amounts before filing."
        )

    # What the bank says, which wins if they disagree.
    if expected_cents is not None and total and total != expected_cents:
        notices.append(
            f"The receipt total is ${total / 100:,.2f} but the charge was "
            f"${expected_cents / 100:,.2f}. A tip added later would explain "
            f"the difference."
        )

    return {
        "merchant": str(raw.get("merchant", "")).strip(),
        "items": items,
        "subtotal_cents": _as_int(raw.get("subtotal_cents")) or items_sum,
        "tax_cents": tax,
        "tip_cents": tip,
        "total_cents": total or computed,
        "items_sum_cents": items_sum,
        "expected_cents": expected_cents,
        "problems": problems,
        "notices": notices,
        "applicable": not problems,
    }


# --------------------------------------------------------------------------- #
# Categorising a receipt's items, all at once
# --------------------------------------------------------------------------- #
ITEMS_PROMPT = """Decide which budget category each line of a receipt
belongs to.

Bought at: {merchant}

The categories:
{cats}

The lines:
{items}

Give one answer per line, in the same order, using the line numbers
shown. Most lines from one shop usually belong to the same category —
say so rather than inventing variety. Use "{unsure}" for a line whose
category is genuinely unclear.

Tax and tip are not listed here; they are handled separately.

Return JSON only."""


def _items_schema(names: list[str], count: int) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "lines": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "line": {"type": "integer",
                                 "description": "the line number shown"},
                        "category": {"type": "string",
                                     "enum": names + [UNSURE]},
                    },
                    "required": ["line", "category"],
                },
            },
        },
        "required": ["lines"],
    }


async def run_categorise_items(
    db: Session, user: User, provider: Any, request: dict[str, Any]
) -> dict[str, Any]:
    """Decide which category each receipt line belongs to.

    Memory first. A line filed before at this shop is not a question --
    supermarket slips print truncated codes like COTTAGE CH and
    GV MXDVG 12Z, which a model cannot reliably reason about and which
    only ever need teaching once. Asking again every shop is the thing
    that makes itemising tedious.

    Only the remainder goes to the model, so a familiar receipt is also
    faster and may not need the inference host at all.
    """
    from app.budget import item_memory

    items = request.get("items") or []
    if not items:
        raise ValueError("categorise_items needs items")
    merchant = (request.get("merchant") or "").strip()

    cats = [c for c in _categories(db, user) if not c.is_unallocated]
    if not cats:
        raise ValueError("there are no categories to choose from")
    by_id = {str(c.id): c for c in cats}

    descriptions = [str(it.get("description", "")) for it in items]
    known = item_memory.recall(db, user.id, merchant, descriptions)

    assigned: dict[int, dict[str, Any]] = {}
    unknown: list[tuple[int, dict[str, Any]]] = []
    for n, it in enumerate(items, 1):
        rec = known.get(str(it.get("description", "")))
        cat = by_id.get(str(rec.category_id)) if rec else None
        if rec is not None and cat is not None:
            assigned[n] = {
                "category_id": str(cat.id),
                "category_name": cat.name,
                "source": "memory",
                "memory_scope": rec.source,
                "times_seen": rec.times_seen,
                "expense_type_id": (
                    str(rec.expense_type_id) if rec.expense_type_id else None
                ),
            }
        else:
            unknown.append((n, it))

    model_name = None
    if unknown:
        # Renumber 1..N for the model. Memory may have removed lines
        # from the middle, and showing it "1, 4, 7" invites answers
        # against numbers the validator would reject. `unknown` keeps
        # the mapping back to the receipt's real line numbers.
        shown = "\n".join(
            f"  {shown_n}. {it.get('description', '')}  "
            f"${int(it.get('amount_cents') or 0) / 100:,.2f}"
            for shown_n, (_, it) in enumerate(unknown, 1)
        )
        prompt = ITEMS_PROMPT.format(
            merchant=merchant or "(unknown)",
            cats="\n".join(
                f"  - {c.name}"
                + (f": {c.spend_criteria}" if c.spend_criteria else "")
                for c in cats
            ),
            items=shown,
            unsure=UNSURE,
        )
        res = await provider.chat(
            prompt, schema=_items_schema([c.name for c in cats], len(unknown))
        )
        model_name = res.model
        picked = _validate_item_categories(
            _parse_json(res.content), cats, len(unknown)
        )
        for row in picked["lines"]:
            shown_n = int(row.get("line") or 0)
            if not (1 <= shown_n <= len(unknown)):
                continue
            if row.get("category_id"):
                real_line = unknown[shown_n - 1][0]
                assigned[real_line] = {
                    "category_id": row["category_id"],
                    "category_name": row["category_name"],
                    "source": "model",
                }

    return {
        "lines": [
            {"line": n, **assigned.get(n, {
                "category_id": None, "category_name": None, "source": None})}
            for n in range(1, len(items) + 1)
        ],
        "undecided": [n for n in range(1, len(items) + 1) if n not in assigned],
        "from_memory": sum(
            1 for v in assigned.values() if v.get("source") == "memory"
        ),
        "asked_model": len(unknown),
        "model": model_name,
    }


def _validate_item_categories(
    raw: dict[str, Any], cats: list[Category], count: int
) -> dict[str, Any]:
    """Map the model's answers back onto line numbers.

    A missing or unrecognised line is left undecided rather than
    guessed: the review screen shows it as needing a choice, which is
    the honest outcome and costs one tap.
    """
    by_name = {c.name: c for c in cats}
    assigned: dict[int, dict[str, str]] = {}

    for row in raw.get("lines") or []:
        idx = _as_int(row.get("line"))
        name = str(row.get("category", "")).strip()
        if idx is None or not (1 <= idx <= count):
            continue
        cat = by_name.get(name)
        if cat is None:
            continue
        assigned[idx] = {"category_id": str(cat.id), "category_name": cat.name}

    return {
        "lines": [
            {"line": i, **assigned.get(i, {"category_id": None,
                                           "category_name": None})}
            for i in range(1, count + 1)
        ],
        "undecided": [i for i in range(1, count + 1) if i not in assigned],
    }


# --------------------------------------------------------------------------- #
# Expense type: what a charge WAS
# --------------------------------------------------------------------------- #
# A second, separate question from "which envelope does this come out
# of". The envelope is about funding -- Date Fund, Discretionary -- and
# the same burrito comes out of a different one depending on who was
# there. The type is about the purchase itself, and is what makes "how
# much on fast food" answerable at all.
#
# Same model, same shape, same 83%-ish job as picking a category, and
# the same rule: it suggests, nothing files itself silently.
TYPE_PROMPT = """What KIND of spending is this charge?

This is not about which budget envelope pays for it. It is about what
was actually bought, so that spending can be totalled by kind later.

The charge:
  {merchant}
  {amount}{item}

Kinds already in use, and standard ones available:
{types}

Pick the ONE kind that fits best. Prefer a kind marked "in use" over an
equivalent one marked "available" — two names for the same thing split
every total between them.

Only answer "{new}" when nothing on the list is a reasonable fit, and
then give a short, general name that FUTURE charges could also use.
"Chipotle" is a bad new kind; "Fast food" is a good one.

Answer "{unsure}" when the merchant name does not say what was bought.

Return JSON only."""

NEW_TYPE = "NEW"


def _type_schema(names: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "expense_type": {"type": "string", "enum": names + [NEW_TYPE, UNSURE]},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "new_name": {
                "type": "string",
                "description": f"a short general name, ONLY when expense_type is "
                               f"{NEW_TYPE}; otherwise an empty string",
            },
            "new_description": {
                "type": "string",
                "description": f"one line saying what belongs in the new kind, ONLY "
                               f"when expense_type is {NEW_TYPE}; otherwise empty",
            },
            "why": {"type": "string", "description": "one short sentence"},
        },
        # All required. An optional property is one the model omits --
        # the lesson from the conditional threshold and from
        # matched_phrase. Unused ones come back as empty strings.
        "required": ["expense_type", "confidence", "new_name", "new_description", "why"],
    }


async def run_classify_expense_type(
    db: Session, user: User, provider: Any, request: dict[str, Any]
) -> dict[str, Any]:
    """Say what kind of spending a charge is, coining a kind if needed."""
    from app.analytics import taxonomy

    merchant = (request.get("merchant") or "").strip()
    if not merchant:
        raise ValueError("classify_expense_type needs a merchant")
    amount = request.get("amount_cents")
    item = (request.get("item_description") or "").strip()

    offered = taxonomy.suggestions_for_prompt(db, user.id)
    lines = "\n".join(
        f"  - {o['name']} [{'in use' if o['status'] == 'existing' else 'available'}]"
        + (f": {o['description']}" if o["description"] else "")
        for o in offered
    )
    names = [o["name"] for o in offered]

    prompt = TYPE_PROMPT.format(
        merchant=merchant,
        amount=f"${int(amount or 0) / 100:,.2f}",
        item=f"\n  line item: {item}" if item else "",
        types=lines,
        new=NEW_TYPE,
        unsure=UNSURE,
    )
    res = await provider.chat(prompt, schema=_type_schema(names))
    out = _validate_expense_type(_parse_json(res.content), offered)
    out["model"] = res.model
    return out


def _validate_expense_type(
    raw: dict[str, Any], offered: list[dict[str, str]]
) -> dict[str, Any]:
    """Check the answer before anyone acts on it.

    Nothing is created here. The handler returns a PROPOSAL; the route
    that applies it is what calls taxonomy.resolve_or_create, so a
    suggestion the user never accepts cannot grow the taxonomy.
    """
    from app.analytics.taxonomy import MAX_NAME, slugify

    picked = str(raw.get("expense_type", "")).strip()
    by_name = {o["name"]: o for o in offered}
    base = {
        "confidence": str(raw.get("confidence", "low")).strip().lower(),
        "why": str(raw.get("why", "")).strip(),
    }

    if picked == NEW_TYPE:
        name = str(raw.get("new_name", "")).strip()
        if not name or len(name) > MAX_NAME:
            # Asked to coin and unable to, which is a refusal in
            # disguise. Treat it as one.
            return {**base, "expense_type_name": None, "is_new": False, "unsure": True}

        # The model is told to prefer an existing kind and still
        # proposes near-synonyms. Catching the collision here is what
        # keeps "Fast food" and "Fast Foods" from both existing.
        slug = slugify(name)
        for o in offered:
            if slugify(o["name"]) == slug:
                return {
                    **base,
                    "expense_type_name": o["name"],
                    "is_new": o["status"] != "existing",
                    "new_description": o["description"] or None,
                    "unsure": False,
                    "collapsed_from": name,
                }
        return {
            **base,
            "expense_type_name": name,
            "is_new": True,
            "new_description": str(raw.get("new_description", "")).strip() or None,
            "unsure": False,
        }

    if picked == UNSURE or picked not in by_name:
        return {**base, "expense_type_name": None, "is_new": False, "unsure": True}

    chosen = by_name[picked]
    return {
        **base,
        "expense_type_name": chosen["name"],
        # An "available" standard name does not exist as a row yet, so
        # accepting it still creates one.
        "is_new": chosen["status"] != "existing",
        "new_description": chosen["description"] or None,
        "unsure": False,
    }


# --------------------------------------------------------------------------- #
# Asking a question about your own spending
# --------------------------------------------------------------------------- #
# The model COMPILES the question into a query. It does not answer it.
#
# This is ADR 0006 applied to reporting. There, the model could describe
# a split but not perform one; here it can work out that "how much on
# fast food last month" means sum / type=Fast food / previous calendar
# month, but it cannot add up forty transactions, and asked to try it
# will produce a confident, plausible, wrong total. A wrong total is
# worse than no answer: there is nothing in the sentence to show it is
# wrong.
#
# So: model picks the filter, SQL computes the number, model writes the
# sentence around a number it was handed. Every figure the user reads
# came out of the database.
COMPILE_PROMPT = """Turn a question about someone's own spending into a query.

You are NOT answering the question. You are choosing the filters that
will be run against their transactions. Do not state or estimate any
amount.

Today is {today}.

Kinds of spending they use:
{types}

Their budget envelopes:
{cats}

The question:
  {question}

Choose:
  metric    — "spend" for how much went out, "income" for money in,
              "net" for the difference, "count" for how many times,
              "average" for the typical amount per transaction.
  period    — the stretch of time asked about. "custom" only if a
              specific range is named, and then fill start and end.
  group_by  — "expense_type" if they asked for a breakdown by kind,
              "category" by envelope, "merchant" by shop, "month" or
              "week" for a trend over time, "none" for a single total.
  expense_types — kinds to filter to, copied EXACTLY from the list
              above. Empty when the question is about everything.
  categories — envelopes to filter to, copied exactly. Usually empty.
  merchant  — a shop name to match on, when they named a specific
              place rather than a kind of spending. Otherwise empty.

If the question names something that is not on either list, put it in
`merchant` rather than inventing a kind.

Return JSON only."""

_PERIOD_CHOICES = [
    "today", "yesterday", "this_week", "last_week", "this_month", "last_month",
    "last_7_days", "last_14_days", "last_30_days", "last_90_days",
    "last_3_months", "last_6_months", "last_12_months",
    "this_year", "last_year", "all_time", "custom",
]


def _compile_schema(type_names: list[str], cat_names: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "metric": {"type": "string",
                       "enum": ["spend", "income", "net", "count", "average"]},
            "period": {"type": "string", "enum": _PERIOD_CHOICES},
            "start": {"type": "string",
                      "description": "YYYY-MM-DD, only when period is custom, "
                                     "else empty string"},
            "end": {"type": "string",
                    "description": "YYYY-MM-DD, only when period is custom, "
                                   "else empty string"},
            "group_by": {"type": "string",
                         "enum": ["none", "expense_type", "category",
                                  "merchant", "month", "week"]},
            "expense_types": {"type": "array",
                              "items": {"type": "string", "enum": type_names}},
            "categories": {"type": "array",
                           "items": {"type": "string", "enum": cat_names}},
            "merchant": {"type": "string",
                         "description": "a shop name, or an empty string"},
        },
        "required": ["metric", "period", "start", "end", "group_by",
                     "expense_types", "categories", "merchant"],
    }


PHRASE_PROMPT = """Answer the question using ONLY the figures below.

The question:
  {question}

What the database returned:
{facts}

Write two or three short sentences. Lead with the number they asked
for. If a comparison figure is shown, say which way it moved.

Every amount you write must appear above. Do not add up, work out or
estimate anything that is not already there — if a figure is not
listed, it is not known, and saying so is correct. No preamble, no
advice unless it follows directly from the numbers."""


async def run_ask(
    db: Session, user: User, provider: Any, request: dict[str, Any]
) -> dict[str, Any]:
    """Answer a plain-English question with figures computed in SQL."""
    from datetime import date as _date

    from app.analytics import queries as Q
    from app.analytics import taxonomy
    from app.analytics.period import PeriodError, resolve

    question = (request.get("question") or "").strip()
    if not question:
        raise ValueError("ask requires a question")

    today = _date.fromisoformat(request["today"]) if request.get("today") else _date.today()

    types = taxonomy.active(db, user.id)
    cats = [c for c in _categories(db, user)]
    type_names = [t.name for t in types]
    cat_names = [c.name for c in cats]

    prompt = COMPILE_PROMPT.format(
        today=today.isoformat(),
        question=question,
        types="\n".join(
            f"  - {t.name}" + (f": {t.description}" if t.description else "")
            for t in types
        ) or "  (none yet)",
        cats="\n".join(f"  - {c.name}" for c in cats) or "  (none yet)",
    )
    res = await provider.chat(prompt, schema=_compile_schema(type_names, cat_names))
    raw = _parse_json(res.content)

    # --- resolve the compiled spec against real rows -------------------
    period_key = str(raw.get("period") or "this_month")
    spec: Any = period_key
    if period_key == "custom":
        spec = {"start": raw.get("start"), "end": raw.get("end")}
    try:
        period = resolve(spec, today=today)
    except (PeriodError, ValueError):
        # A period we cannot parse is the one failure that must not be
        # papered over: answering for the wrong month looks identical
        # to answering for the right one.
        period = resolve("this_month", today=today)
        unparsed_period = True
    else:
        unparsed_period = False

    by_type_slug = {taxonomy.slugify(t.name): t for t in types}
    by_cat_name = {c.name.lower(): c for c in cats}

    wanted_types, wanted_cats, unresolved = [], [], []
    for n in raw.get("expense_types") or []:
        t = by_type_slug.get(taxonomy.slugify(str(n)))
        (wanted_types.append(t.id) if t else unresolved.append(str(n)))
    for n in raw.get("categories") or []:
        c = by_cat_name.get(str(n).strip().lower())
        (wanted_cats.append(c.id) if c else unresolved.append(str(n)))

    group_by = str(raw.get("group_by") or "none")
    q = Q.SpendQuery(
        period=period,
        metric=str(raw.get("metric") or "spend"),  # type: ignore[arg-type]
        group_by=None if group_by == "none" else group_by,  # type: ignore[arg-type]
        expense_type_ids=tuple(wanted_types),
        category_ids=tuple(wanted_cats),
        merchant_contains=(str(raw.get("merchant") or "").strip() or None),
        limit=12,
    )
    result = Q.run(db, user.id, q)

    # --- let the model put the computed figures into words -------------
    answer = _fallback_sentence(result)
    try:
        phrased = await provider.chat(
            PHRASE_PROMPT.format(question=question, facts=_facts_block(result))
        )
        text = phrased.content.strip()
        if text:
            answer = text
    except Exception as exc:  # noqa: BLE001
        # The numbers are already correct; only the prose failed. A
        # plain sentence beats failing the whole question.
        log.warning("phrasing failed, using the plain sentence: %s", exc)

    return {
        "question": question,
        "answer": answer,
        # Returned so the UI can show WHAT was actually asked of the
        # data. A question the model read differently than the user
        # meant is invisible otherwise -- the wrong answer looks the
        # same as the right one.
        "query": {
            "metric": q.metric,
            "period": period.as_dict(),
            "group_by": q.group_by,
            "expense_types": [t.name for t in types if t.id in set(wanted_types)],
            "categories": [c.name for c in cats if c.id in set(wanted_cats)],
            "merchant": q.merchant_contains,
        },
        "result": result.as_dict(),
        "unresolved_filters": unresolved,
        "period_not_understood": unparsed_period,
        "model": res.model,
    }


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _facts_block(result: Any) -> str:
    """The computed result, written out for the phrasing call.

    Deliberately prose-ish rather than JSON: the model is being asked to
    read numbers out, and JSON invites it to restructure them.
    """
    lines = [
        f"  period: {result.period.label} "
        f"({result.period.start} to {result.period.end})",
        f"  {result.metric}: {_money(result.total_cents)}",
        f"  transactions: {result.count}",
    ]
    if result.previous_total_cents is not None and result.previous_period:
        d = result.delta_cents or 0
        lines.append(
            f"  same figure for {result.previous_period.label}: "
            f"{_money(result.previous_total_cents)} "
            f"({'up' if d > 0 else 'down' if d < 0 else 'unchanged'} "
            f"{_money(abs(d))})"
        )
    for g in result.groups:
        lines.append(f"  {g.label}: {_money(g.cents)} over {g.count} transactions")
    return "\n".join(lines)


def _fallback_sentence(result: Any) -> str:
    """What to say when the phrasing call fails. Always true, never pretty."""
    verb = {"spend": "Spent", "income": "Received", "net": "Net",
            "count": "Transactions:", "average": "Average"}.get(result.metric, "Total")
    if result.metric == "count":
        return f"{verb} {result.count} in {result.period.label}."
    return (
        f"{verb} {_money(result.total_cents)} in {result.period.label}, "
        f"across {result.count} transaction{'s' if result.count != 1 else ''}."
    )


# --------------------------------------------------------------------------- #
# Narrating the insights screen
# --------------------------------------------------------------------------- #
NARRATE_PROMPT = """Explain what these figures say about someone's month.

{facts}

Write at most four short sentences, in plain words, addressed to them.
Say what changed and what is worth noticing. Every number you use must
appear above — do not calculate anything new, including percentages or
totals that are not listed.

Lead with whatever is most worth their attention, not with the largest
number. No greeting, no sign-off, no generic budgeting advice."""


async def run_narrate_insights(
    db: Session, user: User, provider: Any, request: dict[str, Any]
) -> dict[str, Any]:
    """Turn the computed insight cards into a short plain-English read.

    The cards arrive already calculated. This step adds language, never
    arithmetic -- which is why it can safely run on the small local
    model.
    """
    from datetime import date as _date

    from app.analytics import insights
    from app.analytics.period import resolve

    today = _date.fromisoformat(request["today"]) if request.get("today") else _date.today()
    period = resolve(request.get("period") or "this_month", today=today)
    cards = insights.build(db, user.id, period=period, today=today)

    res = await provider.chat(NARRATE_PROMPT.format(facts=_narrate_facts(cards)))
    return {
        "period": period.as_dict(),
        "narrative": res.content.strip(),
        "cards": cards,
        "model": res.model,
    }


def _narrate_facts(cards: list[dict[str, Any]]) -> str:
    out: list[str] = []
    for c in cards:
        kind = c.get("kind")
        if kind == "flow":
            out.append(
                f"In: {_money(c['in_cents'])}. Out: {_money(c['out_cents'])}. "
                f"Net: {_money(c['net_cents'])} over {c['txn_count']} transactions."
            )
            if c.get("previous_out_cents") is not None:
                out.append(f"Spending last period: {_money(c['previous_out_cents'])}.")
        elif kind == "by_type":
            out.append("Spending by kind:")
            out += [
                f"  {g['label']}: {_money(g['cents'])} "
                f"(was {_money(g['previous_cents'])})"
                for g in c["groups"][:8]
            ]
        elif kind == "movers":
            out.append("Biggest changes:")
            out += [
                f"  {i['label']}: {_money(i['now_cents'])}, "
                f"{'up' if i['delta_cents'] > 0 else 'down'} "
                f"{_money(abs(i['delta_cents']))}"
                for i in c["items"]
            ]
        elif kind == "recurring":
            out.append(
                f"{c['count']} repeating charges costing {_money(c['monthly_cents'])} "
                f"a month."
            )
            out += [
                f"  {i['merchant']}: {_money(i['typical_cents'])} {i['cadence']}"
                for i in c["items"][:6]
            ]
            out += [f"  {i['merchant']} appears to have stopped." for i in c.get("ended", [])]
        elif kind == "unusual":
            out += [
                f"{i['label']} is {_money(i['cents'])}, against a usual "
                f"{_money(i['typical_cents'])}."
                for i in c["items"]
            ]
        elif kind == "envelopes":
            over = [i for i in c["items"] if i["overspent"]]
            out += [
                f"{i['name']}: funded {_money(i['funded_cents'])}, "
                f"spent {_money(i['spent_cents'])}."
                for i in over[:5]
            ]
        elif kind == "pace":
            out.append(
                f"{c['days_elapsed']} of {c['days_total']} days in, "
                f"{_money(c['spent_cents'])} spent, on course for "
                f"{_money(c['projected_cents'])}."
            )
        elif kind == "top_merchants":
            out += [
                f"{g['label']}: {_money(g['cents'])} ({g['share_pct']}% of spending)"
                for g in c["items"][:4]
            ]
        elif kind == "untyped":
            out.append(
                f"{_money(c['cents'])} ({c['share_pct']}% of spending) has no kind "
                f"assigned, so the breakdown above is incomplete."
            )
    return "\n".join(out) or "No spending recorded."
