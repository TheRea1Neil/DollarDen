"""Let a job wait for the GPU instead of failing.

The AI host is also Neil's gaming PC. gemma4 needs ~9.5 GB of its
16 GB card (measured: 9,815 MB resident), so a game using more than
about 6.3 GB leaves no room. Before this, a job dispatched at that
moment either fought the game for VRAM or failed outright and was gone.

Neither is right. The wanted behaviour is: run immediately whenever
there is room, and when there is not, wait and try again rather than
give up.

So a job can now be `deferred` with a `not_before`, and the worker that
already polls for queued work picks it up again once that time passes.
Three columns, no new scheduler.

`attempts` exists so a job cannot defer forever. A receipt that has
waited past the deadline runs anyway -- a stuttered frame is a smaller
cost than a transaction that never gets categorised.

Revision ID: 0009_deferred_jobs
Revises: 0008_item_memory
"""
from alembic import op
import sqlalchemy as sa

revision = "0009_deferred_jobs"
down_revision = "0008_item_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # When this job may next be attempted. NULL means "now".
    op.add_column("ai_jobs",
                  sa.Column("not_before", sa.DateTime(timezone=True), nullable=True))
    # Why it is waiting, in words the app can show the user.
    op.add_column("ai_jobs", sa.Column("defer_reason", sa.Text(), nullable=True))
    op.add_column("ai_jobs",
                  sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"))
    # The worker scans for work due now; without this it would sort the
    # whole table on every poll.
    op.create_index("ix_ai_jobs_status_not_before", "ai_jobs",
                    ["status", "not_before"])


def downgrade() -> None:
    op.drop_index("ix_ai_jobs_status_not_before", table_name="ai_jobs")
    op.drop_column("ai_jobs", "attempts")
    op.drop_column("ai_jobs", "defer_reason")
    op.drop_column("ai_jobs", "not_before")
