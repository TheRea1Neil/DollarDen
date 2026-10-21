"""SQLAlchemy declarative base and shared column types.

Money is ALWAYS BigInteger cents. See docs/adr/0002.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Explicit naming so Alembic autogenerate produces stable constraint names.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def uuid_pk() -> Mapped[uuid.UUID]:
    from sqlalchemy.dialects.postgresql import UUID as PG_UUID

    return mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def created_at_col() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# Money. Named so a reviewer can see the unit at the call site.
Cents = BigInteger
