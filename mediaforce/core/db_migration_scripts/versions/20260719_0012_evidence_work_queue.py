"""Add bounded evidence work queue state.

Revision ID: 20260719_0012
Revises: 20260719_0011
Create Date: 2026-07-19 16:00:00
"""

import sqlalchemy as sa
# noinspection PyPackageRequirements
from alembic import op

revision = "20260719_0012"
down_revision = "20260719_0011"
branch_labels = None
depends_on = None

_WORK_COLUMNS = (
    ("work_batch_id", sa.Text()),
    ("work_status", sa.Text()),
    ("work_source_fingerprint", sa.Text()),
    ("leased_at", sa.Text()),
    ("lease_expires_at", sa.Text()),
    ("heartbeat_at", sa.Text()),
    ("worker_id", sa.Text()),
    ("process_pid", sa.Integer()),
)


def upgrade() -> None:
    if not _has_table("evidence_queue_state"):
        op.create_table(
            "evidence_queue_state",
            sa.Column("queue_name", sa.Text(), primary_key=True),
            sa.Column("batch_id", sa.Text(), nullable=True),
            sa.Column("status", sa.Text(), nullable=False, server_default="idle"),
            sa.Column("scope_json", sa.Text(), nullable=True),
            sa.Column("evidence_kinds_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("is_paused", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("cancel_requested", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("item_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("completed_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("cancelled_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.Text(), nullable=True),
            sa.Column("started_at", sa.Text(), nullable=True),
            sa.Column("finished_at", sa.Text(), nullable=True),
            sa.Column("updated_at", sa.Text(), nullable=False),
        )
    for column_name, column_type in _WORK_COLUMNS:
        if not _has_column("library_item_evidence_state", column_name):
            op.add_column(
                "library_item_evidence_state",
                sa.Column(column_name, column_type, nullable=True),
            )
    if not _has_index(
            "library_item_evidence_state",
            "idx_library_item_evidence_state_work_claim",
    ):
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


def downgrade() -> None:
    if _has_index(
            "library_item_evidence_state",
            "idx_library_item_evidence_state_work_claim",
    ):
        op.drop_index(
            "idx_library_item_evidence_state_work_claim",
            table_name="library_item_evidence_state",
        )
    for column_name, _column_type in reversed(_WORK_COLUMNS):
        if _has_column("library_item_evidence_state", column_name):
            op.drop_column("library_item_evidence_state", column_name)
    if _has_table("evidence_queue_state"):
        op.drop_table("evidence_queue_state")


def _has_table(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()


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
