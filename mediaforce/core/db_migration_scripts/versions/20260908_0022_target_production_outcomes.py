"""Retain immutable target-boundary production lineage without backfilling it."""

import sqlalchemy as sa
from alembic import op

revision = "20260908_0022"
down_revision = "20260822_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "target_lineage_json" not in {
        column["name"] for column in inspector.get_columns("staged_artifacts")
    }:
        op.add_column(
            "staged_artifacts",
            sa.Column("target_lineage_json", sa.Text(), nullable=True),
        )
    if not inspector.has_table("target_production_outcomes"):
        op.create_table(
            "target_production_outcomes",
            sa.Column("receipt_id", sa.Text(), primary_key=True),
            sa.Column("library_item_id", sa.Integer(), nullable=False),
            sa.Column("observation_id", sa.Text(), nullable=False),
            sa.Column("manifest_run_id", sa.Text(), nullable=False),
            sa.Column("item_index", sa.Integer(), nullable=False),
            sa.Column("encode_job_id", sa.Text(), nullable=False),
            sa.Column("promoted_content_fingerprint", sa.Text(), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=False),
            sa.Column("payload_sha256", sa.Text(), nullable=False),
            sa.Column("recorded_at", sa.Text(), nullable=False),
            sa.UniqueConstraint(
                "library_item_id",
                "manifest_run_id",
                "item_index",
                "encode_job_id",
                "promoted_content_fingerprint",
                name="uq_target_production_outcome",
            ),
        )
    if "idx_target_production_boundary" not in {
        index["name"] for index in inspector.get_indexes("target_production_outcomes")
    }:
        op.create_index(
            "idx_target_production_boundary",
            "target_production_outcomes",
            ["observation_id"],
        )
    for operation in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER IF NOT EXISTS target_production_outcomes_no_{operation.lower()} "
            f"BEFORE {operation} ON target_production_outcomes BEGIN "
            "SELECT RAISE(ABORT, 'target_production_outcomes is append-only'); END"
        )


def downgrade() -> None:
    for operation in ("update", "delete"):
        op.execute(f"DROP TRIGGER target_production_outcomes_no_{operation}")
    op.drop_table("target_production_outcomes")
    op.drop_column("staged_artifacts", "target_lineage_json")
