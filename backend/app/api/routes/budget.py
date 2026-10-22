"""Budget endpoints: categories, rules, split plans, paychecks, review queue."""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.budget.categorize import resolve_review, signed_amount
from app.budget.ledger import Entry, LedgerError, audit_balances, post
from app.budget.treatment import Treatment, explain
from app.budget.paycheck import apply_plan, candidates, predict_next, unapply
from app.budget.reconcile import (
    compute as compute_reconciliation,
)
from app.budget.reconcile import (
    ensure_unallocated,
    sweep_to_unallocated,
    unallocated_category,
)
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
    is_unallocated: bool


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
        is_unallocated=c.is_unallocated,
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
# Accounts and balances
# --------------------------------------------------------------------------- #
class AccountOut(BaseModel):
    id: str
    name: str
    official_name: str | None
    mask: str | None
    type: str | None
    subtype: str | None
    balance_cents: int
    available_cents: int | None
    is_tracked: bool
    as_of: str | None

    @property
    def is_cash(self) -> bool:
        return (self.type or "") == "depository"


def _account_out(a: Account) -> AccountOut:
    return AccountOut(
        id=str(a.id), name=a.name or "Account", official_name=a.official_name,
        mask=a.mask, type=a.type, subtype=a.subtype,
        balance_cents=a.current_cents or 0, available_cents=a.available_cents,
        is_tracked=a.is_tracked,
        as_of=a.balance_as_of.isoformat() if a.balance_as_of else None,
    )


@router.get("/accounts", response_model=list[AccountOut])
def list_accounts(
    tracked_only: bool = False,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[AccountOut]:
    """Every account from every linked institution.

    Linking a bank brings in everything it exposes -- mortgages, 401ks, auto
    loans -- most of which is noise for day-to-day budgeting. `is_tracked`
    decides what appears on the home screen.
    """
    q = select(Account).where(Account.user_id == user.id)
    if tracked_only:
        q = q.where(Account.is_tracked.is_(True))
    rows = db.scalars(q.order_by(Account.type, Account.name)).all()
    return [_account_out(a) for a in rows]


class TrackedIn(BaseModel):
    is_tracked: bool


@router.post("/accounts/{account_id}/tracked", response_model=AccountOut)
def set_tracked(
    account_id: str, body: TrackedIn,
    user: User = Depends(current_user), db: Session = Depends(get_db),
) -> AccountOut:
    """Show or hide an account on the home screen.

    Purely a display choice: transactions keep syncing either way, so hiding
    an account never silently stops tracking its spending.
    """
    acct = _own(db, Account, account_id, user)
    acct.is_tracked = body.is_tracked
    db.commit()
    return _account_out(acct)


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
# Applying an AI proposal
# --------------------------------------------------------------------------- #
class ProposedCategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: str
    description: str | None = None
    target_cents: int | None = Field(default=None, ge=0)
    alert_at_cents: int | None = Field(default=None, ge=0)

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if v not in KINDS:
            raise ValueError(f"kind must be one of {sorted(KINDS)}")
        return v


class ProposedRuleIn(BaseModel):
    category_name: str
    order_index: int
    rule_type: str
    params: dict = Field(default_factory=dict)

    @field_validator("rule_type")
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in {t.value for t in RuleType}:
            raise ValueError(f"rule_type must be one of {[t.value for t in RuleType]}")
        return v


class ApplyProposalIn(BaseModel):
    """Apply a proposal the user has reviewed and possibly edited.

    Deliberately takes the full edited payload rather than a job id: what
    gets applied is whatever the user approved on screen, not whatever the
    model originally said.
    """

    categories: list[ProposedCategoryIn] = Field(default_factory=list)
    split_rules: list[ProposedRuleIn] = Field(default_factory=list)
    plan_name: str = Field(default="Standard", min_length=1, max_length=120)
    # Create categories without touching the split plan.
    categories_only: bool = False


class ApplyProposalOut(BaseModel):
    created_categories: list[str]
    reused_categories: list[str]
    plan_id: str | None
    plan_version: int | None


@router.post("/apply-proposal", response_model=ApplyProposalOut)
def apply_proposal(
    body: ApplyProposalIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> ApplyProposalOut:
    """Turn a reviewed proposal into real categories and a split plan.

    Existing categories are REUSED by name, never duplicated, so applying a
    proposal twice does not leave the user with "Food" and "Food (2)".
    """
    existing = {
        c.name: c
        for c in db.scalars(
            select(Category).where(
                Category.user_id == user.id, Category.archived_at.is_(None)
            )
        ).all()
    }

    created: list[str] = []
    reused: list[str] = []
    by_name: dict[str, Category] = dict(existing)

    for spec in body.categories:
        found = existing.get(spec.name)
        if found is not None:
            reused.append(spec.name)
            # Only fill gaps. Overwriting a target the user already set
            # would quietly undo their own decision.
            if found.target_cents is None and spec.target_cents is not None:
                found.target_cents = spec.target_cents
            if found.alert_at_cents is None and spec.alert_at_cents is not None:
                found.alert_at_cents = spec.alert_at_cents
            continue

        cat = Category(
            user_id=user.id, name=spec.name, kind=spec.kind,
            description=spec.description, target_cents=spec.target_cents,
            alert_at_cents=spec.alert_at_cents, sort_order=len(by_name),
        )
        db.add(cat)
        db.flush()
        by_name[spec.name] = cat
        created.append(spec.name)

    if body.categories_only or not body.split_rules:
        db.commit()
        return ApplyProposalOut(created_categories=created, reused_categories=reused,
                                plan_id=None, plan_version=None)

    rules_in: list[SplitRuleIn] = []
    for r in sorted(body.split_rules, key=lambda x: x.order_index):
        cat = by_name.get(r.category_name)
        if cat is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"Rule references '{r.category_name}', which is not among the "
                f"categories being created or already present",
            )
        rules_in.append(SplitRuleIn(
            category_id=str(cat.id), order_index=len(rules_in) + 1,
            rule_type=r.rule_type, params=r.params,
        ))

    # Same structural validation as a hand-made plan: a proposal gets no
    # special trust just because a model produced it.
    _validate_plan(rules_in)

    for p in db.scalars(
        select(SplitPlan).where(SplitPlan.user_id == user.id, SplitPlan.is_active.is_(True))
    ).all():
        p.is_active = False

    prior = db.scalars(
        select(SplitPlan).where(SplitPlan.user_id == user.id).order_by(SplitPlan.version.desc())
    ).first()
    plan = SplitPlan(user_id=user.id, name=body.plan_name,
                     version=(prior.version + 1) if prior else 1, is_active=True)
    db.add(plan)
    db.flush()
    for r in rules_in:
        db.add(SplitRule(plan_id=plan.id, category_id=uuid.UUID(r.category_id),
                         order_index=r.order_index, rule_type=r.rule_type,
                         params=r.params, enabled=True))
    db.commit()

    return ApplyProposalOut(
        created_categories=created, reused_categories=reused,
        plan_id=str(plan.id), plan_version=plan.version,
    )


# --------------------------------------------------------------------------- #
# Opening balances and reconciliation
# --------------------------------------------------------------------------- #
@router.get("/reconciliation")
def reconciliation(
    user: User = Depends(current_user), db: Session = Depends(get_db)
) -> dict[str, object]:
    """Does the budget agree with the bank?

    Allocated should equal cash minus card debt. Anything else is money
    without a purpose, and is surfaced rather than quietly absorbed.
    """
    rec = compute_reconciliation(db, user.id)
    sink = unallocated_category(db, user.id)
    return {
        **rec.as_dict(),
        "unallocated_category_id": str(sink.id) if sink else None,
    }


class OpeningAllocationIn(BaseModel):
    category_id: str
    amount_cents: int = Field(gt=0)


class OpeningBalanceIn(BaseModel):
    """Start budgeting from the money you already have.

    Every category gets its opening amount, and whatever is left lands in
    the unallocated sink so the total still matches the bank.
    """

    allocations: list[OpeningAllocationIn] = Field(default_factory=list)
    # Put the leftover somewhere other than the default sink, if wanted.
    remainder_category_id: str | None = None
    # Refuse rather than over-allocate past what actually exists.
    allow_over_allocation: bool = False


@router.post("/opening-balance")
def set_opening_balance(
    body: OpeningBalanceIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    rec = compute_reconciliation(db, user.id)

    if rec.allocated_cents != 0:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Categories already hold money. Opening balances are for starting "
            "from scratch; adjust categories individually instead.",
        )

    requested = sum(a.amount_cents for a in body.allocations)
    if requested > rec.available_cents and not body.allow_over_allocation:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Tried to allocate {requested} cents but only "
            f"{rec.available_cents} is available "
            f"(cash {rec.cash_cents} minus card debt {rec.card_debt_cents}).",
        )

    entries: list[Entry] = []
    for a in body.allocations:
        cat = _own(db, Category, a.category_id, user)
        if cat.archived_at is not None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"'{cat.name}' is archived",
            )
        entries.append(
            Entry(cat.id, a.amount_cents, "opening_balance", None, "opening balance")
        )

    if entries:
        post(db, user.id, entries)

    # Whatever is left has no purpose yet, and must still be accounted for
    # or the total stops matching the bank.
    remainder = rec.available_cents - requested
    sink_id = None
    if remainder != 0:
        if body.remainder_category_id:
            sink = _own(db, Category, body.remainder_category_id, user)
        else:
            sink = ensure_unallocated(db, user.id)
        post(db, user.id,
             [Entry(sink.id, remainder, "opening_balance", None, "unallocated remainder")])
        sink_id = str(sink.id)

    db.commit()
    after = compute_reconciliation(db, user.id)
    return {
        "allocated_cents": after.allocated_cents,
        "remainder_cents": remainder,
        "remainder_category_id": sink_id,
        **after.as_dict(),
    }


@router.post("/reconcile")
def reconcile_now(
    user: User = Depends(current_user), db: Session = Depends(get_db)
) -> dict[str, object]:
    """Sweep any difference into the unallocated sink.

    Works both ways: if more is allocated than exists -- spending outran
    income -- the sink goes negative rather than the shortfall being hidden.
    """
    moved = sweep_to_unallocated(db, user.id)
    db.commit()
    rec = compute_reconciliation(db, user.id)
    return {"moved_cents": moved, **rec.as_dict()}


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
    cash = [
        a for a in list_accounts(tracked_only=True, user=user, db=db)
        if (a.type or "") == "depository"
    ]
    excluded_count = len(
        db.scalars(
            select(Transaction).where(
                Transaction.user_id == user.id,
                Transaction.treatment != Treatment.SPEND.value,
                Transaction.removed_at.is_(None),
            )
        ).all()
    )
    rec = compute_reconciliation(db, user.id)
    return {
        "reconciliation": rec.as_dict(),
        "cards": [c.model_dump() for c in cards],
        "card_debt_cents": sum(c.balance_cents for c in cards),
        "cash_accounts": [a.model_dump() for a in cash],
        # Actual money in the bank. Deliberately separate from the category
        # total, which is COMMITTED money and will not match: card purchases
        # draw categories down before the cash leaves the account.
        "cash_cents": sum(a.balance_cents for a in cash),
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


# --------------------------------------------------------------------------- #
# Ordering
#
# Two different orders exist and users conflate them, so they are two
# separate endpoints with two separate words:
#
#   DISPLAY order  Category.sort_order -- what order the budget list shows.
#                  Cosmetic. Moves nothing.
#   FUNDING order  SplitRule.order_index -- what order a paycheck fills
#                  categories in. This one moves money, and reordering it
#                  is a plan revision like any other edit.
# --------------------------------------------------------------------------- #
class ReorderIn(BaseModel):
    """Category ids in the order the user dragged them into."""

    category_ids: list[str] = Field(min_length=1)


@router.post("/categories/reorder", response_model=list[CategoryOut])
def reorder_categories(
    body: ReorderIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[CategoryOut]:
    """Set display order from a dragged list.

    Does NOT change how paychecks are split -- see /plans/{id}/reorder.
    """
    owned = {
        str(c.id): c
        for c in db.scalars(
            select(Category).where(
                Category.user_id == user.id, Category.archived_at.is_(None)
            )
        ).all()
    }

    unknown = [i for i in body.category_ids if i not in owned]
    if unknown:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"Unknown category: {unknown[0]}"
        )
    if len(set(body.category_ids)) != len(body.category_ids):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Duplicate category in order"
        )

    for position, cid in enumerate(body.category_ids):
        owned[cid].sort_order = position

    # Anything the client did not send keeps a stable place after the
    # dragged ones rather than silently jumping to the top.
    for offset, c in enumerate(
        sorted(
            (c for cid, c in owned.items() if cid not in set(body.category_ids)),
            key=lambda c: (c.sort_order, c.name),
        )
    ):
        c.sort_order = len(body.category_ids) + offset

    db.commit()
    rows = db.scalars(
        select(Category)
        .where(Category.user_id == user.id, Category.archived_at.is_(None))
        .order_by(Category.sort_order, Category.name)
    ).all()
    return [_cat_out(c) for c in rows]


@router.post("/plans/{plan_id}/reorder", response_model=PlanOut)
def reorder_plan(
    plan_id: str,
    body: ReorderIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> PlanOut:
    """Reorder FUNDING priority by dragging categories.

    This changes who gets paid first out of a paycheck, so it saves a new
    plan version exactly like any other edit, and it is validated -- a drag
    that puts the remainder rule somewhere other than last is rejected
    rather than silently repaired.
    """
    plan = _own(db, SplitPlan, plan_id, user)
    rows = db.scalars(
        select(SplitRule).where(SplitRule.plan_id == plan.id)
    ).all()
    by_cat = {str(r.category_id): r for r in rows}

    if set(body.category_ids) != set(by_cat):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Reorder must list every category in the plan exactly once",
        )

    rules = [
        SplitRuleIn(
            category_id=cid, order_index=position + 1,
            rule_type=by_cat[cid].rule_type, params=by_cat[cid].params or {},
            enabled=by_cat[cid].enabled,
        )
        for position, cid in enumerate(body.category_ids)
    ]
    _validate_plan(rules)

    plan.is_active = False
    new = SplitPlan(user_id=user.id, name=plan.name,
                    version=plan.version + 1, is_active=True)
    db.add(new)
    db.flush()
    for r in rules:
        db.add(SplitRule(plan_id=new.id, category_id=uuid.UUID(r.category_id),
                         order_index=r.order_index, rule_type=r.rule_type,
                         params=r.params, enabled=r.enabled))
    db.commit()
    return _plan_out(db, new)


# --------------------------------------------------------------------------- #
# Applying a single AI-designed category
# --------------------------------------------------------------------------- #
class ApplyCategoryIn(BaseModel):
    """One reviewed category, optionally funded by one new rule.

    Separate from /apply-proposal because adding a category should not
    rewrite a split plan the user has already tuned. The rule, if given,
    is INSERTED into the active plan -- everything else keeps its relative
    funding priority.
    """

    category: ProposedCategoryIn
    rule_type: str | None = None
    params: dict = Field(default_factory=dict)
    # Where in the funding order it lands. None means "last, but still
    # ahead of the remainder" -- the safe default, since a new category
    # jumping the queue would starve rent.
    order_index: int | None = Field(default=None, ge=1)

    # An optional matching rule, so future transactions land here without
    # being reviewed by hand. The model suggests one; the user confirms it.
    matcher: dict | None = None

    @field_validator("matcher")
    @classmethod
    def _usable(cls, v: dict | None) -> dict | None:
        if v is None:
            return v
        trimmed = {k: x for k, x in v.items() if x not in (None, "", [], {})}
        if not trimmed:
            # An empty matcher would claim every transaction.
            return None
        return trimmed

    @field_validator("rule_type")
    @classmethod
    def _known(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if v == RuleType.REMAINDER.value:
            raise ValueError(
                "a single category cannot take the remainder; edit the plan instead"
            )
        if v not in {t.value for t in RuleType}:
            raise ValueError(f"rule_type must be one of {[t.value for t in RuleType]}")
        return v


class ApplyCategoryOut(BaseModel):
    category: CategoryOut
    plan_id: str | None
    plan_version: int | None
    funding_position: int | None
    matcher_rule_id: str | None = None


@router.post("/apply-category", response_model=ApplyCategoryOut)
def apply_category(
    body: ApplyCategoryIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> ApplyCategoryOut:
    spec = body.category
    clash = db.scalars(
        select(Category).where(
            Category.user_id == user.id,
            Category.name == spec.name,
            Category.archived_at.is_(None),
        )
    ).first()
    if clash is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"'{spec.name}' already exists"
        )

    # Resolve the plan BEFORE creating anything. Relying on the session
    # rolling back a flushed category after a late failure works, but it
    # makes the endpoint's correctness depend on teardown rather than on
    # the code you can read here.
    plan = None
    if body.rule_type is not None:
        plan = db.scalars(
            select(SplitPlan).where(
                SplitPlan.user_id == user.id, SplitPlan.is_active.is_(True)
            )
        ).first()
        if plan is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "There is no active split plan to add this rule to. Create a "
                "budget plan first, or add the category without a funding rule.",
            )

    count = db.scalar(
        select(func.count())
        .select_from(Category)
        .where(Category.user_id == user.id, Category.archived_at.is_(None))
    )
    cat = Category(
        user_id=user.id, name=spec.name, kind=spec.kind,
        description=spec.description, target_cents=spec.target_cents,
        alert_at_cents=spec.alert_at_cents, sort_order=int(count or 0),
    )
    db.add(cat)
    db.flush()

    matcher_rule_id: str | None = None
    if body.matcher:
        rule = CategoryRule(user_id=user.id, category_id=cat.id,
                            matcher=body.matcher, priority=100, enabled=True)
        db.add(rule)
        db.flush()
        matcher_rule_id = str(rule.id)

    if body.rule_type is None:
        db.commit()
        return ApplyCategoryOut(category=_cat_out(cat), plan_id=None,
                                plan_version=None, funding_position=None,
                                matcher_rule_id=matcher_rule_id)

    assert plan is not None  # guaranteed above when rule_type is set
    rows = db.scalars(
        select(SplitRule).where(SplitRule.plan_id == plan.id)
        .order_by(SplitRule.order_index)
    ).all()
    existing = [
        SplitRuleIn(category_id=str(r.category_id), order_index=r.order_index,
                    rule_type=r.rule_type, params=r.params or {}, enabled=r.enabled)
        for r in rows
    ]

    new_rule = SplitRuleIn(
        category_id=str(cat.id), order_index=0,
        rule_type=body.rule_type, params=body.params,
    )

    # The remainder rule must stay last, so the furthest a new rule can go
    # is immediately before it.
    last_allowed = len(existing)
    if existing and existing[-1].rule_type == RuleType.REMAINDER.value:
        last_allowed = len(existing) - 1

    position = last_allowed if body.order_index is None else min(
        body.order_index - 1, last_allowed
    )
    ordered = existing[:position] + [new_rule] + existing[position:]
    for i, r in enumerate(ordered):
        r.order_index = i + 1
    _validate_plan(ordered)

    plan.is_active = False
    new_plan = SplitPlan(user_id=user.id, name=plan.name,
                         version=plan.version + 1, is_active=True)
    db.add(new_plan)
    db.flush()
    for r in ordered:
        db.add(SplitRule(plan_id=new_plan.id, category_id=uuid.UUID(r.category_id),
                         order_index=r.order_index, rule_type=r.rule_type,
                         params=r.params, enabled=r.enabled))
    db.commit()
    return ApplyCategoryOut(
        category=_cat_out(cat), plan_id=str(new_plan.id),
        plan_version=new_plan.version, funding_position=position + 1,
        matcher_rule_id=matcher_rule_id,
    )
