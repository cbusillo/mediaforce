"""Add quality-search shadow evaluations.

Revision ID: 20260724_0018
Revises: 20260724_0017
Create Date: 2026-07-24 16:45:00
"""

import sqlalchemy as sa
# noinspection PyPackageRequirements
from alembic import op

revision = "20260724_0018"
down_revision = "20260724_0017"
branch_labels = None
depends_on = None

TABLE_NAME = "quality_search_observations"
COLUMN_NAME = "shadow_json"


def upgrade() -> None:
    if COLUMN_NAME not in _column_names():
        op.add_column(TABLE_NAME, sa.Column(COLUMN_NAME, sa.Text(), nullable=True))


def downgrade() -> None:
    if COLUMN_NAME in _column_names():
        if op.get_bind().exec_driver_sql(
            f'SELECT 1 FROM "{TABLE_NAME}" WHERE "{COLUMN_NAME}" IS NOT NULL LIMIT 1'
        ).first() is not None:
            raise RuntimeError(
                "Cannot remove quality-search shadow payloads because they are part of immutable observation hashes"
            )
        op.drop_column(TABLE_NAME, COLUMN_NAME)


def _column_names() -> set[str]:
    return {
        str(column["name"])
        for column in sa.inspect(op.get_bind()).get_columns(TABLE_NAME)
    }
