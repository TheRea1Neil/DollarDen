"""Shared scenario definitions and ground truth for the allocation experiment.

Ground truth comes from the real split engine. That is defensible rather
than circular: the engine provably conserves money (integer cents, exact
remainder) and provably honours order, and those two properties are the
thing under test. Where a scenario has genuine judgement in it, it is
flagged so the scoring does not pretend otherwise.
"""
from __future__ import annotations

import pathlib

import sys
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent.parent / "backend"))

from app.budget.split import RuleType, SplitRule, apply_split

# Neil's actual budget intent, stated once in three forms:
#   - as prose descriptions (what arm B gets)
#   - as JSON rules        (what arm A executes)
# The prose must express exactly the same intent, or the comparison is
# rigged.
CATEGORIES = [
    dict(
        key="tithing", name="Tithing", kind="obligation",
        description="Ten percent of my gross pay, taken first, before "
                    "anything else is funded.",
        rule=("percent", {"rate": "0.10", "of": "gross"}),
    ),
    dict(
        key="rent", name="Rent", kind="obligation",
        description="A flat $350 every paycheck for rent. Always the same "
                    "amount.",
        rule=("fixed", {"amount_cents": 35000}),
    ),
    dict(
        key="health", name="Health", kind="obligation",
        description="A flat $150 every paycheck for health costs.",
        rule=("fixed", {"amount_cents": 15000}),
    ),
    dict(
        key="carins", name="Car Insurance", kind="obligation",
        description="$50 per paycheck, but ONLY while this category holds "
                    "less than $450. Once it reaches $450 it gets nothing "
                    "until it drops below again.",
        rule=("conditional", {
            "when": {"field": "balance", "op": "<", "value_cents": 45000},
            "then": {"rule_type": "fixed", "params": {"amount_cents": 5000}},
        }),
    ),
    dict(
        key="food", name="Food", kind="spending",
        description="Groceries. Top this back up to $300 each paycheck — "
                    "add only what is needed to reach $300, nothing more. "
                    "If it already holds $300 or more, add nothing.",
        rule=("refill_to", {"target_cents": 30000}),
    ),
    dict(
        key="savings", name="Savings", kind="savings_bucket",
        description="Emergency savings. Top up to $2,000 — add only the "
                    "shortfall needed to reach $2,000, never more.",
        rule=("refill_to", {"target_cents": 200000}),
    ),
    dict(
        key="carfund", name="Car Fund", kind="savings_bucket",
        description="Everything left over after all the above goes here. "
                    "This is the catch-all for the remainder of the "
                    "paycheck.",
        rule=("remainder", {}),
    ),
]

ORDER = [c["key"] for c in CATEGORIES]


def engine_split(gross_cents: int, balances: dict[str, int]) -> dict[str, int]:
    """Ground truth: the deterministic engine."""
    rules = [
        SplitRule(
            category_id=c["key"], order_index=i + 1,
            rule_type=RuleType(c["rule"][0]), params=c["rule"][1], enabled=True,
        )
        for i, c in enumerate(CATEGORIES)
    ]
    res = apply_split(gross_cents, rules, dict(balances))
    out = {k: 0 for k in ORDER}
    for a in res.allocations:
        out[a.category_id] = out.get(a.category_id, 0) + a.amount_cents
    return out


# Each scenario names what it is really probing.
SCENARIOS = [
    dict(
        id="typical",
        probe="the ordinary case",
        gross_cents=200000,
        balances=dict(tithing=0, rent=0, health=0, carins=40000,
                      food=12000, savings=180000, carfund=0),
    ),
    dict(
        id="conditional_off",
        probe="a conditional that must NOT fire (car insurance at $460)",
        gross_cents=200000,
        balances=dict(tithing=0, rent=0, health=0, carins=46000,
                      food=12000, savings=180000, carfund=0),
    ),
    dict(
        id="refill_already_full",
        probe="refill targets already met — must add nothing",
        gross_cents=200000,
        balances=dict(tithing=0, rent=0, health=0, carins=50000,
                      food=35000, savings=250000, carfund=0),
    ),
    dict(
        id="tight",
        probe="not enough to cover everything — ordering decides who loses",
        gross_cents=60000,
        balances=dict(tithing=0, rent=0, health=0, carins=0,
                      food=0, savings=0, carfund=0),
    ),
    dict(
        id="very_tight",
        probe="cannot even cover rent after tithing",
        gross_cents=40000,
        balances=dict(tithing=0, rent=0, health=0, carins=0,
                      food=0, savings=0, carfund=0),
    ),
    dict(
        id="bonus",
        probe="a large cheque — most of it should land in the remainder",
        gross_cents=500000,
        balances=dict(tithing=0, rent=0, health=0, carins=40000,
                      food=12000, savings=180000, carfund=0),
    ),
    dict(
        id="odd_cents",
        probe="a gross that does not divide evenly by ten",
        gross_cents=183733,
        balances=dict(tithing=0, rent=0, health=0, carins=44999,
                      food=29999, savings=199999, carfund=0),
    ),
    dict(
        id="exact_fit",
        probe="the paycheck exactly covers the obligations, zero remainder",
        gross_cents=61111,
        balances=dict(tithing=0, rent=0, health=0, carins=45000,
                      food=30000, savings=200000, carfund=0),
    ),
]


if __name__ == "__main__":
    print(f"{'scenario':20} {'gross':>9}  " +
          "".join(f"{k:>9}" for k in ORDER) + "      sum")
    print("-" * 110)
    for s in SCENARIOS:
        out = engine_split(s["gross_cents"], s["balances"])
        total = sum(out.values())
        assert total == s["gross_cents"], (s["id"], total, s["gross_cents"])
        print(f"{s['id']:20} {s['gross_cents']/100:>9,.2f}  " +
              "".join(f"{out[k]/100:>9,.2f}" for k in ORDER) +
              f"  {total/100:>9,.2f}")
    print("\nall scenarios conserve the paycheck exactly")
