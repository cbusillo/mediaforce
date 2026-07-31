"""Record scan run terminal status.

Revision ID: 20260731_0020
Revises: 20260726_0019
Create Date: 2026-07-31 00:00:00
"""

import sqlalchemy as sa
# noinspection PyPackageRequirements
from alembic import op

revision = "20260731_0020"
down_revision = "20260726_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not _has_column("scan_runs", "status"):
        op.add_column("scan_runs", sa.Column("status", sa.Text(), nullable=False, server_default="running"))
    if not _has_column("scan_runs", "error"):
        op.add_column("scan_runs", sa.Column("error", sa.Text(), nullable=True))
    op.execute(sa.text(
        "UPDATE scan_runs SET status = 'completed' "
        "WHERE completed_at IS NOT NULL AND status = 'running'"
    ))


def downgrade() -> None:
    if _has_column("scan_runs", "error"):
        op.drop_column("scan_runs", "error")
    if _has_column("scan_runs", "status"):
        op.drop_column("scan_runs", "status")


def _has_column(table_name: str, column_name: str) -> bool:
    return any(
        str(column.get("name") or "") == column_name
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    )
