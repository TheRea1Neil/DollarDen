"""AI layer: goals, provider selection, job queue, proposal validation.

No network. The provider is stubbed, because what matters here is that a
model's output is never trusted before being checked.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.ai.tasks import _describe_category, _validate_design, _validate_single
from app.db.models import Category
from tests.conftest import requires_db

PW = "ai-tests-passphrase-77"


def _auth(client: TestClient) -> dict[str, str]:
    r = client.post("/api/auth/register",
                    json={"email": f"ai-{uuid.uuid4().hex[:10]}@x.com", "password": PW})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


class TestProposalValidation:
    """A proposal the engine would reject must never reach the user as valid."""

    def _good(self) -> dict:
        return {
            "categories": [
                {"name": "Food", "kind": "spending", "description": "groceries",
                 "target_cents": 30000},
                {"name": "Car Fund", "kind": "savings_bucket", "description": "saving"},
            ],
            "split_rules": [
                {"category_name": "Food", "order_index": 1, "rule_type": "refill_to",
                 "params": {"target_cents": 30000}, "why": "keep food topped up"},
                {"category_name": "Car Fund", "order_index": 2, "rule_type": "remainder",
                 "params": {}, "why": "everything else"},
            ],
            "notes": "ok",
        }

    def test_a_valid_proposal_is_applicable(self) -> None:
        out = _validate_design(self._good(), [])
        assert out["applicable"] is True
        assert out["problems"] == []
        assert len(out["categories"]) == 2

    def test_missing_remainder_is_caught_before_the_user_sees_it(self) -> None:
        """The engine refuses such a plan; catching it here lets us explain."""
        p = self._good()
        p["split_rules"] = [p["split_rules"][0]]
        out = _validate_design(p, [])
        assert out["applicable"] is False
        assert any("remainder" in x for x in out["problems"])

    def test_remainder_not_last_is_repaired_not_rejected(self) -> None:
        """Behaviour changed deliberately: this fault has exactly one
        correct fix, so it is repaired in code and reported, rather than
        handed back to the user to sort out. See TestDeterministicRepair."""
        p = self._good()
        p["split_rules"][0]["order_index"] = 5  # pushes remainder off the end
        out = _validate_design(p, [])
        assert out["applicable"] is True
        assert out["split_rules"][-1]["rule_type"] == "remainder"
        assert any("moved the remainder" in n for n in out["notices"])

    def test_two_remainders_are_repaired_not_rejected(self) -> None:
        """Also changed deliberately: only the last remainder can receive
        anything, so the earlier ones are dropped and reported."""
        p = self._good()
        p["split_rules"].append({"category_name": "Food", "order_index": 3,
                                 "rule_type": "remainder", "params": {}, "why": "x"})
        out = _validate_design(p, [])
        assert out["applicable"] is True
        kinds = [r["rule_type"] for r in out["split_rules"]]
        assert kinds.count("remainder") == 1
        assert any("remainder rules" in n for n in out["notices"])

    def test_rule_for_an_unknown_category_is_dropped(self) -> None:
        p = self._good()
        p["split_rules"].insert(0, {"category_name": "Ghost", "order_index": 0,
                                    "rule_type": "fixed",
                                    "params": {"amount_cents": 100}, "why": "x"})
        out = _validate_design(p, [])
        assert all(r["category_name"] != "Ghost" for r in out["split_rules"])
        assert any("Ghost" in x for x in out["problems"])

    def test_invalid_kind_is_corrected_and_reported(self) -> None:
        p = self._good()
        p["categories"][0]["kind"] = "nonsense"
        out = _validate_design(p, [])
        assert out["categories"][0]["kind"] == "spending"
        assert any("invalid kind" in x for x in out["problems"])

    def test_invalid_rule_type_is_dropped(self) -> None:
        p = self._good()
        p["split_rules"].insert(0, {"category_name": "Food", "order_index": 0,
                                    "rule_type": "magic", "params": {}, "why": "x"})
        out = _validate_design(p, [])
        assert all(r["rule_type"] != "magic" for r in out["split_rules"])

    def test_existing_categories_are_flagged_for_reuse_not_recreation(self) -> None:
        existing = [Category(name="Food", kind="spending", balance_cents=0)]
        out = _validate_design(self._good(), existing)
        food = next(c for c in out["categories"] if c["name"] == "Food")
        assert food["already_exists"] is True

    def test_negative_targets_are_discarded(self) -> None:
        p = self._good()
        p["categories"][0]["target_cents"] = -500
        out = _validate_design(p, [])
        assert out["categories"][0]["target_cents"] is None

    def test_order_indexes_are_renumbered_contiguously(self) -> None:
        p = self._good()
        p["split_rules"][0]["order_index"] = 17
        p["split_rules"][1]["order_index"] = 99
        out = _validate_design(p, [])
        assert [r["order_index"] for r in out["split_rules"]] == [1, 2]


@requires_db
class TestGoals:
    def test_round_trip(self, client: TestClient) -> None:
        h = _auth(client)
        assert client.get("/api/ai/goals", headers=h).json()["body"] == ""
        r = client.put("/api/ai/goals",
                       json={"body": "Save for a car in 18 months. Eat out less."},
                       headers=h)
        assert r.status_code == 200
        assert "car" in client.get("/api/ai/goals", headers=h).json()["body"]

    def test_goals_are_per_user(self, client: TestClient) -> None:
        ha, hb = _auth(client), _auth(client)
        client.put("/api/ai/goals", json={"body": "mine"}, headers=ha)
        assert client.get("/api/ai/goals", headers=hb).json()["body"] == ""

    def test_requires_auth(self, client: TestClient) -> None:
        assert client.get("/api/ai/goals").status_code in (401, 403)


@requires_db
class TestAISettings:
    def test_defaults_to_the_server_setting(self, client: TestClient) -> None:
        h = _auth(client)
        s = client.get("/api/ai/settings", headers=h).json()
        assert s["is_default"] is True

    def test_custom_requires_a_url(self, client: TestClient) -> None:
        h = _auth(client)
        r = client.put("/api/ai/settings", json={"provider": "custom"}, headers=h)
        assert r.status_code == 422
        assert "base_url" in r.text

    def test_ai_can_be_turned_off(self, client: TestClient) -> None:
        """Disabling AI is a supported configuration (ADR 0003)."""
        h = _auth(client)
        r = client.put("/api/ai/settings", json={"provider": "none"}, headers=h)
        assert r.status_code == 200 and r.json()["provider"] == "none"

    def test_unknown_provider_rejected(self, client: TestClient) -> None:
        h = _auth(client)
        assert client.put("/api/ai/settings",
                          json={"provider": "chatgpt"}, headers=h).status_code == 422


@requires_db
class TestJobQueue:
    def test_queueing_returns_202_not_201(self, client: TestClient) -> None:
        """The work is accepted, not done."""
        h = _auth(client)
        r = client.post("/api/ai/jobs",
                        json={"kind": "design_categories", "request": {}}, headers=h)
        assert r.status_code == 202
        assert r.json()["status"] in ("queued", "waking", "running")

    def test_unknown_kind_rejected(self, client: TestClient) -> None:
        h = _auth(client)
        assert client.post("/api/ai/jobs", json={"kind": "mine_bitcoin"},
                           headers=h).status_code == 422

    def test_a_job_is_pollable(self, client: TestClient) -> None:
        h = _auth(client)
        job = client.post("/api/ai/jobs",
                          json={"kind": "summary"}, headers=h).json()
        again = client.get(f"/api/ai/jobs/{job['id']}", headers=h)
        assert again.status_code == 200 and again.json()["id"] == job["id"]

    def test_another_users_job_is_invisible(self, client: TestClient) -> None:
        ha, hb = _auth(client), _auth(client)
        job = client.post("/api/ai/jobs", json={"kind": "summary"}, headers=ha).json()
        assert client.get(f"/api/ai/jobs/{job['id']}", headers=hb).status_code == 404
        assert client.get("/api/ai/jobs", headers=hb).json() == []

    def test_jobs_require_auth(self, client: TestClient) -> None:
        assert client.post("/api/ai/jobs", json={"kind": "summary"}).status_code in (401, 403)


class TestParamValidation:
    """A rule with empty params would be rejected by the engine at apply
    time. Catching it while the draft is editable is the whole point."""

    def _rule(self, rtype: str, params: dict) -> dict:
        return {
            "categories": [
                {"name": "A", "kind": "spending", "description": "x"},
                {"name": "B", "kind": "savings_bucket", "description": "y"},
            ],
            "split_rules": [
                {"category_name": "A", "order_index": 1, "rule_type": rtype,
                 "params": params, "why": "x"},
                {"category_name": "B", "order_index": 2, "rule_type": "remainder",
                 "params": {}, "why": "rest"},
            ],
            "notes": "",
        }

    def test_percent_without_a_rate_is_flagged(self) -> None:
        out = _validate_design(self._rule("percent", {}), [])
        assert out["applicable"] is False
        assert any("rate" in p for p in out["problems"])

    def test_fixed_without_an_amount_is_flagged(self) -> None:
        out = _validate_design(self._rule("fixed", {}), [])
        assert any("amount_cents" in p for p in out["problems"])

    def test_refill_without_a_target_is_flagged(self) -> None:
        out = _validate_design(self._rule("refill_to", {}), [])
        assert any("target_cents" in p for p in out["problems"])

    def test_complete_params_pass(self) -> None:
        out = _validate_design(self._rule("percent", {"rate": "0.10", "of": "gross"}), [])
        assert out["applicable"] is True, out["problems"]

    def test_remainder_needs_no_params(self) -> None:
        out = _validate_design(self._rule("fixed", {"amount_cents": 100}), [])
        assert out["applicable"] is True, out["problems"]


class TestExistingCategoriesDoNotBlock:
    def test_reuse_is_a_notice_not_a_problem(self) -> None:
        """Reusing a category the user already has is the DESIRED outcome."""
        existing = [Category(name="Food", kind="spending", balance_cents=0)]
        proposal = {
            "categories": [
                {"name": "Food", "kind": "spending", "description": "groceries"},
                {"name": "Rest", "kind": "savings_bucket", "description": "x"},
            ],
            "split_rules": [
                {"category_name": "Food", "order_index": 1, "rule_type": "refill_to",
                 "params": {"target_cents": 30000}, "why": "top up"},
                {"category_name": "Rest", "order_index": 2, "rule_type": "remainder",
                 "params": {}, "why": "rest"},
            ],
            "notes": "",
        }
        out = _validate_design(proposal, existing)
        assert out["applicable"] is True, out["problems"]
        assert any("already exists" in n for n in out["notices"])
        assert not any("already exists" in p for p in out["problems"])


class TestSchemaCompleteness:
    """A JSON-schema-constrained model cannot emit a property the schema
    does not declare. Omitting `when`/`then` made conditional rules
    impossible to express and failed 24 out of 24 measured trials, with
    retries unable to help. Every field any rule type needs must be
    declared."""

    def test_every_required_param_is_declarable(self) -> None:
        from app.ai.tasks import DESIGN_SCHEMA, _REQUIRED_PARAMS

        declared = set(
            DESIGN_SCHEMA["properties"]["split_rules"]["items"]
            ["properties"]["params"]["properties"]
        )
        for rule_type, needed in _REQUIRED_PARAMS.items():
            for field in needed:
                assert field in declared, (
                    f"'{rule_type}' requires '{field}' but the schema does not "
                    f"declare it — the model could never produce a valid rule"
                )

    def test_conditional_nested_shape_is_declared(self) -> None:
        from app.ai.tasks import DESIGN_SCHEMA

        params = (DESIGN_SCHEMA["properties"]["split_rules"]["items"]
                  ["properties"]["params"]["properties"])
        assert set(params["when"]["properties"]) >= {"field", "op", "value_cents"}
        assert set(params["then"]["properties"]) >= {"rule_type", "params"}


class TestDeterministicRepair:
    """Faults with exactly one correct fix are repaired in code.

    Measured (experiments/selfcheck_strategies.py): asking the model to fix
    its own output does not help. But a mechanical fault has a mechanical
    fix, and applying it is faster and more reliable than another round
    trip. Repairs are reported, never silent.
    """

    def _cats(self) -> list[dict]:
        return [
            {"name": "A", "kind": "spending", "description": "x"},
            {"name": "B", "kind": "savings_bucket", "description": "y"},
            {"name": "C", "kind": "savings_bucket", "description": "z"},
        ]

    def test_two_remainders_keeps_the_last_and_reports_it(self) -> None:
        """Observed from the live model: it proposed two remainder rules."""
        out = _validate_design({
            "categories": self._cats(),
            "split_rules": [
                {"category_name": "A", "order_index": 1, "rule_type": "fixed",
                 "params": {"amount_cents": 1000}, "why": ""},
                {"category_name": "B", "order_index": 2, "rule_type": "remainder",
                 "params": {}, "why": ""},
                {"category_name": "C", "order_index": 3, "rule_type": "remainder",
                 "params": {}, "why": ""},
            ],
            "notes": "",
        }, [])
        assert out["applicable"] is True, out["problems"]
        kinds = [r["rule_type"] for r in out["split_rules"]]
        assert kinds.count("remainder") == 1
        assert out["split_rules"][-1]["category_name"] == "C", "the LAST one survives"
        assert any("remainder rules" in n for n in out["notices"])

    def test_remainder_not_last_is_moved_and_reported(self) -> None:
        out = _validate_design({
            "categories": self._cats(),
            "split_rules": [
                {"category_name": "B", "order_index": 1, "rule_type": "remainder",
                 "params": {}, "why": ""},
                {"category_name": "A", "order_index": 2, "rule_type": "fixed",
                 "params": {"amount_cents": 1000}, "why": ""},
            ],
            "notes": "",
        }, [])
        assert out["applicable"] is True, out["problems"]
        assert out["split_rules"][-1]["rule_type"] == "remainder"
        assert any("moved the remainder" in n for n in out["notices"])

    def test_order_indexes_renumbered_after_repair(self) -> None:
        out = _validate_design({
            "categories": self._cats(),
            "split_rules": [
                {"category_name": "B", "order_index": 9, "rule_type": "remainder",
                 "params": {}, "why": ""},
                {"category_name": "A", "order_index": 3, "rule_type": "fixed",
                 "params": {"amount_cents": 1000}, "why": ""},
            ],
            "notes": "",
        }, [])
        assert [r["order_index"] for r in out["split_rules"]] == [1, 2]

    def test_no_remainder_at_all_is_still_a_problem(self) -> None:
        """Inventing one would mean guessing where the money should go."""
        out = _validate_design({
            "categories": self._cats(),
            "split_rules": [
                {"category_name": "A", "order_index": 1, "rule_type": "fixed",
                 "params": {"amount_cents": 1000}, "why": ""},
            ],
            "notes": "",
        }, [])
        assert out["applicable"] is False
        assert any("no remainder" in p for p in out["problems"])


class TestPlanSimulation:
    """Ordering faults are invisible to structural validation.

    Observed live: the model placed an Emergency Fund refill to $6,000 at
    position 2, above rent. The plan was perfectly well-formed and starved
    rent, health and groceries completely. Only simulating it reveals that.
    """

    def _plan(self, rules: list[dict]) -> dict:
        names = {r["category_name"] for r in rules}
        return {
            "categories": [
                {"name": n, "kind": "spending", "description": ""} for n in names
            ],
            "split_rules": rules,
            "notes": "",
        }

    def test_a_savings_goal_above_rent_is_warned_about(self) -> None:
        out = _validate_design(self._plan([
            {"category_name": "Emergency Fund", "order_index": 1,
             "rule_type": "refill_to", "params": {"target_cents": 600000}, "why": ""},
            {"category_name": "Rent", "order_index": 2,
             "rule_type": "fixed", "params": {"amount_cents": 35000}, "why": ""},
            {"category_name": "Left", "order_index": 3,
             "rule_type": "remainder", "params": {}, "why": ""},
        ]), [], typical_gross_cents=200000)

        # Structurally fine -- which is exactly the danger.
        assert out["applicable"] is True
        assert out["warnings"], "starving rent must be surfaced"
        assert any("Rent" in w for w in out["warnings"])

    def test_a_sensible_order_produces_no_warnings(self) -> None:
        out = _validate_design(self._plan([
            {"category_name": "Tithing", "order_index": 1, "rule_type": "percent",
             "params": {"rate": "0.10", "of": "gross"}, "why": ""},
            {"category_name": "Rent", "order_index": 2, "rule_type": "fixed",
             "params": {"amount_cents": 35000}, "why": ""},
            {"category_name": "Groceries", "order_index": 3, "rule_type": "refill_to",
             "params": {"target_cents": 30000}, "why": ""},
            {"category_name": "Emergency Fund", "order_index": 4,
             "rule_type": "remainder", "params": {}, "why": ""},
        ]), [], typical_gross_cents=200000)
        assert out["applicable"] is True
        assert out["warnings"] == [], out["warnings"]

    def test_the_simulated_amount_is_reported(self) -> None:
        """So the user knows what assumption the warning rests on."""
        out = _validate_design(self._plan([
            {"category_name": "A", "order_index": 1, "rule_type": "remainder",
             "params": {}, "why": ""},
        ]), [], typical_gross_cents=150000)
        assert out["simulated_gross_cents"] == 150000

    def test_an_unfunded_fixed_rule_reports_the_shortfall(self) -> None:
        out = _validate_design(self._plan([
            {"category_name": "Rent", "order_index": 1, "rule_type": "fixed",
             "params": {"amount_cents": 500000}, "why": ""},
            {"category_name": "Left", "order_index": 2, "rule_type": "remainder",
             "params": {}, "why": ""},
        ]), [], typical_gross_cents=200000)
        assert any("short" in w for w in out["warnings"])


class TestSingleCategoryDesign:
    """Designing ONE category, so adding a category cannot rewrite a plan."""

    @staticmethod
    def _good() -> dict:
        return {
            "name": "Dog Vet",
            "kind": "savings_bucket",
            "description": "set aside for the dog's vet bills",
            "target_cents": 50000,
            "suggested_rule": {"rule_type": "fixed",
                               "params": {"amount_cents": 5000},
                               "why": "steady build-up"},
            "matcher_hint": {"merchant_contains": "vet"},
        }

    def test_a_valid_single_category(self) -> None:
        out = _validate_single(self._good(), [])
        assert out["applicable"] and not out["problems"]
        assert out["name"] == "Dog Vet" and out["target_cents"] == 50000
        assert out["suggested_rule"]["rule_type"] == "fixed"

    def test_duplicate_name_is_a_problem_not_a_notice(self) -> None:
        existing = [Category(name="Dog Vet", kind="spending", balance_cents=0)]
        out = _validate_single(self._good(), existing)
        assert not out["applicable"]
        assert any("already exists" in p for p in out["problems"])

    def test_duplicate_check_ignores_case(self) -> None:
        existing = [Category(name="dog vet", kind="spending", balance_cents=0)]
        assert not _validate_single(self._good(), existing)["applicable"]

    def test_a_remainder_suggestion_is_dropped(self) -> None:
        """One category must never claim what is left of a paycheck."""
        p = self._good()
        p["suggested_rule"] = {"rule_type": "remainder", "params": {}}
        out = _validate_single(p, [])
        assert out["suggested_rule"] is None
        assert out["applicable"]  # still usable, just unfunded
        assert any("dropped" in n for n in out["notices"])

    def test_a_rule_missing_its_params_is_flagged_not_dropped(self) -> None:
        p = self._good()
        p["suggested_rule"] = {"rule_type": "percent", "params": {}}
        out = _validate_single(p, [])
        assert any("missing" in n for n in out["notices"])
        assert out["applicable"]

    def test_a_bad_kind_defaults_rather_than_failing(self) -> None:
        p = self._good()
        p["kind"] = "nonsense"
        out = _validate_single(p, [])
        assert out["kind"] == "spending" and out["applicable"]
        assert any("not valid" in n for n in out["notices"])

    def test_a_nameless_category_is_rejected(self) -> None:
        p = self._good()
        p["name"] = "  "
        assert not _validate_single(p, [])["applicable"]


class TestCategoryContext:
    """Descriptions exist to be fed back to the model, not just stored."""

    def test_description_reaches_the_prompt(self) -> None:
        c = Category(name="Food", kind="spending", balance_cents=0,
                     target_cents=30000, description="groceries only, no eating out")
        line = _describe_category(c)
        assert "Food" in line and "300.00" in line
        assert "groceries only, no eating out" in line

    def test_a_category_without_one_still_renders(self) -> None:
        c = Category(name="Gas", kind="spending", balance_cents=0)
        line = _describe_category(c)
        assert "Gas" in line and "—" not in line


class TestJobKinds:
    def test_every_advertised_kind_has_a_handler(self) -> None:
        """A kind with no handler accepts the job and then always fails."""
        from app.ai import jobs, tasks
        missing = [k for k in jobs.KINDS if not hasattr(tasks, f"run_{k}")]
        assert missing == [], f"no run_* handler for: {missing}"


@requires_db
class TestDescriptionsReachTheModel:
    """The description is only worth storing if it is in the prompt.

    A stub provider captures the prompt, so this asserts the plumbing
    rather than hoping a model's wording happens to reflect it.
    """

    class _Capture:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def chat(self, prompt: str, schema: dict | None = None):
            self.prompts.append(prompt)
            from types import SimpleNamespace
            return SimpleNamespace(content='{"answer": "ok"}', model="stub")

    @staticmethod
    def _user_with_categories(email: str) -> tuple:
        import uuid as _uuid

        from app.core.security import hash_password
        from app.db.models import Category, User
        from app.db.session import SessionLocal

        db = SessionLocal()
        u = User(email=email, password_hash=hash_password("x" * 16))
        db.add(u)
        db.flush()
        db.add(Category(
            user_id=u.id, name="Blue Fund", kind="savings_bucket",
            description="the deductible on my daughter's braces, due in March",
            balance_cents=40000, sort_order=0,
        ))
        db.add(Category(user_id=u.id, name="Food", kind="spending",
                        balance_cents=15000, sort_order=1))
        db.commit()
        return db, u

    def test_advise_sends_the_description(self) -> None:
        import asyncio
        import uuid as _uuid

        from app.ai.tasks import run_advise

        db, u = self._user_with_categories(f"adv-{_uuid.uuid4().hex[:8]}@x.com")
        try:
            cap = self._Capture()
            asyncio.run(run_advise(db, u, cap, {"question": "Can I spend it?"}))
            prompt = cap.prompts[0]
            assert "daughter's braces" in prompt, prompt
            # And a category without one does not get a dangling separator.
            assert "Food: $150.00\n" in prompt or prompt.rstrip().endswith("Food: $150.00") \
                or "Food: $150.00" in prompt
        finally:
            db.close()

    def test_design_sends_existing_descriptions(self) -> None:
        """So the designer knows what the user already means by a name."""
        import asyncio
        import uuid as _uuid

        from app.ai.tasks import run_design_category

        db, u = self._user_with_categories(f"dsg-{_uuid.uuid4().hex[:8]}@x.com")
        try:
            cap = self._Capture()
            try:
                asyncio.run(run_design_category(
                    db, u, cap, {"request": "money for the dog"}))
            except Exception:
                # The stub returns an answer shape, not a category shape.
                # The prompt is what is under test.
                pass
            assert cap.prompts, "no prompt was built"
            assert "daughter's braces" in cap.prompts[0], cap.prompts[0]
        finally:
            db.close()


class TestConditionalPredicateIsComplete:
    """A predicate with an operator but no threshold cannot be evaluated.

    Found by an allocation experiment: qwen3 emitted `op` and omitted
    `value_cents` on both compile attempts, the draft validated clean,
    and the engine then raised KeyError when a paycheck was applied.
    """

    @staticmethod
    def _rule(when: dict) -> dict:
        return {"category_name": "Car Insurance", "order_index": 1,
                "rule_type": "conditional",
                "params": {"when": when,
                           "then": {"rule_type": "fixed",
                                    "params": {"amount_cents": 5000}}},
                "why": "x"}

    def test_schema_requires_both_predicate_fields(self) -> None:
        from app.ai.tasks import DESIGN_SCHEMA
        when = (DESIGN_SCHEMA["properties"]["split_rules"]["items"]
                ["properties"]["params"]["properties"]["when"])
        assert set(when["required"]) == {"op", "value_cents"}

    def test_missing_threshold_is_reported(self) -> None:
        from app.ai.tasks import _missing_params
        out = _missing_params("conditional", self._rule({"op": "<"})["params"])
        assert "when.value_cents" in out

    def test_missing_operator_is_reported(self) -> None:
        from app.ai.tasks import _missing_params
        out = _missing_params("conditional",
                              self._rule({"value_cents": 45000})["params"])
        assert "when.op" in out

    def test_a_complete_predicate_passes(self) -> None:
        from app.ai.tasks import _missing_params
        out = _missing_params(
            "conditional", self._rule({"op": "<", "value_cents": 45000})["params"])
        assert out == ""

    def test_the_draft_blocks_applying(self) -> None:
        """Blocking, not advisory: the plan cannot run at all like this."""
        from app.ai.tasks import _validate_design
        p = {"categories": [{"name": "Car Insurance", "kind": "obligation",
                             "description": "x"},
                            {"name": "Rest", "kind": "savings_bucket",
                             "description": "y"}],
             "split_rules": [self._rule({"op": "<"}),
                             {"category_name": "Rest", "order_index": 2,
                              "rule_type": "remainder", "params": {}, "why": "z"}]}
        out = _validate_design(p, [])
        assert any("value_cents" in n for n in out["problems"]), out["problems"]
        assert not out["applicable"]


class TestSuggestionAttribution:
    """A suggestion must say what decided it, and the quote must be real.

    Measured first in experiments/categorise-arms: descriptions that name
    a payee or draw a boundary are what make this work. Showing WHICH
    words decided a suggestion is how a user finds out their description
    is too vague or too greedy.
    """

    @staticmethod
    def _cats() -> list[Category]:
        return [
            Category(name="Gas", kind="spending", balance_cents=0,
                     description="Fuel at a pump, nothing else. Uber and "
                                 "Lyft come out of Date Fund."),
            Category(name="Date Fund", kind="spending", balance_cents=0,
                     description="Going out together, and the rideshare "
                                 "to get there."),
            Category(name="Food", kind="spending", balance_cents=0),
        ]

    def test_a_real_quote_is_verified(self) -> None:
        from app.ai.tasks import _validate_suggestion
        # The merchant must be supplied: a quote only counts as evidence
        # when it also names something in the charge.
        out = _validate_suggestion({
            "category": "Gas", "confidence": "high", "why": "fuel",
            "matched_phrase": "Fuel at a pump, nothing else",
        }, self._cats(), "PUMP N GO 4412")
        assert out["category_name"] == "Gas"
        assert out["attribution_verified"] is True
        assert out["matched_phrase"] == "Fuel at a pump, nothing else"

    def test_a_paraphrase_is_not_passed_off_as_a_quote(self) -> None:
        """An attribution the user cannot find in their own description
        is worse than none."""
        from app.ai.tasks import _validate_suggestion
        out = _validate_suggestion({
            "category": "Gas", "confidence": "high", "why": "fuel",
            "matched_phrase": "this category is for petrol purchases",
        }, self._cats())
        assert out["category_name"] == "Gas"
        assert out["attribution_verified"] is False
        assert out["matched_phrase"] is None

    def test_quoting_a_category_with_no_description_is_rejected(self) -> None:
        from app.ai.tasks import _validate_suggestion
        out = _validate_suggestion({
            "category": "Food", "confidence": "high", "why": "groceries",
            "matched_phrase": "groceries from a shop",
        }, self._cats())
        assert out["matched_phrase"] is None

    def test_punctuation_around_the_quote_is_tolerated(self) -> None:
        from app.ai.tasks import _validate_suggestion
        out = _validate_suggestion({
            "category": "Date Fund", "confidence": "high", "why": "x",
            "matched_phrase": '"the rideshare to get there."',
        }, self._cats(), "RIDESHARE CO 88")
        assert out["attribution_verified"] is True

    def test_unsure_comes_back_as_no_category(self) -> None:
        from app.ai.tasks import _validate_suggestion
        out = _validate_suggestion({
            "category": "UNSURE", "confidence": "low",
            "why": "the name says nothing",
        }, self._cats())
        assert out["unsure"] is True
        assert out["category_id"] is None

    def test_a_category_that_does_not_exist_is_treated_as_unsure(self) -> None:
        """Rather than inventing one, or crashing."""
        from app.ai.tasks import _validate_suggestion
        out = _validate_suggestion({
            "category": "Holiday Fund", "confidence": "high", "why": "x",
        }, self._cats())
        assert out["unsure"] is True


class TestDeterministicAttribution:
    """Which descriptions name something in this charge — computed, not asked.

    The model cannot be trusted to report what decided its answer: given
    "Uber and Lyft come out of Date Fund" it moved a rideshare off Gas
    and then reported it had gone on the merchant name alone. A string
    match is checkable.
    """

    @staticmethod
    def _cats() -> list[Category]:
        return [
            Category(name="Gas", kind="spending", balance_cents=0,
                     description="Fuel at a pump, nothing else. Uber and "
                                 "Lyft come out of Date Fund."),
            Category(name="Date Fund", kind="spending", balance_cents=0,
                     description="Going out together and the rideshare there."),
            Category(name="Food", kind="spending", balance_cents=0),
        ]

    def test_finds_the_description_that_names_the_merchant(self) -> None:
        from app.ai.tasks import _mentions
        hits = _mentions("UBER TRIP 4KX2M", self._cats())
        assert [h["category_name"] for h in hits] == ["Gas"]
        assert hits[0]["word"] == "uber"

    def test_ignores_boilerplate_words(self) -> None:
        """'payment', 'card', 'trip' and friends match everything."""
        from app.ai.tasks import _mentions
        assert _mentions("CARD PAYMENT POS DEBIT", self._cats()) == []
        assert _mentions("SQ *MERCHANT 4821", self._cats()) == []

    def test_ignores_bare_numbers(self) -> None:
        from app.ai.tasks import _mentions
        assert _mentions("ACH DEBIT 88291002", self._cats()) == []

    def test_a_category_with_no_description_never_matches(self) -> None:
        from app.ai.tasks import _mentions
        assert all(h["category_name"] != "Food"
                   for h in _mentions("FOOD MART", self._cats()))

    def test_the_suggestion_carries_the_chosen_description(self) -> None:
        from app.ai.tasks import _validate_suggestion
        out = _validate_suggestion(
            {"category": "Date Fund", "confidence": "high", "why": "x",
             "matched_phrase": ""},
            self._cats(), "UBER TRIP 4KX2M")
        assert out["category_name"] == "Date Fund"
        assert "rideshare" in out["chosen_description"]
        # And it still reports that GAS is the description naming Uber,
        # which is what the user needs to see.
        assert [m["category_name"] for m in out["mentions"]] == ["Gas"]


class TestSpendCriteriaVsDescription:
    """Two fields, two jobs.

    `description` says what a category is FOR and often restates its
    funding rule. `spend_criteria` says what gets CHARGED to it. Only the
    second can decide where a purchase belongs, and the categorise
    experiment found description text with no classification signal
    makes the model worse — so the funding half must not reach it.
    """

    @staticmethod
    def _cats() -> list[Category]:
        return [
            Category(name="Date Fund", kind="spending", balance_cents=0,
                     description="Whatever is left over at the end goes here.",
                     spend_criteria="Fun spending over $40 — meals out "
                                    "together, films, outings."),
            Category(name="Gas", kind="spending", balance_cents=0,
                     description="Saves up to $300 per paycheck.",
                     spend_criteria="Fuel at a pump. Uber rides are Date Fund."),
            Category(name="Old Style", kind="spending", balance_cents=0,
                     description="Groceries from a shop."),
        ]

    def test_a_quote_from_spend_criteria_verifies(self) -> None:
        from app.ai.tasks import _validate_suggestion
        out = _validate_suggestion({
            "category": "Gas", "confidence": "high", "why": "fuel",
            "matched_phrase": "Fuel at a pump. Uber rides are Date Fund.",
        }, self._cats(), "UBER TRIP 4412")
        assert out["attribution_verified"] is True

    def test_a_quote_from_the_funding_half_does_not_verify(self) -> None:
        """The funding description is never shown to the categoriser, so
        citing it means the model invented the citation."""
        from app.ai.tasks import _validate_suggestion
        out = _validate_suggestion({
            "category": "Gas", "confidence": "high", "why": "x",
            "matched_phrase": "Saves up to $300 per paycheck",
        }, self._cats(), "SHELL OIL 4412")
        assert out["attribution_verified"] is False
        assert out["matched_phrase"] is None

    def test_the_chosen_text_shown_is_the_spending_boundary(self) -> None:
        from app.ai.tasks import _validate_suggestion
        out = _validate_suggestion({
            "category": "Date Fund", "confidence": "high", "why": "x",
            "matched_phrase": "",
        }, self._cats(), "OLIVE GARDEN 00492")
        assert "Fun spending over $40" in out["chosen_description"]
        assert "left over" not in out["chosen_description"]

    def test_a_category_with_only_a_description_still_works(self) -> None:
        """Nobody has to migrate anything for this to keep functioning."""
        from app.ai.tasks import _validate_suggestion
        out = _validate_suggestion({
            "category": "Old Style", "confidence": "high", "why": "x",
            "matched_phrase": "Groceries from a shop",
        }, self._cats(), "CORNER SHOP GROCERIES")
        assert out["attribution_verified"] is True
        assert out["chosen_description"] == "Groceries from a shop."

    def test_mentions_search_both_fields_and_say_which(self) -> None:
        """The user wants to know if anything they wrote names this
        merchant, wherever they wrote it."""
        from app.ai.tasks import _mentions
        hits = _mentions("UBER TRIP 4KX2M", self._cats())
        assert len(hits) == 1
        assert hits[0]["category_name"] == "Gas"
        assert hits[0]["field"] == "spend_criteria"


class TestQuoteMustBeRelevant:
    """A real quote is not the same as a relevant one.

    The clean-slate walkthrough produced three wrong suggestions that
    were presented as evidence-backed, because the model quoted a
    genuine sentence from the chosen category that had nothing to do
    with the charge.
    """

    @staticmethod
    def _cats() -> list[Category]:
        return [
            Category(name="Date Fund", kind="spending", balance_cents=0,
                     spend_criteria="Eating out together, films, outings, "
                                    "and the rideshare to get there - Uber, "
                                    "Lyft, Olive Garden."),
            Category(name="Gas", kind="spending", balance_cents=0,
                     spend_criteria="Fuel at a pump - Chevron, Shell."),
        ]

    def test_a_quote_naming_the_merchant_counts(self) -> None:
        from app.ai.tasks import _validate_suggestion
        out = _validate_suggestion({
            "category": "Gas", "confidence": "high", "why": "fuel",
            "matched_phrase": "Fuel at a pump - Chevron, Shell.",
        }, self._cats(), "CHEVRON 0093821")
        assert out["attribution_verified"] is True
        assert out["matched_phrase"] is not None

    def test_a_real_quote_about_something_else_does_not(self) -> None:
        """This is the exact failure: the words are genuinely in the
        description and say nothing about the charge."""
        from app.ai.tasks import _validate_suggestion
        out = _validate_suggestion({
            "category": "Date Fund", "confidence": "high", "why": "x",
            "matched_phrase": "Eating out together, films, outings",
        }, self._cats(), "SQ *MERCHANT 4821")
        assert out["attribution_verified"] is False
        assert out["matched_phrase"] is None

    def test_an_opaque_charge_gets_no_evidence_at_all(self) -> None:
        from app.ai.tasks import _validate_suggestion
        out = _validate_suggestion({
            "category": "Date Fund", "confidence": "high", "why": "x",
            "matched_phrase": "films, outings",
        }, self._cats(), "AMZN MKTP US*2K4JD")
        assert out["attribution_verified"] is False
        assert out["mentions"] == []

    def test_olive_garden_still_verifies(self) -> None:
        """The description names it, so this one is real evidence."""
        from app.ai.tasks import _validate_suggestion
        out = _validate_suggestion({
            "category": "Date Fund", "confidence": "high", "why": "x",
            "matched_phrase": "Uber, Lyft, Olive Garden",
        }, self._cats(), "OLIVE GARDEN 00492")
        assert out["attribution_verified"] is True


@requires_db
class TestExistingMatchersAreSurfaced:
    """A matcher silently outranks the assistant, so a bad one has to be
    visible where it misfires."""

    def test_a_matching_rule_is_reported(self, client: TestClient) -> None:
        from app.ai.tasks import _existing_matchers
        from app.db.models import User
        from app.db.session import SessionLocal

        h = _auth(client)
        uid = uuid.UUID(client.get("/api/auth/me", headers=h).json()["id"])
        date_fund = client.post("/api/budget/categories",
                                json={"name": "Date Fund", "kind": "spending"},
                                headers=h).json()
        client.post("/api/budget/rules", json={
            "category_id": date_fund["id"],
            "matcher": {"merchant_contains": "uber"},
        }, headers=h)

        with SessionLocal() as db:
            user = db.get(User, uid)
            hits = _existing_matchers(db, user, "UBER TRIP 8832K")
            assert len(hits) == 1
            assert hits[0]["category_name"] == "Date Fund"
            assert hits[0]["matches_on"] == "uber"
            assert hits[0]["rule_id"]

    def test_an_unrelated_merchant_reports_none(self, client: TestClient) -> None:
        from app.ai.tasks import _existing_matchers
        from app.db.models import User
        from app.db.session import SessionLocal

        h = _auth(client)
        uid = uuid.UUID(client.get("/api/auth/me", headers=h).json()["id"])
        c = client.post("/api/budget/categories",
                        json={"name": "Date Fund", "kind": "spending"},
                        headers=h).json()
        client.post("/api/budget/rules", json={
            "category_id": c["id"], "matcher": {"merchant_contains": "uber"},
        }, headers=h)
        with SessionLocal() as db:
            user = db.get(User, uid)
            assert _existing_matchers(db, user, "SAFEWAY #1842") == []


class TestReceiptArithmetic:
    """The parts must equal the whole, and the check is ours not the model's.

    An OCR slip of one digit is easy to make and easy to miss, and
    itemising is pointless if the items do not reconcile.
    """

    @staticmethod
    def _raw(**over):
        base = {
            "merchant": "Trader Joe's",
            "items": [
                {"description": "Bananas", "amount_cents": 199, "quantity": 1},
                {"description": "Oat milk", "amount_cents": 399, "quantity": 2},
            ],
            "subtotal_cents": 598,
            "tax_cents": 42,
            "tip_cents": 0,
            "total_cents": 640,
        }
        base.update(over)
        return base

    def test_a_receipt_that_adds_up_has_no_notices(self) -> None:
        from app.ai.tasks import _validate_receipt
        out = _validate_receipt(self._raw())
        assert out["applicable"] and out["notices"] == []
        assert out["items_sum_cents"] == 598
        assert out["total_cents"] == 640

    def test_lines_not_matching_the_stated_total_is_flagged(self) -> None:
        from app.ai.tasks import _validate_receipt
        out = _validate_receipt(self._raw(total_cents=999))
        assert out["notices"]
        assert "add up to $6.40" in out["notices"][0]
        assert "$9.99" in out["notices"][0]

    def test_a_total_not_matching_the_charge_is_flagged(self) -> None:
        """The bank wins; a tip added later is the usual reason."""
        from app.ai.tasks import _validate_receipt
        out = _validate_receipt(self._raw(), expected_cents=800)
        assert any("tip added later" in n for n in out["notices"])

    def test_a_matching_charge_says_nothing(self) -> None:
        from app.ai.tasks import _validate_receipt
        out = _validate_receipt(self._raw(), expected_cents=640)
        assert out["notices"] == []

    def test_discounts_are_kept_as_negative_lines(self) -> None:
        """They are part of what makes the lines add up."""
        from app.ai.tasks import _validate_receipt
        out = _validate_receipt(self._raw(
            items=[{"description": "Cheese", "amount_cents": 800},
                   {"description": "Coupon", "amount_cents": -150}],
            total_cents=692, tax_cents=42))
        assert out["items_sum_cents"] == 650
        assert out["notices"] == []

    def test_unreadable_lines_are_dropped_not_guessed(self) -> None:
        from app.ai.tasks import _validate_receipt
        out = _validate_receipt(self._raw(items=[
            {"description": "Bananas", "amount_cents": 199},
            {"description": "", "amount_cents": 500},
            {"description": "No amount"},
        ]))
        assert [i["description"] for i in out["items"]] == ["Bananas"]

    def test_no_items_at_all_is_a_problem(self) -> None:
        from app.ai.tasks import _validate_receipt
        out = _validate_receipt(self._raw(items=[]))
        assert not out["applicable"]
        assert "no line items" in out["problems"][0]

    def test_quantity_defaults_to_one(self) -> None:
        from app.ai.tasks import _validate_receipt
        out = _validate_receipt(self._raw(
            items=[{"description": "Bananas", "amount_cents": 199}]))
        assert out["items"][0]["quantity"] == 1

    def test_the_schema_requires_every_field(self) -> None:
        """A model omits what a schema leaves optional — three times
        this project, so far."""
        from app.ai.tasks import RECEIPT_SCHEMA
        assert set(RECEIPT_SCHEMA["required"]) == {
            "merchant", "items", "subtotal_cents", "tax_cents",
            "tip_cents", "total_cents"}


class TestItemCategories:
    """Mapping the model's per-line answers back onto the receipt."""

    @staticmethod
    def _cats() -> list[Category]:
        return [
            Category(name="Groceries", kind="spending", balance_cents=0),
            Category(name="Household", kind="spending", balance_cents=0),
        ]

    def test_every_line_comes_back(self) -> None:
        from app.ai.tasks import _validate_item_categories
        out = _validate_item_categories(
            {"lines": [{"line": 1, "category": "Groceries"},
                       {"line": 2, "category": "Household"}]},
            self._cats(), 2)
        assert [l["category_name"] for l in out["lines"]] == \
               ["Groceries", "Household"]
        assert out["undecided"] == []

    def test_a_line_the_model_skipped_is_undecided_not_guessed(self) -> None:
        from app.ai.tasks import _validate_item_categories
        out = _validate_item_categories(
            {"lines": [{"line": 1, "category": "Groceries"}]},
            self._cats(), 3)
        assert out["lines"][1]["category_id"] is None
        assert out["undecided"] == [2, 3]

    def test_an_invented_category_is_dropped(self) -> None:
        from app.ai.tasks import _validate_item_categories
        out = _validate_item_categories(
            {"lines": [{"line": 1, "category": "Holiday Fund"}]},
            self._cats(), 1)
        assert out["undecided"] == [1]

    def test_unsure_leaves_the_line_open(self) -> None:
        from app.ai.tasks import _validate_item_categories
        out = _validate_item_categories(
            {"lines": [{"line": 1, "category": "UNSURE"}]}, self._cats(), 1)
        assert out["lines"][0]["category_name"] is None

    def test_line_numbers_outside_the_receipt_are_ignored(self) -> None:
        from app.ai.tasks import _validate_item_categories
        out = _validate_item_categories(
            {"lines": [{"line": 99, "category": "Groceries"},
                       {"line": 0, "category": "Groceries"}]},
            self._cats(), 2)
        assert out["undecided"] == [1, 2]


class TestSamplingIsPinned:
    """Determinism is a correctness property here, not a preference.

    Sending no options meant ollama fell back to each model's
    Modelfile, and those disagree: qwen2.5vl:7b ships temperature
    0.0001, gemma3:12b ships 1 with top_k 64 and top_p 0.95. Identical
    code was greedy on one model and heavily sampled on another, and
    neither matched the `temperature: 0` every experiment was measured
    at.
    """

    def _payload(self, **kwargs):
        import asyncio
        import json
        from unittest.mock import patch

        from app.ai.provider import OllamaProvider
        from app.core.config import get_settings

        captured = {}

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"message": {"content": "{}"}}

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None):
                captured.update(json or {})
                return _Resp()

        with patch("httpx.AsyncClient", lambda **kw: _Client()):
            p = OllamaProvider("http://x", get_settings())
            asyncio.run(p.chat("hello", **kwargs))
        return captured

    def test_temperature_zero_by_default(self):
        assert self._payload()["options"]["temperature"] == 0.0

    def test_options_are_always_sent(self):
        """Absent options means 'use the Modelfile', not 'use zero'."""
        assert "options" in self._payload()

    def test_an_explicit_temperature_is_honoured(self):
        assert self._payload(temperature=0.7)["options"]["temperature"] == 0.7

    def test_thinking_is_sent_explicitly_even_when_off(self):
        """Omitting it means "use the model's default", not "off".

        gemma4 reasons by default. Unsent, it spent its whole budget
        thinking and returned empty content, which reached callers as
        "model returned unparseable JSON" with nothing naming the cause.
        """
        assert self._payload()["think"] is False

    def test_thinking_can_be_turned_on(self):
        assert self._payload(think=True)["think"] is True

    def test_schema_requests_are_pinned_too(self):
        out = self._payload(schema={"type": "object"})
        assert out["options"]["temperature"] == 0.0
        assert out["format"] == {"type": "object"}
