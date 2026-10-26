"""Remember where a receipt line was filed, so it is asked once.

Supermarket receipts print truncated codes: COTTAGE CH, GV MXDVG 12Z,
HF SMK SGE. A model can reason about "cottage cheese" and cannot
reliably reason about "COTTAGE CH" -- on the first real receipt it
filed the legible lines and declined the rest, which is the correct
behaviour for a guess but means the same question comes back every
shop.

These names are unlearnable by reasoning and trivial to learn by
memory. Told once that COTTAGE CH is groceries, there is no reason
ever to ask again.

SCOPE is per merchant, with a fallback across shops. Walmart's "GV"
is its Great Value house brand and means nothing at another chain, so
the merchant-scoped row wins. But a name every shop files the same way
-- BANANAS -- should not need teaching at each one, so a lookup that
finds the description at other merchants will use it IF THEY ALL AGREE.
Disagreement means ambiguity, and ambiguity goes back to the human.

Deliberately NOT a CategoryRule. Those match transactions by merchant
and run inside the auto-categorise path, where a bad one silently
redirects real money -- Neil has already been bitten by a "fun" matcher
catching FUNDRAISER. This only ever pre-fills a line on a screen he is
looking at before submitting.

Revision ID: 0008_item_memory
Revises: 0007_expense_types
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0008_item_memory"
down_revision = "0007_expense_types"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "item_memory",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Normalised merchant. Never blank: a memory always records
        # where it was learned, and cross-shop reuse is decided at
        # lookup time rather than by writing a second "global" row that
        # could drift out of step with the rows it summarises.
        sa.Column("merchant_slug", sa.String(120), nullable=False),
        sa.Column("description_slug", sa.String(120), nullable=False),
        # As last printed, for display. The slug is what matches.
        sa.Column("description", sa.String(200), nullable=False),
        sa.Column(
            "category_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("categories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The reporting axis, learned at the same time and for free.
        sa.Column(
            "expense_type_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("expense_types.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("times_seen", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "user_id", "merchant_slug", "description_slug",
            name="uq_item_memory_user_merchant_desc",
        ),
    )
    op.create_index("ix_item_memory_user_id", "item_memory", ["user_id"])
    # The cross-shop lookup: every merchant that has seen this name.
    op.create_index(
        "ix_item_memory_user_desc", "item_memory", ["user_id", "description_slug"]
    )


def downgrade() -> None:
    op.drop_index("ix_item_memory_user_desc", table_name="item_memory")
    op.drop_index("ix_item_memory_user_id", table_name="item_memory")
    op.drop_table("item_memory")
