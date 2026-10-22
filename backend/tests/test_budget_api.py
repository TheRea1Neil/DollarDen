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
