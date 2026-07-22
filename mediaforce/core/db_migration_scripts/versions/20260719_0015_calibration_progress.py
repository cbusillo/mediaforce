"""Add calibration heartbeat and progress telemetry.

Revision ID: 20260719_0015
Revises: 20260719_0014
Create Date: 2026-07-19 23:45:00
"""

import sqlalchemy as sa
# noinspection PyPackageRequirements
from alembic import op

revision = "20260719_0015"
down_revision = "20260719_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not _has_column("calibration_jobs", "heartbeat_at"):
        op.add_column("calibration_jobs", sa.Column("heartbeat_at", sa.Text(), nullable=True))
    if not _has_column("calibration_jobs", "progress_json"):
        op.add_column("calibration_jobs", sa.Column("progress_json", sa.Text(), nullable=True))


def downgrade() -> None:
    if _has_column("calibration_jobs", "progress_json"):
        op.drop_column("calibration_jobs", "progress_json")
    if _has_column("calibration_jobs", "heartbeat_at"):
        op.drop_column("calibration_jobs", "heartbeat_at")


def _has_column(table_name: str, column_name: str) -> bool:
    return any(
        str(column.get("name") or "") == column_name
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    )
