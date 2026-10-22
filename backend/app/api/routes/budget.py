"""Budget endpoints: categories, rules, split plans, paychecks, review queue."""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.budget.categorize import resolve_review, signed_amount
from app.budget.ledger import LedgerError, audit_balances
from app.budget.treatment import Treatment, explain
from app.budget.paycheck import apply_plan, candidates, predict_next, unapply
from app.budget.split import RuleType, SplitPlanError, SplitRule as EngineRule, apply_split
from app.db.models import (
    Account,
    Allocation,
    Category,
    CategoryRule,
    Paycheck,
    ReviewItem,
    SplitPlan,
    SplitRule,
    Transaction,
    User,
)
from app.db.session import get_db

router = APIRouter(prefix="/budget", tags=["budget"])

KINDS = {"spending", "savings_bucket", "obligation"}


def _own(db: Session, model, obj_id: str, user: User):
    """Fetch a row belonging to this user, or 404.

    404 rather than 403 deliberately: a 403 would confirm the row exists.
    """
    try:
        pk = uuid.UUID(obj_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found") from None
    obj = db.scalars(select(model).where(model.id == pk, model.user_id == user.id)).first()
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return obj


# --------------------------------------------------------------------------- #
# Categories
# --------------------------------------------------------------------------- #
class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: str
    description: str | None = None
    target_cents: int | None = Field(default=None, ge=0)
    alert_at_cents: int | None = Field(default=None, ge=0)
    sort_order: int = 0

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if v not in KINDS:
            raise ValueError(f"kind must be one of {sorted(KINDS)}")
        return v


class CategoryPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    target_cents: int | None = Field(default=None, ge=0)
    alert_at_cents: int | None = Field(default=None, ge=0)
    sort_order: int | None = None


class CategoryOut(BaseModel):
    id: str
    name: str
    kind: str
    description: str | None
    balance_cents: int
    target_cents: int | None
    alert_at_cents: int | None
    sort_order: int
    archived: bool
    below_alert: bool
    overspent: bool


def _cat_out(c: Category) -> CategoryOut:
    return CategoryOut(
        id=str(c.id), name=c.name, kind=c.kind, description=c.description,
        balance_cents=c.balance_cents, target_cents=c.target_cents,
        alert_at_cents=c.alert_at_cents, sort_order=c.sort_order,
        archived=c.archived_at is not None,
        below_alert=c.alert_at_cents is not None and c.balance_cents < c.alert_at_cents,
        # A negative balance means more was spent than the category held. It
        # is a real state, not an error, and the user needs to see it.
        overspent=c.balance_cents < 0,
    )


@router.get("/categories", response_model=list[CategoryOut])
def list_categories(
    include_archived: bool = False,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[CategoryOut]:
    q = select(Category).where(Category.user_id == user.id)
    if not include_archived:
        q = q.where(Category.archived_at.is_(None))
    rows = db.scalars(q.order_by(Category.sort_order, Category.name)).all()
    return [_cat_out(c) for c in rows]


@router.post("/categories", response_model=CategoryOut, status_code=status.HTTP_201_CREATED)
def create_category(
    body: CategoryIn, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> CategoryOut:
    clash = db.scalars(
        select(Category).where(
            Category.user_id == user.id,
            Category.name == body.name,
            Category.archived_at.is_(None),
        )
    ).first()
    if clash is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "A category with that name already exists")

    c = Category(user_id=user.id, **body.model_dump())
    db.add(c)
    db.commit()
    return _cat_out(c)


@router.patch("/categories/{category_id}", response_model=CategoryOut)
def update_category(
    category_id: str, body: CategoryPatch,
    user: User = Depends(current_user), db: Session = Depends(get_db),
) -> CategoryOut:
    c = _own(db, Category, category_id, user)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(c, k, v)
    db.commit()
    return _cat_out(c)


@router.delete("/categories/{category_id}", response_model=CategoryOut)
def archive_category(
    category_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> CategoryOut:
    """Archive, never delete.

    Deleting would orphan the allocation history that explains past
    balances. An archived category keeps its ledger and stops accepting
    new allocations.
    """
    c = _own(db, Category, category_id, user)
    if c.archived_at is None:
        c.archived_at = datetime.now(UTC)
        db.commit()
    return _cat_out(c)


@router.post("/categories/{category_id}/restore", response_model=CategoryOut)
def restore_category(
    category_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> CategoryOut:
    c = _own(db, Category, category_id, user)
    clash = db.scalars(
        select(Category).where(
            Category.user_id == user.id, Category.name == c.name,
            Category.archived_at.is_(None), Category.id != c.id,
        )
    ).first()
    if clash is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "An active category already uses that name"
        )
    c.archived_at = None
    db.commit()
    return _cat_out(c)


class AllocationOut(BaseModel):
    id: str
    amount_cents: int
    source: str
    note: str | None
    created_at: str
    transaction_id: str | None


@router.get("/categories/{category_id}/allocations", response_model=list[AllocationOut])
def category_ledger(
    category_id: str, limit: int = 100,
    user: User = Depends(current_user), db: Session = Depends(get_db),
) -> list[AllocationOut]:
    c = _own(db, Category, category_id, user)
    rows = db.scalars(
        select(Allocation)
        .where(Allocation.category_id == c.id)
        .order_by(Allocation.created_at.desc())
        .limit(min(limit, 500))
    ).all()
    return [
        AllocationOut(
            id=str(a.id), amount_cents=a.amount_cents, source=a.source, note=a.note,
            created_at=a.created_at.isoformat(),
            transaction_id=str(a.transaction_id) if a.transaction_id else None,
        )
        for a in rows
    ]


# --------------------------------------------------------------------------- #
# Category rules
# --------------------------------------------------------------------------- #
class RuleIn(BaseModel):
    category_id: str
    matcher: dict
    priority: int = 100
    enabled: bool = True

    @field_validator("matcher")
    @classmethod
    def _non_empty(cls, v: dict) -> dict:
        if not v:
            # A rule with no conditions would claim every transaction.
            raise ValueError("matcher must contain at least one condition")
        return v


class RuleOut(BaseModel):
    id: str
    category_id: str
    matcher: dict
    priority: int
    enabled: bool


@router.get("/rules", response_model=list[RuleOut])
def list_rules(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[RuleOut]:
    rows = db.scalars(
        select(CategoryRule).where(CategoryRule.user_id == user.id).order_by(CategoryRule.priority)
    ).all()
    return [
        RuleOut(id=str(r.id), category_id=str(r.category_id), matcher=r.matcher,
                priority=r.priority, enabled=r.enabled)
        for r in rows
    ]


@router.post("/rules", response_model=RuleOut, status_code=status.HTTP_201_CREATED)
def create_rule(
    body: RuleIn, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> RuleOut:
    cat = _own(db, Category, body.category_id, user)
    r = CategoryRule(user_id=user.id, category_id=cat.id, matcher=body.matcher,
                     priority=body.priority, enabled=body.enabled)
    db.add(r)
    db.commit()
    return RuleOut(id=str(r.id), category_id=str(r.category_id), matcher=r.matcher,
                   priority=r.priority, enabled=r.enabled)


@router.delete("/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_rule(
    rule_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> None:
    r = _own(db, CategoryRule, rule_id, user)
    db.delete(r)
    db.commit()


# --------------------------------------------------------------------------- #
# Split plans
# --------------------------------------------------------------------------- #
class SplitRuleIn(BaseModel):
    category_id: str
    order_index: int
    rule_type: str
    params: dict = Field(default_factory=dict)
    enabled: bool = True

    @field_validator("rule_type")
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in {t.value for t in RuleType}:
            raise ValueError(f"rule_type must be one of {[t.value for t in RuleType]}")
        return v


class PlanIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    rules: list[SplitRuleIn] = Field(min_length=1)


class PlanOut(BaseModel):
    id: str
    name: str
    version: int
    is_active: bool
    rules: list[SplitRuleIn]


def _plan_out(db: Session, p: SplitPlan) -> PlanOut:
    rows = db.scalars(
        select(SplitRule).where(SplitRule.plan_id == p.id).order_by(SplitRule.order_index)
    ).all()
    return PlanOut(
        id=str(p.id), name=p.name, version=p.version, is_active=p.is_active,
        rules=[
            SplitRuleIn(category_id=str(r.category_id), order_index=r.order_index,
                        rule_type=r.rule_type, params=r.params or {}, enabled=r.enabled)
            for r in rows
        ],
    )


def _validate_plan(rules: list[SplitRuleIn]) -> None:
    """Reject a plan the engine could not apply.

    Catching this at save time means the user finds out while editing, not
    when a paycheck arrives.
    """
    engine = [
        EngineRule(category_id=r.category_id, order_index=r.order_index,
                   rule_type=RuleType(r.rule_type), params=r.params, enabled=r.enabled)
        for r in rules
    ]
    try:
        # A dry run with a nominal amount surfaces structural problems:
        # no remainder rule, remainder not last, bad percentages.
        apply_split(1000_00, engine, {r.category_id: 0 for r in rules})
    except SplitPlanError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.get("/plans", response_model=list[PlanOut])
def list_plans(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[PlanOut]:
    rows = db.scalars(
        select(SplitPlan).where(SplitPlan.user_id == user.id).order_by(SplitPlan.created_at)
    ).all()
    return [_plan_out(db, p) for p in rows]


@router.post("/plans", response_model=PlanOut, status_code=status.HTTP_201_CREATED)
def create_plan(
    body: PlanIn, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> PlanOut:
    for r in body.rules:
        _own(db, Category, r.category_id, user)
    _validate_plan(body.rules)

    # Only one plan is active at a time.
    for p in db.scalars(
        select(SplitPlan).where(SplitPlan.user_id == user.id, SplitPlan.is_active.is_(True))
    ).all():
        p.is_active = False

    plan = SplitPlan(user_id=user.id, name=body.name, version=1, is_active=True)
    db.add(plan)
    db.flush()
    for r in body.rules:
        db.add(SplitRule(plan_id=plan.id, category_id=uuid.UUID(r.category_id),
                         order_index=r.order_index, rule_type=r.rule_type,
                         params=r.params, enabled=r.enabled))
    db.commit()
    return _plan_out(db, plan)


@router.put("/plans/{plan_id}", response_model=PlanOut)
def revise_plan(
    plan_id: str, body: PlanIn,
    user: User = Depends(current_user), db: Session = Depends(get_db),
) -> PlanOut:
    """Save edits as a NEW VERSION rather than mutating the old one.

    Paychecks reference the plan version that produced them, so history
    stays explainable after the rules change (docs/06).
    """
    old = _own(db, SplitPlan, plan_id, user)
    for r in body.rules:
        _own(db, Category, r.category_id, user)
    _validate_plan(body.rules)

    old.is_active = False
    new = SplitPlan(user_id=user.id, name=body.name, version=old.version + 1, is_active=True)
    db.add(new)
    db.flush()
    for r in body.rules:
        db.add(SplitRule(plan_id=new.id, category_id=uuid.UUID(r.category_id),
                         order_index=r.order_index, rule_type=r.rule_type,
                         params=r.params, enabled=r.enabled))
    db.commit()
    return _plan_out(db, new)


# --------------------------------------------------------------------------- #
# Paychecks
# --------------------------------------------------------------------------- #
class CandidateOut(BaseModel):
    transaction_id: str
    date: str
    name: str
    merchant_name: str | None
    inflow_cents: int
    already_recorded: bool


@router.get("/paychecks/candidates", response_model=list[CandidateOut])
def paycheck_candidates(
    user: User = Depends(current_user), db: Session = Depends(get_db)
) -> list[CandidateOut]:
    """Deposits that look like income.

    Proposals only: an unrecognised large deposit (a tax refund, say) must
    never be split automatically as though it were a paycheck.
    """
    recorded = {
        p.transaction_id
        for p in db.scalars(select(Paycheck).where(Paycheck.user_id == user.id)).all()
        if p.transaction_id
    }
    return [
        CandidateOut(
            transaction_id=str(t.id), date=t.date.isoformat(), name=t.name,
            merchant_name=t.merchant_name, inflow_cents=t.inflow_cents,
            already_recorded=t.id in recorded,
        )
        for t in candidates(db, user.id)
    ]


class PaycheckIn(BaseModel):
    transaction_id: str | None = None
    gross_cents: int | None = Field(default=None, gt=0)
    received_on: date | None = None
    plan_id: str | None = None
    apply: bool = True


class PaycheckOut(BaseModel):
    id: str
    gross_cents: int
    received_on: str
    applied: bool
    allocated_cents: int = 0
    unfunded: list[dict] = Field(default_factory=list)


@router.post("/paychecks", response_model=PaycheckOut, status_code=status.HTTP_201_CREATED)
def record_paycheck(
    body: PaycheckIn, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> PaycheckOut:
    txn = None
    if body.transaction_id:
        txn = _own(db, Transaction, body.transaction_id, user)
        dup = db.scalars(
            select(Paycheck).where(
                Paycheck.user_id == user.id, Paycheck.transaction_id == txn.id
            )
        ).first()
        if dup is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "That deposit is already recorded as a paycheck"
            )

    gross = body.gross_cents or (txn.inflow_cents if txn else None)
    if not gross:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Provide gross_cents, or a transaction_id with an inflow",
        )

    pc = Paycheck(
        user_id=user.id,
        transaction_id=txn.id if txn else None,
        gross_cents=gross,
        received_on=body.received_on or (txn.date if txn else date.today()),
    )
    db.add(pc)
    db.flush()

    out = PaycheckOut(id=str(pc.id), gross_cents=gross,
                      received_on=pc.received_on.isoformat(), applied=False)

    if body.apply:
        if body.plan_id:
            plan = _own(db, SplitPlan, body.plan_id, user)
        else:
            plan = db.scalars(
                select(SplitPlan).where(
                    SplitPlan.user_id == user.id, SplitPlan.is_active.is_(True)
                )
            ).first()
            if plan is None:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    "No active split plan. Create one, or pass apply=false.",
                )
        try:
            res = apply_plan(db, user.id, pc, plan)
        except (SplitPlanError, LedgerError) as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        out.applied = True
        out.allocated_cents = res.allocated_cents
        out.unfunded = [{"category_id": c, "shortfall_cents": s} for c, s in res.unfunded]

    db.commit()
    return out


@router.post("/paychecks/{paycheck_id}/unapply", response_model=PaycheckOut)
def undo_paycheck(
    paycheck_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> PaycheckOut:
    pc = _own(db, Paycheck, paycheck_id, user)
    unapply(db, user.id, pc)
    db.commit()
    return PaycheckOut(id=str(pc.id), gross_cents=pc.gross_cents,
                       received_on=pc.received_on.isoformat(), applied=False)


@router.get("/next-payday")
def next_payday(
    user: User = Depends(current_user), db: Session = Depends(get_db)
) -> dict[str, object]:
    return predict_next(db, user.id)


# --------------------------------------------------------------------------- #
# Review queue
# --------------------------------------------------------------------------- #
class ReviewOut(BaseModel):
    id: str
    transaction_id: str
    reason: str
    date: str
    name: str
    merchant_name: str | None
    amount_cents: int
    suggestions: dict | None


@router.get("/review", response_model=list[ReviewOut])
def list_review(
    user: User = Depends(current_user), db: Session = Depends(get_db)
) -> list[ReviewOut]:
    rows = db.execute(
        select(ReviewItem, Transaction)
        .join(Transaction, Transaction.id == ReviewItem.transaction_id)
        .where(ReviewItem.user_id == user.id, ReviewItem.resolved_at.is_(None))
        .order_by(Transaction.date.desc())
    ).all()
    return [
        ReviewOut(
            id=str(r.id), transaction_id=str(t.id), reason=r.reason,
            date=t.date.isoformat(), name=t.name, merchant_name=t.merchant_name,
            amount_cents=signed_amount(t), suggestions=r.suggestions,
        )
        for r, t in rows
    ]


class SplitIn(BaseModel):
    category_id: str
    amount_cents: int


class NewCategoryIn(BaseModel):
    """Create a category as part of resolving a review.

    Finding a charge that fits nowhere is the normal moment to realise a
    category is missing, so the flow allows it without leaving the queue.
    """

    name: str = Field(min_length=1, max_length=120)
    kind: str = "spending"
    target_cents: int | None = Field(default=None, ge=0)
    alert_at_cents: int | None = Field(default=None, ge=0)

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if v not in KINDS:
            raise ValueError(f"kind must be one of {sorted(KINDS)}")
        return v


class ResolveIn(BaseModel):
    splits: list[SplitIn] = Field(default_factory=list)
    remember: bool = False
    # Create this category and allocate the whole transaction to it.
    new_category: NewCategoryIn | None = None


@router.post("/review/{review_id}/resolve", status_code=status.HTTP_204_NO_CONTENT)
def resolve(
    review_id: str, body: ResolveIn,
    user: User = Depends(current_user), db: Session = Depends(get_db),
) -> None:
    rv = _own(db, ReviewItem, review_id, user)
    if rv.resolved_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Already resolved")

    pairs: list[tuple[uuid.UUID, int]] = []

    if body.new_category is not None:
        if body.splits:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "Provide either splits or new_category, not both",
            )
        clash = db.scalars(
            select(Category).where(
                Category.user_id == user.id,
                Category.name == body.new_category.name,
                Category.archived_at.is_(None),
            )
        ).first()
        if clash is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "A category with that name already exists"
            )
        cat = Category(user_id=user.id, **body.new_category.model_dump())
        db.add(cat)
        db.flush()

        txn = db.get(Transaction, rv.transaction_id)
        if txn is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Transaction missing")
        pairs = [(cat.id, signed_amount(txn))]
    else:
        if not body.splits:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "Provide splits or new_category",
            )
        for s in body.splits:
            cat = _own(db, Category, s.category_id, user)
            pairs.append((cat.id, s.amount_cents))

    try:
        resolve_review(db, user.id, rv, pairs)
    except (ValueError, LedgerError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    if body.remember and len(pairs) == 1:
        # Only single-category decisions become rules: a deliberate split
        # says nothing reliable about the next transaction.
        txn = db.get(Transaction, rv.transaction_id)
        merchant = (txn.merchant_name or txn.name or "").strip() if txn else ""
        if merchant:
            db.add(CategoryRule(user_id=user.id, category_id=pairs[0][0],
                                matcher={"merchant_contains": merchant.lower()}, priority=50))
    db.commit()


# --------------------------------------------------------------------------- #
# Transactions excluded from the budget
# --------------------------------------------------------------------------- #
class ExcludedOut(BaseModel):
    transaction_id: str
    date: str
    name: str
    merchant_name: str | None
    amount_cents: int
    treatment: str
    reason: str


@router.get("/excluded", response_model=list[ExcludedOut])
def list_excluded(
    limit: int = 100, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> list[ExcludedOut]:
    """Transactions deliberately not counted against any category.

    Exposed so an exclusion is never silent. If something was misclassified
    the user can see it and change it.
    """
    rows = db.scalars(
        select(Transaction)
        .where(
            Transaction.user_id == user.id,
            Transaction.treatment != Treatment.SPEND.value,
            Transaction.removed_at.is_(None),
        )
        .order_by(Transaction.date.desc())
        .limit(min(limit, 500))
    ).all()
    return [
        ExcludedOut(
            transaction_id=str(t.id), date=t.date.isoformat(), name=t.name,
            merchant_name=t.merchant_name, amount_cents=signed_amount(t),
            treatment=t.treatment, reason=explain(Treatment(t.treatment)),
        )
        for t in rows
    ]


class ReclassifyIn(BaseModel):
    treatment: str

    @field_validator("treatment")
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in {t.value for t in Treatment}:
            raise ValueError(f"treatment must be one of {[t.value for t in Treatment]}")
        return v


@router.post("/transactions/{transaction_id}/treatment")
def reclassify(
    transaction_id: str, body: ReclassifyIn,
    user: User = Depends(current_user), db: Session = Depends(get_db),
) -> dict[str, object]:
    """Change how a transaction is treated.

    Moving something OUT of `spend` reverses any allocations it made, so the
    category balance is corrected rather than left reflecting a transaction
    that no longer counts.
    """
    from app.budget.categorize import categorise
    from app.budget.ledger import allocations_for_transaction, reverse

    txn = _own(db, Transaction, transaction_id, user)
    old = Treatment(txn.treatment)
    new = Treatment(body.treatment)
    if old is new:
        return {"treatment": new.value, "changed": False}

    txn.treatment = new.value

    if old.affects_budget and not new.affects_budget:
        existing = allocations_for_transaction(db, txn)
        if existing:
            reverse(db, user.id, existing, f"reclassified as {new.value}")
        open_review = db.scalars(
            select(ReviewItem).where(
                ReviewItem.transaction_id == txn.id, ReviewItem.resolved_at.is_(None)
            )
        ).first()
        if open_review is not None:
            open_review.resolved_at = datetime.now(UTC)
            open_review.resolution = {"excluded_as": new.value}

    outcome = None
    if not old.affects_budget and new.affects_budget:
        outcome = categorise(db, user.id, txn)

    db.commit()
    return {"treatment": new.value, "changed": True, "categorised": outcome,
            "reason": explain(new)}


# --------------------------------------------------------------------------- #
# Credit cards
# --------------------------------------------------------------------------- #
class CardOut(BaseModel):
    account_id: str
    name: str
    mask: str | None
    balance_cents: int
    as_of: str | None


@router.get("/cards", response_model=list[CardOut])
def credit_cards(
    user: User = Depends(current_user), db: Session = Depends(get_db)
) -> list[CardOut]:
    """Outstanding credit card balances.

    Not part of the budget -- card spending already drew down categories at
    purchase time. This answers a different question: how much needs paying,
    and it updates on its own once the payment posts.
    """
    rows = db.scalars(
        select(Account).where(
            Account.user_id == user.id,
            Account.type == "credit",
            Account.is_tracked.is_(True),
        ).order_by(Account.name)
    ).all()
    return [
        CardOut(
            account_id=str(a.id), name=a.name or "Credit card", mask=a.mask,
            balance_cents=a.current_cents or 0,
            as_of=a.balance_as_of.isoformat() if a.balance_as_of else None,
        )
        for a in rows
    ]


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #
@router.get("/summary")
def summary(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, object]:
    cats = db.scalars(
        select(Category).where(Category.user_id == user.id, Category.archived_at.is_(None))
        .order_by(Category.sort_order, Category.name)
    ).all()
    pending = len(
        db.scalars(
            select(ReviewItem).where(
                ReviewItem.user_id == user.id, ReviewItem.resolved_at.is_(None)
            )
        ).all()
    )
    drift = audit_balances(db, user.id)
    cards = credit_cards(user=user, db=db)
    excluded_count = len(
        db.scalars(
            select(Transaction).where(
                Transaction.user_id == user.id,
                Transaction.treatment != Treatment.SPEND.value,
                Transaction.removed_at.is_(None),
            )
        ).all()
    )
    return {
        "cards": [c.model_dump() for c in cards],
        "card_debt_cents": sum(c.balance_cents for c in cards),
        "excluded_count": excluded_count,
        "categories": [_cat_out(c).model_dump() for c in cats],
        "total_allocated_cents": sum(c.balance_cents for c in cats),
        "alerts": [_cat_out(c).model_dump() for c in cats if _cat_out(c).below_alert],
        "overspent": [_cat_out(c).model_dump() for c in cats if c.balance_cents < 0],
        "pending_review": pending,
        "next_payday": predict_next(db, user.id),
        # Should always be empty. Surfaced rather than hidden so a ledger
        # inconsistency is visible instead of quietly wrong.
        "ledger_drift": [{"category_id": str(c), "cached": a, "actual": b} for c, a, b in drift],
    }
