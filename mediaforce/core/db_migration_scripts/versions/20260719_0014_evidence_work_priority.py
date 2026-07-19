"""Add evidence-work priority and operator reason.

Revision ID: 20260719_0014
Revises: 20260719_0013
Create Date: 2026-07-19 19:30:00
"""

import sqlalchemy as sa
# noinspection PyPackageRequirements
from alembic import op

revision = "20260719_0014"
down_revision = "20260719_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not _has_column("library_item_evidence_state", "work_priority"):
        op.add_column(
            "library_item_evidence_state",
            sa.Column("work_priority", sa.Integer(), nullable=False, server_default="100"),
        )
    if not _has_column("library_item_evidence_state", "work_reason"):
        op.add_column(
            "library_item_evidence_state",
            sa.Column("work_reason", sa.Text(), nullable=True),
        )
    if _has_index(
            "library_item_evidence_state",
            "idx_library_item_evidence_state_work_claim",
    ):
        op.drop_index(
            "idx_library_item_evidence_state_work_claim",
            table_name="library_item_evidence_state",
        )
    op.create_index(
        "idx_library_item_evidence_state_work_claim",
        "library_item_evidence_state",
        [
            "work_batch_id",
            "work_status",
            "retry_not_before",
            "work_priority",
            "evidence_kind",
            "library_item_id",
        ],
    )


def downgrade() -> None:
    if _has_index(
            "library_item_evidence_state",
            "idx_library_item_evidence_state_work_claim",
    ):
        op.drop_index(
            "idx_library_item_evidence_state_work_claim",
            table_name="library_item_evidence_state",
        )
    op.create_index(
        "idx_library_item_evidence_state_work_claim",
        "library_item_evidence_state",
        [
            "work_batch_id",
            "work_status",
            "retry_not_before",
            "evidence_kind",
            "library_item_id",
        ],
    )
    if _has_column("library_item_evidence_state", "work_reason"):
        op.drop_column("library_item_evidence_state", "work_reason")
    if _has_column("library_item_evidence_state", "work_priority"):
        op.drop_column("library_item_evidence_state", "work_priority")


def _has_column(table_name: str, column_name: str) -> bool:
    return any(
        str(column.get("name") or "") == column_name
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    )


def _has_index(table_name: str, index_name: str) -> bool:
    return any(
        str(index.get("name") or "") == index_name
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
    )
