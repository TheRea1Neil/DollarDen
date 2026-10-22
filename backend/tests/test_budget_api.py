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
