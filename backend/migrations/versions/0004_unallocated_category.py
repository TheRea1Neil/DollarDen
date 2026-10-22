"""Mark one category as the unallocated sink.

Money that is not yet assigned a purpose has to live somewhere, or the
allocated total silently stops matching what is actually in the bank. One
category per user is flagged as the sink, and the opening-balance flow and
paycheck remainders drain into it.

Revision ID: 0004_unallocated_category
Revises: 0003_budget_treatment
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_unallocated_category"
down_revision = "0003_budget_treatment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "categories",
        sa.Column("is_unallocated", sa.Boolean(), nullable=False, server_default="false"),
    )
    # At most one sink per user. A partial unique index enforces it in the
    # database rather than trusting every code path to remember.
    op.create_index(
        "uq_categories_one_unallocated",
        "categories",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("is_unallocated AND archived_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_categories_one_unallocated", table_name="categories")
    op.drop_column("categories", "is_unallocated")
