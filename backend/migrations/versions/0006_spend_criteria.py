"""Separate what a category is FOR from what gets CHARGED to it.

One `description` field was doing two jobs. "Put whatever is left over
here" says how money gets IN -- which the split rules already state
deterministically -- while "any fun spending over $40" says what comes
OUT. Only the second can decide where a charge belongs.

Note what this is NOT: an accuracy fix. Measured in
experiments/categorise-arms, a field carrying both sentences scored
identically to one carrying only the spending half (83%, same failures).

It is a labelling fix. Asked for a "description", users write the
funding intent, because that is what a category feels like it is for --
and then filing has nothing to go on. A field labelled "what gets
charged here" asks for the thing that works.

Revision ID: 0006_spend_criteria
Revises: 0005_user_ai_settings
"""
from alembic import op
import sqlalchemy as sa

revision = "0006_spend_criteria"
down_revision = "0005_user_ai_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "categories",
        sa.Column("spend_criteria", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("categories", "spend_criteria")
