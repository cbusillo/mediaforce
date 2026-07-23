"""Add encode schedule close deadlines.

Revision ID: 20260723_0016
Revises: 20260719_0015
Create Date: 2026-07-23 17:30:00
"""

import sqlalchemy as sa
# noinspection PyPackageRequirements
from alembic import op

revision = "20260723_0016"
down_revision = "20260719_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not _has_column("encode_jobs", "schedule_close_deadline_at"):
        op.add_column("encode_jobs", sa.Column("schedule_close_deadline_at", sa.Text(), nullable=True))


def downgrade() -> None:
    if _has_column("encode_jobs", "schedule_close_deadline_at"):
        op.drop_column("encode_jobs", "schedule_close_deadline_at")


def _has_column(table_name: str, column_name: str) -> bool:
    return any(
        str(column.get("name") or "") == column_name
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    )
