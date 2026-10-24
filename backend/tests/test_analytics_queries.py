"""The query engine, against a real ledger.

The cases that matter most are the exclusions. A total that is too
LARGE because it swept up envelope transfers reads exactly like a
correct one, and nobody audits a number that looks plausible.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta

from app.analytics import queries as Q
from app.analytics import taxonomy
from app.analytics.period import Period, month_period
from app.budget.ledger import Entry, post
from app.core.security import hash_password
from app.db.models import Account, Category, PlaidItem, Transaction, User
from app.db.session import SessionLocal
from tests.conftest import requires_db

TODAY = date(2026, 10, 24)
THIS_MONTH = month_period(2026, 10)


def _user(db):
    u = User(email=f"an-{uuid.uuid4().hex[:8]}@x.com", password_hash=hash_password("x" * 12))
    db.add(u); db.flush()
    item = PlaidItem(user_id=u.id, plaid_item_id=f"it-{uuid.uuid4().hex[:10]}",
                     access_token_encrypted=b"x")
    db.add(item); db.flush()
    acct = Account(user_id=u.id, item_id=item.id,
                   plaid_account_id=f"ac-{uuid.uuid4().hex[:10]}", name="Checking")
    db.add(acct); db.flush()
    return u, acct


def _cat(db, u, name, kind="spending"):
    c = Category(user_id=u.id, name=name, kind=kind)
    db.add(c); db.flush()
    return c


def _spend(db, u, acct, cat, cents, *, on: date, merchant="Shop", etype=None):
    """One charge, filed into a category, optionally typed."""
    t = Transaction(
        user_id=u.id, account_id=acct.id,
        plaid_transaction_id=f"tx-{uuid.uuid4().hex[:14]}",
        outflow_cents=cents, date=on, name=merchant, merchant_name=merchant,
    )
    db.add(t); db.flush()
    rows = post(db, u.id, [Entry(cat.id, -cents, "manual")], transaction_id=t.id)
    if etype is not None:
        for a in rows:
            a.expense_type_id = etype.id
        db.flush()
    return t


@requires_db
class TestWhatCounts:
    def test_spend_is_the_sum_of_negative_allocations(self):
        with SessionLocal() as db:
            u, acct = _user(db)
            food = _cat(db, u, "Food")
            _spend(db, u, acct, food, 1_250, on=date(2026, 10, 3))
            _spend(db, u, acct, food, 3_400, on=date(2026, 10, 9))

            res = Q.run(db, u.id, Q.SpendQuery(period=THIS_MONTH))
            assert res.total_cents == 4_650
            assert res.count == 2

    def test_funding_an_envelope_is_not_spending(self):
        with SessionLocal() as db:
            u, acct = _user(db)
            food = _cat(db, u, "Food")
            post(db, u.id, [Entry(food.id, 50_000, "opening_balance")])
            _spend(db, u, acct, food, 1_000, on=date(2026, 10, 3))

            assert Q.run(db, u.id, Q.SpendQuery(period=THIS_MONTH)).total_cents == 1_000

    def test_moving_money_between_envelopes_is_not_spending(self):
        """The one that would silently inflate every total on the screen."""
        with SessionLocal() as db:
            u, acct = _user(db)
            disc, groc = _cat(db, u, "Discretionary"), _cat(db, u, "Groceries")
            post(db, u.id, [Entry(disc.id, 10_000, "opening_balance")])
            # A hand transfer: out of one, into the other, no transaction.
            post(db, u.id, [Entry(disc.id, -5_000, "manual"),
                            Entry(groc.id, 5_000, "manual")])
            _spend(db, u, acct, groc, 2_000, on=date(2026, 10, 10))

            res = Q.run(db, u.id, Q.SpendQuery(period=THIS_MONTH))
            assert res.total_cents == 2_000, "the transfer leaked into spending"

    def test_a_removed_transaction_drops_out(self):
        with SessionLocal() as db:
            u, acct = _user(db)
            food = _cat(db, u, "Food")
            t = _spend(db, u, acct, food, 9_900, on=date(2026, 10, 5))
            _spend(db, u, acct, food, 100, on=date(2026, 10, 6))
            t.removed_at = date(2026, 10, 7)
            db.flush()

            assert Q.run(db, u.id, Q.SpendQuery(period=THIS_MONTH)).total_cents == 100

    def test_one_itemised_receipt_is_one_transaction(self):
        """Four envelopes, one trip to the shop. "4 visits" would be a lie."""
        with SessionLocal() as db:
            u, acct = _user(db)
            a, b = _cat(db, u, "Groceries"), _cat(db, u, "Household")
            t = Transaction(
                user_id=u.id, account_id=acct.id,
                plaid_transaction_id=f"tx-{uuid.uuid4().hex[:14]}",
                outflow_cents=5_000, date=date(2026, 10, 12), name="Target",
            )
            db.add(t); db.flush()
            post(db, u.id, [Entry(a.id, -3_000, "receipt_split"),
                            Entry(b.id, -2_000, "receipt_split")],
                 transaction_id=t.id)

            res = Q.run(db, u.id, Q.SpendQuery(period=THIS_MONTH))
            assert res.total_cents == 5_000
            assert res.count == 1

    def test_other_users_spending_is_invisible(self):
        with SessionLocal() as db:
            mine, my_acct = _user(db)
            theirs, their_acct = _user(db)
            _spend(db, mine, my_acct, _cat(db, mine, "Food"), 1_000, on=date(2026, 10, 2))
            _spend(db, theirs, their_acct, _cat(db, theirs, "Food"), 99_000,
                   on=date(2026, 10, 2))

            assert Q.run(db, mine.id, Q.SpendQuery(period=THIS_MONTH)).total_cents == 1_000


@requires_db
class TestPeriodBoundaries:
    def test_the_last_day_of_the_month_is_included(self):
        """Inclusive at both ends -- an exclusive end loses the 31st."""
        with SessionLocal() as db:
            u, acct = _user(db)
            food = _cat(db, u, "Food")
            _spend(db, u, acct, food, 777, on=date(2026, 10, 31))
            assert Q.run(db, u.id, Q.SpendQuery(period=THIS_MONTH)).total_cents == 777

    def test_the_first_day_is_included(self):
        with SessionLocal() as db:
            u, acct = _user(db)
            food = _cat(db, u, "Food")
            _spend(db, u, acct, food, 555, on=date(2026, 10, 1))
            assert Q.run(db, u.id, Q.SpendQuery(period=THIS_MONTH)).total_cents == 555

    def test_the_comparison_period_is_the_previous_month(self):
        with SessionLocal() as db:
            u, acct = _user(db)
            food = _cat(db, u, "Food")
            _spend(db, u, acct, food, 10_000, on=date(2026, 9, 15))
            _spend(db, u, acct, food, 12_000, on=date(2026, 10, 15))

            res = Q.run(db, u.id, Q.SpendQuery(period=THIS_MONTH))
            assert res.total_cents == 12_000
            assert res.previous_total_cents == 10_000
            assert res.delta_cents == 2_000


@requires_db
class TestGrouping:
    def test_by_expense_type_with_an_untyped_bucket(self):
        with SessionLocal() as db:
            u, acct = _user(db)
            food = _cat(db, u, "Food")
            ff, _ = taxonomy.resolve_or_create(db, u.id, "Fast food")
            _spend(db, u, acct, food, 1_200, on=date(2026, 10, 3), etype=ff)
            _spend(db, u, acct, food, 800, on=date(2026, 10, 4), etype=ff)
            _spend(db, u, acct, food, 5_000, on=date(2026, 10, 5))  # no type

            res = Q.run(db, u.id, Q.SpendQuery(period=THIS_MONTH, group_by="expense_type"))
            by = {g.label: g.cents for g in res.groups}
            assert by["Fast food"] == 2_000
            assert by[Q.UNTYPED_LABEL] == 5_000, "untyped spend must stay visible"
            assert sum(by.values()) == res.total_cents

    def test_the_same_type_across_two_envelopes_adds_up(self):
        """The reason this axis exists at all."""
        with SessionLocal() as db:
            u, acct = _user(db)
            date_fund, disc = _cat(db, u, "Date Fund"), _cat(db, u, "Discretionary")
            ff, _ = taxonomy.resolve_or_create(db, u.id, "Fast food")
            _spend(db, u, acct, date_fund, 2_400, on=date(2026, 10, 3), etype=ff)
            _spend(db, u, acct, disc, 1_100, on=date(2026, 10, 8), etype=ff)

            res = Q.run(db, u.id, Q.SpendQuery(
                period=THIS_MONTH, expense_type_ids=(ff.id,)))
            assert res.total_cents == 3_500

    def test_months_come_back_in_time_order_not_size_order(self):
        with SessionLocal() as db:
            u, acct = _user(db)
            food = _cat(db, u, "Food")
            _spend(db, u, acct, food, 9_000, on=date(2026, 8, 5))
            _spend(db, u, acct, food, 1_000, on=date(2026, 9, 5))
            _spend(db, u, acct, food, 5_000, on=date(2026, 10, 5))

            res = Q.run(db, u.id, Q.SpendQuery(
                period=Period(date(2026, 8, 1), date(2026, 10, 31), "q"),
                group_by="month", compare_to_previous=False))
            assert [g.label for g in res.groups] == ["2026-08", "2026-09", "2026-10"]


@requires_db
class TestMetrics:
    def test_average_is_per_transaction(self):
        with SessionLocal() as db:
            u, acct = _user(db)
            food = _cat(db, u, "Food")
            for cents in (1_000, 2_000, 3_000):
                _spend(db, u, acct, food, cents, on=date(2026, 10, 7))

            res = Q.run(db, u.id, Q.SpendQuery(period=THIS_MONTH, metric="average"))
            assert res.total_cents == 2_000

    def test_average_of_nothing_is_zero_not_a_crash(self):
        with SessionLocal() as db:
            u, _ = _user(db)
            assert Q.run(db, u.id, Q.SpendQuery(
                period=THIS_MONTH, metric="average")).total_cents == 0


@requires_db
class TestDrillDown:
    def test_the_rows_behind_a_total_are_retrievable(self):
        with SessionLocal() as db:
            u, acct = _user(db)
            food = _cat(db, u, "Food")
            ff, _ = taxonomy.resolve_or_create(db, u.id, "Fast food")
            _spend(db, u, acct, food, 1_234, on=date(2026, 10, 3),
                   merchant="Chick-fil-A", etype=ff)

            rows = Q.transactions_for(db, u.id, Q.SpendQuery(
                period=THIS_MONTH, expense_type_ids=(ff.id,)))
            assert len(rows) == 1
            assert rows[0]["merchant"] == "Chick-fil-A"
            assert rows[0]["amount_cents"] == 1_234
            assert rows[0]["expense_type"] == "Fast food"
            assert rows[0]["category"] == "Food"
