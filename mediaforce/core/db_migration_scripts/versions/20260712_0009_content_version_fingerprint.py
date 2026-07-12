"""Add a bounded content fingerprint for replacement detection.

Revision ID: 20260712_0009
Revises: 20260712_0008
Create Date: 2026-07-12 18:45:00
"""

import sqlalchemy as sa
# noinspection PyPackageRequirements
from alembic import op

revision = "20260712_0009"
down_revision = "20260712_0008"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(str(column.get("name")) == column_name for column in inspector.get_columns(table_name))


def upgrade() -> None:
    if not _has_column("library_items", "content_version_fingerprint"):
        op.add_column("library_items", sa.Column("content_version_fingerprint", sa.Text(), nullable=True))


def downgrade() -> None:
    if _has_column("library_items", "content_version_fingerprint"):
        op.drop_column("library_items", "content_version_fingerprint")
