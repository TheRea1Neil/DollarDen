"""Insights, questions and the expense-type taxonomy.

Everything numeric here is computed in app/analytics. The AI endpoints
live in routes/ai.py as queued jobs, because the inference host sleeps
and nothing in the UI can block on it -- but the figures on the
insights screen do NOT wait for the model. The screen is fully useful
with AI switched off; the model only adds a paragraph on top (ADR 0003).
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.analytics import insights as insights_mod
from app.analytics import queries as Q
from app.analytics import recurring as recurring_mod
from app.analytics import taxonomy
from app.analytics.period import PeriodError, resolve
from app.api.deps import current_user
from app.db.models import Allocation, Category, ExpenseType, Transaction, User
from app.db.session import get_db

router = APIRouter(prefix="/analytics", tags=["analytics"])


def _period(spec: str, today: date):
    try:
        return resolve(spec, today=today)
    except PeriodError as exc:
        # A period we cannot parse is a 400, never a silent fallback:
        # answering for the wrong month is indistinguishable from
        # answering for the right one.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


def _today(override: str | None) -> date:
    """Let the client say what day it is.

    The phone knows the user's timezone; the Pi is on UTC. Asked at
    22:00 on the 31st, a server-side `date.today()` answers for next
    month.
    """
    if not override:
        return datetime.now(UTC).date()
    try:
        return date.fromisoformat(override)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "today must be YYYY-MM-DD") from None


def _ids(raw: list[str] | None) -> tuple[uuid.UUID, ...]:
    out = []
    for s in raw or []:
        try:
            out.append(uuid.UUID(s))
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"not an id: {s}") from None
    return tuple(out)


# --------------------------------------------------------------------------- #
# Insights
# --------------------------------------------------------------------------- #
@router.get("/insights")
def get_insights(
    period: str = Query("this_month"),
    today: str | None = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    now = _today(today)
    p = _period(period, now)
    return {
        "period": p.as_dict(),
        "cards": insights_mod.build(db, user.id, period=p, today=now),
    }


@router.get("/spend")
def get_spend(
    period: str = Query("this_month"),
    today: str | None = Query(None),
    group_by: str | None = Query(None),
    metric: str = Query("spend"),
    expense_type_id: list[str] | None = Query(None),
    category_id: list[str] | None = Query(None),
    merchant: str | None = Query(None),
    limit: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    now = _today(today)
    q = Q.SpendQuery(
        period=_period(period, now),
        metric=metric,  # type: ignore[arg-type]
        group_by=group_by,  # type: ignore[arg-type]
        expense_type_ids=_ids(expense_type_id),
        category_ids=_ids(category_id),
        merchant_contains=merchant,
        limit=limit,
    )
    try:
        return Q.run(db, user.id, q).as_dict()
    except Q.QueryError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.get("/spend/transactions")
def get_spend_transactions(
    period: str = Query("this_month"),
    today: str | None = Query(None),
    expense_type_id: list[str] | None = Query(None),
    category_id: list[str] | None = Query(None),
    merchant: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """The rows behind a figure. Every total on screen opens into this."""
    now = _today(today)
    q = Q.SpendQuery(
        period=_period(period, now),
        expense_type_ids=_ids(expense_type_id),
        category_ids=_ids(category_id),
        merchant_contains=merchant,
        compare_to_previous=False,
    )
    return {"transactions": Q.transactions_for(db, user.id, q, limit=limit)}


@router.get("/recurring")
def get_recurring(
    today: str | None = Query(None),
    lookback_days: int = Query(400, ge=60, le=1100),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    now = _today(today)
    items = recurring_mod.detect(db, user.id, today=now, lookback_days=lookback_days)
    live = [r for r in items if not r.likely_ended]
    return {
        "monthly_cents": recurring_mod.monthly_commitment_cents(items),
        "annual_cents": sum(r.annual_cents for r in live),
        "active_count": len(live),
        "items": [r.as_dict() for r in items],
    }


# --------------------------------------------------------------------------- #
# Expense types
# --------------------------------------------------------------------------- #
class TypeOut(BaseModel):
    id: str
    name: str
    slug: str
    description: str | None
    created_by: str
    reviewed: bool
    spend_cents: int = 0
    txn_count: int = 0


class TypeIn(BaseModel):
    name: str = Field(min_length=1, max_length=taxonomy.MAX_NAME)
    description: str | None = None


class TypePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=taxonomy.MAX_NAME)
    description: str | None = None
    reviewed: bool | None = None


def _type_out(t: ExpenseType, cents: int = 0, n: int = 0) -> TypeOut:
    return TypeOut(
        id=str(t.id),
        name=t.name,
        slug=t.slug,
        description=t.description,
        created_by=t.created_by,
        reviewed=t.reviewed_at is not None,
        spend_cents=cents,
        txn_count=n,
    )


@router.get("/expense-types", response_model=list[TypeOut])
def list_types(
    period: str = Query("last_90_days"),
    today: str | None = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[TypeOut]:
    """Types with how much has gone through each, so dead ones are visible."""
    p = _period(period, _today(today))
    rows = db.execute(
        select(
            Allocation.expense_type_id,
            func.sum(func.abs(Allocation.amount_cents)),
            func.count(func.distinct(Allocation.transaction_id)),
        )
        .join(Transaction, Transaction.id == Allocation.transaction_id)
        .where(
            Allocation.user_id == user.id,
            Allocation.amount_cents < 0,
            Allocation.expense_type_id.isnot(None),
            Transaction.removed_at.is_(None),
            Transaction.date >= p.start,
            Transaction.date <= p.end,
        )
        .group_by(Allocation.expense_type_id)
    ).all()
    totals = {r[0]: (int(r[1]), int(r[2])) for r in rows}
    return [
        _type_out(t, *totals.get(t.id, (0, 0))) for t in taxonomy.active(db, user.id)
    ]


@router.get("/expense-types/suggestions")
def type_suggestions(
    db: Session = Depends(get_db), user: User = Depends(current_user)
) -> dict[str, Any]:
    """Existing types plus unused standard ones, for a picker."""
    return {"suggestions": taxonomy.suggestions_for_prompt(db, user.id)}


@router.get("/expense-types/unreviewed", response_model=list[TypeOut])
def list_unreviewed(
    db: Session = Depends(get_db), user: User = Depends(current_user)
) -> list[TypeOut]:
    """Types the assistant coined that nobody has confirmed."""
    return [_type_out(t) for t in taxonomy.unreviewed(db, user.id)]


@router.post("/expense-types", response_model=TypeOut,
             status_code=status.HTTP_201_CREATED)
def create_type(
    body: TypeIn, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> TypeOut:
    try:
        t, created = taxonomy.resolve_or_create(
            db, user.id, body.name, description=body.description, created_by="user"
        )
    except taxonomy.TaxonomyError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if not created:
        # Not an error -- the caller asked for a type by a name that
        # already resolves to one. Saying so is more useful than a 409
        # they would have to translate into "use the existing one".
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"“{t.name}” already covers that. Use it rather than adding a second name "
            f"for the same thing.",
        )
    db.commit()
    return _type_out(t)


@router.patch("/expense-types/{type_id}", response_model=TypeOut)
def patch_type(
    type_id: str,
    body: TypePatch,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> TypeOut:
    t = _own_type(db, type_id, user)
    data = body.model_dump(exclude_unset=True)

    if "name" in data and data["name"]:
        new_slug = taxonomy.slugify(data["name"])
        clash = taxonomy.find_by_slug(db, user.id, new_slug)
        if clash and clash.id != t.id:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"“{clash.name}” already uses that name. Merge the two instead.",
            )
        t.name, t.slug = data["name"].strip(), new_slug
    if "description" in data:
        t.description = (data["description"] or "").strip() or None
    if data.get("reviewed"):
        t.reviewed_at = datetime.now(UTC)

    db.commit()
    return _type_out(t)


class MergeIn(BaseModel):
    into_id: str


@router.post("/expense-types/{type_id}/merge", response_model=TypeOut)
def merge_type(
    type_id: str,
    body: MergeIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> TypeOut:
    """Fold one type into another and relabel its history."""
    src = _own_type(db, type_id, user)
    dst = _own_type(db, body.into_id, user)
    try:
        taxonomy.merge(db, user.id, src.id, dst.id)
    except taxonomy.TaxonomyError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    db.commit()
    return _type_out(dst)


@router.delete("/expense-types/{type_id}", status_code=status.HTTP_204_NO_CONTENT)
def archive_type(
    type_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> None:
    """Archive, never delete.

    Spending already filed under it keeps pointing here, so historical
    totals do not change shape because someone tidied up today. Use
    merge instead when the history should move.
    """
    t = _own_type(db, type_id, user)
    t.archived_at = datetime.now(UTC)
    db.commit()


def _own_type(db: Session, type_id: str, user: User) -> ExpenseType:
    try:
        pk = uuid.UUID(type_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found") from None
    t = db.scalars(
        select(ExpenseType).where(ExpenseType.id == pk, ExpenseType.user_id == user.id)
    ).first()
    if t is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return t


# --------------------------------------------------------------------------- #
# Labelling spending
# --------------------------------------------------------------------------- #
class AssignIn(BaseModel):
    # Either an existing type...
    expense_type_id: str | None = None
    # ...or a name, which is resolved and created only if nothing matches.
    name: str | None = None
    description: str | None = None
    created_by: Literal["user", "ai"] = "user"


@router.post("/transactions/{transaction_id}/expense-type")
def set_transaction_type(
    transaction_id: str,
    body: AssignIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Label every outflow allocation on one transaction.

    The common case: one charge, one kind of spending. A receipt split
    across several kinds is labelled per allocation instead, below.
    """
    try:
        pk = uuid.UUID(transaction_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found") from None

    txn = db.scalars(
        select(Transaction).where(Transaction.id == pk, Transaction.user_id == user.id)
    ).first()
    if txn is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    et = _resolve_assignment(db, user, body)
    rows = db.scalars(
        select(Allocation).where(
            Allocation.user_id == user.id,
            Allocation.transaction_id == txn.id,
            Allocation.amount_cents < 0,
        )
    ).all()
    if not rows:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This transaction has not been filed into a category yet, so there is "
            "nothing to label.",
        )
    for a in rows:
        a.expense_type_id = et.id if et else None
    db.commit()
    return {
        "transaction_id": str(txn.id),
        "expense_type": _type_out(et).model_dump() if et else None,
        "allocations_updated": len(rows),
    }


@router.post("/allocations/{allocation_id}/expense-type")
def set_allocation_type(
    allocation_id: str,
    body: AssignIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Label one line of an itemised receipt."""
    try:
        pk = uuid.UUID(allocation_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found") from None
    a = db.scalars(
        select(Allocation).where(Allocation.id == pk, Allocation.user_id == user.id)
    ).first()
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if a.amount_cents > 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Only money going out has a kind. Funding a category is not an expense.",
        )
    et = _resolve_assignment(db, user, body)
    a.expense_type_id = et.id if et else None
    db.commit()
    return {
        "allocation_id": str(a.id),
        "expense_type": _type_out(et).model_dump() if et else None,
    }


def _resolve_assignment(db: Session, user: User, body: AssignIn) -> ExpenseType | None:
    """Turn either half of AssignIn into a type, or None to clear it."""
    if body.expense_type_id:
        return _own_type(db, body.expense_type_id, user)
    if body.name:
        try:
            et, _ = taxonomy.resolve_or_create(
                db, user.id, body.name,
                description=body.description, created_by=body.created_by,
            )
        except taxonomy.TaxonomyError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        return et
    return None


@router.get("/untyped")
def list_untyped(
    period: str = Query("last_90_days"),
    today: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Filed spending with no kind yet -- the queue for labelling."""
    p = _period(period, _today(today))
    rows = db.execute(
        select(
            Transaction.id,
            Transaction.date,
            func.coalesce(Transaction.merchant_name, Transaction.name).label("merchant"),
            func.sum(func.abs(Allocation.amount_cents)).label("cents"),
            func.min(Category.name).label("category"),
        )
        .join(Allocation, Allocation.transaction_id == Transaction.id)
        .join(Category, Category.id == Allocation.category_id)
        .where(
            Allocation.user_id == user.id,
            Allocation.amount_cents < 0,
            Allocation.expense_type_id.is_(None),
            Transaction.removed_at.is_(None),
            Transaction.date >= p.start,
            Transaction.date <= p.end,
        )
        .group_by(Transaction.id, Transaction.date, Transaction.merchant_name,
                  Transaction.name)
        .order_by(Transaction.date.desc())
        .limit(limit)
    ).all()
    return {
        "period": p.as_dict(),
        "transactions": [
            {
                "transaction_id": str(r.id),
                "date": r.date.isoformat(),
                "merchant": r.merchant,
                "amount_cents": int(r.cents),
                "category": r.category,
            }
            for r in rows
        ],
    }
