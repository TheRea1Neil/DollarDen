"""A second axis: what the money WAS, alongside which envelope it came FROM.

Categories answer "where did this come out of" -- Date Fund, Groceries,
Ch JesusChrist Donation. They are balances, and they are shaped by how
Neil wants to divide a paycheck.

That makes them the wrong axis for "how much did I spend on fast food
this month". A Chick-fil-A run might come out of Date Fund, Groceries or
Discretionary depending on who he was with, and Discretionary holds a
dozen unrelated kinds of spending. Asking an envelope what it was spent
ON has no answer.

So expense types are a separate taxonomy over the SAME money:

    envelope (funding)   x   expense type (reporting)
    Date Fund                Fast food
    Discretionary            Fast food
    Groceries                Groceries

Grain is the ALLOCATION, not the transaction. One Target run split by
the receipt itemiser is already several allocations; each line gets its
own type, so "household" and "groceries" out of one charge both land in
the right bucket. Spend on a type is the sum of its negative
allocations, which means analytics and envelope balances are computed
from one set of rows and cannot disagree.

`merged_into_id` exists because the model is allowed to coin new types.
Left unchecked that gives you Fast food, Fast Food and Quick Service
Restaurant inside a month -- the same fragmentation that produced 22
matcher rules with 11 exact duplicates. Merging has to keep the old row
so historical allocations still resolve, hence a pointer rather than a
delete.

Revision ID: 0007_expense_types
Revises: 0006_spend_criteria
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0007_expense_types"
down_revision = "0006_spend_criteria"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "expense_types",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(80), nullable=False),
        # Normalised form used to catch near-duplicates before they are
        # created. "Fast Food", "fast food" and "fast-food" all slug to
        # "fast food".
        sa.Column("slug", sa.String(80), nullable=False),
        # What belongs in this type. Read by the model when it files a
        # charge, same as spend_criteria on a category.
        sa.Column("description", sa.Text(), nullable=True),
        # user | ai | seed -- so the UI can offer "the assistant invented
        # these three types this week, keep them?"
        sa.Column("created_by", sa.String(16), nullable=False, server_default="user"),
        # Null while an AI-coined type is unconfirmed.
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "merged_into_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("expense_types.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_expense_types_user_id", "expense_types", ["user_id"])
    op.create_index(
        "uq_expense_types_user_slug_active",
        "expense_types",
        ["user_id", "slug"],
        unique=True,
        postgresql_where=sa.text("archived_at IS NULL"),
    )

    op.add_column(
        "allocations",
        sa.Column(
            "expense_type_id",
            postgresql.UUID(as_uuid=True),
            # SET NULL, not CASCADE: losing a type must never delete the
            # money that was filed under it.
            sa.ForeignKey("expense_types.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_allocations_user_type_time",
        "allocations",
        ["user_id", "expense_type_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_allocations_user_type_time", table_name="allocations")
    op.drop_column("allocations", "expense_type_id")
    op.drop_index("uq_expense_types_user_slug_active", table_name="expense_types")
    op.drop_index("ix_expense_types_user_id", table_name="expense_types")
    op.drop_table("expense_types")
