"""Expense types: the duplicate guard, merging, and the HTTP surface.

The single failure mode worth most of these tests is fragmentation.
"Fast food" and "Fast Food" coexisting does not raise an error; it
quietly halves an answer, and the user has no way to see why.
"""
from __future__ import annotations

import uuid
from datetime import date

from fastapi.testclient import TestClient

from app.analytics import taxonomy
from app.analytics.taxonomy import slugify
from app.db.session import SessionLocal
from tests.conftest import requires_db

PW = "analytics-passphrase-99"


def _auth(client: TestClient) -> dict[str, str]:
    r = client.post("/api/auth/register",
                    json={"email": f"an-{uuid.uuid4().hex[:10]}@x.com", "password": PW})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


class TestSlug:
    def test_case_and_punctuation_collapse(self):
        assert slugify("Fast Food") == slugify("fast-food") == slugify("FAST FOOD")

    def test_plurals_collapse(self):
        assert slugify("Subscription") == slugify("Subscriptions")
        assert slugify("Groceries") == slugify("Grocery")

    def test_ampersand_matches_and(self):
        assert slugify("Charity & Giving") == slugify("Charity and Giving")

    def test_filler_words_are_dropped(self):
        assert slugify("Other Household") == slugify("Household")

    def test_a_double_s_is_not_a_plural(self):
        assert slugify("Fitness") == "fitness"


@requires_db
class TestCreation:
    def test_create_and_list(self, client: TestClient):
        h = _auth(client)
        r = client.post("/api/analytics/expense-types",
                        json={"name": "Fast food", "description": "Drive-through"},
                        headers=h)
        assert r.status_code == 201, r.text
        assert r.json()["reviewed"] is True, "a type the user typed is reviewed"

        rows = client.get("/api/analytics/expense-types", headers=h).json()
        assert [t["name"] for t in rows] == ["Fast food"]

    def test_a_near_duplicate_is_refused_with_the_existing_name(self, client: TestClient):
        h = _auth(client)
        client.post("/api/analytics/expense-types", json={"name": "Fast food"}, headers=h)
        r = client.post("/api/analytics/expense-types",
                        json={"name": "Fast Foods"}, headers=h)
        assert r.status_code == 409
        assert "Fast food" in r.json()["detail"]
        assert len(client.get("/api/analytics/expense-types", headers=h).json()) == 1

    def test_renaming_onto_another_type_is_refused(self, client: TestClient):
        h = _auth(client)
        client.post("/api/analytics/expense-types", json={"name": "Coffee"}, headers=h)
        r = client.post("/api/analytics/expense-types", json={"name": "Fuel"}, headers=h)
        r2 = client.patch(f"/api/analytics/expense-types/{r.json()['id']}",
                          json={"name": "coffee"}, headers=h)
        assert r2.status_code == 409

    def test_suggestions_mark_what_already_exists(self, client: TestClient):
        h = _auth(client)
        client.post("/api/analytics/expense-types", json={"name": "Coffee"}, headers=h)
        out = client.get("/api/analytics/expense-types/suggestions", headers=h).json()
        by = {s["name"]: s["status"] for s in out["suggestions"]}
        assert by["Coffee"] == "existing"
        assert by["Groceries"] == "available", "unused standards are offered, not created"
        # Offered, not materialised.
        assert len(client.get("/api/analytics/expense-types", headers=h).json()) == 1

    def test_another_users_type_is_not_found(self, client: TestClient):
        a, b = _auth(client), _auth(client)
        tid = client.post("/api/analytics/expense-types",
                          json={"name": "Coffee"}, headers=a).json()["id"]
        assert client.patch(f"/api/analytics/expense-types/{tid}",
                            json={"description": "x"}, headers=b).status_code == 404


@requires_db
class TestResolveOrCreate:
    def test_an_existing_slug_is_reused_rather_than_forked(self, client: TestClient):
        h = _auth(client)
        client.post("/api/analytics/expense-types", json={"name": "Fast food"}, headers=h)
        with SessionLocal() as db:
            from app.db.models import User
            from sqlalchemy import select
            uid = db.scalars(select(User.id).order_by(User.created_at.desc())).first()
            et, created = taxonomy.resolve_or_create(db, uid, "FAST FOODS")
            assert created is False
            assert et.name == "Fast food"

    def test_a_starter_name_arrives_with_its_description(self, client: TestClient):
        h = _auth(client)
        _ = client.get("/api/analytics/expense-types", headers=h)
        with SessionLocal() as db:
            from sqlalchemy import select

            from app.db.models import User
            uid = db.scalars(select(User.id).order_by(User.created_at.desc())).first()
            et, created = taxonomy.resolve_or_create(db, uid, "subscriptions",
                                                     created_by="ai")
            assert created is True
            assert et.description, "a standard name should not arrive bare"
            assert et.reviewed_at is None, "an AI-coined type waits for a look"


@requires_db
class TestMerge:
    def test_merging_moves_the_history_and_archives_the_source(self, client: TestClient):
        h = _auth(client)
        a = client.post("/api/analytics/expense-types",
                        json={"name": "Fast food"}, headers=h).json()
        b = client.post("/api/analytics/expense-types",
                        json={"name": "Takeaway"}, headers=h).json()

        r = client.post(f"/api/analytics/expense-types/{b['id']}/merge",
                        json={"into_id": a["id"]}, headers=h)
        assert r.status_code == 200
        assert r.json()["id"] == a["id"]

        names = [t["name"] for t in
                 client.get("/api/analytics/expense-types", headers=h).json()]
        assert names == ["Fast food"]

    def test_merging_into_itself_is_refused(self, client: TestClient):
        h = _auth(client)
        a = client.post("/api/analytics/expense-types",
                        json={"name": "Coffee"}, headers=h).json()
        assert client.post(f"/api/analytics/expense-types/{a['id']}/merge",
                           json={"into_id": a["id"]}, headers=h).status_code == 400


@requires_db
class TestAssignment:
    def test_labelling_a_transaction_labels_its_allocations(self, client: TestClient):
        from tests.test_analytics_queries import _cat, _spend, _user

        h = _auth(client)
        with SessionLocal() as db:
            from sqlalchemy import select

            from app.db.models import User
            uid = db.scalars(select(User.id).order_by(User.created_at.desc())).first()
            u = db.get(User, uid)
            from app.db.models import Account, PlaidItem
            item = PlaidItem(user_id=u.id, plaid_item_id=f"it-{uuid.uuid4().hex[:10]}",
                             access_token_encrypted=b"x")
            db.add(item); db.flush()
            acct = Account(user_id=u.id, item_id=item.id,
                           plaid_account_id=f"ac-{uuid.uuid4().hex[:10]}", name="C")
            db.add(acct); db.flush()
            cat = _cat(db, u, "Food")
            t = _spend(db, u, acct, cat, 1_500, on=date.today(), merchant="Taco Bell")
            db.commit()
            txn_id = str(t.id)

        r = client.post(f"/api/analytics/transactions/{txn_id}/expense-type",
                        json={"name": "Fast food"}, headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["allocations_updated"] == 1
        assert r.json()["expense_type"]["name"] == "Fast food"

    def test_labelling_an_unfiled_transaction_explains_itself(self, client: TestClient):
        """A 409 with a reason, not a 500 or a silent no-op."""
        from app.db.models import Account, PlaidItem, Transaction, User

        h = _auth(client)
        with SessionLocal() as db:
            from sqlalchemy import select
            uid = db.scalars(select(User.id).order_by(User.created_at.desc())).first()
            item = PlaidItem(user_id=uid, plaid_item_id=f"it-{uuid.uuid4().hex[:10]}",
                             access_token_encrypted=b"x")
            db.add(item); db.flush()
            acct = Account(user_id=uid, item_id=item.id,
                           plaid_account_id=f"ac-{uuid.uuid4().hex[:10]}", name="C")
            db.add(acct); db.flush()
            t = Transaction(user_id=uid, account_id=acct.id,
                            plaid_transaction_id=f"tx-{uuid.uuid4().hex[:14]}",
                            outflow_cents=500, date=date.today(), name="Mystery")
            db.add(t); db.commit()
            txn_id = str(t.id)

        r = client.post(f"/api/analytics/transactions/{txn_id}/expense-type",
                        json={"name": "Fast food"}, headers=h)
        assert r.status_code == 409
        assert "categor" in r.json()["detail"].lower()
