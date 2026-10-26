"""Receipt-line memory: ask once, then remember.

Mounted under /budget because it belongs to the same flow as the review
queue and the receipt screen. See app/budget/item_memory.py for why this
is deliberately not a CategoryRule.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.analytics.recurring import normalise_merchant
from app.api.deps import current_user
from app.budget import item_memory
from app.db.models import Category, ExpenseType, ItemMemory, User
from app.db.session import get_db
from sqlalchemy.orm import Session

router = APIRouter(prefix="/budget", tags=["budget"])


class RecallIn(BaseModel):
    merchant: str = ""
    descriptions: list[str] = Field(default_factory=list, max_length=200)


@router.post("/item-memory/recall")
def recall(
    body: RecallIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """What is already known about these lines.

    A line with no memory is simply absent from the result, which leaves
    it for the model or the human rather than inviting a guess.
    """
    found = item_memory.recall(db, user.id, body.merchant, body.descriptions)
    if not found:
        return {"recollections": {}}

    # Resolve names so the client can render without a second round trip,
    # and drop anything whose category has since been archived -- a
    # memory pointing at a dead envelope would pre-fill something that
    # cannot be submitted.
    cat_ids = {r.category_id for r in found.values()}
    cats = {
        c.id: c
        for c in db.scalars(
            select(Category).where(
                Category.id.in_(cat_ids),
                Category.user_id == user.id,
                Category.archived_at.is_(None),
            )
        ).all()
    }
    type_ids = {r.expense_type_id for r in found.values() if r.expense_type_id}
    types = {
        t.id: t
        for t in db.scalars(
            select(ExpenseType).where(
                ExpenseType.id.in_(type_ids),
                ExpenseType.user_id == user.id,
                ExpenseType.archived_at.is_(None),
            )
        ).all()
        if type_ids
    }

    out: dict[str, Any] = {}
    for description, r in found.items():
        cat = cats.get(r.category_id)
        if cat is None:
            continue
        et = types.get(r.expense_type_id) if r.expense_type_id else None
        out[description] = {
            **r.as_dict(),
            "category_name": cat.name,
            "expense_type_id": str(et.id) if et else None,
            "expense_type_name": et.name if et else None,
        }
    return {"recollections": out}


class RememberItem(BaseModel):
    description: str = Field(min_length=1, max_length=200)
    category_id: str
    expense_type_id: str | None = None


class RememberIn(BaseModel):
    merchant: str = Field(min_length=1)
    items: list[RememberItem] = Field(default_factory=list, max_length=200)


@router.post("/item-memory", status_code=status.HTTP_200_OK)
def remember(
    body: RememberIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Record where these lines were filed at this shop."""
    if not body.items:
        return {"remembered": 0}

    wanted = {i.category_id for i in body.items}
    owned = {
        str(c.id)
        for c in db.scalars(
            select(Category).where(
                Category.id.in_([uuid.UUID(w) for w in _valid_uuids(wanted)]),
                Category.user_id == user.id,
                Category.archived_at.is_(None),
            )
        ).all()
    }

    n = 0
    for item in body.items:
        if item.category_id not in owned:
            # Silently skipping would teach nothing and say nothing.
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"unknown or archived category for '{item.description}'",
            )
        et_id = None
        if item.expense_type_id:
            try:
                et_id = uuid.UUID(item.expense_type_id)
            except ValueError:
                et_id = None
        row = item_memory.remember(
            db, user.id, body.merchant, item.description,
            uuid.UUID(item.category_id), et_id,
        )
        if row is not None:
            n += 1
    db.commit()
    return {"remembered": n}


def _valid_uuids(values: set[str]) -> list[str]:
    out = []
    for v in values:
        try:
            uuid.UUID(v)
            out.append(v)
        except ValueError:
            continue
    return out


@router.get("/item-memory")
def list_memory(
    merchant: str | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Everything learned, so a wrong memory can be found and removed."""
    stmt = (
        select(ItemMemory, Category.name)
        .join(Category, Category.id == ItemMemory.category_id)
        .where(ItemMemory.user_id == user.id)
    )
    if merchant:
        stmt = stmt.where(ItemMemory.merchant_slug == normalise_merchant(merchant))
    rows = db.execute(
        stmt.order_by(ItemMemory.merchant_slug, ItemMemory.description_slug).limit(limit)
    ).all()
    return {
        "items": [
            {
                "id": str(m.id),
                "merchant": m.merchant_slug,
                "description": m.description,
                "category_id": str(m.category_id),
                "category_name": cat_name,
                "expense_type_id": str(m.expense_type_id) if m.expense_type_id else None,
                "times_seen": m.times_seen,
                "last_seen_at": m.last_seen_at.isoformat() if m.last_seen_at else None,
            }
            for m, cat_name in rows
        ]
    }


@router.delete("/item-memory/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
def forget(
    memory_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> None:
    try:
        pk = uuid.UUID(memory_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found") from None
    if not item_memory.forget(db, user.id, pk):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    db.commit()
