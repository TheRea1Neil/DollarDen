"""Allocation ledger invariants (docs/06).

The ledger is where a subtle bug produces quietly wrong numbers for months,
so these assert the invariants directly against a real database.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.budget.ledger import (
    Entry,
    LedgerError,
    audit_balances,
    post,
    recompute_balance,
    reverse,
)
from app.core.security import hash_password
from app.db.models import Category, User
from app.db.session import SessionLocal
from tests.conftest import requires_db


def _user(db) -> User:
    u = User(email=f"led-{uuid.uuid4().hex[:8]}@x.com", password_hash=hash_password("x" * 12))
    db.add(u)
    db.flush()
    return u


def _cat(db, user, name="Food", kind="spending", **kw) -> Category:
    c = Category(user_id=user.id, name=f"{name}-{uuid.uuid4().hex[:6]}", kind=kind, **kw)
    db.add(c)
    db.flush()
    return c


@requires_db
class TestBalanceIsDerived:
    def test_balance_equals_sum_of_allocations(self) -> None:
        with SessionLocal() as db:
            u, c = _user(db), None
            c = _cat(db, u)
            post(db, u.id, [Entry(c.id, 30000, "paycheck_rule")])
            post(db, u.id, [Entry(c.id, -4210, "auto_categorise")])
            post(db, u.id, [Entry(c.id, -1299, "manual")])
            assert c.balance_cents == 30000 - 4210 - 1299 == 24491
            assert audit_balances(db, u.id) == []
            db.rollback()

    def test_recompute_repairs_a_tampered_cache(self) -> None:
        """The cache must never be authoritative."""
        with SessionLocal() as db:
            u = _user(db)
            c = _cat(db, u)
            post(db, u.id, [Entry(c.id, 10000, "paycheck_rule")])

            c.balance_cents = 999999          # simulate drift
            db.flush()
            assert audit_balances(db, u.id) != []

            recompute_balance(db, u.id, c.id)
            assert c.balance_cents == 10000
            assert audit_balances(db, u.id) == []
            db.rollback()

    def test_many_entries_stay_exact(self) -> None:
        """Integer cents must not drift over hundreds of operations."""
        with SessionLocal() as db:
            u = _user(db)
            c = _cat(db, u)
            expected = 0
            for i in range(200):
                amt = (i + 1) * 7 * (1 if i % 3 else -1)
                post(db, u.id, [Entry(c.id, amt, "manual")])
                expected += amt
            assert c.balance_cents == expected
            assert audit_balances(db, u.id) == []
            db.rollback()


@requires_db
class TestLedgerRefusesCorruption:
    def test_zero_amount_rejected(self) -> None:
        with SessionLocal() as db:
            u = _user(db)
            c = _cat(db, u)
            with pytest.raises(LedgerError, match="zero-amount"):
                post(db, u.id, [Entry(c.id, 0, "manual")])
            db.rollback()

    def test_unknown_source_rejected(self) -> None:
        with SessionLocal() as db:
            u = _user(db)
            c = _cat(db, u)
            with pytest.raises(LedgerError, match="unknown allocation source"):
                post(db, u.id, [Entry(c.id, 100, "made_up")])
            db.rollback()

    def test_archived_category_rejected(self) -> None:
        with SessionLocal() as db:
            u = _user(db)
            c = _cat(db, u, archived_at=datetime.now(UTC))
            with pytest.raises(LedgerError, match="archived"):
                post(db, u.id, [Entry(c.id, 100, "manual")])
            db.rollback()

    def test_another_users_category_rejected(self) -> None:
        """Cross-user writes must be impossible, not merely unlikely."""
        with SessionLocal() as db:
            a, b = _user(db), _user(db)
            cat_b = _cat(db, b)
            with pytest.raises(LedgerError, match="unknown category"):
                post(db, a.id, [Entry(cat_b.id, 500, "manual")])
            db.rollback()

    def test_an_invalid_batch_writes_nothing(self) -> None:
        """Validation happens before any write, so there is no partial apply."""
        with SessionLocal() as db:
            u = _user(db)
            good = _cat(db, u, name="Good")
            post(db, u.id, [Entry(good.id, 5000, "manual")])

            with pytest.raises(LedgerError):
                post(db, u.id, [Entry(good.id, 100, "manual"), Entry(good.id, 0, "manual")])

            recompute_balance(db, u.id, good.id)
            assert good.balance_cents == 5000, "a rejected batch must not be partially applied"
            db.rollback()


@requires_db
class TestCorrections:
    def test_reverse_restores_the_balance_without_deleting_history(self) -> None:
        with SessionLocal() as db:
            u = _user(db)
            c = _cat(db, u)
            created = post(db, u.id, [Entry(c.id, 7500, "paycheck_rule")])
            assert c.balance_cents == 7500

            reverse(db, u.id, created, "mistake")
            assert c.balance_cents == 0

            from sqlalchemy import select

            from app.db.models import Allocation

            rows = db.scalars(select(Allocation).where(Allocation.category_id == c.id)).all()
            assert len(rows) == 2, "the original entry must survive its correction"
            assert {r.source for r in rows} == {"paycheck_rule", "correction"}
            db.rollback()

    def test_audit_finds_nothing_after_normal_use(self) -> None:
        with SessionLocal() as db:
            u = _user(db)
            food, tithe = _cat(db, u, "Food"), _cat(db, u, "Tithing", "obligation")
            post(db, u.id, [Entry(food.id, 30000, "paycheck_rule"),
                            Entry(tithe.id, 20000, "paycheck_rule")])
            post(db, u.id, [Entry(food.id, -2599, "auto_categorise")])
            reverse(db, u.id, post(db, u.id, [Entry(tithe.id, -5000, "manual")]), "oops")
            assert audit_balances(db, u.id) == []
            db.rollback()
