"""The `payday` and `calculate` tools.

Between them they answer "how far does my food money have to stretch"
without the model doing a single sum in its head -- which it does
fluently and sometimes wrongly, and a daily allowance that is off by a
third reads exactly as confident as a right one.

`payday` supplies facts only: today, the expected payday, how many days
off it is. `calculate` does the arithmetic the question happens to
need. The split matters: an earlier version pre-divided every envelope
balance by the days remaining, which answered one scenario and answered
it wrongly for bills -- on the real budget it produced "Rent $3,076.59
-> $1,025.53 /day", which is not a thing. Facts plus a calculator
answers any scenario and invents none.

Still ADR 0006 and 0009: the model decides WHICH sum, the engine
computes it.
"""
from __future__ import annotations

import uuid
from datetime import date

from app.ai.ask_agent import _run_payday, tools_for
from app.db.models import (Account, Category, PlaidItem, Transaction, User)
from app.db.session import SessionLocal
from tests.conftest import requires_db


def _seed(db) -> tuple[User, Account]:
    u = User(email=f"pd-{uuid.uuid4().hex[:8]}@x.com", password_hash="x")
    db.add(u); db.flush()
    item = PlaidItem(user_id=u.id, plaid_item_id=f"it-{uuid.uuid4().hex[:8]}",
                     access_token_encrypted=b"x")
    db.add(item); db.flush()
    acct = Account(user_id=u.id, item_id=item.id,
                   plaid_account_id=f"a-{uuid.uuid4().hex[:8]}",
                   name="Checking", type="depository", is_tracked=True)
    db.add(acct); db.flush()
    return u, acct


def _deposit(db, u, acct, on: date, cents: int = 200000) -> None:
    db.add(Transaction(
        user_id=u.id, account_id=acct.id,
        plaid_transaction_id=f"t-{uuid.uuid4().hex[:10]}",
        outflow_cents=0, inflow_cents=cents, date=on,
        name="ACH CREDIT PAYROLL", pfc_primary="INCOME",
        treatment="ignored"))
    db.flush()


def _cat(db, u, name: str, cents: int, *, unallocated: bool = False,
         kind: str = "spending") -> Category:
    c = Category(user_id=u.id, name=name, kind=kind,
                 balance_cents=cents, is_unallocated=unallocated)
    db.add(c); db.flush()
    return c


def _cats(db, u) -> list[Category]:
    return list(db.query(Category).filter(Category.user_id == u.id).all())


@requires_db
class TestItAlwaysKnowsTheDate:
    def test_today_is_reported_even_with_no_payday_history(self) -> None:
        """A brand new budget still gets an answer about the date."""
        with SessionLocal() as db:
            u, _ = _seed(db)
            out = _run_payday(db, u, date(2026, 11, 16), _cats(db, u))
            assert out["today"] == "2026-11-16"
            assert out["weekday"] == "Monday"
            assert out["next_payday"]["known"] is False
            assert out["next_payday"]["reason"], "say WHY it is not known"


@requires_db
class TestThePaydayItself:
    def test_it_reports_days_until_not_just_a_date(self) -> None:
        """The day count is the whole point: the model must not subtract."""
        with SessionLocal() as db:
            u, acct = _seed(db)
            # Every 14 days, so the next is 14 after the last.
            for on in [date(2026, 11, 13), date(2026, 10, 30),
                       date(2026, 10, 16), date(2026, 10, 2)]:
                _deposit(db, u, acct, on)

            out = _run_payday(db, u, date(2026, 11, 16), _cats(db, u))
            p = out["next_payday"]
            assert p["known"] is True
            assert p["expected"] == "2026-11-27"
            assert p["days_until_expected"] == 11
            assert p["paid_every_days"] == 14
            # The range is carried through, not flattened to one date.
            assert p["days_until_earliest"] <= p["days_until_expected"]
            assert p["days_until_latest"] >= p["days_until_expected"]
            assert p["worked_out_from"] == "deposits"


@requires_db
class TestAnOverduePaydayDoesNotProduceNonsense:
    """Asking "how long must this last" is most likely when pay is LATE.

    The day count goes negative, which is honest, but it has to be
    labelled as overdue — handed over bare it is something the model
    would happily divide a balance by.
    """

    def test_it_says_it_is_overdue(self) -> None:
        with SessionLocal() as db:
            u, acct = _seed(db)
            for on in [date(2026, 10, 16), date(2026, 10, 2),
                       date(2026, 9, 18), date(2026, 9, 4)]:
                _deposit(db, u, acct, on)
            # Expected payday was 2026-10-30; it is now well past.
            out = _run_payday(db, u, date(2026, 11, 16), _cats(db, u))
            p = out["next_payday"]
            assert p["days_until_expected"] < 0
            assert p["overdue_by_days"] == -p["days_until_expected"]


class TestTheToolIsOffered:
    def test_payday_is_in_the_tool_list(self) -> None:
        names = {t["function"]["name"] for t in tools_for([], [])}
        assert "payday" in names

    def test_it_takes_no_arguments(self) -> None:
        """Nothing to get wrong, so nothing to validate."""
        tool = next(t["function"] for t in tools_for([], ["Food"])
                    if t["function"]["name"] == "payday")
        assert tool["parameters"]["properties"] == {}

    def test_the_prompt_tells_the_model_not_to_count_days(self) -> None:
        """The tool is useless if the model still does it in its head."""
        from app.ai.ask_agent import PROMPT

        assert "payday" in PROMPT
        assert "Do not count days between dates yourself" in PROMPT

    def test_the_prompt_shows_how_the_two_tools_combine(self) -> None:
        """Facts and a calculator only help if it knows to pair them."""
        from app.ai.ask_agent import PROMPT

        assert "`calculate` to divide" in PROMPT
