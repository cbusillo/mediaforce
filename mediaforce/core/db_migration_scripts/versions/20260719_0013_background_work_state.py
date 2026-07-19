"""Add durable catalog and evidence background-work controls.

Revision ID: 20260719_0013
Revises: 20260719_0012
Create Date: 2026-07-19 18:00:00
"""

import sqlalchemy as sa
# noinspection PyPackageRequirements
from alembic import op

revision = "20260719_0013"
down_revision = "20260719_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not _has_table("background_work_state"):
        op.create_table(
            "background_work_state",
            sa.Column("work_area", sa.Text(), primary_key=True),
            sa.Column("is_paused", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("updated_at", sa.Text(), nullable=False),
        )


def downgrade() -> None:
    if _has_table("background_work_state"):
        op.drop_table("background_work_state")


def _has_table(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()
