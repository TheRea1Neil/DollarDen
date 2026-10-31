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



@requires_db
class TestAskAgent:
    """The agent loop that replaced the compile pipeline.

    The properties that must survive the change are the ones that made
    the old design safe: every figure comes out of SQL, an empty result
    is distinguishable from a real zero, and the loop terminates.
    """

    def _run(self, client, script):
        import asyncio

        from sqlalchemy import select

        from app.ai import tasks
        from app.ai.provider import ChatResult
        from app.db.models import User

        h = _auth(client)
        _seed(client, h)

        class Stub:
            def __init__(self):
                self.script = list(script)
                self.seen = []

            async def chat(self, prompt, **kw):
                self.seen.append(kw.get("messages"))
                if not self.script:
                    return ChatResult(content="", model="stub")
                step = self.script.pop(0)
                if isinstance(step, str):
                    return ChatResult(content=step, model="stub")
                return ChatResult(content="", model="stub", tool_calls=step)

        stub = Stub()
        with SessionLocal() as db:
            uid = db.scalars(select(User.id).order_by(User.created_at.desc())).first()
            out = asyncio.run(tasks.run_ask(
                db, db.get(User, uid), stub,
                {"question": "q", "today": "2026-10-24"}))
        return out, stub

    def _call(self, name, **args):
        return [{"name": name, "arguments": args}]

    def test_a_figure_comes_from_sql_not_the_model(self, client):
        out, _ = self._run(client, [
            self._call("spending", metric="spend", period="this_month"),
            self._call("answer", text="You spent $162.50."),
        ])
        step = out["steps"][0]
        assert step["tool"] == "spending"
        assert step["result"]["total"] == "$162.50", "SQL produced this"
        assert "$162.50" in out["answer"]

    def test_the_lookups_are_returned_so_the_answer_can_be_audited(self, client):
        out, _ = self._run(client, [
            self._call("spending", metric="spend", period="this_month"),
            self._call("answer", text="ok"),
        ])
        assert out["steps"], "an answer you cannot check is a claim"
        assert out["steps"][0]["arguments"]["period"] == "this_month"

    def test_an_empty_result_is_not_reported_as_zero_spending(self, client):
        """The groceries/$0.00 failure: a filter matching nothing must
        not look like a real zero."""
        out, _ = self._run(client, [
            self._call("spending", metric="spend", period="this_month",
                       merchant="groceries"),
            self._call("answer", text="Nothing matched that shop."),
        ])
        res = out["steps"][0]["result"]
        assert res["transactions"] == 0
        assert "not necessarily zero spending" in res["note"]

    def test_it_can_fetch_the_budget_and_a_figure_in_one_question(self, client):
        """What routing made impossible."""
        out, _ = self._run(client, [
            self._call("budget"),
            self._call("spending", metric="spend", period="this_month"),
            self._call("answer", text="Both."),
        ])
        tools = [s["tool"] for s in out["steps"]]
        assert tools == ["budget", "spending"]
        assert "HOW THIS BUDGET WORKS" in out["steps"][0]["result"]["budget"]

    def test_an_unknown_period_is_an_error_not_a_wrong_month(self, client):
        out, _ = self._run(client, [
            self._call("spending", metric="spend", period="since_easter"),
            self._call("answer", text="I could not read that date range."),
        ])
        assert "error" in out["steps"][0]["result"]

    def test_the_loop_is_capped(self, client):
        from app.ai.ask_agent import MAX_TOOL_CALLS
        out, _ = self._run(client, [
            self._call("spending", metric="spend", period="this_month")
            for _ in range(40)
        ])
        assert out["tool_calls"] <= MAX_TOOL_CALLS
        assert out["answer"], "it must still say something"

    def test_prose_instead_of_the_answer_tool_is_accepted(self, client):
        out, _ = self._run(client, ["You spent $162.50 this month."])
        assert "$162.50" in out["answer"]

    def test_a_model_that_says_nothing_still_returns_something_useful(self, client):
        out, _ = self._run(client, [])
        assert "could not work that out" in out["answer"]

    def test_tool_enums_only_offer_envelopes_that_exist(self, client):
        """So it cannot ask for a category that is not there — the
        mistake behind the silent zero."""
        from app.ai.ask_agent import tools_for
        tools = tools_for([], ["Food & Groceries", "Health"])
        spending = next(t for t in tools if t["function"]["name"] == "spending")
        enum = spending["function"]["parameters"]["properties"]["categories"]["items"]["enum"]
        assert enum == ["Food & Groceries", "Health"]

    def test_no_empty_enum_when_the_user_has_no_types(self, client):
        """gemma4 rejects enum: [] outright."""
        from app.ai.ask_agent import tools_for
        tools = tools_for([], [])
        spending = next(t for t in tools if t["function"]["name"] == "spending")
        props = spending["function"]["parameters"]["properties"]
        assert "expense_types" not in props and "categories" not in props


class TestAnswerIsPlainText:
    """The screen renders the answer verbatim.

    Markdown arrives with its punctuation attached: `**Utilities**`
    appears on the phone as asterisks. The prompt asks for plain text,
    which mostly works; this is the part that guarantees it.
    """

    def test_bold_is_unwrapped(self):
        from app.ai.ask_agent import strip_markdown
        assert strip_markdown("you need **Utilities** next") == \
            "you need Utilities next"

    def test_headings_go(self):
        from app.ai.ask_agent import strip_markdown
        assert strip_markdown("## Summary\nYou spent $5.") == "Summary\nYou spent $5."

    def test_bullets_become_plain_dashes(self):
        from app.ai.ask_agent import strip_markdown
        assert strip_markdown("* one\n* two") == "- one\n- two"

    def test_code_ticks_go(self):
        from app.ai.ask_agent import strip_markdown
        assert strip_markdown("the `Gas` envelope") == "the Gas envelope"

    def test_a_lone_asterisk_in_prose_survives(self):
        """Stripping must not mangle ordinary text."""
        from app.ai.ask_agent import strip_markdown
        assert strip_markdown("2 * 3 is 6") == "2 * 3 is 6"

    def test_money_is_untouched(self):
        from app.ai.ask_agent import strip_markdown
        assert "$1,234.56" in strip_markdown("that came to **$1,234.56** total")
