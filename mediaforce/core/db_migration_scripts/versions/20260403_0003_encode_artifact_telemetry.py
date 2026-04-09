"""Add richer staged artifact encode telemetry.

Revision ID: 20260403_0003
Revises: 20260401_0002
Create Date: 2026-04-03 13:35:00
"""

import sqlalchemy as sa
# noinspection PyPackageRequirements
from alembic import op

revision = "20260403_0003"
down_revision = "20260401_0002"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(str(column.get("name")) == column_name for column in inspector.get_columns(table_name))


def upgrade() -> None:
    columns = [
        ("encode_origin", sa.Text()),
        ("encode_job_id", sa.Text()),
        ("encode_worker_id", sa.Text()),
        ("encode_host_key", sa.Text()),
        ("encode_host_label", sa.Text()),
        ("encode_host_mode", sa.Text()),
        ("encode_media_access", sa.Text()),
        ("source_path", sa.Text()),
        ("source_rel_path", sa.Text()),
        ("source_size_bytes", sa.Integer()),
        ("source_duration_seconds", sa.REAL()),
        ("source_video_codec", sa.Text()),
        ("encode_started_at", sa.Text()),
        ("encode_completed_at", sa.Text()),
        ("encode_duration_seconds", sa.REAL()),
        ("bytes_saved", sa.Integer()),
        ("size_ratio", sa.REAL()),
    ]
    for column_name, column_type in columns:
        if _has_column("staged_artifacts", column_name):
            continue
        op.add_column("staged_artifacts", sa.Column(column_name, column_type, nullable=True))


def downgrade() -> None:
    op.drop_column("staged_artifacts", "size_ratio")
    op.drop_column("staged_artifacts", "bytes_saved")
    op.drop_column("staged_artifacts", "encode_duration_seconds")
    op.drop_column("staged_artifacts", "encode_completed_at")
    op.drop_column("staged_artifacts", "encode_started_at")
    op.drop_column("staged_artifacts", "source_video_codec")
    op.drop_column("staged_artifacts", "source_duration_seconds")
    op.drop_column("staged_artifacts", "source_size_bytes")
    op.drop_column("staged_artifacts", "source_rel_path")
    op.drop_column("staged_artifacts", "source_path")
    op.drop_column("staged_artifacts", "encode_media_access")
    op.drop_column("staged_artifacts", "encode_host_mode")
    op.drop_column("staged_artifacts", "encode_host_label")
    op.drop_column("staged_artifacts", "encode_host_key")
    op.drop_column("staged_artifacts", "encode_worker_id")
    op.drop_column("staged_artifacts", "encode_job_id")
    op.drop_column("staged_artifacts", "encode_origin")
