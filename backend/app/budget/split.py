"""Paycheck split engine.

Pure functions over integer cents. No I/O, no database, no AI -- so it is
trivially testable and deterministic. See docs/06-budget-engine.md.

Invariants (asserted, and covered by property-based tests):
  * sum(allocations) == gross exactly
  * no allocation is negative
  * rules are applied in order; earlier rules win when money runs short
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from enum import StrEnum


class RuleType(StrEnum):
    PERCENT = "percent"
    FIXED = "fixed"
    REFILL_TO = "refill_to"
    CONDITIONAL = "conditional"
    REMAINDER = "remainder"


class PercentBase(StrEnum):
    GROSS = "gross"
    REMAINING = "remaining"


@dataclass(frozen=True)
class SplitRule:
    category_id: str
    order_index: int
    rule_type: RuleType
    params: dict = field(default_factory=dict)
    enabled: bool = True


@dataclass(frozen=True)
class Allocation:
    category_id: str
    amount_cents: int
    rule_type: RuleType


@dataclass
class SplitResult:
    allocations: list[Allocation]
    unfunded: list[tuple[str, int]]  # (category_id, shortfall_cents)
    remaining_cents: int

    @property
    def total_cents(self) -> int:
        return sum(a.amount_cents for a in self.allocations)


class SplitPlanError(ValueError):
    """The plan itself is invalid -- refuse rather than guess."""


def _pct(amount_cents: int, rate: Decimal) -> int:
    """Percentage of a cent amount, rounded half-up to the nearest cent.

    Decimal, not float: 10% of 123457 must be a stable, auditable number.
    """
    return int((Decimal(amount_cents) * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def apply_split(
    gross_cents: int,
    rules: list[SplitRule],
    balances: dict[str, int],
) -> SplitResult:
    """Split `gross_cents` across categories per `rules`.

    `balances` maps category_id -> current balance, needed by refill_to and
    conditional rules.
    """
    if gross_cents < 0:
        raise SplitPlanError("gross_cents must not be negative")

    active = sorted((r for r in rules if r.enabled), key=lambda r: r.order_index)

    remainder_rules = [r for r in active if r.rule_type is RuleType.REMAINDER]
    if len(remainder_rules) != 1:
        # Unallocated money is a correctness hole, so this is fatal, not a warning.
        raise SplitPlanError(
            f"a split plan needs exactly one remainder rule, found {len(remainder_rules)}"
        )

    # The remainder rule may sit ANYWHERE in the order. It used to be
    # forced last, which was a guess about intent dressed up as a
    # correctness rule: "sweep up what is left at THIS point, and fund
    # nothing after it" is a thing people deliberately want -- one
    # paycheck straight into savings, the rest of the plan paused,
    # without editing and then un-editing every rule below.
    #
    # Nothing about the invariants needs it last. Wherever it lands it
    # takes `remaining`, so `remaining` is 0 afterwards and no money
    # goes unallocated; rules below it simply want more than the 0 left
    # and report themselves in `unfunded`, which the app already
    # surfaces as "N categories got nothing". Loud, and not a refusal.

    allocations: list[Allocation] = []
    unfunded: list[tuple[str, int]] = []
    remaining = gross_cents

    for rule in active:
        wanted = _desired_amount(rule, gross_cents, remaining, balances)
        if wanted <= 0:
            continue

        granted = min(wanted, remaining)
        if granted < wanted:
            # Tell the user rather than silently underfunding.
            unfunded.append((rule.category_id, wanted - granted))

        if granted > 0:
            allocations.append(Allocation(rule.category_id, granted, rule.rule_type))
            remaining -= granted

    result = SplitResult(allocations=allocations, unfunded=unfunded, remaining_cents=remaining)

    assert result.total_cents == gross_cents - remaining, "allocation accounting drifted"
    assert all(a.amount_cents > 0 for a in result.allocations), "non-positive allocation"
    return result


def _desired_amount(
    rule: SplitRule, gross: int, remaining: int, balances: dict[str, int]
) -> int:
    p = rule.params

    def need(key: str) -> Any:
        """A required param, or a malformed-plan error naming it.

        Bare p[key] raised KeyError, which is not SplitPlanError, so it
        escaped every caller that guards against a bad plan: saving one
        became a 500 instead of a 422, and the same rule reaching a real
        paycheck would have crashed the split rather than reporting
        itself. A model will omit any property a schema does not mark
        required (docs/10), so missing params are expected input, not an
        impossible state.
        """
        if key not in p or p[key] in (None, ""):
            raise SplitPlanError(
                f"{rule.rule_type.value} rule for category {rule.category_id} "
                f"is missing {key}"
            )
        return p[key]

    def whole(key: str) -> int:
        value = need(key)
        try:
            return int(value)
        except (TypeError, ValueError):
            raise SplitPlanError(
                f"{rule.rule_type.value} rule for category {rule.category_id} "
                f"has a non-numeric {key}: {value!r}"
            ) from None

    match rule.rule_type:
        case RuleType.PERCENT:
            try:
                rate = Decimal(str(need("rate")))
            except InvalidOperation:
                raise SplitPlanError(
                    f"percent rule for category {rule.category_id} has a "
                    f"non-numeric rate: {p.get('rate')!r}"
                ) from None
            if not (0 <= rate <= 1):
                raise SplitPlanError(f"percent rate out of range: {rate}")
            base_kind = PercentBase(p.get("of", PercentBase.GROSS))
            base = gross if base_kind is PercentBase.GROSS else remaining
            return _pct(base, rate)

        case RuleType.FIXED:
            return whole("amount_cents")

        case RuleType.REFILL_TO:
            target = whole("target_cents")
            current = balances.get(rule.category_id, 0)
            shortfall = max(0, target - current)
            cap = p.get("max_contribution_cents")
            return min(shortfall, int(cap)) if cap is not None else shortfall

        case RuleType.CONDITIONAL:
            if not _predicate_holds(need("when"), rule.category_id, balances):
                return 0
            then = need("then")
            if not isinstance(then, dict) or not then.get("rule_type"):
                raise SplitPlanError(
                    f"conditional rule for category {rule.category_id} has no "
                    f"usable 'then' branch"
                )
            try:
                inner_type = RuleType(then["rule_type"])
            except ValueError:
                raise SplitPlanError(
                    f"conditional rule for category {rule.category_id} has an "
                    f"unknown then.rule_type: {then['rule_type']!r}"
                ) from None
            inner = SplitRule(
                category_id=rule.category_id,
                order_index=rule.order_index,
                rule_type=inner_type,
                params=then.get("params", {}),
            )
            if inner.rule_type is RuleType.CONDITIONAL:
                raise SplitPlanError("conditional rules may not nest")
            return _desired_amount(inner, gross, remaining, balances)

        case RuleType.REMAINDER:
            return remaining

    raise SplitPlanError(f"unknown rule type: {rule.rule_type}")


def _predicate_holds(when: dict, category_id: str, balances: dict[str, int]) -> bool:
    """Evaluate a conditional's predicate.

    Supported: {"field": "balance", "op": "<", "value_cents": 45000}
    The field may name another category via "category_id".
    """
    target_cat = when.get("category_id", category_id)
    field_name = when.get("field", "balance")
    if field_name != "balance":
        raise SplitPlanError(f"unsupported predicate field: {field_name}")

    current = balances.get(target_cat, 0)

    # A predicate missing its threshold or operator is a malformed plan,
    # not a runtime surprise. Raising SplitPlanError means callers that
    # already handle an invalid plan handle this too; a bare KeyError
    # escaped them and surfaced as a 500 when a paycheck was recorded.
    for required in ("value_cents", "op"):
        if when.get(required) in (None, ""):
            raise SplitPlanError(
                f"conditional rule is missing '{required}'; it cannot be "
                f"evaluated"
            )
    try:
        value = int(when["value_cents"])
    except (TypeError, ValueError) as exc:
        raise SplitPlanError(
            f"conditional threshold is not a whole number of cents: "
            f"{when['value_cents']!r}"
        ) from exc
    op = when["op"]

    match op:
        case "<":
            return current < value
        case "<=":
            return current <= value
        case ">":
            return current > value
        case ">=":
            return current >= value
        case "==":
            return current == value
        case _:
            raise SplitPlanError(f"unsupported predicate operator: {op}")
