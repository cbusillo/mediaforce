"""Add persisted cadence evidence to library items.

Revision ID: 20260711_0005
Revises: 20260403_0004
Create Date: 2026-07-11 18:00:00
"""

import sqlalchemy as sa
# noinspection PyPackageRequirements
from alembic import op

revision = "20260711_0005"
down_revision = "20260403_0004"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(str(column.get("name")) == column_name for column in inspector.get_columns(table_name))


def upgrade() -> None:
    if not _has_column("library_items", "cadence_summary_json"):
        op.add_column("library_items", sa.Column("cadence_summary_json", sa.Text(), nullable=True))


def downgrade() -> None:
    if _has_column("library_items", "cadence_summary_json"):
        op.drop_column("library_items", "cadence_summary_json")
