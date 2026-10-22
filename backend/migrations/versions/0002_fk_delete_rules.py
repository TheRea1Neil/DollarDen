"""Give three foreign keys explicit delete rules.

Deleting a user cascades to categories and split_plans, but allocations,
split_rules and paychecks referenced them with NO ACTION, so the cascade hit
a foreign-key violation and the delete failed outright.

allocations.category_id and split_rules.category_id get CASCADE. Categories
are never deleted through the API -- they are archived -- so the
archive-not-delete rule still holds where it is actually enforced.

paychecks.split_plan_id gets SET NULL: a paycheck is a historical fact and
should survive its plan being removed, even if it can no longer name it.

Revision ID: 0002_fk_delete_rules
Revises: 7a7843383e7b
"""
from __future__ import annotations

from alembic import op

revision = "0002_fk_delete_rules"
down_revision = "7a7843383e7b"
branch_labels = None
depends_on = None

CHANGES = [
    ("allocations", "fk_allocations_category_id_categories", "category_id", "categories", "CASCADE"),
    ("split_rules", "fk_split_rules_category_id_categories", "category_id", "categories", "CASCADE"),
    ("paychecks", "fk_paychecks_split_plan_id_split_plans", "split_plan_id", "split_plans", "SET NULL"),
]


def upgrade() -> None:
    for table, name, col, ref, rule in CHANGES:
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(name, table, ref, [col], ["id"], ondelete=rule)


def downgrade() -> None:
    for table, name, col, ref, _ in CHANGES:
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(name, table, ref, [col], ["id"])
