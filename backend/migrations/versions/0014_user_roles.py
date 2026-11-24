"""Give accounts a role, so an instance can have an owner.

Until now every account was equal and anyone who could reach the
server could register one. That is fine for a single household where
the one account belongs to the person who built the server, and wrong
the moment the thing is installed by someone else: an open
registration endpoint on a public URL means a stranger can create an
account and spend the owner's finite Plaid connections.

So: `owner` configures the instance and creates accounts; `member`
uses it.

**This migration deliberately promotes nobody.** The obvious rule --
"the oldest account becomes the owner" -- is wrong on the only
instance that currently exists, where the oldest account is
`bg-a1c8cfbea3@example.com`, left over from an early test run. Handing
ownership of a live budget server to a throwaway account because it
happens to sort first is exactly the kind of clever default that is
discovered months later.

A fresh install has no accounts at all, so the first-run wizard names
the owner. An existing install promotes one deliberately:

    docker compose exec api python -m app.ownership you@example.com

Revision ID: 0014_user_roles
Revises: 0013_close_paycheck_reviews
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_user_roles"
down_revision = "0013_close_paycheck_reviews"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("role", sa.String(16), nullable=False, server_default="member"),
    )
    op.create_index("ix_users_role", "users", ["role"])
    op.execute(
        "COMMENT ON COLUMN users.role IS "
        "'owner configures the instance and creates accounts; member uses it. "
        "No one is promoted automatically -- see app/ownership.py.'"
    )


def downgrade() -> None:
    op.drop_index("ix_users_role", table_name="users")
    op.drop_column("users", "role")
