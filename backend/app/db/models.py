"""Database models.

Two rules enforced throughout:
  1. Every user-owned table carries user_id (multi-tenant from migration 0001).
  2. Money is BigInteger cents, with a `_cents` suffix. Never a float.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _user_fk() -> Mapped[uuid.UUID]:
    return mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #
class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _pk()
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="America/Denver")
    # NULL means follow the server default. "none" disables AI entirely,
    # which is a supported configuration rather than a degraded one.
    ai_provider: Mapped[str | None] = mapped_column(String(16))
    ai_base_url: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = _now()


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    # The refresh token itself is never stored, only its hash.
    refresh_token_hash: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = _now()


# --------------------------------------------------------------------------- #
# Plaid
# --------------------------------------------------------------------------- #
class PlaidItem(Base):
    """One connection to one institution. THIS is what consumes a credit.

    Never call /item/remove on these. Re-auth goes through update mode.
    See docs/04-plaid-integration.md.
    """

    __tablename__ = "plaid_items"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    plaid_item_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    institution_id: Mapped[str | None] = mapped_column(String(64))
    institution_name: Mapped[str | None] = mapped_column(String(200))
    # Encrypted at rest with TOKEN_ENCRYPTION_KEY, separate from the DB password.
    access_token_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    sync_cursor: Mapped[str | None] = mapped_column(Text)
    last_webhook_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_webhook_code: Mapped[str | None] = mapped_column(String(64))
    last_force_refresh_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="healthy")
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consent_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()

    accounts: Mapped[list[Account]] = relationship(back_populates="item")


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    item_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("plaid_items.id", ondelete="CASCADE"), nullable=False
    )
    plaid_account_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(200))
    official_name: Mapped[str | None] = mapped_column(String(200))
    mask: Mapped[str | None] = mapped_column(String(16))
    type: Mapped[str | None] = mapped_column(String(32))
    subtype: Mapped[str | None] = mapped_column(String(48))
    current_cents: Mapped[int | None] = mapped_column(BigInteger)
    available_cents: Mapped[int | None] = mapped_column(BigInteger)
    is_tracked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    balance_as_of: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    item: Mapped[PlaidItem] = relationship(back_populates="accounts")


class Transaction(Base):
    """An immutable fact from Plaid. Categorisation lives in Allocation.

    Sign convention: Plaid returns POSITIVE for money leaving the account.
    We normalise at the sync boundary into outflow/inflow, never one signed
    column. See docs/03-data-model.md.
    """

    __tablename__ = "transactions"
    __table_args__ = (
        Index("ix_transactions_user_date", "user_id", "date"),
        Index("ix_transactions_user_pending", "user_id", "pending"),
        Index("ix_transactions_user_treatment", "user_id", "treatment"),
    )

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    # Idempotency key for /transactions/sync.
    plaid_transaction_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    outflow_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    inflow_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    iso_currency_code: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    date: Mapped[date] = mapped_column(Date, nullable=False)
    datetime_: Mapped[datetime | None] = mapped_column("datetime", DateTime(timezone=True))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    merchant_name: Mapped[str | None] = mapped_column(String(300))
    pending: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pending_replaced_by: Mapped[str | None] = mapped_column(String(128))
    # spend | card_payment | transfer | ignored -- see app/budget/treatment.py.
    # Only `spend` allocates against a category.
    treatment: Mapped[str] = mapped_column(String(16), nullable=False, default="spend")
    pfc_primary: Mapped[str | None] = mapped_column(String(64))
    pfc_detailed: Mapped[str | None] = mapped_column(String(96))
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()


# --------------------------------------------------------------------------- #
# Budget
# --------------------------------------------------------------------------- #
class Category(Base):
    """A balance, not a report line. Archived, never deleted."""

    __tablename__ = "categories"
    __table_args__ = (
        Index(
            "uq_categories_user_name_active",
            "user_id",
            "name",
            unique=True,
            postgresql_where="archived_at IS NULL",
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # spending|savings_bucket|obligation
    # What this category is FOR. Human-facing, and read by the
    # assistant when it writes summaries and advice.
    description: Mapped[str | None] = mapped_column(Text)
    # What gets CHARGED here -- the only half that can decide where a
    # purchase belongs.
    #
    # Measured: carrying the funding sentence alongside it scored
    # IDENTICALLY (83%, same failures), so this is not an accuracy fix.
    # It is a field whose label tells the user what to write. Asked for
    # a "description", people write "whatever is left over goes here",
    # which is true and useless for filing.
    spend_criteria: Mapped[str | None] = mapped_column(Text)
    # Cache of sum(allocations). Never written outside the ledger service.
    balance_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    target_cents: Mapped[int | None] = mapped_column(BigInteger)
    alert_at_cents: Mapped[int | None] = mapped_column(BigInteger)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # The sink for money with no purpose yet. At most one per user, enforced
    # by a partial unique index (migration 0004).
    is_unallocated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()


class Allocation(Base):
    """Append-only ledger. A category's balance is the sum of its entries."""

    __tablename__ = "allocations"
    __table_args__ = (
        Index("ix_allocations_user_cat_time", "user_id", "category_id", "created_at"),
        Index("ix_allocations_user_type_time", "user_id", "expense_type_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    # CASCADE so deleting a user tears down cleanly. Categories are never
    # deleted through the API -- they are archived (see routes/budget.py) --
    # so this does not weaken the archive-not-delete rule in practice.
    category_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("categories.id", ondelete="CASCADE"), nullable=False
    )
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("transactions.id", ondelete="CASCADE")
    )
    paycheck_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("paychecks.id", ondelete="CASCADE")
    )
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)  # +into / -out of
    # What this money WAS, as opposed to which envelope it came out of.
    # Set on outflows only; funding a category is not an expense. See
    # ExpenseType and migration 0007.
    expense_type_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("expense_types.id", ondelete="SET NULL")
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Numeric(3, 2))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()


class ExpenseType(Base):
    """What a charge WAS. The reporting axis, orthogonal to the envelope.

    A category answers "which pot did this come out of". That question
    has a different answer from "what did I buy", and only the second
    one can be aggregated across the budget -- fast food comes out of
    Date Fund some weeks and Discretionary others, and Discretionary
    holds a dozen unrelated kinds of spending.

    Types may be coined by the model when nothing existing fits, which
    is why `slug`, `created_by` and `merged_into_id` exist: unchecked
    creation fragments the taxonomy, and a fragmented taxonomy makes
    every number on the insights screen quietly wrong.
    """

    __tablename__ = "expense_types"
    __table_args__ = (
        Index("ix_expense_types_user_id", "user_id"),
        Index(
            "uq_expense_types_user_slug_active",
            "user_id",
            "slug",
            unique=True,
            postgresql_where="archived_at IS NULL",
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    # Null while a type the assistant invented is still unconfirmed.
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Merged away, but kept so historical allocations still resolve.
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("expense_types.id", ondelete="SET NULL")
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()


class CategoryRule(Base):
    __tablename__ = "category_rules"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    category_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("categories.id", ondelete="CASCADE"), nullable=False
    )
    matcher: Mapped[dict] = mapped_column(JSONB, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class SplitPlan(Base):
    """Versioned so historical paychecks still explain themselves."""

    __tablename__ = "split_plans"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _now()

    rules: Mapped[list[SplitRule]] = relationship(
        back_populates="plan", order_by="SplitRule.order_index"
    )


class SplitRule(Base):
    __tablename__ = "split_rules"

    id: Mapped[uuid.UUID] = _pk()
    plan_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("split_plans.id", ondelete="CASCADE"), nullable=False
    )
    category_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("categories.id", ondelete="CASCADE"), nullable=False
    )
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # percent | fixed | refill_to | conditional | remainder
    rule_type: Mapped[str] = mapped_column(String(32), nullable=False)
    params: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    plan: Mapped[SplitPlan] = relationship(back_populates="rules")


class Paycheck(Base):
    __tablename__ = "paychecks"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("transactions.id", ondelete="SET NULL")
    )
    gross_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    received_on: Mapped[date] = mapped_column(Date, nullable=False)
    split_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("split_plans.id", ondelete="SET NULL")
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()


# --------------------------------------------------------------------------- #
# Review, receipts, AI, goals
# --------------------------------------------------------------------------- #
class ReviewItem(Base):
    __tablename__ = "review_items"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False
    )
    reason: Mapped[str] = mapped_column(String(48), nullable=False)
    suggestions: Mapped[dict | None] = mapped_column(JSONB)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _now()


class Receipt(Base):
    __tablename__ = "receipts"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("transactions.id", ondelete="SET NULL")
    )
    image_path: Mapped[str] = mapped_column(Text, nullable=False)
    line_items: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    created_at: Mapped[datetime] = _now()


class ItemMemory(Base):
    """Where one receipt line was filed last time.

    Scoped to a merchant, because "GV MXDVG 12Z" is Walmart's house
    brand and means nothing elsewhere. A lookup that misses falls back
    to the same description at other shops, and uses it only when they
    all agree -- see app/budget/item_memory.py.

    Not a CategoryRule: this pre-fills a line on a screen the user is
    about to submit, and never moves money on its own.
    """

    __tablename__ = "item_memory"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "merchant_slug", "description_slug",
            name="uq_item_memory_user_merchant_desc",
        ),
        Index("ix_item_memory_user_id", "user_id"),
        Index("ix_item_memory_user_desc", "user_id", "description_slug"),
    )

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    merchant_slug: Mapped[str] = mapped_column(String(120), nullable=False)
    description_slug: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(String(200), nullable=False)
    category_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("categories.id", ondelete="CASCADE"),
        nullable=False,
    )
    expense_type_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("expense_types.id", ondelete="SET NULL")
    )
    times_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()


class AIJob(Base):
    __tablename__ = "ai_jobs"
    __table_args__ = (
        Index("ix_ai_jobs_status_not_before", "status", "not_before"),
    )

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    provider: Mapped[str | None] = mapped_column(String(16))
    request: Mapped[dict] = mapped_column(JSONB, nullable=False)
    result: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    # Set while status is "deferred": the GPU was busy, or it is the
    # middle of the night. The worker re-claims the job once this
    # passes. See app/ai/gpu.py and migration 0009.
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    defer_reason: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = _now()
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserGoal(Base):
    __tablename__ = "user_goals"
    __table_args__ = (UniqueConstraint("user_id", name="uq_user_goals_user_id"),)

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    body: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
