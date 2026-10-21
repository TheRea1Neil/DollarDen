"""Repository layer.

The single most likely serious bug in a two-user finance app is one user
reading another's rows. Rather than trusting every handler to remember a
filter, scoping is structural here: UserScopedRepository takes a user_id at
construction and every query it builds is filtered by it.

Handlers should never build a bare select() against a user-owned table.
"""
from __future__ import annotations

import uuid
from typing import Generic, TypeVar

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.db.base import Base

T = TypeVar("T", bound=Base)


class UserScopedRepository(Generic[T]):
    """CRUD scoped to exactly one user. The filter cannot be forgotten."""

    def __init__(self, db: Session, model: type[T], user_id: uuid.UUID) -> None:
        if not hasattr(model, "user_id"):
            raise TypeError(
                f"{model.__name__} has no user_id column; it is not user-scoped. "
                "Use a plain query, and justify it."
            )
        self.db = db
        self.model = model
        self.user_id = user_id

    def _base(self) -> Select[tuple[T]]:
        return select(self.model).where(self.model.user_id == self.user_id)  # type: ignore[attr-defined]

    def all(self) -> list[T]:
        return list(self.db.scalars(self._base()).all())

    def get(self, obj_id: uuid.UUID) -> T | None:
        """Returns None for another user's row, exactly as if it did not exist.

        Deliberately not a 403: that would confirm the row's existence.
        """
        return self.db.scalars(self._base().where(self.model.id == obj_id)).first()  # type: ignore[attr-defined]

    def add(self, obj: T) -> T:
        if getattr(obj, "user_id", None) not in (None, self.user_id):
            raise PermissionError("refusing to persist an object owned by a different user")
        obj.user_id = self.user_id  # type: ignore[attr-defined]
        self.db.add(obj)
        self.db.flush()
        return obj

    def delete(self, obj: T) -> None:
        if getattr(obj, "user_id") != self.user_id:
            raise PermissionError("refusing to delete another user's row")
        self.db.delete(obj)
