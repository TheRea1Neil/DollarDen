"""An incomplete suggested rule must never reach the app usable-looking.

`_validate_single` used to flag a rule with missing params as a notice
and return it anyway. The app showed it, the user applied it, and the
split engine 500'd. A dropped rule costs one tap to add by hand; a
broken one costs a crash at apply time.
"""
from __future__ import annotations

from app.ai.tasks import _validate_single


def _raw(**over):
    base = {
        "name": "Subscriptions",
        "kind": "spending",
        "description": "Streaming and app subscriptions.",
    }
    base.update(over)
    return base


def test_refill_to_without_a_target_borrows_the_category_target():
    out = _validate_single(
        _raw(target_cents=4000, suggested_rule={"rule_type": "refill_to", "params": {}}),
        existing=[],
    )
    assert out["suggested_rule"]["params"]["target_cents"] == 4000
    assert any("own target" in n for n in out["notices"])


def test_refill_to_with_no_target_anywhere_is_dropped_not_returned_broken():
    out = _validate_single(
        _raw(suggested_rule={"rule_type": "refill_to", "params": {}}), existing=[]
    )
    assert out["suggested_rule"] is None
    assert any("dropped" in n for n in out["notices"])
    # The category itself is still worth creating.
    assert out["applicable"] is True
    assert out["name"] == "Subscriptions"


def test_a_complete_rule_passes_through_untouched():
    rule = {"rule_type": "fixed", "params": {"amount_cents": 1500}}
    out = _validate_single(_raw(suggested_rule=rule), existing=[])
    assert out["suggested_rule"]["params"] == {"amount_cents": 1500}
    assert not any("dropped" in n for n in out["notices"])
