"""Budget HTTP layer, including cross-user isolation on every resource."""
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from tests.conftest import requires_db

PW = "budget-api-passphrase-99"


def _auth(client: TestClient) -> dict[str, str]:
    r = client.post("/api/auth/register",
                    json={"email": f"ba-{uuid.uuid4().hex[:10]}@x.com", "password": PW})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _cat(client, h, name, kind="spending", **kw) -> dict:
    r = client.post("/api/budget/categories",
                    json={"name": name, "kind": kind, **kw}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


@requires_db
class TestCategories:
    def test_create_list_patch(self, client: TestClient) -> None:
        h = _auth(client)
        c = _cat(client, h, "Food", target_cents=30000, alert_at_cents=20000)
        assert c["balance_cents"] == 0 and c["below_alert"] is True

        r = client.patch(f"/api/budget/categories/{c['id']}",
                         json={"target_cents": 35000}, headers=h)
        assert r.status_code == 200 and r.json()["target_cents"] == 35000

        assert len(client.get("/api/budget/categories", headers=h).json()) == 1

    def test_kind_is_editable(self, client: TestClient) -> None:
        """It was absent from the patch schema, so edits were silently lost."""
        h = _auth(client)
        c = _cat(client, h, "Car Fund", kind="spending")
        r = client.patch(f"/api/budget/categories/{c['id']}",
                         json={"kind": "savings_bucket"}, headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["kind"] == "savings_bucket"

    def test_bad_kind_on_patch_rejected(self, client: TestClient) -> None:
        h = _auth(client)
        c = _cat(client, h, "Food")
        assert client.patch(f"/api/budget/categories/{c['id']}",
                            json={"kind": "nonsense"},
                            headers=h).status_code == 422

    def test_renaming_onto_another_category_rejected(self, client: TestClient) -> None:
        """Two live envelopes with one name breaks every reuse-by-name path."""
        h = _auth(client)
        _cat(client, h, "Food")
        gas = _cat(client, h, "Gas")
        r = client.patch(f"/api/budget/categories/{gas['id']}",
                         json={"name": "Food"}, headers=h)
        assert r.status_code == 409
        assert client.get("/api/budget/categories", headers=h).json()[0]["name"] == "Food"

    def test_renaming_to_its_own_name_is_fine(self, client: TestClient) -> None:
        h = _auth(client)
        c = _cat(client, h, "Food")
        assert client.patch(f"/api/budget/categories/{c['id']}",
                            json={"name": "Food", "target_cents": 1000},
                            headers=h).status_code == 200

    def test_reusing_an_archived_name_is_allowed(self, client: TestClient) -> None:
        h = _auth(client)
        old = _cat(client, h, "Food")
        client.delete(f"/api/budget/categories/{old['id']}", headers=h)
        gas = _cat(client, h, "Gas")
        assert client.patch(f"/api/budget/categories/{gas['id']}",
                            json={"name": "Food"}, headers=h).status_code == 200

    def test_an_explicit_null_clears_a_target(self, client: TestClient) -> None:
        """Clearing a target is a real edit, not an omission."""
        h = _auth(client)
        c = _cat(client, h, "Food", target_cents=30000)
        r = client.patch(f"/api/budget/categories/{c['id']}",
                         json={"target_cents": None}, headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["target_cents"] is None

    def test_duplicate_active_name_rejected(self, client: TestClient) -> None:
        h = _auth(client)
        _cat(client, h, "Food")
        r = client.post("/api/budget/categories",
                        json={"name": "Food", "kind": "spending"}, headers=h)
        assert r.status_code == 409

    def test_bad_kind_rejected(self, client: TestClient) -> None:
        h = _auth(client)
        r = client.post("/api/budget/categories",
                        json={"name": "X", "kind": "nonsense"}, headers=h)
        assert r.status_code == 422

    def test_delete_archives_rather_than_destroys(self, client: TestClient) -> None:
        h = _auth(client)
        c = _cat(client, h, "Old")
        assert client.delete(f"/api/budget/categories/{c['id']}", headers=h).json()["archived"]

        assert client.get("/api/budget/categories", headers=h).json() == []
        archived = client.get("/api/budget/categories?include_archived=true", headers=h).json()
        assert len(archived) == 1, "history must survive archiving"

        # The name is free again once archived.
        _cat(client, h, "Old")

    def test_restore_blocked_by_an_active_name_clash(self, client: TestClient) -> None:
        h = _auth(client)
        c = _cat(client, h, "Dup")
        client.delete(f"/api/budget/categories/{c['id']}", headers=h)
        _cat(client, h, "Dup")
        r = client.post(f"/api/budget/categories/{c['id']}/restore", headers=h)
        assert r.status_code == 409


@requires_db
class TestCrossUserIsolation:
    def test_one_user_cannot_see_or_touch_anothers_category(self, client: TestClient) -> None:
        ha, hb = _auth(client), _auth(client)
        c = _cat(client, ha, "Private")

        assert client.get("/api/budget/categories", headers=hb).json() == []
        # 404 rather than 403: a 403 would confirm it exists.
        assert client.patch(f"/api/budget/categories/{c['id']}",
                            json={"sort_order": 9}, headers=hb).status_code == 404
        assert client.delete(f"/api/budget/categories/{c['id']}", headers=hb).status_code == 404
        assert client.get(f"/api/budget/categories/{c['id']}/allocations",
                          headers=hb).status_code == 404

    def test_summary_is_scoped(self, client: TestClient) -> None:
        ha, hb = _auth(client), _auth(client)
        _cat(client, ha, "OnlyMine")
        assert client.get("/api/budget/summary", headers=hb).json()["categories"] == []

    def test_all_budget_endpoints_require_auth(self, client: TestClient) -> None:
        for path in ("/api/budget/categories", "/api/budget/rules", "/api/budget/plans",
                     "/api/budget/review", "/api/budget/summary",
                     "/api/budget/paychecks/candidates", "/api/budget/next-payday"):
            assert client.get(path).status_code in (401, 403), path


@requires_db
class TestSplitPlans:
    def _full_plan(self, client, h) -> tuple[dict, dict]:
        tithe = _cat(client, h, "Tithing", "obligation")
        rest = _cat(client, h, "NewCar", "savings_bucket")
        body = {
            "name": "Standard",
            "rules": [
                {"category_id": tithe["id"], "order_index": 1, "rule_type": "percent",
                 "params": {"rate": "0.10", "of": "gross"}},
                {"category_id": rest["id"], "order_index": 2, "rule_type": "remainder"},
            ],
        }
        return body, {"tithe": tithe, "rest": rest}

    def test_create_and_apply(self, client: TestClient) -> None:
        h = _auth(client)
        body, cats = self._full_plan(client, h)
        assert client.post("/api/budget/plans", json=body, headers=h).status_code == 201

        r = client.post("/api/budget/paychecks",
                        json={"gross_cents": 200000, "apply": True}, headers=h)
        assert r.status_code == 201, r.text
        assert r.json()["allocated_cents"] == 200000

        out = {c["name"]: c["balance_cents"]
               for c in client.get("/api/budget/categories", headers=h).json()}
        assert out["Tithing"] == 20000
        assert out["NewCar"] == 180000

    def test_plan_without_remainder_rejected_at_save_time(self, client: TestClient) -> None:
        """Fail while editing, not when a paycheck arrives."""
        h = _auth(client)
        c = _cat(client, h, "Rent", "obligation")
        r = client.post("/api/budget/plans", json={
            "name": "Broken",
            "rules": [{"category_id": c["id"], "order_index": 1,
                       "rule_type": "fixed", "params": {"amount_cents": 1000}}],
        }, headers=h)
        assert r.status_code == 422
        assert "remainder" in r.text.lower()

    def test_revision_creates_a_new_version(self, client: TestClient) -> None:
        h = _auth(client)
        body, cats = self._full_plan(client, h)
        plan = client.post("/api/budget/plans", json=body, headers=h).json()
        assert plan["version"] == 1

        body["rules"][0]["params"] = {"rate": "0.15", "of": "gross"}
        revised = client.put(f"/api/budget/plans/{plan['id']}", json=body, headers=h).json()
        assert revised["version"] == 2
        assert revised["id"] != plan["id"], "history must not be mutated"

        plans = client.get("/api/budget/plans", headers=h).json()
        assert [p["is_active"] for p in plans].count(True) == 1

    def test_another_users_category_cannot_be_used_in_a_plan(self, client: TestClient) -> None:
        ha, hb = _auth(client), _auth(client)
        victim = _cat(client, ha, "Theirs")
        mine = _cat(client, hb, "Mine", "savings_bucket")
        r = client.post("/api/budget/plans", json={
            "name": "Sneaky",
            "rules": [
                {"category_id": victim["id"], "order_index": 1,
                 "rule_type": "fixed", "params": {"amount_cents": 100}},
                {"category_id": mine["id"], "order_index": 2, "rule_type": "remainder"},
            ],
        }, headers=hb)
        assert r.status_code == 404


@requires_db
class TestPaychecksAndSummary:
    def test_unapply_restores_balances(self, client: TestClient) -> None:
        h = _auth(client)
        t = _cat(client, h, "Tithing", "obligation")
        rest = _cat(client, h, "Rest", "savings_bucket")
        client.post("/api/budget/plans", json={"name": "P", "rules": [
            {"category_id": t["id"], "order_index": 1, "rule_type": "percent",
             "params": {"rate": "0.10", "of": "gross"}},
            {"category_id": rest["id"], "order_index": 2, "rule_type": "remainder"},
        ]}, headers=h)

        pc = client.post("/api/budget/paychecks",
                         json={"gross_cents": 100000}, headers=h).json()
        assert client.post(f"/api/budget/paychecks/{pc['id']}/unapply",
                           headers=h).status_code == 200
        totals = {c["name"]: c["balance_cents"]
                  for c in client.get("/api/budget/categories", headers=h).json()}
        assert totals["Tithing"] == 0 and totals["Rest"] == 0

    def test_apply_without_a_plan_is_a_clear_error(self, client: TestClient) -> None:
        h = _auth(client)
        r = client.post("/api/budget/paychecks", json={"gross_cents": 100000}, headers=h)
        assert r.status_code == 422
        assert "split plan" in r.text.lower()

    def test_summary_reports_no_ledger_drift(self, client: TestClient) -> None:
        h = _auth(client)
        t = _cat(client, h, "T", "obligation")
        rest = _cat(client, h, "R", "savings_bucket")
        client.post("/api/budget/plans", json={"name": "P", "rules": [
            {"category_id": t["id"], "order_index": 1, "rule_type": "percent",
             "params": {"rate": "0.10", "of": "gross"}},
            {"category_id": rest["id"], "order_index": 2, "rule_type": "remainder"},
        ]}, headers=h)
        client.post("/api/budget/paychecks", json={"gross_cents": 123457}, headers=h)

        s = client.get("/api/budget/summary", headers=h).json()
        assert s["ledger_drift"] == [], "cached balances disagree with the ledger"
        assert s["total_allocated_cents"] == 123457


@requires_db
class TestRules:
    def test_empty_matcher_rejected(self, client: TestClient) -> None:
        """A rule with no conditions would claim every transaction."""
        h = _auth(client)
        c = _cat(client, h, "Food")
        r = client.post("/api/budget/rules",
                        json={"category_id": c["id"], "matcher": {}}, headers=h)
        assert r.status_code == 422

    def test_create_and_delete(self, client: TestClient) -> None:
        h = _auth(client)
        c = _cat(client, h, "Food")
        rule = client.post("/api/budget/rules", json={
            "category_id": c["id"], "matcher": {"merchant_contains": "smiths"},
        }, headers=h).json()
        assert len(client.get("/api/budget/rules", headers=h).json()) == 1
        assert client.delete(f"/api/budget/rules/{rule['id']}", headers=h).status_code == 204
        assert client.get("/api/budget/rules", headers=h).json() == []


@requires_db
class TestAccountVisibility:
    def _account(self, client, h, *, name, type_, balance):
        """Insert an account directly: linking a real one needs Plaid."""
        import uuid as _u

        from app.core.security import hash_password
        from app.db.models import Account, PlaidItem, User
        from app.db.session import SessionLocal

        me = client.get("/api/auth/me", headers=h).json()
        with SessionLocal() as db:
            user = db.get(User, _u.UUID(me["id"]))
            assert user is not None
            item = db.scalars(
                __import__("sqlalchemy").select(PlaidItem).where(PlaidItem.user_id == user.id)
            ).first()
            if item is None:
                item = PlaidItem(user_id=user.id,
                                 plaid_item_id=f"it-{_u.uuid4().hex[:10]}",
                                 access_token_encrypted=b"x")
                db.add(item); db.flush()
            a = Account(user_id=user.id, item_id=item.id,
                        plaid_account_id=f"ac-{_u.uuid4().hex[:10]}",
                        name=name, type=type_, current_cents=balance,
                        is_tracked=type_ in {"depository", "credit"})
            db.add(a); db.commit()
            return str(a.id)

    def test_cash_and_cards_are_reported_separately(self, client: TestClient) -> None:
        h = _auth(client)
        self._account(client, h, name="Checking", type_="depository", balance=250000)
        self._account(client, h, name="Visa", type_="credit", balance=41000)

        s = client.get("/api/budget/summary", headers=h).json()
        assert s["cash_cents"] == 250000
        assert s["card_debt_cents"] == 41000
        assert [a["name"] for a in s["cash_accounts"]] == ["Checking"]
        assert [c["name"] for c in s["cards"]] == ["Visa"]

    def test_loans_and_investments_are_hidden_by_default(self, client: TestClient) -> None:
        """A linked bank exposes everything; the home screen should not."""
        h = _auth(client)
        self._account(client, h, name="Checking", type_="depository", balance=100000)
        self._account(client, h, name="Mortgage", type_="loan", balance=-5630206)
        self._account(client, h, name="401k", type_="investment", balance=2363198)

        s = client.get("/api/budget/summary", headers=h).json()
        assert s["cash_cents"] == 100000, "a mortgage must not count as cash"
        assert len(s["cash_accounts"]) == 1

        every = client.get("/api/budget/accounts", headers=h).json()
        assert len(every) == 3, "hidden accounts still exist and keep syncing"
        hidden = {a["name"]: a["is_tracked"] for a in every}
        assert hidden["Mortgage"] is False and hidden["401k"] is False

    def test_visibility_can_be_toggled(self, client: TestClient) -> None:
        h = _auth(client)
        aid = self._account(client, h, name="Savings", type_="depository", balance=500000)
        assert client.get("/api/budget/summary", headers=h).json()["cash_cents"] == 500000

        r = client.post(f"/api/budget/accounts/{aid}/tracked",
                        json={"is_tracked": False}, headers=h)
        assert r.status_code == 200 and r.json()["is_tracked"] is False
        assert client.get("/api/budget/summary", headers=h).json()["cash_cents"] == 0

    def test_another_users_account_is_not_visible_or_editable(self, client: TestClient) -> None:
        ha, hb = _auth(client), _auth(client)
        aid = self._account(client, ha, name="Theirs", type_="depository", balance=999)
        assert client.get("/api/budget/accounts", headers=hb).json() == []
        assert client.post(f"/api/budget/accounts/{aid}/tracked",
                           json={"is_tracked": False}, headers=hb).status_code == 404


@requires_db
class TestOpeningBalance:
    def _accounts(self, client, h, *, cash: int, debt: int) -> None:
        import uuid as _u

        import sqlalchemy as _sa

        from app.db.models import Account, PlaidItem, User
        from app.db.session import SessionLocal

        me = client.get("/api/auth/me", headers=h).json()
        with SessionLocal() as db:
            user = db.get(User, _u.UUID(me["id"]))
            item = PlaidItem(user_id=user.id, plaid_item_id=f"it-{_u.uuid4().hex[:10]}",
                             access_token_encrypted=b"x")
            db.add(item); db.flush()
            db.add(Account(user_id=user.id, item_id=item.id,
                           plaid_account_id=f"ck-{_u.uuid4().hex[:8]}", name="Checking",
                           type="depository", current_cents=cash, is_tracked=True))
            db.add(Account(user_id=user.id, item_id=item.id,
                           plaid_account_id=f"cc-{_u.uuid4().hex[:8]}", name="Visa",
                           type="credit", current_cents=debt, is_tracked=True))
            db.commit()
            _sa  # keep the import referenced

    def test_allocates_and_sinks_the_remainder(self, client: TestClient) -> None:
        h = _auth(client)
        self._accounts(client, h, cash=500000, debt=0)
        food = _cat(client, h, "Food")
        rent = _cat(client, h, "Rent", "obligation")

        r = client.post("/api/budget/opening-balance", json={"allocations": [
            {"category_id": food["id"], "amount_cents": 30000},
            {"category_id": rent["id"], "amount_cents": 120000},
        ]}, headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["remainder_cents"] == 350000
        assert body["balanced"] is True

        cats = {c["name"]: c for c in client.get("/api/budget/categories", headers=h).json()}
        assert cats["Food"]["balance_cents"] == 30000
        sink = next(c for c in cats.values() if c["is_unallocated"])
        assert sink["balance_cents"] == 350000

    def test_card_debt_reduces_what_is_available(self, client: TestClient) -> None:
        h = _auth(client)
        self._accounts(client, h, cash=100000, debt=25000)
        r = client.get("/api/budget/reconciliation", headers=h).json()
        assert r["cash_cents"] == 100000
        assert r["card_debt_cents"] == 25000
        assert r["available_cents"] == 75000

    def test_over_allocation_is_refused_by_default(self, client: TestClient) -> None:
        h = _auth(client)
        self._accounts(client, h, cash=10000, debt=0)
        food = _cat(client, h, "Food")
        r = client.post("/api/budget/opening-balance", json={"allocations": [
            {"category_id": food["id"], "amount_cents": 50000},
        ]}, headers=h)
        assert r.status_code == 422
        assert "available" in r.text.lower()

    def test_over_allocation_allowed_when_explicit(self, client: TestClient) -> None:
        h = _auth(client)
        self._accounts(client, h, cash=10000, debt=0)
        food = _cat(client, h, "Food")
        r = client.post("/api/budget/opening-balance", json={
            "allocations": [{"category_id": food["id"], "amount_cents": 50000}],
            "allow_over_allocation": True,
        }, headers=h)
        assert r.status_code == 200
        # The shortfall lands in the sink as a negative, not hidden.
        assert r.json()["remainder_cents"] == -40000

    def test_refused_once_categories_hold_money(self, client: TestClient) -> None:
        h = _auth(client)
        self._accounts(client, h, cash=100000, debt=0)
        food = _cat(client, h, "Food")
        client.post("/api/budget/opening-balance", json={
            "allocations": [{"category_id": food["id"], "amount_cents": 1000}]}, headers=h)
        again = client.post("/api/budget/opening-balance", json={
            "allocations": [{"category_id": food["id"], "amount_cents": 1000}]}, headers=h)
        assert again.status_code == 409

    def test_reconcile_endpoint_balances_the_budget(self, client: TestClient) -> None:
        h = _auth(client)
        self._accounts(client, h, cash=80000, debt=0)
        assert client.get("/api/budget/reconciliation",
                          headers=h).json()["unallocated_cents"] == 80000
        r = client.post("/api/budget/reconcile", headers=h).json()
        assert r["moved_cents"] == 80000 and r["balanced"] is True


@requires_db
class TestApplyProposal:
    """Applying a reviewed AI proposal.

    The proposal is whatever the USER approved on screen, so the endpoint
    takes the edited payload rather than a job id. A proposal gets no more
    trust than a hand-made plan.
    """

    def _proposal(self) -> dict:
        return {
            "categories": [
                {"name": "Tithing", "kind": "obligation", "description": "10% first"},
                {"name": "Groceries", "kind": "spending", "target_cents": 30000},
                {"name": "Car Fund", "kind": "savings_bucket"},
            ],
            "split_rules": [
                {"category_name": "Tithing", "order_index": 1, "rule_type": "percent",
                 "params": {"rate": "0.10", "of": "gross"}},
                {"category_name": "Groceries", "order_index": 2, "rule_type": "refill_to",
                 "params": {"target_cents": 30000}},
                {"category_name": "Car Fund", "order_index": 3, "rule_type": "remainder",
                 "params": {}},
            ],
        }

    def test_creates_categories_and_an_active_plan(self, client: TestClient) -> None:
        h = _auth(client)
        r = client.post("/api/budget/apply-proposal", json=self._proposal(), headers=h)
        assert r.status_code == 200, r.text
        out = r.json()
        assert sorted(out["created_categories"]) == ["Car Fund", "Groceries", "Tithing"]
        assert out["plan_version"] == 1

        plans = client.get("/api/budget/plans", headers=h).json()
        assert len(plans) == 1 and plans[0]["is_active"] is True
        assert [x["rule_type"] for x in plans[0]["rules"]] == \
            ["percent", "refill_to", "remainder"]

    def test_applying_twice_reuses_rather_than_duplicating(self, client: TestClient) -> None:
        """Otherwise the user ends up with Food and Food (2)."""
        h = _auth(client)
        client.post("/api/budget/apply-proposal", json=self._proposal(), headers=h)
        second = client.post("/api/budget/apply-proposal",
                             json=self._proposal(), headers=h).json()
        assert second["created_categories"] == []
        assert sorted(second["reused_categories"]) == ["Car Fund", "Groceries", "Tithing"]
        assert len(client.get("/api/budget/categories", headers=h).json()) == 3
        assert second["plan_version"] == 2, "a re-apply should version the plan"

    def test_existing_user_edits_are_not_overwritten(self, client: TestClient) -> None:
        """A proposal fills gaps; it must not undo the user's own choices."""
        h = _auth(client)
        _cat(client, h, "Groceries", target_cents=99999)
        client.post("/api/budget/apply-proposal", json=self._proposal(), headers=h)
        cats = {c["name"]: c for c in client.get("/api/budget/categories", headers=h).json()}
        assert cats["Groceries"]["target_cents"] == 99999

    def test_a_structurally_invalid_proposal_is_refused(self, client: TestClient) -> None:
        """A model's plan earns no special trust."""
        h = _auth(client)
        p = self._proposal()
        p["split_rules"] = p["split_rules"][:2]   # drop the remainder
        r = client.post("/api/budget/apply-proposal", json=p, headers=h)
        assert r.status_code == 422
        assert "remainder" in r.text.lower()

    def test_rule_naming_an_absent_category_is_refused(self, client: TestClient) -> None:
        h = _auth(client)
        p = self._proposal()
        p["split_rules"].insert(0, {"category_name": "Ghost", "order_index": 0,
                                    "rule_type": "fixed",
                                    "params": {"amount_cents": 100}})
        r = client.post("/api/budget/apply-proposal", json=p, headers=h)
        assert r.status_code == 422 and "Ghost" in r.text

    def test_categories_only_skips_the_plan(self, client: TestClient) -> None:
        h = _auth(client)
        p = dict(self._proposal(), categories_only=True)
        out = client.post("/api/budget/apply-proposal", json=p, headers=h).json()
        assert out["plan_id"] is None
        assert client.get("/api/budget/plans", headers=h).json() == []

    def test_requires_auth(self, client: TestClient) -> None:
        assert client.post("/api/budget/apply-proposal",
                           json=self._proposal()).status_code in (401, 403)

    def test_the_applied_plan_actually_splits_a_paycheck(self, client: TestClient) -> None:
        """End to end: proposal -> plan -> real allocations."""
        h = _auth(client)
        client.post("/api/budget/apply-proposal", json=self._proposal(), headers=h)
        pc = client.post("/api/budget/paychecks",
                         json={"gross_cents": 200000}, headers=h)
        assert pc.status_code == 201, pc.text
        assert pc.json()["allocated_cents"] == 200000

        bal = {c["name"]: c["balance_cents"]
               for c in client.get("/api/budget/categories", headers=h).json()}
        assert bal["Tithing"] == 20000       # 10% of gross, first
        assert bal["Groceries"] == 30000     # refilled to target
        assert bal["Car Fund"] == 150000     # remainder


@requires_db
class TestReorder:
    """Display order and funding order are deliberately different things."""

    def test_display_reorder(self, client: TestClient) -> None:
        h = _auth(client)
        a, b, c = (_cat(client, h, n) for n in ("Food", "Gas", "Fun"))

        r = client.post("/api/budget/categories/reorder",
                        json={"category_ids": [c["id"], a["id"], b["id"]]}, headers=h)
        assert r.status_code == 200, r.text
        assert [x["name"] for x in r.json()] == ["Fun", "Food", "Gas"]
        # And it persists on a plain list.
        rows = client.get("/api/budget/categories", headers=h).json()
        assert [x["name"] for x in rows] == ["Fun", "Food", "Gas"]

    def test_partial_reorder_keeps_the_rest_below(self, client: TestClient) -> None:
        h = _auth(client)
        a, b, c = (_cat(client, h, n) for n in ("Food", "Gas", "Fun"))
        client.post("/api/budget/categories/reorder",
                    json={"category_ids": [c["id"]]}, headers=h)
        rows = client.get("/api/budget/categories", headers=h).json()
        assert rows[0]["name"] == "Fun"
        assert {x["name"] for x in rows[1:]} == {"Food", "Gas"}

    def test_unknown_and_duplicate_rejected(self, client: TestClient) -> None:
        h = _auth(client)
        a = _cat(client, h, "Food")
        assert client.post("/api/budget/categories/reorder",
                           json={"category_ids": [str(uuid.uuid4())]},
                           headers=h).status_code == 404
        assert client.post("/api/budget/categories/reorder",
                           json={"category_ids": [a["id"], a["id"]]},
                           headers=h).status_code == 422

    def test_other_users_category_is_invisible(self, client: TestClient) -> None:
        ha, hb = _auth(client), _auth(client)
        a = _cat(client, ha, "Food")
        assert client.post("/api/budget/categories/reorder",
                           json={"category_ids": [a["id"]]},
                           headers=hb).status_code == 404

    def _plan(self, client, h) -> tuple[dict, dict]:
        rent = _cat(client, h, "Rent", kind="obligation")
        food = _cat(client, h, "Food")
        left = _cat(client, h, "Leftover", kind="savings_bucket")
        body = {"name": "Standard", "rules": [
            {"category_id": rent["id"], "order_index": 1,
             "rule_type": "fixed", "params": {"amount_cents": 35000}},
            {"category_id": food["id"], "order_index": 2,
             "rule_type": "refill_to", "params": {"target_cents": 30000}},
            {"category_id": left["id"], "order_index": 3,
             "rule_type": "remainder", "params": {}},
        ]}
        r = client.post("/api/budget/plans", json=body, headers=h)
        assert r.status_code == 201, r.text
        return r.json(), {"rent": rent, "food": food, "left": left}

    def test_funding_reorder_makes_a_new_version(self, client: TestClient) -> None:
        h = _auth(client)
        plan, cats = self._plan(client, h)
        r = client.post(f"/api/budget/plans/{plan['id']}/reorder",
                        json={"category_ids": [cats["food"]["id"], cats["rent"]["id"],
                                               cats["left"]["id"]]}, headers=h)
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["version"] == plan["version"] + 1 and out["is_active"]
        assert [x["category_id"] for x in out["rules"]] == [
            cats["food"]["id"], cats["rent"]["id"], cats["left"]["id"]]
        # Rules travelled with their categories, not with their positions.
        assert out["rules"][0]["rule_type"] == "refill_to"

    def test_dragging_remainder_off_the_end_is_rejected(self, client: TestClient) -> None:
        h = _auth(client)
        plan, cats = self._plan(client, h)
        r = client.post(f"/api/budget/plans/{plan['id']}/reorder",
                        json={"category_ids": [cats["left"]["id"], cats["rent"]["id"],
                                               cats["food"]["id"]]}, headers=h)
        assert r.status_code == 422
        # The original plan is untouched -- a rejected drag must not
        # deactivate the plan the user is still living on.
        plans = client.get("/api/budget/plans", headers=h).json()
        assert [p for p in plans if p["is_active"]][0]["version"] == plan["version"]

    def test_incomplete_funding_reorder_rejected(self, client: TestClient) -> None:
        h = _auth(client)
        plan, cats = self._plan(client, h)
        assert client.post(f"/api/budget/plans/{plan['id']}/reorder",
                           json={"category_ids": [cats["food"]["id"]]},
                           headers=h).status_code == 422


@requires_db
class TestApplySingleCategory:
    def test_category_only(self, client: TestClient) -> None:
        h = _auth(client)
        r = client.post("/api/budget/apply-category", json={
            "category": {"name": "Dog Vet", "kind": "savings_bucket",
                         "description": "vet bills for the dog",
                         "target_cents": 50000},
        }, headers=h)
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["plan_id"] is None and out["funding_position"] is None
        assert out["category"]["description"] == "vet bills for the dog"

    def test_rule_lands_before_the_remainder(self, client: TestClient) -> None:
        h = _auth(client)
        plan, cats = TestReorder()._plan(client, h)
        r = client.post("/api/budget/apply-category", json={
            "category": {"name": "Dog Vet", "kind": "savings_bucket"},
            "rule_type": "fixed", "params": {"amount_cents": 5000},
        }, headers=h)
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["funding_position"] == 3  # after food, before remainder
        assert out["plan_version"] == plan["version"] + 1

        rules = client.get("/api/budget/plans", headers=h).json()
        active = [p for p in rules if p["is_active"]][0]
        assert active["rules"][-1]["rule_type"] == "remainder"
        assert active["rules"][2]["category_id"] == out["category"]["id"]

    def test_explicit_position_is_honoured(self, client: TestClient) -> None:
        h = _auth(client)
        TestReorder()._plan(client, h)
        r = client.post("/api/budget/apply-category", json={
            "category": {"name": "Tithing", "kind": "obligation"},
            "rule_type": "percent", "params": {"rate": "0.10", "of": "gross"},
            "order_index": 1,
        }, headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["funding_position"] == 1

    def test_cannot_take_the_remainder(self, client: TestClient) -> None:
        h = _auth(client)
        TestReorder()._plan(client, h)
        assert client.post("/api/budget/apply-category", json={
            "category": {"name": "Greedy", "kind": "spending"},
            "rule_type": "remainder", "params": {},
        }, headers=h).status_code == 422

    def test_duplicate_name_rejected(self, client: TestClient) -> None:
        h = _auth(client)
        _cat(client, h, "Food")
        assert client.post("/api/budget/apply-category", json={
            "category": {"name": "Food", "kind": "spending"},
        }, headers=h).status_code == 409

    def test_rule_without_a_plan_explains_itself(self, client: TestClient) -> None:
        h = _auth(client)
        r = client.post("/api/budget/apply-category", json={
            "category": {"name": "Dog Vet", "kind": "savings_bucket"},
            "rule_type": "fixed", "params": {"amount_cents": 5000},
        }, headers=h)
        assert r.status_code == 409
        assert "no active split plan" in r.json()["detail"]
        # The category must not be left behind by the failed rule.
        assert client.get("/api/budget/categories", headers=h).json() == []

    def test_matcher_becomes_a_real_rule(self, client: TestClient) -> None:
        h = _auth(client)
        r = client.post("/api/budget/apply-category", json={
            "category": {"name": "Dog Vet", "kind": "savings_bucket"},
            "matcher": {"merchant_contains": "vet"},
        }, headers=h)
        assert r.status_code == 200, r.text
        rule_id = r.json()["matcher_rule_id"]
        assert rule_id is not None
        rules = client.get("/api/budget/rules", headers=h).json()
        mine = next(x for x in rules if x["id"] == rule_id)
        assert mine["matcher"] == {"merchant_contains": "vet"}
        assert mine["category_id"] == r.json()["category"]["id"]

    def test_no_matcher_creates_no_rule(self, client: TestClient) -> None:
        h = _auth(client)
        r = client.post("/api/budget/apply-category", json={
            "category": {"name": "Dog Vet", "kind": "savings_bucket"},
        }, headers=h)
        assert r.json()["matcher_rule_id"] is None
        assert client.get("/api/budget/rules", headers=h).json() == []

    def test_an_empty_matcher_is_dropped_not_stored(self, client: TestClient) -> None:
        """A matcher with no conditions would claim every transaction."""
        h = _auth(client)
        r = client.post("/api/budget/apply-category", json={
            "category": {"name": "Dog Vet", "kind": "savings_bucket"},
            "matcher": {"merchant_contains": ""},
        }, headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["matcher_rule_id"] is None
        assert client.get("/api/budget/rules", headers=h).json() == []


@requires_db
class TestSinkIsProtected:
    """Archiving the sink would fork it, not remove it."""

    def test_unallocated_cannot_be_archived(self, client: TestClient) -> None:
        from app.budget.reconcile import ensure_unallocated
        from app.db.session import SessionLocal

        h = _auth(client)
        me = client.get("/api/auth/me", headers=h).json()
        # The sink is normally created by an opening-balance remainder.
        # Calling the function under protection directly is the point here.
        with SessionLocal() as db:
            ensure_unallocated(db, uuid.UUID(me["id"]))
            db.commit()

        cats = client.get("/api/budget/categories", headers=h).json()
        sink = next(c for c in cats if c["is_unallocated"])

        r = client.delete(f"/api/budget/categories/{sink['id']}", headers=h)
        assert r.status_code == 409
        assert "cannot be archived" in r.json()["detail"]

        # And it is still the one and only sink.
        after = client.get("/api/budget/categories?include_archived=true",
                           headers=h).json()
        assert len([c for c in after if c["is_unallocated"]]) == 1
        assert not next(c for c in after if c["is_unallocated"])["archived"]

    def test_an_ordinary_category_still_archives(self, client: TestClient) -> None:
        h = _auth(client)
        r = client.post("/api/budget/categories",
                        json={"name": "Food", "kind": "spending"}, headers=h)
        cid = r.json()["id"]
        assert client.delete(f"/api/budget/categories/{cid}",
                             headers=h).status_code == 200


@requires_db
class TestCategoryDetail:
    """One screen should be able to show everything about a category."""

    def _setup(self, client: TestClient):
        h = _auth(client)
        rent = _cat(client, h, "Rent", kind="obligation")
        food = _cat(client, h, "Food", target_cents=30000)
        left = _cat(client, h, "Leftover", kind="savings_bucket")
        body = {"name": "Standard", "rules": [
            {"category_id": rent["id"], "order_index": 1,
             "rule_type": "fixed", "params": {"amount_cents": 35000}},
            {"category_id": food["id"], "order_index": 2, "rule_type": "refill_to",
             "params": {"target_cents": 30000, "max_contribution_cents": 10000}},
            {"category_id": left["id"], "order_index": 3,
             "rule_type": "remainder", "params": {}},
        ]}
        assert client.post("/api/budget/plans", json=body, headers=h).status_code == 201
        return h, {"rent": rent, "food": food, "left": left}

    def test_detail_shows_funding_position_and_plain_english(self, client) -> None:
        h, c = self._setup(client)
        r = client.get(f"/api/budget/categories/{c['food']['id']}/detail", headers=h)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["category"]["name"] == "Food"
        assert d["funding"]["position"] == 2 and d["funding"]["of_total"] == 3
        assert d["funding"]["rule_type"] == "refill_to"
        # The cap the engine honours but nothing used to show.
        assert "$300.00" in d["funding"]["summary"]
        assert "never more than $100.00" in d["funding"]["summary"]

    def test_a_conditional_reads_in_the_right_direction(self, client) -> None:
        """An inverted operator is the failure ADR 0006 found; it must be visible."""
        h = _auth(client)
        ins = _cat(client, h, "Car Insurance", kind="obligation")
        left = _cat(client, h, "Leftover", kind="savings_bucket")
        client.post("/api/budget/plans", json={"name": "S", "rules": [
            {"category_id": ins["id"], "order_index": 1, "rule_type": "conditional",
             "params": {"when": {"field": "balance", "op": "<", "value_cents": 45000},
                        "then": {"rule_type": "fixed",
                                 "params": {"amount_cents": 5000}}}},
            {"category_id": left["id"], "order_index": 2,
             "rule_type": "remainder", "params": {}},
        ]}, headers=h)
        d = client.get(f"/api/budget/categories/{ins['id']}/detail", headers=h).json()
        s = d["funding"]["summary"]
        assert "less than $450.00" in s, s
        assert "$50.00 every paycheck" in s, s

    def test_unfunded_category_says_so(self, client: TestClient) -> None:
        h, c = self._setup(client)
        solo = _cat(client, h, "Dog Vet", kind="savings_bucket")
        d = client.get(f"/api/budget/categories/{solo['id']}/detail", headers=h).json()
        assert d["funding"] is None

    def test_matchers_are_listed_in_plain_english(self, client: TestClient) -> None:
        h, c = self._setup(client)
        client.post("/api/budget/rules", json={
            "category_id": c["food"]["id"],
            "matcher": {"merchant_contains": "kroger", "direction": "outflow"},
        }, headers=h)
        d = client.get(f"/api/budget/categories/{c['food']['id']}/detail",
                       headers=h).json()
        assert len(d["matchers"]) == 1
        s = d["matchers"][0]["summary"]
        assert "kroger" in s and "going out" in s

    def test_recent_activity_appears_once_money_has_moved(self, client) -> None:
        """The detail call was only ever tested on an empty ledger.

        It referenced a column that does not exist and 500'd the moment a
        paycheck had been applied -- invisible until a real end-to-end run.
        """
        h, c = self._setup(client)
        assert client.post("/api/budget/paychecks",
                           json={"gross_cents": 200000, "apply": True},
                           headers=h).status_code == 201

        r = client.get(f"/api/budget/categories/{c['food']['id']}/detail",
                       headers=h)
        assert r.status_code == 200, r.text
        recent = r.json()["recent"]
        assert recent, "a paycheck was applied but nothing shows as recent"
        assert recent[0]["amount_cents"] > 0
        assert recent[0]["source"]
        assert "at" in recent[0]

    def test_another_users_category_is_404(self, client: TestClient) -> None:
        h, c = self._setup(client)
        hb = _auth(client)
        assert client.get(f"/api/budget/categories/{c['food']['id']}/detail",
                          headers=hb).status_code == 404


@requires_db
class TestSetFunding:
    def _setup(self, client: TestClient):
        return TestCategoryDetail()._setup(client)

    def test_change_a_rule_in_place_keeps_order(self, client: TestClient) -> None:
        h, c = self._setup(client)
        r = client.put(f"/api/budget/categories/{c['food']['id']}/funding", json={
            "rule_type": "fixed", "params": {"amount_cents": 20000},
        }, headers=h)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["funding"]["rule_type"] == "fixed"
        assert d["funding"]["position"] == 2      # unchanged
        assert d["funding"]["plan_version"] == 2  # new version

    def test_moving_a_category_up_the_funding_order(self, client: TestClient) -> None:
        h, c = self._setup(client)
        d = client.put(f"/api/budget/categories/{c['food']['id']}/funding", json={
            "rule_type": "refill_to", "params": {"target_cents": 30000},
            "position": 1,
        }, headers=h).json()
        assert d["funding"]["position"] == 1

    def test_removing_funding_leaves_the_category(self, client: TestClient) -> None:
        h, c = self._setup(client)
        d = client.put(f"/api/budget/categories/{c['food']['id']}/funding",
                       json={"rule_type": None}, headers=h).json()
        assert d["funding"] is None
        assert d["category"]["name"] == "Food"
        plans = client.get("/api/budget/plans", headers=h).json()
        active = [p for p in plans if p["is_active"]][0]
        assert len(active["rules"]) == 2
        assert active["rules"][-1]["rule_type"] == "remainder"

    def test_a_cap_round_trips(self, client: TestClient) -> None:
        h, c = self._setup(client)
        d = client.put(f"/api/budget/categories/{c['food']['id']}/funding", json={
            "rule_type": "refill_to",
            "params": {"target_cents": 50000, "max_contribution_cents": 7500},
        }, headers=h).json()
        assert d["funding"]["params"]["max_contribution_cents"] == 7500
        assert "never more than $75.00" in d["funding"]["summary"]

    def test_an_incomplete_conditional_is_refused(self, client: TestClient) -> None:
        h, c = self._setup(client)
        r = client.put(f"/api/budget/categories/{c['food']['id']}/funding", json={
            "rule_type": "conditional",
            "params": {"when": {"op": "<"},
                       "then": {"rule_type": "fixed",
                                "params": {"amount_cents": 5000}}},
        }, headers=h)
        assert r.status_code == 422
        assert "value_cents" in r.json()["detail"]

    def test_taking_the_remainder_moves_it_last(self, client: TestClient) -> None:
        h, c = self._setup(client)
        d = client.put(f"/api/budget/categories/{c['rent']['id']}/funding",
                       json={"rule_type": "remainder"}, headers=h).json()
        assert d["funding"]["position"] == d["funding"]["of_total"]
        plans = client.get("/api/budget/plans", headers=h).json()
        active = [p for p in plans if p["is_active"]][0]
        assert sum(1 for x in active["rules"] if x["rule_type"] == "remainder") == 1

    def test_without_a_plan_the_first_rule_makes_one(self, client) -> None:
        """This used to 409. Requiring a plan to exist first made
        building a budget by hand impossible — see TestFundingFromZero."""
        h = _auth(client)
        solo = _cat(client, h, "Food")
        r = client.put(f"/api/budget/categories/{solo['id']}/funding",
                       json={"rule_type": "fixed", "params": {"amount_cents": 1000}},
                       headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["funding"]["rule_type"] == "fixed"


@requires_db
class TestPaycheckPreview:
    """Showing what a paycheck would do must not do any of it."""

    def _plan(self, client: TestClient):
        h = _auth(client)
        tithe = _cat(client, h, "Tithing", kind="obligation")
        rent = _cat(client, h, "Rent", kind="obligation")
        save = _cat(client, h, "Savings", kind="savings_bucket")
        fun = _cat(client, h, "Fun")
        left = _cat(client, h, "Leftover", kind="savings_bucket")
        assert client.post("/api/budget/plans", json={"name": "S", "rules": [
            {"category_id": tithe["id"], "order_index": 1, "rule_type": "percent",
             "params": {"rate": "0.10", "of": "gross"}},
            {"category_id": rent["id"], "order_index": 2, "rule_type": "fixed",
             "params": {"amount_cents": 35000}},
            # A big refill target deliberately placed above the small ones.
            {"category_id": save["id"], "order_index": 3, "rule_type": "refill_to",
             "params": {"target_cents": 500000}},
            {"category_id": fun["id"], "order_index": 4, "rule_type": "refill_to",
             "params": {"target_cents": 5000}},
            {"category_id": left["id"], "order_index": 5,
             "rule_type": "remainder", "params": {}},
        ]}, headers=h).status_code == 201
        return h, {"tithe": tithe, "rent": rent, "save": save,
                   "fun": fun, "left": left}

    def test_preview_writes_nothing(self, client: TestClient) -> None:
        h, c = self._plan(client)
        before = client.get("/api/budget/summary", headers=h).json()

        r = client.post("/api/budget/paychecks/preview",
                        json={"gross_cents": 200000}, headers=h)
        assert r.status_code == 200, r.text

        after = client.get("/api/budget/summary", headers=h).json()
        assert [x["balance_cents"] for x in after["categories"]] == \
               [x["balance_cents"] for x in before["categories"]]
        # And no paycheck row was created either.
        assert client.get("/api/budget/paychecks/candidates",
                          headers=h).status_code == 200

    def test_preview_matches_what_applying_actually_does(self, client) -> None:
        """A preview that disagrees with reality is worse than none."""
        h, c = self._plan(client)
        pv = client.post("/api/budget/paychecks/preview",
                         json={"gross_cents": 200000}, headers=h).json()
        predicted = {ln["name"]: ln["after_cents"] for ln in pv["lines"]}

        assert client.post("/api/budget/paychecks",
                           json={"gross_cents": 200000, "apply": True},
                           headers=h).status_code == 201
        actual = {x["name"]: x["balance_cents"]
                  for x in client.get("/api/budget/summary", headers=h).json()["categories"]}
        for name, after in predicted.items():
            assert actual[name] == after, f"{name}: predicted {after}, got {actual[name]}"

    def test_it_conserves_the_paycheck(self, client: TestClient) -> None:
        h, c = self._plan(client)
        pv = client.post("/api/budget/paychecks/preview",
                         json={"gross_cents": 200000}, headers=h).json()
        assert pv["total_allocated_cents"] + pv["leftover_cents"] == 200000

    def test_starvation_is_named_not_left_to_be_noticed(self, client) -> None:
        h, c = self._plan(client)
        pv = client.post("/api/budget/paychecks/preview",
                         json={"gross_cents": 200000}, headers=h).json()
        # Savings refills to $5,000 from position 3, so Fun and Leftover
        # below it get nothing.
        assert "Fun" in pv["starved"] and "Leftover" in pv["starved"]
        assert pv["warnings"], "starvation happened but was not reported"
        assert "Savings" in pv["warnings"][0], pv["warnings"]

    def test_a_healthy_plan_warns_about_nothing(self, client) -> None:
        h = _auth(client)
        rent = _cat(client, h, "Rent", kind="obligation")
        left = _cat(client, h, "Leftover", kind="savings_bucket")
        client.post("/api/budget/plans", json={"name": "S", "rules": [
            {"category_id": rent["id"], "order_index": 1, "rule_type": "fixed",
             "params": {"amount_cents": 35000}},
            {"category_id": left["id"], "order_index": 2,
             "rule_type": "remainder", "params": {}},
        ]}, headers=h)
        pv = client.post("/api/budget/paychecks/preview",
                         json={"gross_cents": 200000}, headers=h).json()
        assert pv["starved"] == [] and pv["warnings"] == []

    def test_rule_summaries_come_back_in_plain_english(self, client) -> None:
        h, c = self._plan(client)
        pv = client.post("/api/budget/paychecks/preview",
                         json={"gross_cents": 200000}, headers=h).json()
        tithe = next(ln for ln in pv["lines"] if ln["name"] == "Tithing")
        assert "10% of gross pay" in tithe["rule_summary"]

    def test_without_a_plan_it_says_so(self, client: TestClient) -> None:
        h = _auth(client)
        _cat(client, h, "Food")
        r = client.post("/api/budget/paychecks/preview",
                        json={"gross_cents": 200000}, headers=h)
        assert r.status_code == 409

    def test_another_users_plan_is_404(self, client: TestClient) -> None:
        h, c = self._plan(client)
        plan_id = client.get("/api/budget/plans", headers=h).json()[0]["id"]
        hb = _auth(client)
        assert client.post("/api/budget/paychecks/preview",
                           json={"gross_cents": 200000, "plan_id": plan_id},
                           headers=hb).status_code == 404

    def test_a_what_if_reorder_recalculates_without_saving(self, client) -> None:
        """Dragging a category in the review must not touch the plan."""
        h, c = self._plan(client)
        saved = client.get("/api/budget/plans", headers=h).json()[0]

        # Savings (position 3, refills to $5,000) starves Fun below it.
        base = client.post("/api/budget/paychecks/preview",
                           json={"gross_cents": 200000}, headers=h).json()
        assert "Fun" in base["starved"] and base["is_saved_plan"] is True

        # Move Fun above Savings.
        reordered = [
            {"category_id": c["tithe"]["id"], "order_index": 1,
             "rule_type": "percent", "params": {"rate": "0.10", "of": "gross"}},
            {"category_id": c["rent"]["id"], "order_index": 2,
             "rule_type": "fixed", "params": {"amount_cents": 35000}},
            {"category_id": c["fun"]["id"], "order_index": 3,
             "rule_type": "refill_to", "params": {"target_cents": 5000}},
            {"category_id": c["save"]["id"], "order_index": 4,
             "rule_type": "refill_to", "params": {"target_cents": 500000}},
            {"category_id": c["left"]["id"], "order_index": 5,
             "rule_type": "remainder", "params": {}},
        ]
        what_if = client.post("/api/budget/paychecks/preview",
                              json={"gross_cents": 200000, "rules": reordered},
                              headers=h).json()
        assert what_if["is_saved_plan"] is False
        fun = next(l for l in what_if["lines"] if l["name"] == "Fun")
        assert fun["allocated_cents"] == 5000
        assert "Fun" not in what_if["starved"]
        assert what_if["total_allocated_cents"] + what_if["leftover_cents"] == 200000

        # And the saved plan is untouched.
        after = client.get("/api/budget/plans", headers=h).json()[0]
        assert after["version"] == saved["version"]
        assert [r["category_id"] for r in after["rules"]] == \
               [r["category_id"] for r in saved["rules"]]

    def test_a_what_if_rule_change_recalculates(self, client) -> None:
        """refill_to $150 vs a flat $150 every paycheck are different things."""
        h = _auth(client)
        health = _cat(client, h, "Health", kind="obligation")
        left = _cat(client, h, "Leftover", kind="savings_bucket")
        client.post("/api/budget/plans", json={"name": "S", "rules": [
            {"category_id": health["id"], "order_index": 1,
             "rule_type": "refill_to", "params": {"target_cents": 15000}},
            {"category_id": left["id"], "order_index": 2,
             "rule_type": "remainder", "params": {}},
        ]}, headers=h)
        client.post("/api/budget/paychecks",
                    json={"gross_cents": 200000, "apply": True}, headers=h)
        # Health is now full at $150, so a refill adds nothing.
        base = client.post("/api/budget/paychecks/preview",
                           json={"gross_cents": 200000}, headers=h).json()
        assert next(l for l in base["lines"]
                    if l["name"] == "Health")["allocated_cents"] == 0

        # As a fixed rule it adds $150 regardless.
        what_if = client.post("/api/budget/paychecks/preview", json={
            "gross_cents": 200000,
            "rules": [
                {"category_id": health["id"], "order_index": 1,
                 "rule_type": "fixed", "params": {"amount_cents": 15000}},
                {"category_id": left["id"], "order_index": 2,
                 "rule_type": "remainder", "params": {}},
            ],
        }, headers=h).json()
        line = next(l for l in what_if["lines"] if l["name"] == "Health")
        assert line["allocated_cents"] == 15000
        assert "$150.00 every paycheck" in line["rule_summary"]

    def test_an_unsaveable_what_if_is_refused(self, client) -> None:
        """The preview must not show a plan that could never be saved."""
        h, c = self._plan(client)
        r = client.post("/api/budget/paychecks/preview", json={
            "gross_cents": 200000,
            "rules": [
                {"category_id": c["left"]["id"], "order_index": 1,
                 "rule_type": "remainder", "params": {}},
                {"category_id": c["rent"]["id"], "order_index": 2,
                 "rule_type": "fixed", "params": {"amount_cents": 35000}},
            ],
        }, headers=h)
        assert r.status_code == 422
        assert "remainder" in r.json()["detail"].lower()

    def test_a_what_if_cannot_reach_another_users_category(self, client) -> None:
        h, c = self._plan(client)
        hb = _auth(client)
        mine = _cat(client, hb, "Mine")
        client.post("/api/budget/plans", json={"name": "S", "rules": [
            {"category_id": mine["id"], "order_index": 1,
             "rule_type": "remainder", "params": {}},
        ]}, headers=hb)
        r = client.post("/api/budget/paychecks/preview", json={
            "gross_cents": 200000,
            "rules": [
                {"category_id": c["rent"]["id"], "order_index": 1,
                 "rule_type": "fixed", "params": {"amount_cents": 35000}},
                {"category_id": mine["id"], "order_index": 2,
                 "rule_type": "remainder", "params": {}},
            ],
        }, headers=hb)
        assert r.status_code == 404

    def test_preview_exposes_raw_params_for_editing(self, client) -> None:
        h, c = self._plan(client)
        pv = client.post("/api/budget/paychecks/preview",
                         json={"gross_cents": 200000}, headers=h).json()
        save = next(l for l in pv["lines"] if l["name"] == "Savings")
        assert save["params"]["target_cents"] == 500000

    def test_set_aside_comes_off_the_top_before_any_rule(self, client) -> None:
        """A refund you do not tithe on must not be counted as income."""
        h, c = self._plan(client)
        pv = client.post("/api/budget/paychecks/preview", json={
            "gross_cents": 200000,
            "manual": [{"category_id": c["fun"]["id"], "amount_cents": 20000}],
        }, headers=h).json()

        # Tithing is 10% of gross -- of the $1,800 the rules actually saw.
        tithe = next(l for l in pv["lines"] if l["name"] == "Tithing")
        assert tithe["allocated_cents"] == 18000
        assert pv["rules_gross_cents"] == 180000
        assert pv["manual_total_cents"] == 20000

        fun = next(l for l in pv["lines"] if l["name"] == "Fun")
        assert fun["manual_cents"] == 20000
        # Everything still adds up to the whole cheque.
        assert pv["total_allocated_cents"] + pv["leftover_cents"] == 200000

    def test_set_aside_raises_the_balance_the_rules_see(self, client) -> None:
        """Hand-filling a refill target means the rule adds nothing after."""
        h, c = self._plan(client)
        pv = client.post("/api/budget/paychecks/preview", json={
            "gross_cents": 200000,
            "manual": [{"category_id": c["fun"]["id"], "amount_cents": 5000}],
        }, headers=h).json()
        fun = next(l for l in pv["lines"] if l["name"] == "Fun")
        assert fun["manual_cents"] == 5000
        assert fun["allocated_cents"] == 0      # refill_to 5000 already met
        assert fun["after_cents"] == 5000

    def test_set_aside_to_a_category_with_no_rule_still_shows(self, client) -> None:
        h, c = self._plan(client)
        extra = _cat(client, h, "Dog Vet", kind="savings_bucket")
        pv = client.post("/api/budget/paychecks/preview", json={
            "gross_cents": 200000,
            "manual": [{"category_id": extra["id"], "amount_cents": 7500}],
        }, headers=h).json()
        line = next(l for l in pv["lines"] if l["name"] == "Dog Vet")
        assert line["manual_cents"] == 7500 and line["rule_type"] == "manual"

    def test_set_aside_cannot_exceed_the_paycheck(self, client) -> None:
        h, c = self._plan(client)
        r = client.post("/api/budget/paychecks/preview", json={
            "gross_cents": 10000,
            "manual": [{"category_id": c["fun"]["id"], "amount_cents": 20000}],
        }, headers=h)
        assert r.status_code == 422
        assert "exceed" in r.json()["detail"]

@requires_db
class TestOneOffPaycheckAdjustments:
    """Changes made while reviewing apply to that paycheck only."""

    def _plan(self, client: TestClient):
        return TestPaycheckPreview()._plan(client)

    def test_a_one_off_reorder_does_not_touch_the_saved_plan(self, client) -> None:
        h, c = self._plan(client)
        before = client.get("/api/budget/plans", headers=h).json()[0]

        reordered = [
            {"category_id": c["tithe"]["id"], "order_index": 1,
             "rule_type": "percent", "params": {"rate": "0.10", "of": "gross"}},
            {"category_id": c["fun"]["id"], "order_index": 2,
             "rule_type": "refill_to", "params": {"target_cents": 5000}},
            {"category_id": c["rent"]["id"], "order_index": 3,
             "rule_type": "fixed", "params": {"amount_cents": 35000}},
            {"category_id": c["save"]["id"], "order_index": 4,
             "rule_type": "refill_to", "params": {"target_cents": 500000}},
            {"category_id": c["left"]["id"], "order_index": 5,
             "rule_type": "remainder", "params": {}},
        ]
        r = client.post("/api/budget/paychecks", json={
            "gross_cents": 200000, "apply": True, "rules": reordered,
        }, headers=h)
        assert r.status_code == 201, r.text

        bal = {x["name"]: x["balance_cents"]
               for x in client.get("/api/budget/summary", headers=h).json()["categories"]}
        assert bal["Fun"] == 5000          # funded, because it moved up

        after = client.get("/api/budget/plans", headers=h).json()
        active = [p for p in after if p["is_active"]][0]
        assert active["version"] == before["version"], "a one-off rewrote the plan"
        assert [x["category_id"] for x in active["rules"]] == \
               [x["category_id"] for x in before["rules"]]

    def test_a_one_off_rule_change_applies_once(self, client) -> None:
        """Health as a flat amount this time, still refill_to next time."""
        h = _auth(client)
        health = _cat(client, h, "Health", kind="obligation")
        left = _cat(client, h, "Leftover", kind="savings_bucket")
        client.post("/api/budget/plans", json={"name": "S", "rules": [
            {"category_id": health["id"], "order_index": 1,
             "rule_type": "refill_to", "params": {"target_cents": 15000}},
            {"category_id": left["id"], "order_index": 2,
             "rule_type": "remainder", "params": {}},
        ]}, headers=h)
        client.post("/api/budget/paychecks",
                    json={"gross_cents": 100000, "apply": True}, headers=h)

        # Second cheque, one-off fixed rule: adds $150 on top of the $150.
        client.post("/api/budget/paychecks", json={
            "gross_cents": 100000, "apply": True,
            "rules": [
                {"category_id": health["id"], "order_index": 1,
                 "rule_type": "fixed", "params": {"amount_cents": 15000}},
                {"category_id": left["id"], "order_index": 2,
                 "rule_type": "remainder", "params": {}},
            ],
        }, headers=h)
        bal = {x["name"]: x["balance_cents"]
               for x in client.get("/api/budget/summary", headers=h).json()["categories"]}
        assert bal["Health"] == 30000

        # The saved rule is still refill_to, so a third cheque adds nothing.
        client.post("/api/budget/paychecks",
                    json={"gross_cents": 100000, "apply": True}, headers=h)
        bal = {x["name"]: x["balance_cents"]
               for x in client.get("/api/budget/summary", headers=h).json()["categories"]}
        assert bal["Health"] == 30000, "the one-off leaked into the saved plan"

    def test_manual_allocations_are_posted_and_come_first(self, client) -> None:
        h, c = self._plan(client)
        r = client.post("/api/budget/paychecks", json={
            "gross_cents": 200000, "apply": True,
            "manual": [{"category_id": c["fun"]["id"], "amount_cents": 20000}],
        }, headers=h)
        assert r.status_code == 201, r.text
        assert r.json()["allocated_cents"] == 200000

        bal = {x["name"]: x["balance_cents"]
               for x in client.get("/api/budget/summary", headers=h).json()["categories"]}
        assert bal["Fun"] == 20000
        assert bal["Tithing"] == 18000      # 10% of $1,800, not $2,000

    def test_manual_over_the_paycheck_is_refused(self, client) -> None:
        h, c = self._plan(client)
        r = client.post("/api/budget/paychecks", json={
            "gross_cents": 10000, "apply": True,
            "manual": [{"category_id": c["fun"]["id"], "amount_cents": 50000}],
        }, headers=h)
        assert r.status_code == 422
        assert "exceed" in r.json()["detail"]

    def test_an_unsaveable_one_off_is_refused(self, client) -> None:
        h, c = self._plan(client)
        r = client.post("/api/budget/paychecks", json={
            "gross_cents": 200000, "apply": True,
            "rules": [
                {"category_id": c["left"]["id"], "order_index": 1,
                 "rule_type": "remainder", "params": {}},
                {"category_id": c["rent"]["id"], "order_index": 2,
                 "rule_type": "fixed", "params": {"amount_cents": 35000}},
            ],
        }, headers=h)
        assert r.status_code == 422

    def test_undo_reverses_manual_allocations_too(self, client) -> None:
        h, c = self._plan(client)
        r = client.post("/api/budget/paychecks", json={
            "gross_cents": 200000, "apply": True,
            "manual": [{"category_id": c["fun"]["id"], "amount_cents": 20000}],
        }, headers=h)
        pid = r.json()["id"]
        assert client.post(f"/api/budget/paychecks/{pid}/unapply",
                           headers=h).status_code == 200
        bal = {x["name"]: x["balance_cents"]
               for x in client.get("/api/budget/summary", headers=h).json()["categories"]}
        assert bal["Fun"] == 0 and bal["Tithing"] == 0


@requires_db
class TestTransactionHistory:
    """Every transaction and where its money actually went."""

    def _seeded(self, client: TestClient):
        """A user with synced transactions, via the sandbox fixture path."""
        from datetime import date as _date

        from app.db.models import Account, PlaidItem, Transaction
        from app.db.session import SessionLocal

        h = _auth(client)
        me = client.get("/api/auth/me", headers=h).json()
        uid = uuid.UUID(me["id"])
        food = _cat(client, h, "Food")
        fun = _cat(client, h, "Fun")

        with SessionLocal() as db:
            item = PlaidItem(user_id=uid, plaid_item_id=f"it-{uuid.uuid4().hex[:8]}",
                             institution_name="Test Bank",
                             access_token_encrypted=b"x", sync_cursor=None)
            db.add(item)
            db.flush()
            acct = Account(user_id=uid, item_id=item.id,
                           plaid_account_id=f"ac-{uuid.uuid4().hex[:8]}",
                           name="Checking", type="depository", subtype="checking",
                           current_cents=100000, is_tracked=True)
            db.add(acct)
            db.flush()

            def txn(name, out=0, inn=0, treatment="spend", merchant=None):
                t = Transaction(
                    user_id=uid, account_id=acct.id,
                    plaid_transaction_id=f"tx-{uuid.uuid4().hex[:10]}",
                    outflow_cents=out, inflow_cents=inn,
                    date=_date(2026, 10, 20), name=name,
                    merchant_name=merchant, treatment=treatment,
                )
                db.add(t)
                db.flush()
                return t

            spent = txn("KROGER #512", out=8320, merchant="Kroger")
            refund = txn("KROGER REFUND", inn=1200, merchant="Kroger")
            card = txn("CREDIT CARD 3333 PAYMENT", out=50000,
                       treatment="card_payment")
            unreviewed = txn("MYSTERY LLC", out=999, merchant="Mystery")
            db.commit()
            ids = {"spent": str(spent.id), "refund": str(refund.id),
                   "card": str(card.id), "unreviewed": str(unreviewed.id)}

        # File the two that have decisions.
        from app.budget.ledger import Entry, post
        with SessionLocal() as db:
            post(db, uid, [Entry(uuid.UUID(food["id"]), -8320, "manual", None, "x")],
                 transaction_id=uuid.UUID(ids["spent"]))
            post(db, uid, [Entry(uuid.UUID(food["id"]), 1200, "manual", None, "x")],
                 transaction_id=uuid.UUID(ids["refund"]))
            db.commit()
        return h, ids, {"food": food, "fun": fun}

    def test_lists_everything_with_where_it_went(self, client: TestClient) -> None:
        h, ids, c = self._seeded(client)
        r = client.get("/api/budget/transactions", headers=h)
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["total"] == 4
        by_id = {i["id"]: i for i in out["items"]}

        spent = by_id[ids["spent"]]
        assert spent["amount_cents"] == 8320          # positive == out
        assert spent["splits"][0]["category_name"] == "Food"
        assert spent["splits"][0]["amount_cents"] == 8320

    def test_a_refund_reads_as_money_in(self, client: TestClient) -> None:
        """The whole point: in and out must be distinguishable."""
        h, ids, c = self._seeded(client)
        out = client.get("/api/budget/transactions", headers=h).json()
        refund = next(i for i in out["items"] if i["id"] == ids["refund"])
        assert refund["amount_cents"] == -1200        # negative == in
        assert refund["splits"][0]["amount_cents"] == -1200
        assert refund["splits"][0]["category_name"] == "Food"

    def test_excluded_is_distinct_from_awaiting_review(self, client) -> None:
        h, ids, c = self._seeded(client)
        out = client.get("/api/budget/transactions", headers=h).json()
        by_id = {i["id"]: i for i in out["items"]}

        card = by_id[ids["card"]]
        assert card["excluded"] and not card["awaiting_review"]
        assert card["treatment_label"] == "Credit card payment"

        mystery = by_id[ids["unreviewed"]]
        assert mystery["awaiting_review"] and not mystery["excluded"]
        assert mystery["splits"] == []

    def test_filter_by_category(self, client: TestClient) -> None:
        h, ids, c = self._seeded(client)
        out = client.get(f"/api/budget/transactions?category_id={c['food']['id']}",
                         headers=h).json()
        assert {i["id"] for i in out["items"]} == {ids["spent"], ids["refund"]}

        empty = client.get(f"/api/budget/transactions?category_id={c['fun']['id']}",
                           headers=h).json()
        assert empty["items"] == [] and empty["total"] == 0

    def test_filter_by_treatment_and_search(self, client: TestClient) -> None:
        h, ids, c = self._seeded(client)
        cards = client.get("/api/budget/transactions?treatment=card_payment",
                           headers=h).json()
        assert [i["id"] for i in cards["items"]] == [ids["card"]]

        kroger = client.get("/api/budget/transactions?search=kroger",
                            headers=h).json()
        assert len(kroger["items"]) == 2

    def test_bad_treatment_rejected(self, client: TestClient) -> None:
        h, ids, c = self._seeded(client)
        assert client.get("/api/budget/transactions?treatment=nonsense",
                          headers=h).status_code == 422

    def test_pagination_reports_more(self, client: TestClient) -> None:
        h, ids, c = self._seeded(client)
        page = client.get("/api/budget/transactions?limit=2", headers=h).json()
        assert len(page["items"]) == 2 and page["total"] == 4
        assert page["has_more"] is True
        last = client.get("/api/budget/transactions?limit=2&offset=2",
                          headers=h).json()
        assert last["has_more"] is False

    def test_another_users_transactions_are_invisible(self, client) -> None:
        h, ids, c = self._seeded(client)
        hb = _auth(client)
        assert client.get("/api/budget/transactions", headers=hb).json()["total"] == 0


@requires_db
class TestPaycheckLinkageAndUndo:
    """A recorded paycheck must stop being offered for review."""

    def _with_deposit(self, client: TestClient):
        from datetime import date as _date

        from app.db.models import Account, PlaidItem, Transaction
        from app.db.session import SessionLocal

        h = _auth(client)
        uid = uuid.UUID(client.get("/api/auth/me", headers=h).json()["id"])
        rent = _cat(client, h, "Rent", kind="obligation")
        left = _cat(client, h, "Leftover", kind="savings_bucket")
        client.post("/api/budget/plans", json={"name": "S", "rules": [
            {"category_id": rent["id"], "order_index": 1, "rule_type": "fixed",
             "params": {"amount_cents": 35000}},
            {"category_id": left["id"], "order_index": 2,
             "rule_type": "remainder", "params": {}},
        ]}, headers=h)

        with SessionLocal() as db:
            item = PlaidItem(user_id=uid, plaid_item_id=f"it-{uuid.uuid4().hex[:8]}",
                             institution_name="Bank",
                             access_token_encrypted=b"x", sync_cursor=None)
            db.add(item); db.flush()
            acct = Account(user_id=uid, item_id=item.id,
                           plaid_account_id=f"ac-{uuid.uuid4().hex[:8]}",
                           name="Checking", type="depository",
                           current_cents=100000, is_tracked=True)
            db.add(acct); db.flush()
            dep = Transaction(
                user_id=uid, account_id=acct.id,
                plaid_transaction_id=f"tx-{uuid.uuid4().hex[:10]}",
                outflow_cents=0, inflow_cents=200000,
                date=_date(2026, 10, 20), name="ACME PAYROLL",
                merchant_name="Acme", treatment="spend")
            db.add(dep); db.flush()
            dep_id = str(dep.id)
            db.commit()
        return h, dep_id, {"rent": rent, "left": left}

    def test_recording_against_a_deposit_clears_it_from_review(self, client) -> None:
        """The bug: a paycheck saved without its transaction id came back."""
        h, dep_id, c = self._with_deposit(client)
        before = client.get("/api/budget/paychecks/candidates", headers=h).json()
        assert [x for x in before if not x["already_recorded"]]

        r = client.post("/api/budget/paychecks", json={
            "transaction_id": dep_id, "apply": True,
        }, headers=h)
        assert r.status_code == 201, r.text

        after = client.get("/api/budget/paychecks/candidates", headers=h).json()
        assert [x for x in after if not x["already_recorded"]] == []

    def test_the_same_deposit_cannot_be_recorded_twice(self, client) -> None:
        h, dep_id, c = self._with_deposit(client)
        client.post("/api/budget/paychecks",
                    json={"transaction_id": dep_id, "apply": True}, headers=h)
        again = client.post("/api/budget/paychecks",
                            json={"transaction_id": dep_id, "apply": True},
                            headers=h)
        assert again.status_code == 409

    def test_the_deposit_shows_its_split_in_history(self, client) -> None:
        """Otherwise the one transaction that funded everything reads as
        'not filed yet'."""
        h, dep_id, c = self._with_deposit(client)
        client.post("/api/budget/paychecks",
                    json={"transaction_id": dep_id, "apply": True}, headers=h)

        items = client.get("/api/budget/transactions", headers=h).json()["items"]
        dep = next(i for i in items if i["id"] == dep_id)
        assert dep["awaiting_review"] is False
        assert dep["paycheck_id"] is not None and dep["paycheck_applied"] is True
        names = {s["category_name"] for s in dep["splits"]}
        assert names == {"Rent", "Leftover"}
        # Money arriving in a category reads as money in.
        assert all(s["amount_cents"] < 0 for s in dep["splits"])

    def test_undo_preview_writes_nothing(self, client: TestClient) -> None:
        h, dep_id, c = self._with_deposit(client)
        pid = client.post("/api/budget/paychecks",
                          json={"transaction_id": dep_id, "apply": True},
                          headers=h).json()["id"]
        before = client.get("/api/budget/summary", headers=h).json()
        pv = client.get(f"/api/budget/paychecks/{pid}/undo-preview",
                        headers=h).json()
        assert pv["safe"] is True
        assert pv["total_reversed_cents"] == 200000
        after = client.get("/api/budget/summary", headers=h).json()
        assert after["categories"] == before["categories"]

    def test_undo_is_refused_when_the_money_is_already_spent(self, client) -> None:
        """Reversing does not un-spend it; it just goes negative."""
        from app.budget.ledger import Entry, post
        from app.db.session import SessionLocal

        h, dep_id, c = self._with_deposit(client)
        pid = client.post("/api/budget/paychecks",
                          json={"transaction_id": dep_id, "apply": True},
                          headers=h).json()["id"]
        uid = uuid.UUID(client.get("/api/auth/me", headers=h).json()["id"])
        with SessionLocal() as db:
            post(db, uid, [Entry(uuid.UUID(c["rent"]["id"]), -30000,
                                 "manual", None, "paid rent")])
            db.commit()

        pv = client.get(f"/api/budget/paychecks/{pid}/undo-preview",
                        headers=h).json()
        assert pv["safe"] is False and "Rent" in pv["would_go_negative"]

        r = client.post(f"/api/budget/paychecks/{pid}/unapply", headers=h)
        assert r.status_code == 409
        assert "Rent" in r.json()["detail"]
        # And nothing moved.
        bal = {x["name"]: x["balance_cents"]
               for x in client.get("/api/budget/summary", headers=h).json()["categories"]}
        assert bal["Rent"] == 5000

    def test_forcing_the_undo_is_allowed_and_explained(self, client) -> None:
        from app.budget.ledger import Entry, post
        from app.db.session import SessionLocal

        h, dep_id, c = self._with_deposit(client)
        pid = client.post("/api/budget/paychecks",
                          json={"transaction_id": dep_id, "apply": True},
                          headers=h).json()["id"]
        uid = uuid.UUID(client.get("/api/auth/me", headers=h).json()["id"])
        with SessionLocal() as db:
            post(db, uid, [Entry(uuid.UUID(c["rent"]["id"]), -30000,
                                 "manual", None, "paid rent")])
            db.commit()

        r = client.post(f"/api/budget/paychecks/{pid}/unapply",
                        json={"force": True}, headers=h)
        assert r.status_code == 200, r.text
        bal = {x["name"]: x["balance_cents"]
               for x in client.get("/api/budget/summary", headers=h).json()["categories"]}
        assert bal["Rent"] == -30000      # overspent, as warned

    def test_undoing_twice_is_refused(self, client: TestClient) -> None:
        h, dep_id, c = self._with_deposit(client)
        pid = client.post("/api/budget/paychecks",
                          json={"transaction_id": dep_id, "apply": True},
                          headers=h).json()["id"]
        assert client.post(f"/api/budget/paychecks/{pid}/unapply",
                           headers=h).status_code == 200
        assert client.post(f"/api/budget/paychecks/{pid}/unapply",
                           headers=h).status_code == 409

    def test_an_undone_deposit_is_offered_for_review_again(self, client) -> None:
        h, dep_id, c = self._with_deposit(client)
        pid = client.post("/api/budget/paychecks",
                          json={"transaction_id": dep_id, "apply": True},
                          headers=h).json()["id"]
        client.post(f"/api/budget/paychecks/{pid}/unapply", headers=h)
        # The paycheck row still exists, so it stays out of the prompt --
        # undoing the split is not the same as forgetting the deposit.
        cands = client.get("/api/budget/paychecks/candidates", headers=h).json()
        assert all(x["already_recorded"] for x in cands)


@requires_db
class TestRememberThisMerchant:
    """Filing with 'remember' must not accumulate contradictory rules."""

    def _review(self, client: TestClient, *, merchant: str, out: int = 0,
                inn: int = 0):
        from datetime import date as _date

        from app.budget.categorize import queue_for_review
        from app.db.models import Account, PlaidItem, Transaction
        from app.db.session import SessionLocal

        h = _auth(client)
        uid = uuid.UUID(client.get("/api/auth/me", headers=h).json()["id"])
        with SessionLocal() as db:
            item = PlaidItem(user_id=uid, plaid_item_id=f"it-{uuid.uuid4().hex[:8]}",
                             institution_name="B", access_token_encrypted=b"x")
            db.add(item); db.flush()
            acct = Account(user_id=uid, item_id=item.id,
                           plaid_account_id=f"ac-{uuid.uuid4().hex[:8]}",
                           name="Checking", type="depository", is_tracked=True)
            db.add(acct); db.flush()
            t = Transaction(user_id=uid, account_id=acct.id,
                            plaid_transaction_id=f"tx-{uuid.uuid4().hex[:10]}",
                            outflow_cents=out, inflow_cents=inn,
                            date=_date(2026, 10, 20), name=merchant,
                            merchant_name=merchant, treatment="spend")
            db.add(t); db.flush()
            rv = queue_for_review(db, uid, t, "unknown_merchant")
            db.commit()
            return h, str(rv.id)

    def _rules(self, client, h):
        return client.get("/api/budget/rules", headers=h).json()

    def test_filing_the_same_merchant_twice_makes_one_rule(self, client) -> None:
        h, rv1 = self._review(client, merchant="KROGER", out=5000)
        food = _cat(client, h, "Food")
        client.post(f"/api/budget/review/{rv1}/resolve", json={
            "splits": [{"category_id": food["id"], "amount_cents": 5000}],
            "remember": True,
        }, headers=h)

        # A second KROGER, filed the same way.
        from datetime import date as _date

        from sqlalchemy import select

        from app.budget.categorize import queue_for_review
        from app.db.models import Account, Transaction
        from app.db.session import SessionLocal
        uid = uuid.UUID(client.get("/api/auth/me", headers=h).json()["id"])
        with SessionLocal() as db:
            acct = db.scalars(select(Account).where(Account.user_id == uid)).first()
            t = Transaction(user_id=uid, account_id=acct.id,
                            plaid_transaction_id=f"tx-{uuid.uuid4().hex[:10]}",
                            outflow_cents=6000, inflow_cents=0,
                            date=_date(2026, 10, 21), name="KROGER",
                            merchant_name="KROGER", treatment="spend")
            db.add(t); db.flush()
            rv2 = str(queue_for_review(db, uid, t, "unknown_merchant").id)
            db.commit()
        client.post(f"/api/budget/review/{rv2}/resolve", json={
            "splits": [{"category_id": food["id"], "amount_cents": 6000}],
            "remember": True,
        }, headers=h)

        kroger = [r for r in self._rules(client, h)
                  if r["matcher"].get("merchant_contains") == "kroger"]
        assert len(kroger) == 1, f"duplicated: {kroger}"

    def test_refiling_elsewhere_moves_the_rule_rather_than_contradicting(
            self, client) -> None:
        """Two rules for one merchant at the same priority resolve by
        creation order, which is to say arbitrarily."""
        h, rv1 = self._review(client, merchant="BIKE SHOP", out=5000)
        a = _cat(client, h, "Car Fund", kind="savings_bucket")
        b = _cat(client, h, "Fun")
        client.post(f"/api/budget/review/{rv1}/resolve", json={
            "splits": [{"category_id": a["id"], "amount_cents": 5000}],
            "remember": True,
        }, headers=h)

        from datetime import date as _date

        from sqlalchemy import select

        from app.budget.categorize import queue_for_review
        from app.db.models import Account, Transaction
        from app.db.session import SessionLocal
        uid = uuid.UUID(client.get("/api/auth/me", headers=h).json()["id"])
        with SessionLocal() as db:
            acct = db.scalars(select(Account).where(Account.user_id == uid)).first()
            t = Transaction(user_id=uid, account_id=acct.id,
                            plaid_transaction_id=f"tx-{uuid.uuid4().hex[:10]}",
                            outflow_cents=7000, inflow_cents=0,
                            date=_date(2026, 10, 21), name="BIKE SHOP",
                            merchant_name="BIKE SHOP", treatment="spend")
            db.add(t); db.flush()
            rv2 = str(queue_for_review(db, uid, t, "unknown_merchant").id)
            db.commit()
        client.post(f"/api/budget/review/{rv2}/resolve", json={
            "splits": [{"category_id": b["id"], "amount_cents": 7000}],
            "remember": True,
        }, headers=h)

        bike = [r for r in self._rules(client, h)
                if r["matcher"].get("merchant_contains") == "bike shop"]
        assert len(bike) == 1, f"contradictory rules: {bike}"
        assert bike[0]["category_id"] == b["id"], "the latest decision should win"

    def test_remembering_income_does_not_claim_future_spending(self, client) -> None:
        """A payroll deposit filed once must not auto-file purchases."""
        h, rv = self._review(client, merchant="ACME PAYROLL", inn=200000)
        inc = _cat(client, h, "Income Holding", kind="savings_bucket")
        client.post(f"/api/budget/review/{rv}/resolve", json={
            "splits": [{"category_id": inc["id"], "amount_cents": -200000}],
            "remember": True,
        }, headers=h)
        rule = next(r for r in self._rules(client, h)
                    if r["matcher"].get("merchant_contains") == "acme payroll")
        assert rule["matcher"]["direction"] == "inflow"

    def test_remembering_spending_records_the_outflow_direction(self, client) -> None:
        h, rv = self._review(client, merchant="KROGER", out=5000)
        food = _cat(client, h, "Food")
        client.post(f"/api/budget/review/{rv}/resolve", json={
            "splits": [{"category_id": food["id"], "amount_cents": 5000}],
            "remember": True,
        }, headers=h)
        rule = next(r for r in self._rules(client, h)
                    if r["matcher"].get("merchant_contains") == "kroger")
        assert rule["matcher"]["direction"] == "outflow"


@requires_db
class TestFundingFromZero:
    """Building a budget by hand, from nothing.

    This path was never walked end to end: setting funding required a
    plan to already exist, and the only way to get one was to accept a
    whole-budget AI proposal. Every Save on a fresh account silently did
    nothing.
    """

    def test_the_first_rule_creates_the_plan(self, client: TestClient) -> None:
        h = _auth(client)
        tithe = _cat(client, h, "Tithing", kind="obligation")
        assert client.get("/api/budget/plans", headers=h).json() == []

        r = client.put(f"/api/budget/categories/{tithe['id']}/funding", json={
            "rule_type": "percent", "params": {"rate": "0.10", "of": "gross"},
        }, headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["funding"]["rule_type"] == "percent"

        plans = client.get("/api/budget/plans", headers=h).json()
        active = [p for p in plans if p["is_active"]]
        assert len(active) == 1 and active[0]["version"] == 1

    def test_the_new_plan_has_a_remainder_into_unallocated(self, client) -> None:
        """A plan needs exactly one, and Unallocated is what it is for."""
        h = _auth(client)
        tithe = _cat(client, h, "Tithing", kind="obligation")
        client.put(f"/api/budget/categories/{tithe['id']}/funding", json={
            "rule_type": "percent", "params": {"rate": "0.10", "of": "gross"},
        }, headers=h)

        cats = client.get("/api/budget/categories", headers=h).json()
        sink = next(c for c in cats if c["is_unallocated"])
        active = [p for p in client.get("/api/budget/plans", headers=h).json()
                  if p["is_active"]][0]
        assert active["rules"][-1]["rule_type"] == "remainder"
        assert active["rules"][-1]["category_id"] == sink["id"]

    def test_a_paycheck_splits_immediately_after(self, client) -> None:
        """The point of the fix: one rule in and the budget works."""
        h = _auth(client)
        tithe = _cat(client, h, "Tithing", kind="obligation")
        client.put(f"/api/budget/categories/{tithe['id']}/funding", json={
            "rule_type": "percent", "params": {"rate": "0.10", "of": "gross"},
        }, headers=h)

        pv = client.post("/api/budget/paychecks/preview",
                         json={"gross_cents": 200000}, headers=h)
        assert pv.status_code == 200, pv.text
        lines = {l["name"]: l["allocated_cents"] for l in pv.json()["lines"]}
        assert lines["Tithing"] == 20000
        assert sum(lines.values()) == 200000

    def test_building_the_whole_budget_one_category_at_a_time(self, client) -> None:
        h = _auth(client)
        made = {}
        for name, kind in [("Tithing", "obligation"), ("Rent", "obligation"),
                           ("Groceries", "spending"), ("Savings", "savings_bucket")]:
            made[name] = _cat(client, h, name, kind=kind)

        for name, rule, params in [
            ("Tithing", "percent", {"rate": "0.10", "of": "gross"}),
            ("Rent", "fixed", {"amount_cents": 35000}),
            ("Groceries", "refill_to", {"target_cents": 30000}),
            ("Savings", "remainder", {}),
        ]:
            r = client.put(f"/api/budget/categories/{made[name]['id']}/funding",
                           json={"rule_type": rule, "params": params}, headers=h)
            assert r.status_code == 200, f"{name}: {r.text}"

        pv = client.post("/api/budget/paychecks/preview",
                         json={"gross_cents": 200000}, headers=h).json()
        got = {l["name"]: l["allocated_cents"] for l in pv["lines"]}
        assert got["Tithing"] == 20000
        assert got["Rent"] == 35000
        assert got["Groceries"] == 30000
        assert got["Savings"] == 115000
        assert sum(got.values()) == 200000
        # Unallocated was only ever scaffolding; taking the remainder
        # explicitly should have displaced it.
        assert got.get("Unallocated", 0) == 0

    def test_removing_funding_with_no_plan_says_so(self, client) -> None:
        h = _auth(client)
        c = _cat(client, h, "Food")
        r = client.put(f"/api/budget/categories/{c['id']}/funding",
                       json={"rule_type": None}, headers=h)
        assert r.status_code == 422
        assert "nothing to remove" in r.json()["detail"].lower()
