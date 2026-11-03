"""A rule missing a required param must be a plan error, not a crash.

Found in production: the model returned a `refill_to` rule with empty
params, the app submitted it, and `_desired_amount` did `p["target_cents"]`
-- a KeyError, which is not SplitPlanError, so every caller that guards
against a bad plan missed it. Saving became a 500, and the same rule
reaching a real paycheck would have crashed the split.

Models omit any property a schema does not mark required (docs/10), so
these are expected inputs, not impossible states.
"""
from __future__ import annotations

import pytest

from app.budget.split import RuleType, SplitPlanError, SplitRule, apply_split


def _rule(rule_type: str, params: dict, cid: str = "c1") -> SplitRule:
    return SplitRule(category_id=cid, order_index=1,
                     rule_type=RuleType(rule_type), params=params)


def _plan(rule: SplitRule) -> list[SplitRule]:
    """The rule under test, plus the remainder rule every plan must end
    with -- apply_split checks plan structure before evaluating any rule,
    so a lone rule fails for the wrong reason."""
    return [rule, SplitRule(category_id="sink", order_index=2,
                            rule_type=RuleType.REMAINDER, params={})]


@pytest.mark.parametrize(
    "rule_type,params,missing",
    [
        ("refill_to", {}, "target_cents"),
        ("refill_to", {"target_cents": None}, "target_cents"),
        ("fixed", {}, "amount_cents"),
        ("percent", {}, "rate"),
        ("conditional", {}, "when"),
    ],
)
def test_missing_param_raises_a_plan_error_naming_it(rule_type, params, missing):
    with pytest.raises(SplitPlanError) as err:
        apply_split(1000_00, _plan(_rule(rule_type, params)), {"c1": 0, "sink": 0})
    # The message has to name the field; "invalid plan" sends the reader
    # hunting through a plan that may have a dozen rules.
    assert missing in str(err.value)
    assert "c1" in str(err.value)


@pytest.mark.parametrize(
    "rule_type,params",
    [
        ("fixed", {"amount_cents": "not a number"}),
        ("refill_to", {"target_cents": "soon"}),
        ("percent", {"rate": "half"}),
    ],
)
def test_non_numeric_param_raises_a_plan_error(rule_type, params):
    with pytest.raises(SplitPlanError):
        apply_split(1000_00, _plan(_rule(rule_type, params)), {"c1": 0, "sink": 0})


def test_conditional_with_an_unusable_then_branch():
    rule = _rule("conditional", {"when": {"op": "<", "value_cents": 100},
                                 "then": {"rule_type": "nonsense"}})
    with pytest.raises(SplitPlanError) as err:
        apply_split(1000_00, _plan(rule), {"c1": 0, "sink": 0})
    assert "then.rule_type" in str(err.value)


def test_a_complete_rule_still_works():
    """The guard must not break the normal path."""
    out = apply_split(1000_00, _plan(_rule("refill_to", {"target_cents": 30000})),
                      {"c1": 0, "sink": 0})
    assert out.total_cents == 1000_00  # 30000 to c1, the rest swept up
    assert [(a.category_id, a.amount_cents) for a in out.allocations] == [
        ("c1", 30000), ("sink", 70000)]
