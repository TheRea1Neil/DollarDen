"""The whole budget loop, against a real database.

Uses the plan from docs/06: tithe 10% of gross first, $350 rent, $150 health,
+$50 car insurance only when it is under $450, refill Food to $300, refill
short-term savings to $2000, remainder to the car fund.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta

from sqlalchemy import select

from app.budget.categorize import categorise, resolve_review, signed_amount
from app.budget.ledger import Entry, audit_balances, post
from app.budget.paycheck import apply_plan, predict_next, unapply
from app.core.security import hash_password
from app.db.models import (
    Account,
    Category,
    CategoryRule,
    Paycheck,
    PlaidItem,
    ReviewItem,
    SplitPlan,
    SplitRule,
    Transaction,
    User,
)
from app.db.session import SessionLocal
from tests.conftest import requires_db


def _fixture(db):
    u = User(email=f"flow-{uuid.uuid4().hex[:8]}@x.com", password_hash=hash_password("x" * 12))
    db.add(u); db.flush()

    item = PlaidItem(user_id=u.id, plaid_item_id=f"it-{uuid.uuid4().hex[:10]}",
                     access_token_encrypted=b"x")
    db.add(item); db.flush()
    acct = Account(user_id=u.id, item_id=item.id,
                   plaid_account_id=f"ac-{uuid.uuid4().hex[:10]}", name="Checking")
    db.add(acct); db.flush()

    names = ["Tithing", "Rent", "Health", "CarIns", "Food", "Savings", "NewCar"]
    kinds = ["obligation", "obligation", "obligation", "obligation",
             "spending", "savings_bucket", "savings_bucket"]
    cats = {}
    for n, k in zip(names, kinds, strict=True):
        c = Category(user_id=u.id, name=f"{n}-{uuid.uuid4().hex[:5]}", kind=k)
        db.add(c); db.flush()
        cats[n] = c

    plan = SplitPlan(user_id=u.id, name="Standard", version=1)
    db.add(plan); db.flush()
    spec = [
        (cats["Tithing"], 1, "percent", {"rate": "0.10", "of": "gross"}),
        (cats["Rent"], 2, "fixed", {"amount_cents": 350_00}),
        (cats["Health"], 3, "fixed", {"amount_cents": 150_00}),
        (cats["CarIns"], 4, "conditional",
         {"when": {"field": "balance", "op": "<", "value_cents": 450_00},
          "then": {"rule_type": "fixed", "params": {"amount_cents": 50_00}}}),
        (cats["Food"], 5, "refill_to", {"target_cents": 300_00}),
        (cats["Savings"], 6, "refill_to", {"target_cents": 2000_00}),
        (cats["NewCar"], 7, "remainder", {}),
    ]
    for cat, idx, rtype, params in spec:
        db.add(SplitRule(plan_id=plan.id, category_id=cat.id, order_index=idx,
                         rule_type=rtype, params=params))
    db.flush()
    return u, acct, cats, plan


def _txn(db, u, acct, *, out=0, inflow=0, name="X", merchant=None, pfc=None, day=0):
    t = Transaction(
        user_id=u.id, account_id=acct.id,
        plaid_transaction_id=f"tx-{uuid.uuid4().hex[:14]}",
        outflow_cents=out, inflow_cents=inflow,
        date=date.today() - timedelta(days=day), name=name,
        merchant_name=merchant, pfc_detailed=pfc,
    )
    db.add(t); db.flush()
    return t


@requires_db
class TestPaycheckSplit:
    def test_real_plan_allocates_exactly(self) -> None:
        with SessionLocal() as db:
            u, acct, cats, plan = _fixture(db)
            post(db, u.id, [Entry(cats["Food"].id, 88_00, "opening_balance"),
                            Entry(cats["Savings"].id, 2000_00, "opening_balance"),
                            Entry(cats["CarIns"].id, 400_00, "opening_balance")])

            pc = Paycheck(user_id=u.id, gross_cents=2000_00, received_on=date.today())
            db.add(pc); db.flush()
            res = apply_plan(db, u.id, pc, plan)

            assert res.allocated_cents == 2000_00, "every cent must be allocated"
            assert cats["Tithing"].balance_cents == 200_00
            assert cats["Rent"].balance_cents == 350_00
            assert cats["Health"].balance_cents == 150_00
            assert cats["CarIns"].balance_cents == 450_00     # 400 + 50
            assert cats["Food"].balance_cents == 300_00       # refilled from 88
            assert cats["Savings"].balance_cents == 2000_00   # already full
            assert cats["NewCar"].balance_cents == 1038_00    # remainder
            assert audit_balances(db, u.id) == []
            db.rollback()

    def test_applying_twice_is_refused(self) -> None:
        import pytest

        with SessionLocal() as db:
            u, acct, cats, plan = _fixture(db)
            pc = Paycheck(user_id=u.id, gross_cents=1000_00, received_on=date.today())
            db.add(pc); db.flush()
            apply_plan(db, u.id, pc, plan)
            with pytest.raises(ValueError, match="already been applied"):
                apply_plan(db, u.id, pc, plan)
            db.rollback()

    def test_unapply_restores_every_balance(self) -> None:
        with SessionLocal() as db:
            u, acct, cats, plan = _fixture(db)
            pc = Paycheck(user_id=u.id, gross_cents=1500_00, received_on=date.today())
            db.add(pc); db.flush()
            apply_plan(db, u.id, pc, plan)
            assert sum(c.balance_cents for c in cats.values()) == 1500_00

            unapply(db, u.id, pc)
            assert sum(c.balance_cents for c in cats.values()) == 0
            assert pc.applied_at is None
            assert audit_balances(db, u.id) == []
            db.rollback()

    def test_short_paycheck_reports_unfunded_rules(self) -> None:
        with SessionLocal() as db:
            u, acct, cats, plan = _fixture(db)
            pc = Paycheck(user_id=u.id, gross_cents=400_00, received_on=date.today())
            db.add(pc); db.flush()
            res = apply_plan(db, u.id, pc, plan)

            assert res.allocated_cents == 400_00
            assert res.unfunded, "the user must be told which rules went short"
            assert cats["Tithing"].balance_cents == 40_00
            assert cats["Rent"].balance_cents == 350_00
            assert cats["Health"].balance_cents == 10_00   # clamped
            db.rollback()


@requires_db
class TestDrawDown:
    def test_a_rule_draws_the_right_category_down(self) -> None:
        with SessionLocal() as db:
            u, acct, cats, plan = _fixture(db)
            post(db, u.id, [Entry(cats["Food"].id, 300_00, "opening_balance")])
            db.add(CategoryRule(user_id=u.id, category_id=cats["Food"].id,
                                matcher={"merchant_contains": "smiths"}, priority=10))
            db.flush()

            t = _txn(db, u, acct, out=42_10, name="SMITHS FOOD", merchant="Smiths Food and Drug")
            assert categorise(db, u.id, t) == "rule"
            assert cats["Food"].balance_cents == 300_00 - 42_10
            db.rollback()

    def test_an_unknown_merchant_goes_to_review_not_a_guess(self) -> None:
        with SessionLocal() as db:
            u, acct, cats, plan = _fixture(db)
            t = _txn(db, u, acct, out=19_99, name="MYSTERY LLC", merchant="Mystery LLC")
            assert categorise(db, u.id, t) == "review"

            rv = db.scalars(select(ReviewItem).where(ReviewItem.transaction_id == t.id)).first()
            assert rv is not None and rv.resolved_at is None
            # Nothing was allocated on a guess.
            assert all(c.balance_cents == 0 for c in cats.values())
            db.rollback()

    def test_review_queueing_is_idempotent(self) -> None:
        with SessionLocal() as db:
            u, acct, cats, plan = _fixture(db)
            t = _txn(db, u, acct, out=19_99, merchant="Mystery LLC")
            categorise(db, u.id, t)
            categorise(db, u.id, t)
            rows = db.scalars(select(ReviewItem).where(ReviewItem.transaction_id == t.id)).all()
            assert len(rows) == 1, "a webhook retry must not queue the same review twice"
            db.rollback()

    def test_resolving_a_review_can_split_across_categories(self) -> None:
        with SessionLocal() as db:
            u, acct, cats, plan = _fixture(db)
            post(db, u.id, [Entry(cats["Food"].id, 300_00, "opening_balance"),
                            Entry(cats["NewCar"].id, 500_00, "opening_balance")])
            t = _txn(db, u, acct, out=100_00, merchant="Costco")
            categorise(db, u.id, t)
            rv = db.scalars(select(ReviewItem).where(ReviewItem.transaction_id == t.id)).first()

            resolve_review(db, u.id, rv, [(cats["Food"].id, 60_00), (cats["NewCar"].id, 40_00)])

            assert cats["Food"].balance_cents == 240_00
            assert cats["NewCar"].balance_cents == 460_00
            assert rv.resolved_at is not None
            assert audit_balances(db, u.id) == []
            db.rollback()

    def test_a_partial_split_is_refused(self) -> None:
        import pytest

        with SessionLocal() as db:
            u, acct, cats, plan = _fixture(db)
            t = _txn(db, u, acct, out=100_00, merchant="Costco")
            categorise(db, u.id, t)
            rv = db.scalars(select(ReviewItem).where(ReviewItem.transaction_id == t.id)).first()
            with pytest.raises(ValueError, match="every cent"):
                resolve_review(db, u.id, rv, [(cats["Food"].id, 60_00)])
            db.rollback()

    def test_already_allocated_transactions_are_not_double_counted(self) -> None:
        with SessionLocal() as db:
            u, acct, cats, plan = _fixture(db)
            post(db, u.id, [Entry(cats["Food"].id, 300_00, "opening_balance")])
            db.add(CategoryRule(user_id=u.id, category_id=cats["Food"].id,
                                matcher={"merchant_contains": "kfc"}, priority=10))
            db.flush()
            t = _txn(db, u, acct, out=12_50, merchant="KFC")

            assert categorise(db, u.id, t) == "rule"
            assert categorise(db, u.id, t) == "already_allocated"
            assert cats["Food"].balance_cents == 300_00 - 12_50
            db.rollback()

    def test_a_refund_puts_money_back(self) -> None:
        """Inflow is negative signed amount, so the category goes UP."""
        with SessionLocal() as db:
            u, acct, cats, plan = _fixture(db)
            post(db, u.id, [Entry(cats["Food"].id, 100_00, "opening_balance")])
            db.add(CategoryRule(user_id=u.id, category_id=cats["Food"].id,
                                matcher={"merchant_contains": "smiths"}, priority=10))
            db.flush()
            refund = _txn(db, u, acct, inflow=15_00, merchant="Smiths Food")
            assert signed_amount(refund) == -15_00
            assert categorise(db, u.id, refund) == "rule"
            assert cats["Food"].balance_cents == 115_00
            db.rollback()


@requires_db
class TestPaydayPrediction:
    def test_needs_two_paychecks(self) -> None:
        with SessionLocal() as db:
            u, acct, cats, plan = _fixture(db)
            assert predict_next(db, u.id)["known"] is False
            db.rollback()

    def test_predicts_a_range_from_cadence(self) -> None:
        with SessionLocal() as db:
            u, acct, cats, plan = _fixture(db)
            for weeks in (0, 2, 4, 6):
                db.add(Paycheck(user_id=u.id, gross_cents=2000_00,
                                received_on=date.today() - timedelta(weeks=weeks)))
            db.flush()
            p = predict_next(db, u.id)
            assert p["known"] is True
            assert p["cadence_days"] == 14
            # A range, not false precision.
            assert p["earliest"] <= p["expected"] <= p["latest"]
            db.rollback()


@requires_db
class TestSyncCategorisationWiring:
    def test_new_transactions_have_ids_before_categorisation(self) -> None:
        """Transaction.id is a Python-side uuid4 default, populated only at
        flush. Reading it earlier yields None, which once made the sync path
        categorise an empty list while reporting transactions added.
        """
        with SessionLocal() as db:
            u, acct, cats, plan = _fixture(db)
            t = Transaction(
                user_id=u.id, account_id=acct.id,
                plaid_transaction_id=f"tx-{uuid.uuid4().hex[:14]}",
                outflow_cents=1000, inflow_cents=0, date=date.today(), name="X",
            )
            db.add(t)
            assert t.id is None, "precondition: id is unset before flush"
            db.flush()
            assert t.id is not None, "id must exist once flushed"
            db.rollback()

    def test_categorise_batch_handles_freshly_added_rows(self) -> None:
        with SessionLocal() as db:
            u, acct, cats, plan = _fixture(db)
            post(db, u.id, [Entry(cats["Food"].id, 300_00, "opening_balance")])
            db.add(CategoryRule(user_id=u.id, category_id=cats["Food"].id,
                                matcher={"merchant_contains": "kfc"}, priority=10))
            db.flush()

            from app.budget.categorize import categorise_batch

            rows = [_txn(db, u, acct, out=500, merchant="KFC") for _ in range(3)]
            counts = categorise_batch(db, u.id, rows)
            assert counts.get("rule") == 3, counts
            assert cats["Food"].balance_cents == 300_00 - 1500
            db.rollback()


@requires_db
class TestUserDeletionCascades:
    def test_deleting_a_user_removes_everything_they_own(self) -> None:
        """Migration 0002. Three foreign keys used NO ACTION, so a user
        delete cascaded into categories and then hit a FK violation,
        failing outright and leaving orphans behind.
        """
        from sqlalchemy import func

        from app.db.models import Allocation as A

        with SessionLocal() as db:
            u, acct, cats, plan = _fixture(db)
            pc = Paycheck(user_id=u.id, gross_cents=1000_00, received_on=date.today())
            db.add(pc); db.flush()
            apply_plan(db, u.id, pc, plan)
            t = _txn(db, u, acct, out=2500, merchant="Somewhere")
            categorise(db, u.id, t)
            db.commit()

            uid = u.id
            assert db.scalar(select(func.count()).select_from(A).where(A.user_id == uid)) > 0

            db.delete(db.get(User, uid))
            db.commit()      # would previously raise a ForeignKeyViolation

            for model in (Category, Paycheck, Transaction, ReviewItem, SplitPlan):
                left = db.scalar(
                    select(func.count()).select_from(model).where(model.user_id == uid)
                )
                assert left == 0, f"{model.__name__} rows survived the user delete"
            assert db.scalar(select(func.count()).select_from(A).where(A.user_id == uid)) == 0

    def test_split_rules_do_not_block_the_cascade(self) -> None:
        from sqlalchemy import func

        with SessionLocal() as db:
            u, acct, cats, plan = _fixture(db)
            db.commit()
            uid, plan_id = u.id, plan.id
            db.delete(db.get(User, uid))
            db.commit()
            assert db.scalar(
                select(func.count()).select_from(SplitRule).where(SplitRule.plan_id == plan_id)
            ) == 0


@requires_db
class TestCreditCardDoubleCounting:
    """The scenario that motivated treatment classification."""

    def test_card_purchase_then_payment_counts_once(self) -> None:
        from app.budget.treatment import Treatment

        with SessionLocal() as db:
            u, acct, cats, plan = _fixture(db)
            post(db, u.id, [Entry(cats["Food"].id, 300_00, "opening_balance")])
            db.add(CategoryRule(user_id=u.id, category_id=cats["Food"].id,
                                matcher={"merchant_contains": "smiths"}, priority=10))
            db.flush()

            # 1. Groceries on the credit card -> Food draws down.
            purchase = _txn(db, u, acct, out=42_10, merchant="Smiths Food")
            assert categorise(db, u.id, purchase) == "rule"
            assert cats["Food"].balance_cents == 300_00 - 42_10

            # 2. Paying the card settles debt already accounted for.
            payment = _txn(db, u, acct, out=500_00, name="AUTOMATIC PAYMENT - THANK YOU")
            payment.treatment = Treatment.CARD_PAYMENT.value
            db.flush()
            assert categorise(db, u.id, payment) == "not_budgeted"

            # Food is unchanged: the $42.10 was counted exactly once.
            assert cats["Food"].balance_cents == 300_00 - 42_10
            assert audit_balances(db, u.id) == []
            db.rollback()

    def test_payments_never_reach_the_review_queue(self) -> None:
        from sqlalchemy import func

        from app.budget.treatment import Treatment
        from app.db.models import ReviewItem as RI

        with SessionLocal() as db:
            u, acct, cats, plan = _fixture(db)
            p = _txn(db, u, acct, out=500_00, name="CREDIT CARD PAYMENT")
            p.treatment = Treatment.CARD_PAYMENT.value
            db.flush()
            categorise(db, u.id, p)
            assert db.scalar(
                select(func.count()).select_from(RI).where(RI.transaction_id == p.id)
            ) == 0, "a card payment must not ask the user to categorise it"
            db.rollback()

    def test_reclassifying_to_a_payment_reverses_its_allocation(self) -> None:
        """If something was wrongly counted, excluding it must correct the
        balance rather than leave the category wrong."""
        from app.budget.ledger import allocations_for_transaction, reverse

        with SessionLocal() as db:
            u, acct, cats, plan = _fixture(db)
            post(db, u.id, [Entry(cats["Food"].id, 300_00, "opening_balance")])
            db.add(CategoryRule(user_id=u.id, category_id=cats["Food"].id,
                                matcher={"merchant_contains": "acme"}, priority=10))
            db.flush()

            t = _txn(db, u, acct, out=120_00, merchant="ACME")
            categorise(db, u.id, t)
            assert cats["Food"].balance_cents == 180_00

            reverse(db, u.id, allocations_for_transaction(db, t), "reclassified")
            assert cats["Food"].balance_cents == 300_00
            assert audit_balances(db, u.id) == []
            db.rollback()
