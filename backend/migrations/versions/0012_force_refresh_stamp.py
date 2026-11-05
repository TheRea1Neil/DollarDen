"""Track when we last asked Plaid to go re-read the bank.

/transactions/refresh reaches the institution and is rate limited, so
pull-to-refresh must not fire it on every pull. The stamp has to
survive a restart -- an in-memory throttle resets on every deploy,
which is exactly when someone is pulling repeatedly to see whether the
deploy worked.

Revision ID: 0012_force_refresh_stamp
Revises: 0011_webhook_health
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_force_refresh_stamp"
down_revision = "0011_webhook_health"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("plaid_items",
                  sa.Column("last_force_refresh_at", sa.DateTime(timezone=True)))
    op.execute(
        "COMMENT ON COLUMN public.plaid_items.last_force_refresh_at IS "
        "'When /transactions/refresh was last called for this Item -- the "
        "call that makes Plaid go read the bank, as opposed to returning "
        "what it already holds. Rate limited by Plaid, so pull-to-refresh "
        "throttles on this.'"
    )


def downgrade() -> None:
    op.drop_column("plaid_items", "last_force_refresh_at")
