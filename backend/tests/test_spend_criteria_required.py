"""Every AI-created category must arrive with spend_criteria.

It is the field the auto-categoriser reads to decide where a charge
belongs, and categories created by the AI had none: the single-category
schema listed the property but never required it and never asked for
it, and the whole-plan schema did not have it at all. docs/10 records
the rule this breaks -- a model omits any property a schema does not
require -- and the cost here is a category that cannot attract a
single transaction.
"""
from __future__ import annotations

import re

from app.ai.tasks import (CRITERIA_GUIDANCE, DESIGN_PROMPT, DESIGN_SCHEMA,
                          SINGLE_PROMPT, SINGLE_SCHEMA, _validate_design,
                          _validate_single)


def test_the_single_category_schema_requires_it():
    assert "spend_criteria" in SINGLE_SCHEMA["required"]


def test_the_whole_plan_schema_has_it_and_requires_it():
    cat = DESIGN_SCHEMA["properties"]["categories"]["items"]
    assert "spend_criteria" in cat["properties"], "the field was absent entirely"
    assert "spend_criteria" in cat["required"]


def test_both_prompts_actually_ask_for_it():
    """A required schema field the prompt never mentions invites a
    one-word answer that satisfies the schema and helps nobody."""
    for prompt in (SINGLE_PROMPT, DESIGN_PROMPT):
        assert "spend_criteria" in prompt
        assert "{criteria_guidance}" in prompt


def test_the_guidance_names_the_distinction_that_matters():
    # The whole point is separating what a category is FOR from what
    # gets CHARGED to it; losing that makes the field useless.
    assert "CHARGED" in CRITERIA_GUIDANCE
    assert "FOR" in CRITERIA_GUIDANCE


def test_neither_prompt_has_an_unfilled_placeholder():
    """A missing .format() key raises only on a live request."""
    for name, prompt in (("SINGLE", SINGLE_PROMPT), ("DESIGN", DESIGN_PROMPT)):
        fields = set(re.findall(r"(?<!\{)\{([a-z_]+)\}(?!\})", prompt))
        assert "criteria_guidance" in fields, name


def test_a_single_category_carries_the_criteria_through():
    out = _validate_single(
        {"name": "Eating Out", "kind": "spending",
         "description": "Meals away from home.",
         "spend_criteria": "Restaurants and takeaway; shop groceries are Food."},
        existing=[],
    )
    assert out["spend_criteria"].startswith("Restaurants")


def test_a_whole_plan_carries_the_criteria_through():
    proposal = {
        "categories": [{
            "name": "Eating Out", "kind": "spending",
            "description": "Meals away from home.",
            "spend_criteria": "Restaurants and takeaway.",
        }],
        "split_rules": [{"category_name": "Eating Out", "order_index": 1,
                         "rule_type": "remainder", "params": {}, "why": "rest"}],
        "notes": [],
    }
    out = _validate_design(proposal, existing=[])
    assert out["categories"][0]["spend_criteria"] == "Restaurants and takeaway."


def test_a_missing_criteria_is_none_not_empty_string():
    """None reads as 'not set'; '' would look like a deliberate blank."""
    out = _validate_single(
        {"name": "X", "kind": "spending", "description": "d"}, existing=[])
    assert out["spend_criteria"] is None
