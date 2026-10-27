"""Insights and the question pipeline at the HTTP layer."""
from __future__ import annotations

import uuid
from datetime import date

from fastapi.testclient import TestClient

from app.analytics import taxonomy
from app.db.session import SessionLocal
from tests.conftest import requires_db

PW = "analytics-api-passphrase-99"
TODAY = "2026-10-24"


def _auth(client: TestClient) -> dict[str, str]:
    r = client.post("/api/auth/register",
                    json={"email": f"ai-{uuid.uuid4().hex[:10]}@x.com", "password": PW})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _seed(client: TestClient, h: dict[str, str]) -> dict:
    """A small but realistic month, built through the ORM."""
    from sqlalchemy import select

    from app.budget.ledger import Entry, post
    from app.db.models import Account, Category, PlaidItem, Transaction, User

    with SessionLocal() as db:
        uid = db.scalars(select(User.id).order_by(User.created_at.desc())).first()
        item = PlaidItem(user_id=uid, plaid_item_id=f"it-{uuid.uuid4().hex[:10]}",
                         access_token_encrypted=b"x")
        db.add(item); db.flush()
        acct = Account(user_id=uid, item_id=item.id,
                       plaid_account_id=f"ac-{uuid.uuid4().hex[:10]}", name="C")
        db.add(acct); db.flush()

        cats = {}
        for n in ("Date Fund", "Discretionary", "Groceries"):
            c = Category(user_id=uid, name=n, kind="spending")
            db.add(c); db.flush()
            cats[n] = c
        post(db, uid, [Entry(c.id, 50_000, "opening_balance") for c in cats.values()])

        ff, _ = taxonomy.resolve_or_create(db, uid, "Fast food")
        gro, _ = taxonomy.resolve_or_create(db, uid, "Groceries")

        plan = [
            # Fast food out of two different envelopes -- the whole point.
            (cats["Date Fund"], ff, 2_400, date(2026, 10, 3), "Chick-fil-A"),
            (cats["Discretionary"], ff, 1_150, date(2026, 10, 11), "Taco Bell"),
            (cats["Groceries"], gro, 8_700, date(2026, 10, 6), "Hy-Vee"),
            # Last month, for the comparison.
            (cats["Date Fund"], ff, 900, date(2026, 9, 14), "Chick-fil-A"),
            # Untyped, so the "not yet labelled" card has something to say.
            (cats["Discretionary"], None, 4_000, date(2026, 10, 15), "Mystery LLC"),
        ]
        for cat, et, cents, on, merchant in plan:
            t = Transaction(user_id=uid, account_id=acct.id,
                            plaid_transaction_id=f"tx-{uuid.uuid4().hex[:14]}",
                            outflow_cents=cents, date=on, name=merchant,
                            merchant_name=merchant)
            db.add(t); db.flush()
            rows = post(db, uid, [Entry(cat.id, -cents, "manual")], transaction_id=t.id)
            if et:
                for a in rows:
                    a.expense_type_id = et.id
        db.commit()
        return {"fast_food_id": str(ff.id)}


@requires_db
class TestInsights:
    def test_an_empty_account_says_so_rather_than_showing_zeroes(self, client):
        h = _auth(client)
        cards = client.get(f"/api/analytics/insights?today={TODAY}", headers=h).json()["cards"]
        assert [c["kind"] for c in cards] == ["empty"]

    def test_cards_cover_the_month(self, client):
        h = _auth(client)
        _seed(client, h)
        body = client.get(f"/api/analytics/insights?today={TODAY}", headers=h).json()
        kinds = {c["kind"] for c in body["cards"]}
        assert {"flow", "by_type", "top_merchants"} <= kinds
        assert body["period"]["start"] == "2026-10-01"

    def test_the_type_breakdown_adds_up_to_the_total(self, client):
        h = _auth(client)
        _seed(client, h)
        cards = client.get(f"/api/analytics/insights?today={TODAY}", headers=h).json()["cards"]
        by_type = next(c for c in cards if c["kind"] == "by_type")
        assert sum(g["cents"] for g in by_type["groups"]) == by_type["total_cents"]

    def test_untyped_spending_is_declared(self, client):
        """Otherwise the breakdown looks authoritative while missing money."""
        h = _auth(client)
        _seed(client, h)
        cards = client.get(f"/api/analytics/insights?today={TODAY}", headers=h).json()["cards"]
        untyped = next(c for c in cards if c["kind"] == "untyped")
        assert untyped["cents"] == 4_000

    def test_an_unparseable_period_is_a_400_not_a_guess(self, client):
        h = _auth(client)
        r = client.get("/api/analytics/insights?period=whenever", headers=h)
        assert r.status_code == 400


@requires_db
class TestSpendQueries:
    def test_fast_food_totals_across_envelopes(self, client):
        h = _auth(client)
        ids = _seed(client, h)
        r = client.get(
            f"/api/analytics/spend?today={TODAY}&period=this_month"
            f"&expense_type_id={ids['fast_food_id']}",
            headers=h,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total_cents"] == 3_550
        assert body["previous_total_cents"] == 900

    def test_group_by_merchant(self, client):
        h = _auth(client)
        _seed(client, h)
        body = client.get(
            f"/api/analytics/spend?today={TODAY}&group_by=merchant", headers=h
        ).json()
        assert body["groups"][0]["label"] == "Hy-Vee"

    def test_drill_down_returns_the_rows(self, client):
        h = _auth(client)
        ids = _seed(client, h)
        body = client.get(
            f"/api/analytics/spend/transactions?today={TODAY}"
            f"&expense_type_id={ids['fast_food_id']}",
            headers=h,
        ).json()
        assert {t["merchant"] for t in body["transactions"]} == {"Chick-fil-A", "Taco Bell"}

    def test_the_untyped_queue_lists_what_needs_a_label(self, client):
        h = _auth(client)
        _seed(client, h)
        body = client.get(f"/api/analytics/untyped?today={TODAY}", headers=h).json()
        assert [t["merchant"] for t in body["transactions"]] == ["Mystery LLC"]

    def test_isolation(self, client):
        a = _auth(client)
        _seed(client, a)
        b = _auth(client)
        body = client.get(f"/api/analytics/spend?today={TODAY}", headers=b).json()
        assert body["total_cents"] == 0


@requires_db
class TestAskCompiles:
    """The question pipeline, with the model stubbed.

    What is under test is the half that must never be wrong: resolving
    a compiled spec against real rows and computing the figure. The
    model's job -- choosing filters -- is faked here deliberately, so a
    failure points at the arithmetic rather than at the weather on the
    inference host.
    """

    def test_a_compiled_question_produces_a_sql_computed_number(self, client):
        import asyncio

        from app.ai import tasks
        from app.ai.provider import ChatResult
        from app.db.models import User
        from sqlalchemy import select

        h = _auth(client)
        _seed(client, h)

        class Stub:
            def __init__(self):
                self.calls = 0

            async def chat(self, prompt, **kw):
                self.calls += 1
                if self.calls == 1:
                    return ChatResult(content=(
                        '{"metric":"spend","period":"this_month","start":"","end":"",'
                        '"group_by":"none","expense_types":["Fast food"],'
                        '"categories":[],"merchant":""}'), model="stub")
                # The phrasing call gets the real figures in its prompt.
                assert "$35.50" in prompt
                return ChatResult(content="You spent $35.50 on fast food.", model="stub")

        with SessionLocal() as db:
            uid = db.scalars(select(User.id).order_by(User.created_at.desc())).first()
            user = db.get(User, uid)
            stub = Stub()
            out = asyncio.run(tasks.run_ask(
                db, user, stub,
                {"question": "how much on fast food this month?", "today": "2026-10-24"},
            ))

        assert out["result"]["total_cents"] == 3_550
        assert out["query"]["expense_types"] == ["Fast food"]
        assert out["query"]["period"]["start"] == "2026-10-01"
        assert "$35.50" in out["answer"]

    def test_a_filter_the_model_invents_is_reported_not_silently_dropped(self, client):
        """A zero that comes from a bad filter must not look like a real zero."""
        import asyncio

        from app.ai import tasks
        from app.ai.provider import ChatResult
        from app.db.models import User
        from sqlalchemy import select

        h = _auth(client)
        _seed(client, h)

        class Stub:
            async def chat(self, prompt, **kw):
                return ChatResult(content=(
                    '{"metric":"spend","period":"this_month","start":"","end":"",'
                    '"group_by":"none","expense_types":["Artisanal Cheese"],'
                    '"categories":[],"merchant":""}'), model="stub")

        with SessionLocal() as db:
            uid = db.scalars(select(User.id).order_by(User.created_at.desc())).first()
            out = asyncio.run(tasks.run_ask(
                db, db.get(User, uid), Stub(),
                {"question": "how much on artisanal cheese?", "today": "2026-10-24"},
            ))
        assert out["unresolved_filters"] == ["Artisanal Cheese"]

    def test_the_numbers_survive_a_failed_phrasing_call(self, client):
        """Prose is the optional half. The figure is not."""
        import asyncio

        from app.ai import tasks
        from app.ai.provider import AIUnavailable, ChatResult
        from app.db.models import User
        from sqlalchemy import select

        h = _auth(client)
        _seed(client, h)

        class Stub:
            def __init__(self):
                self.calls = 0

            async def chat(self, prompt, **kw):
                self.calls += 1
                if self.calls == 1:
                    return ChatResult(content=(
                        '{"metric":"spend","period":"this_month","start":"","end":"",'
                        '"group_by":"none","expense_types":[],"categories":[],'
                        '"merchant":""}'), model="stub")
                raise AIUnavailable("deck is asleep")

        with SessionLocal() as db:
            uid = db.scalars(select(User.id).order_by(User.created_at.desc())).first()
            out = asyncio.run(tasks.run_ask(
                db, db.get(User, uid), Stub(),
                {"question": "what did I spend?", "today": "2026-10-24"},
            ))
        assert out["result"]["total_cents"] == 16_250
        assert "$162.50" in out["answer"]


@requires_db
class TestClassifyValidation:
    def test_a_coined_near_duplicate_collapses_onto_the_existing_type(self, client):
        """The model is told to reuse and still proposes synonyms."""
        from app.ai.tasks import _validate_expense_type

        offered = [{"name": "Fast food", "description": "", "status": "existing"}]
        out = _validate_expense_type(
            {"expense_type": "NEW", "new_name": "Fast Foods",
             "new_description": "x", "confidence": "high", "why": ""},
            offered,
        )
        assert out["expense_type_name"] == "Fast food"
        assert out["is_new"] is False
        assert out["collapsed_from"] == "Fast Foods"

    def test_accepting_a_standard_name_still_counts_as_new(self, client):
        from app.ai.tasks import _validate_expense_type

        offered = [{"name": "Groceries", "description": "Supermarket",
                    "status": "available"}]
        out = _validate_expense_type(
            {"expense_type": "Groceries", "new_name": "", "new_description": "",
             "confidence": "high", "why": ""},
            offered,
        )
        assert out["is_new"] is True, "an offered standard has no row yet"
        assert out["new_description"] == "Supermarket"

    def test_unsure_yields_no_type(self, client):
        from app.ai.tasks import _validate_expense_type

        out = _validate_expense_type(
            {"expense_type": "UNSURE", "new_name": "", "new_description": "",
             "confidence": "low", "why": "bare processor string"},
            [{"name": "Fast food", "description": "", "status": "existing"}],
        )
        assert out["unsure"] is True and out["expense_type_name"] is None

    def test_asked_to_coin_and_producing_nothing_is_treated_as_unsure(self, client):
        from app.ai.tasks import _validate_expense_type

        out = _validate_expense_type(
            {"expense_type": "NEW", "new_name": "", "new_description": "",
             "confidence": "high", "why": ""},
            [],
        )
        assert out["unsure"] is True


class TestCompileSchemaIsValid:
    """An empty enum is not just useless, it is rejected outright.

    gemma4 answers a schema containing `enum: []` with

        400  JSON schema conversion failed: enum must be a non-empty array

    A fresh account has no expense types, so every question asked before
    the first type existed failed. The previous model accepted the
    malformed schema silently, so nothing caught it until the model
    changed.
    """

    def _enums(self, node):
        found = []
        if isinstance(node, dict):
            if "enum" in node:
                found.append(node["enum"])
            for v in node.values():
                found += self._enums(v)
        elif isinstance(node, list):
            for v in node:
                found += self._enums(v)
        return found

    def test_no_empty_enum_on_a_fresh_account(self):
        from app.ai.tasks import _compile_schema
        schema = _compile_schema([], [])
        assert all(len(e) > 0 for e in self._enums(schema)), schema

    def test_no_empty_enum_with_categories_but_no_types(self):
        from app.ai.tasks import _compile_schema
        schema = _compile_schema([], ["Food"])
        assert all(len(e) > 0 for e in self._enums(schema))
        assert "expense_types" not in schema["properties"]
        assert "categories" in schema["properties"]

    def test_required_never_names_a_missing_property(self):
        from app.ai.tasks import _compile_schema
        for types, cats in (([], []), ([], ["Food"]), (["Fast food"], []),
                            (["Fast food"], ["Food"])):
            schema = _compile_schema(types, cats)
            assert set(schema["required"]) <= set(schema["properties"]), (types, cats)

    def test_both_present_when_the_user_has_both(self):
        from app.ai.tasks import _compile_schema
        schema = _compile_schema(["Fast food"], ["Food"])
        assert schema["properties"]["expense_types"]["items"]["enum"] == ["Fast food"]
        assert schema["properties"]["categories"]["items"]["enum"] == ["Food"]
