"""Record how each transaction affects the budget.

Credit card payments and transfers between the user's own accounts must not
allocate against categories: the spending they relate to was already counted
when the purchase happened. Without this, paying a card double-counts every
purchase on it.

Revision ID: 0003_budget_treatment
Revises: 0002_fk_delete_rules
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_budget_treatment"
down_revision = "0002_fk_delete_rules"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "transactions",
        sa.Column("treatment", sa.String(16), nullable=False, server_default="spend"),
    )
    op.create_index(
        "ix_transactions_user_treatment", "transactions", ["user_id", "treatment"]
    )


def downgrade() -> None:
    op.drop_index("ix_transactions_user_treatment", table_name="transactions")
    op.drop_column("transactions", "treatment")
