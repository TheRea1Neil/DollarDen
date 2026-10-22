"""The reconciliation invariant: allocated == cash - card debt.

This is the budgeting equivalent of reconciling a bank statement. If it
drifts, the user is being shown a comfortable number that is wrong.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select

from app.budget.ledger import Entry, post
from app.budget.reconcile import compute, ensure_unallocated, sweep_to_unallocated
from app.core.security import hash_password
from app.db.models import Account, Category, PlaidItem, User
from app.db.session import SessionLocal
from tests.conftest import requires_db


def _setup(db, *, cash: int, debt: int):
    u = User(email=f"rec-{uuid.uuid4().hex[:8]}@x.com", password_hash=hash_password("x" * 12))
    db.add(u); db.flush()
    item = PlaidItem(user_id=u.id, plaid_item_id=f"it-{uuid.uuid4().hex[:10]}",
                     access_token_encrypted=b"x")
    db.add(item); db.flush()
    db.add(Account(user_id=u.id, item_id=item.id,
                   plaid_account_id=f"ck-{uuid.uuid4().hex[:8]}",
                   name="Checking", type="depository",
                   current_cents=cash, is_tracked=True))
    db.add(Account(user_id=u.id, item_id=item.id,
                   plaid_account_id=f"cc-{uuid.uuid4().hex[:8]}",
                   name="Visa", type="credit",
                   current_cents=debt, is_tracked=True))
    db.flush()
    return u


def _cat(db, u, name):
    c = Category(user_id=u.id, name=f"{name}-{uuid.uuid4().hex[:5]}", kind="spending")
    db.add(c); db.flush()
    return c


@requires_db
class TestTheInvariant:
    def test_fully_allocated_budget_balances(self) -> None:
        with SessionLocal() as db:
            u = _setup(db, cash=100000, debt=0)
            food = _cat(db, u, "Food")
            post(db, u.id, [Entry(food.id, 100000, "opening_balance")])

            rec = compute(db, u.id)
            assert rec.available_cents == 100000
            assert rec.unallocated_cents == 0
            assert rec.balanced is True
            db.rollback()

    def test_card_spending_diverges_by_exactly_the_card_balance(self) -> None:
        """Buying on a card draws the category down before the cash leaves."""
        with SessionLocal() as db:
            u = _setup(db, cash=100000, debt=0)
            food = _cat(db, u, "Food")
            post(db, u.id, [Entry(food.id, 100000, "opening_balance")])

            # $42 on the card: category down, cash unchanged, debt up.
            post(db, u.id, [Entry(food.id, -4210, "auto_categorise")])
            db.scalars(select(Account).where(
                Account.user_id == u.id, Account.type == "credit")).first().current_cents = 4210
            db.flush()

            rec = compute(db, u.id)
            assert rec.cash_cents == 100000
            assert rec.card_debt_cents == 4210
            assert rec.allocated_cents == 95790
            # 100000 - 4210 == 95790, so still balanced.
            assert rec.unallocated_cents == 0, "divergence must equal the card balance"
            assert rec.balanced is True
            db.rollback()

    def test_paying_the_card_keeps_it_balanced(self) -> None:
        with SessionLocal() as db:
            u = _setup(db, cash=100000, debt=4210)
            food = _cat(db, u, "Food")
            post(db, u.id, [Entry(food.id, 95790, "opening_balance")])
            assert compute(db, u.id).balanced is True

            # Payment clears: cash drops by the same amount the debt did.
            cash = db.scalars(select(Account).where(
                Account.user_id == u.id, Account.type == "depository")).first()
            card = db.scalars(select(Account).where(
                Account.user_id == u.id, Account.type == "credit")).first()
            cash.current_cents = 95790
            card.current_cents = 0
            db.flush()

            rec = compute(db, u.id)
            assert rec.allocated_cents == 95790 and rec.available_cents == 95790
            assert rec.balanced is True, "paying the card must not unbalance the budget"
            db.rollback()

    def test_untracked_accounts_are_excluded(self) -> None:
        """Only the accounts the user chose to track count toward available."""
        with SessionLocal() as db:
            u = _setup(db, cash=100000, debt=0)
            db.scalars(select(Account).where(
                Account.user_id == u.id, Account.type == "depository")
            ).first().is_tracked = False
            db.flush()
            assert compute(db, u.id).cash_cents == 0
            db.rollback()


@requires_db
class TestUnallocatedSink:
    def test_created_once_and_reused(self) -> None:
        with SessionLocal() as db:
            u = _setup(db, cash=0, debt=0)
            a = ensure_unallocated(db, u.id)
            b = ensure_unallocated(db, u.id)
            assert a.id == b.id, "a second sink would break the invariant"
            assert a.is_unallocated is True
            db.rollback()

    def test_sweep_absorbs_the_remainder(self) -> None:
        with SessionLocal() as db:
            u = _setup(db, cash=100000, debt=0)
            food = _cat(db, u, "Food")
            post(db, u.id, [Entry(food.id, 30000, "opening_balance")])
            assert compute(db, u.id).unallocated_cents == 70000

            moved = sweep_to_unallocated(db, u.id)
            assert moved == 70000
            rec = compute(db, u.id)
            assert rec.balanced is True
            assert ensure_unallocated(db, u.id).balance_cents == 70000
            db.rollback()

    def test_sweep_surfaces_a_shortfall_rather_than_hiding_it(self) -> None:
        """Over-allocation leaves the sink NEGATIVE, visibly."""
        with SessionLocal() as db:
            u = _setup(db, cash=10000, debt=0)
            food = _cat(db, u, "Food")
            post(db, u.id, [Entry(food.id, 50000, "opening_balance")])
            assert compute(db, u.id).unallocated_cents == -40000

            sweep_to_unallocated(db, u.id)
            assert ensure_unallocated(db, u.id).balance_cents == -40000
            assert compute(db, u.id).balanced is True
            db.rollback()

    def test_sweeping_twice_is_a_no_op(self) -> None:
        with SessionLocal() as db:
            u = _setup(db, cash=50000, debt=0)
            sweep_to_unallocated(db, u.id)
            assert sweep_to_unallocated(db, u.id) == 0
            db.rollback()
