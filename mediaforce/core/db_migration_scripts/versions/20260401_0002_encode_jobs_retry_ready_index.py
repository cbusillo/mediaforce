"""Add encode queue retry-ready index.

Revision ID: 20260401_0002
Revises: 20260401_0001
Create Date: 2026-04-01 00:30:00
"""

import sqlalchemy as sa
# noinspection PyPackageRequirements
from alembic import op

revision = "20260401_0002"
down_revision = "20260401_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("idx_encode_jobs_status_retry_ready", "encode_jobs",
                    ["status", "retry_not_before", sa.text("created_at ASC")])


def downgrade() -> None:
    op.drop_index("idx_encode_jobs_status_retry_ready", table_name="encode_jobs")
