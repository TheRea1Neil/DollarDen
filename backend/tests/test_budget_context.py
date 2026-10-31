"""The context handed to the assistant when it is asked about the budget.

A question like "what categories am I missing" has no figure to look
up, so the model needs the SHAPE of the budget instead. What it is
given has to be complete enough to reason from and written plainly
enough to reason ABOUT.
"""
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.ai.budget_context import build, describe_rule
from app.db.models import User
from app.db.session import SessionLocal
from sqlalchemy import select
from tests.conftest import requires_db

PW = "context-passphrase-99"


class TestDescribeRule:
    """Rules are written out, not dumped as JSON.

    The model is being asked whether a rule is sensible, and
    {"rate": "0.10"} is harder to reason about than "10% of each
    paycheck".
    """

    def test_percent(self):
        assert describe_rule("percent", {"rate": "0.10"}) == "10% of each paycheck"

    def test_fixed(self):
        assert describe_rule("fixed", {"amount_cents": 95000}) == \
            "$950.00 from each paycheck"

    def test_refill(self):
        assert "topped back up to $400.00" in \
            describe_rule("refill_to", {"target_cents": 40000})

    def test_refill_with_a_cap(self):
        out = describe_rule("refill_to",
                            {"target_cents": 40000, "max_contribution_cents": 10000})
        assert "never more than $100.00" in out

    def test_conditional_reads_as_a_sentence(self):
        out = describe_rule("conditional", {
            "when": {"field": "balance", "op": "<", "value_cents": 45000},
            "then": {"rule_type": "fixed", "params": {"amount_cents": 5000}}})
        assert "$50.00 from each paycheck" in out
        assert "below $450.00" in out

    def test_remainder(self):
        assert "left over" in describe_rule("remainder", {})

    def test_no_rule_says_so_rather_than_being_blank(self):
        assert "by hand" in describe_rule(None, None)


@requires_db
class TestBuild:
    def _auth(self, client):
        r = client.post("/api/auth/register", json={
            "email": f"bc-{uuid.uuid4().hex[:10]}@x.com", "password": PW})
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    def _context(self):
        with SessionLocal() as db:
            uid = db.scalars(select(User.id).order_by(User.created_at.desc())).first()
            return build(db, db.get(User, uid))

    def test_an_empty_budget_says_so(self, client: TestClient):
        self._auth(client)
        out = self._context()
        assert "the budget is empty" in out

    def test_envelopes_carry_everything_needed_to_reason(self, client: TestClient):
        h = self._auth(client)
        r = client.post("/api/budget/categories", headers=h, json={
            "name": "Date Fund", "kind": "spending",
            "description": "Nights out together",
            "spend_criteria": "Restaurants, films, outings",
            "target_cents": 15000, "alert_at_cents": 3000})
        cid = r.json()["id"]
        client.put(f"/api/budget/categories/{cid}/funding", headers=h,
                   json={"rule_type": "fixed", "params": {"amount_cents": 7500}})

        out = self._context()
        assert "Date Fund" in out
        assert "[spending]" in out
        assert "$150.00" in out, "target"
        assert "$30.00" in out, "alert threshold"
        assert "$75.00 from each paycheck" in out, "funding rule in words"
        assert "Nights out together" in out
        assert "Restaurants, films, outings" in out

    def test_the_envelope_system_is_explained(self, client: TestClient):
        """The model cannot judge whether a budget is sensible without
        knowing what an envelope IS."""
        self._auth(client)
        out = self._context()
        assert "HOW THIS BUDGET WORKS" in out
        assert "obligation" in out and "savings_bucket" in out

    def test_accounts_and_headroom_are_included(self, client: TestClient):
        self._auth(client)
        out = self._context()
        for label in ("in the bank", "owed on cards", "not yet allocated"):
            assert label in out, label

    def test_archived_categories_are_left_out(self, client: TestClient):
        h = self._auth(client)
        r = client.post("/api/budget/categories", headers=h,
                        json={"name": "Old Thing", "kind": "spending"})
        client.delete(f"/api/budget/categories/{r.json()['id']}", headers=h)
        assert "Old Thing" not in self._context()

    def test_matchers_are_shown_so_auto_filing_can_be_questioned(self, client):
        h = self._auth(client)
        r = client.post("/api/budget/categories", headers=h,
                        json={"name": "Gas", "kind": "spending"})
        client.post("/api/budget/rules", headers=h, json={
            "category_id": r.json()["id"],
            "matcher": {"merchant_contains": "shell"}})
        assert "shell" in self._context()
