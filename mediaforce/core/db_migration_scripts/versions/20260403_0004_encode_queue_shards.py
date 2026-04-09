"""Add encode queue parent and shard job fields.

Revision ID: 20260403_0004
Revises: 20260403_0003
Create Date: 2026-04-03 16:10:00
"""

import sqlalchemy as sa
# noinspection PyPackageRequirements
from alembic import op

revision = "20260403_0004"
down_revision = "20260403_0003"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(str(column.get("name")) == column_name for column in inspector.get_columns(table_name))


def _has_index(table_name: str, index_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(str(index.get("name")) == index_name for index in inspector.get_indexes(table_name))


def upgrade() -> None:
    if not _has_column("encode_jobs", "job_kind"):
        op.add_column("encode_jobs", sa.Column("job_kind", sa.Text(), nullable=False, server_default="single"))
    if not _has_column("encode_jobs", "parent_job_id"):
        op.add_column("encode_jobs", sa.Column("parent_job_id", sa.Text(), nullable=True))
    if not _has_column("encode_jobs", "manifest_indexes_json"):
        op.add_column("encode_jobs", sa.Column("manifest_indexes_json", sa.Text(), nullable=True))
    if not _has_index("encode_jobs", "idx_encode_jobs_kind_status_created"):
        op.create_index(
            "idx_encode_jobs_kind_status_created",
            "encode_jobs",
            ["job_kind", sa.text("status"), sa.text("created_at ASC")],
        )
    if not _has_index("encode_jobs", "idx_encode_jobs_parent_created"):
        op.create_index(
            "idx_encode_jobs_parent_created",
            "encode_jobs",
            ["parent_job_id", sa.text("created_at ASC")],
        )


def downgrade() -> None:
    op.drop_index("idx_encode_jobs_parent_created", table_name="encode_jobs")
    op.drop_index("idx_encode_jobs_kind_status_created", table_name="encode_jobs")
    op.drop_column("encode_jobs", "manifest_indexes_json")
    op.drop_column("encode_jobs", "parent_job_id")
    op.drop_column("encode_jobs", "job_kind")
