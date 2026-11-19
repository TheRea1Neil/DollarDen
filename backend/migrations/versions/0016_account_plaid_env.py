"""Tie each account to one Plaid environment, permanently.

Sandbox and real banking have to coexist on one instance: people
should try the whole loop with fake banks before spending any of a
small, lifetime allowance of real connections. The obvious design --
one environment per instance, flipped when you are ready -- is worse
than it looks. A sandbox access token is meaningless in production, so
flipping strands every existing connection; and anyone who used the
sandbox PROPERLY, categorising and splitting a paycheck, ends up with
a budget full of imaginary money that no credential swap cleans up.

So the boundary is the account, not the instance. That costs nothing
to build because every table is already scoped by `user_id` and that
isolation is already tested -- a sandbox account is ALREADY a separate
budget, with its own categories, ledger and transactions.

Existing accounts are set to whatever the instance is configured for
right now, so nothing changes behaviour on upgrade. The column
defaults to `sandbox` for anything created later without an explicit
choice, because the failure of guessing wrong in that direction is
free, and in the other direction costs a connection that is never
given back.

Revision ID: 0016_account_plaid_env
Revises: 0015_instance_settings
"""
from __future__ import annotations

import os

import sqlalchemy as sa
from alembic import op

revision = "0016_account_plaid_env"
down_revision = "0015_instance_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("plaid_env", sa.String(16), nullable=False,
                  server_default="sandbox"),
    )

    # Preserve behaviour: every account that already exists keeps using
    # whatever this instance has been talking to.
    current = (os.environ.get("PLAID_ENV") or "sandbox").strip().lower()
    if current not in ("sandbox", "production"):
        current = "sandbox"
    op.execute(sa.text("UPDATE users SET plaid_env = :env").bindparams(env=current))

    op.execute(
        "COMMENT ON COLUMN users.plaid_env IS "
        "'Which Plaid environment this account''s connections belong to. "
        "Fixed at creation: a token issued in one is meaningless in the "
        "other, so changing it would strand every bank already linked.'"
    )


def downgrade() -> None:
    op.drop_column("users", "plaid_env")
