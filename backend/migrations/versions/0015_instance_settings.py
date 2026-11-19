"""Settings an owner can change without an SSH session.

Everything configurable lived in `.env`, which means changing the sync
interval or correcting a mistyped Plaid secret requires a shell on the
box, an editor, and a container restart. That is a reasonable ask of
the person who wrote the software and an unreasonable one of someone
who just installed it.

Stored here, overriding the environment. `.env` keeps working as the
baseline, so an existing instance behaves identically until somebody
actually changes something.

Secrets are encrypted with the same key already protecting Plaid
access tokens. Keeping them out of the database would buy nothing: the
database already holds the far more sensitive thing, and splitting
configuration across two places only makes the portal harder. The one
exception is TOKEN_ENCRYPTION_KEY itself, which stays in the
environment because it is the key that protects everything else and
cannot protect itself.

Revision ID: 0015_instance_settings
Revises: 0014_user_roles
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_instance_settings"
down_revision = "0014_user_roles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "instance_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("value_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_by", sa.String(320), nullable=True),
    )
    op.execute(
        "COMMENT ON TABLE instance_settings IS "
        "'Owner-editable configuration, overriding the environment. "
        "One row per setting. Secrets live in value_encrypted; "
        "everything else in value.'"
    )


def downgrade() -> None:
    op.drop_table("instance_settings")
