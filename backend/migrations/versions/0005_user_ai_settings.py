"""Per-user AI provider settings.

The provider was a single environment variable, which cannot express "use
the house GPU" for one person and "use the Ollama on my laptop" for another.
Each user picks their own, and anyone can turn AI off entirely -- a
first-class configuration, not a degraded one (ADR 0003).

Revision ID: 0005_user_ai_settings
Revises: 0004_unallocated_category
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_user_ai_settings"
down_revision = "0004_unallocated_category"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NULL means "follow the server default", so existing users keep working
    # without a backfill.
    op.add_column("users", sa.Column("ai_provider", sa.String(16), nullable=True))
    op.add_column("users", sa.Column("ai_base_url", sa.String(300), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "ai_base_url")
    op.drop_column("users", "ai_provider")
