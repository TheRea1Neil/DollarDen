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
    if v is None:
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


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
            "description": "one sentence saying what belongs in this "
                           "category and what does not",
        },
    },
    "required": ["description"],
}

DESCRIBE_PROMPT = """Write one sentence describing what a budget category
is for.

  name:        {name}
  kind:        {kind}
  holds now:   {balance}
  full at:     {target}
  funded by:   {funding}
{spending}

Their other categories, so the line draws a boundary rather than
overlapping one:
{others}

Write it in the user's voice, plainly, as if they wrote it themselves.
Say what belongs here and — where it is not obvious — what does not,
because the useful part of a description is usually the edge. For
example: "Groceries from a shop; eating out comes out of Fun."

One sentence. No preamble. Return JSON only."""


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
    return {
        "description": text,
        "applicable": bool(text),
        "problems": [] if text else ["the assistant did not write anything"],
        "notices": [],
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
        if c.description:
            line += f": {c.description}"
        lines.append(line)

    names = [c.name for c in cats]
    prompt = SUGGEST_PROMPT.format(
        merchant=merchant,
        amount=f"${int(amount or 0) / 100:,.2f}",
        cats="\n".join(lines),
        unsure=UNSURE,
    )
    res = await provider.chat(prompt, schema=_suggest_schema(names))
    return _validate_suggestion(_parse_json(res.content), cats, merchant)


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
        if not c.description:
            continue
        low = c.description.lower()
        for w in sorted(words):
            if w in low:
                hits.append({"category_name": c.name, "word": w,
                             "description": c.description})
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
    verified = bool(
        phrase
        and cat.description
        and phrase.lower().strip(' ."\'') in cat.description.lower()
    )

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
        "chosen_description": cat.description or None,
        "unsure": False,
    }
