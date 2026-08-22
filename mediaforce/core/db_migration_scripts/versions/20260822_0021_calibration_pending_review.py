"""Index durable calibration review decisions.

Revision ID: 20260822_0021
Revises: 20260731_0020
Create Date: 2026-08-22 00:00:00
"""

import sqlalchemy as sa
# noinspection PyPackageRequirements
from alembic import op

revision = "20260822_0021"
down_revision = "20260731_0020"
branch_labels = None
depends_on = None

INDEX_NAME = "idx_calibration_jobs_lane_status_finished"


def upgrade() -> None:
    if not _has_index("calibration_jobs", INDEX_NAME):
        op.create_index(
            INDEX_NAME,
            "calibration_jobs",
            ["lane", "status", sa.text("finished_at DESC")],
        )
    op.execute(
        sa.text(
            "WITH ranked_jobs AS ("
            "SELECT job_id, "
            "ROW_NUMBER() OVER ("
            "PARTITION BY prefix ORDER BY created_at DESC, rowid DESC"
            ") AS job_rank "
            "FROM calibration_jobs "
            "WHERE lane = 'sample' AND status = 'completed'"
            ") "
            "UPDATE calibration_jobs SET status = 'pending_review' "
            "WHERE job_id IN ("
            "SELECT job_id FROM ranked_jobs "
            "WHERE job_rank = 1"
            ")"
        )
    )


def downgrade() -> None:
    op.execute(
        "UPDATE calibration_jobs SET status = 'completed' "
        "WHERE status IN ('pending_review', 'superseded')"
    )
    if _has_index("calibration_jobs", INDEX_NAME):
        op.drop_index(INDEX_NAME, table_name="calibration_jobs")


def _has_index(table_name: str, index_name: str) -> bool:
    return any(
        str(index.get("name") or "") == index_name
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
    )
