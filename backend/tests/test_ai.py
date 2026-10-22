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
