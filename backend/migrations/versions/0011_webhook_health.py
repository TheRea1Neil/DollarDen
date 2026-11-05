"""Record when Plaid last reached us, and whether it was accepted.

"Are webhooks working?" was unanswerable after a deploy: the only
evidence was container logs, which a redeploy throws away, and Caddy's
access log, which knows the status code but not which Item or why. A
webhook that silently stops is the worst failure mode here -- the app
looks healthy and simply goes stale.

Revision ID: 0011_webhook_health
Revises: 0010_schema_comments
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_webhook_health"
down_revision = "0010_schema_comments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("plaid_items",
                  sa.Column("last_webhook_at", sa.DateTime(timezone=True)))
    op.add_column("plaid_items",
                  sa.Column("last_webhook_code", sa.String(64)))
    op.execute(
        "COMMENT ON COLUMN public.plaid_items.last_webhook_at IS "
        "'When Plaid last successfully reached the webhook for this Item. "
        "Stale means real-time updates have stopped, which is otherwise "
        "invisible: the Item still reports healthy and the app just goes "
        "quietly out of date.'"
    )
    op.execute(
        "COMMENT ON COLUMN public.plaid_items.last_webhook_code IS "
        "'The webhook_code of that delivery, or rejected:<reason> when "
        "signature verification failed. Container logs do not survive a "
        "redeploy; this does.'"
    )


def downgrade() -> None:
    op.drop_column("plaid_items", "last_webhook_code")
    op.drop_column("plaid_items", "last_webhook_at")
