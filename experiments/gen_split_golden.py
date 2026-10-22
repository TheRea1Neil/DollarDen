#!/usr/bin/env python
"""Generate golden split outputs for the Dart port to match.

The Flutter app carries its own copy of the split engine so a paycheck
preview can recalculate locally as the user drags categories around.
Two implementations of money logic drift unless something holds them
together; this writes the fixtures that do.

    backend/.venv/bin/python experiments/gen_split_golden.py \
        > app/test/split_golden.json

Then run `cd app && flutter test test/split_engine_test.dart`.
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "backend"))

from app.budget.split import RuleType, SplitPlanError, SplitRule, apply_split

cases: list[dict] = []


def case(name: str, gross: int, rules: list[tuple], balances: dict) -> None:
    spec = [dict(category_id=c, rule_type=t, params=p) for c, t, p in rules]
    try:
        res = apply_split(
            gross,
            [SplitRule(c, i + 1, RuleType(t), p, True)
             for i, (c, t, p) in enumerate(rules)],
            dict(balances),
        )
    except SplitPlanError as exc:
        # The port must refuse exactly what the server refuses.
        cases.append(dict(name=name, gross=gross, balances=balances,
                          rules=spec, error=str(exc)))
        return

    out: dict[str, int] = {}
    for a in res.allocations:
        out[a.category_id] = out.get(a.category_id, 0) + a.amount_cents
    cases.append(dict(
        name=name, gross=gross, balances=balances, rules=spec,
        expect=dict(allocations=out, remaining=res.remaining_cents,
                    unfunded=[[c, s] for c, s in res.unfunded]),
    ))


PCT = ("tithe", "percent", {"rate": "0.10", "of": "gross"})
RENT = ("rent", "fixed", {"amount_cents": 35000})
FOOD = ("food", "refill_to", {"target_cents": 30000})
REM = ("rest", "remainder", {})
COND = ("ins", "conditional", {
    "when": {"field": "balance", "op": "<", "value_cents": 45000},
    "then": {"rule_type": "fixed", "params": {"amount_cents": 5000}},
})

case("typical", 200000, [PCT, RENT, FOOD, REM], {"food": 12000})
case("odd cents rounding", 183733, [PCT, RENT, FOOD, REM], {"food": 29999})
case("half-up at exactly .5", 100005, [PCT, REM], {})
case("refill already full", 200000, [PCT, RENT, FOOD, REM], {"food": 35000})
case("refill with a cap", 200000,
     [("food", "refill_to",
       {"target_cents": 50000, "max_contribution_cents": 10000}), REM], {})
case("runs out mid-plan", 40000, [PCT, RENT, FOOD, REM], {})
case("exact fit, zero remainder", 35000, [RENT, REM], {})
case("percent of remaining", 100000,
     [RENT, ("house", "percent", {"rate": "0.60", "of": "remaining"}), REM], {})
case("conditional fires", 200000, [COND, REM], {"ins": 40000})
case("conditional does not fire", 200000, [COND, REM], {"ins": 46000})
case("conditional on another category", 200000,
     [("ins", "conditional",
       {"when": {"field": "balance", "op": ">", "value_cents": 100,
                 "category_id": "food"},
        "then": {"rule_type": "fixed", "params": {"amount_cents": 5000}}}),
      REM], {"food": 500})
case("zero gross", 0, [RENT, REM], {})
case("overspent category refills from negative", 200000, [FOOD, REM],
     {"food": -23266})

# Structural failures: refusing these identically matters as much as the
# arithmetic, because a preview that shows an impossible plan is a lie.
case("no remainder", 200000, [RENT], {})
case("two remainders", 200000, [REM, ("other", "remainder", {})], {})
case("remainder not last", 200000, [REM, RENT], {})
case("conditional with no threshold", 200000,
     [("ins", "conditional",
       {"when": {"field": "balance", "op": "<"},
        "then": {"rule_type": "fixed", "params": {"amount_cents": 5000}}}),
      REM], {})

json.dump(cases, sys.stdout, indent=1)
