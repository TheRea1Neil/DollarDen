"""Close review items for deposits already recorded as paychecks.

A payroll credit arrived before income was diverted away from the
spending review queue, so it sat there asking to be filed into a
single category -- after it had already been split across every
envelope. Harmless but wrong, and confusing in exactly the way a
budget must not be.

The code path that creates a paycheck now closes these itself. This
clears the ones that predate it.

Revision ID: 0013_close_paycheck_reviews
Revises: 0012_force_refresh_stamp
"""
from __future__ import annotations

from alembic import op

revision = "0013_close_paycheck_reviews"
down_revision = "0012_force_refresh_stamp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Only rows whose transaction is genuinely recorded as a paycheck,
    # and only ones still open. Nothing else is touched.
    op.execute(
        """
        UPDATE review_items r
        SET resolved_at = now(),
            resolution = jsonb_build_object(
                'method', 'paycheck',
                'paycheck_id', p.id::text,
                'backfill', '0013_close_paycheck_reviews')
        FROM paychecks p
        WHERE p.transaction_id = r.transaction_id
          AND r.resolved_at IS NULL
        """
    )


def downgrade() -> None:
    # Reopening them would put already-split paychecks back in the
    # queue, which is the bug. Deliberately one-way.
    pass
