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
from decimal import ROUND_HALF_UP, Decimal
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
    if active[-1].rule_type is not RuleType.REMAINDER:
        raise SplitPlanError("the remainder rule must be last")

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

    match rule.rule_type:
        case RuleType.PERCENT:
            rate = Decimal(str(p["rate"]))
            if not (0 <= rate <= 1):
                raise SplitPlanError(f"percent rate out of range: {rate}")
            base_kind = PercentBase(p.get("of", PercentBase.GROSS))
            base = gross if base_kind is PercentBase.GROSS else remaining
            return _pct(base, rate)

        case RuleType.FIXED:
            return int(p["amount_cents"])

        case RuleType.REFILL_TO:
            target = int(p["target_cents"])
            current = balances.get(rule.category_id, 0)
            need = max(0, target - current)
            cap = p.get("max_contribution_cents")
            return min(need, int(cap)) if cap is not None else need

        case RuleType.CONDITIONAL:
            if not _predicate_holds(p["when"], rule.category_id, balances):
                return 0
            inner = SplitRule(
                category_id=rule.category_id,
                order_index=rule.order_index,
                rule_type=RuleType(p["then"]["rule_type"]),
                params=p["then"].get("params", {}),
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
