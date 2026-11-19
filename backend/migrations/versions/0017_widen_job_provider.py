"""Let the provider column hold the provider's name.

`ai_jobs.provider` stores `type(provider).__name__` and was 16
characters. `DeckProvider` is twelve and fitted; renaming it to
`ManagedOllamaProvider` made it twenty-one, and every job began
failing on commit with

    StringDataRightTruncation: value too long for character varying(16)

after the work had already been done. The model answered, the tool
calls ran, and then the result could not be written -- so from the
app it looked like the AI had stopped responding, which is the most
misleading symptom it could have had.

Widened rather than shortened at the call site: the column records
which implementation produced a result, and a name truncated to fit is
a worse record than a wider column. 64 leaves room for names nobody
has thought of yet.

Revision ID: 0017_widen_job_provider
Revises: 0016_account_plaid_env
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_widen_job_provider"
down_revision = "0016_account_plaid_env"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("ai_jobs", "provider",
                    type_=sa.String(64), existing_type=sa.String(16),
                    existing_nullable=True)


def downgrade() -> None:
    # Truncates anything longer, which is why this is one-way in practice.
    op.alter_column("ai_jobs", "provider",
                    type_=sa.String(16), existing_type=sa.String(64),
                    existing_nullable=True)
