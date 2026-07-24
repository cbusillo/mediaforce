"""Add append-only quality-search observations.

Revision ID: 20260724_0017
Revises: 20260723_0016
Create Date: 2026-07-24 12:00:00
"""

import sqlalchemy as sa
# noinspection PyPackageRequirements
from alembic import op

revision = "20260724_0017"
down_revision = "20260723_0016"
branch_labels = None
depends_on = None

TABLE_NAME = "quality_search_observations"
NO_UPDATE_TRIGGER = "quality_search_observations_no_update"
NO_DELETE_TRIGGER = "quality_search_observations_no_delete"
CORRECTION_TRIGGER = "quality_search_observations_validate_correction"


def upgrade() -> None:
    if not _has_table(TABLE_NAME):
        op.create_table(
            TABLE_NAME,
            sa.Column("observation_id", sa.Text(), primary_key=True),
            sa.Column("search_run_id", sa.Text(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("supersedes_observation_id", sa.Text(), nullable=True),
            sa.Column("supersession_reason", sa.Text(), nullable=True),
            sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("library_item_id", sa.Integer(), nullable=False),
            sa.Column("prefix", sa.Text(), nullable=False),
            sa.Column("source_rel_path", sa.Text(), nullable=False),
            sa.Column("source_fingerprint", sa.Text(), nullable=True),
            sa.Column("search_signature_id", sa.Text(), nullable=True),
            sa.Column("policy_hash", sa.Text(), nullable=True),
            sa.Column("search_objective", sa.Text(), nullable=False),
            sa.Column("quality_metric", sa.Text(), nullable=False),
            sa.Column("outcome_kind", sa.Text(), nullable=False),
            sa.Column("authority", sa.Text(), nullable=False),
            sa.Column("learning_eligible", sa.Integer(), nullable=False),
            sa.Column("exclusion_reason", sa.Text(), nullable=True),
            sa.Column("selected_crf", sa.REAL(), nullable=True),
            sa.Column("selected_target", sa.REAL(), nullable=True),
            sa.Column("selected_score", sa.REAL(), nullable=True),
            sa.Column("actual_output_bytes", sa.Integer(), nullable=True),
            sa.Column("candidate_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("search_duration_seconds", sa.REAL(), nullable=True),
            sa.Column("context_json", sa.Text(), nullable=False),
            sa.Column("bounds_json", sa.Text(), nullable=False),
            sa.Column("candidate_trace_json", sa.Text(), nullable=False),
            sa.Column("outcome_json", sa.Text(), nullable=False),
            sa.Column("timing_json", sa.Text(), nullable=False),
            sa.Column("provenance_json", sa.Text(), nullable=False),
            sa.Column("payload_sha256", sa.Text(), nullable=False),
            sa.Column("recorded_at", sa.Text(), nullable=False),
            sa.CheckConstraint("revision >= 0", name="ck_quality_search_observations_revision"),
            sa.CheckConstraint(
                "(supersedes_observation_id IS NULL AND revision = 0 AND supersession_reason IS NULL) "
                "OR (supersedes_observation_id IS NOT NULL AND revision > 0 AND supersession_reason IS NOT NULL)",
                name="ck_quality_search_observations_revision_chain",
            ),
            sa.CheckConstraint(
                "authority IN ('staged_backfill', 'runtime_native', 'correction')",
                name="ck_quality_search_observations_authority",
            ),
            sa.CheckConstraint(
                "outcome_kind IN ('selected', 'deterministic_search_failure', 'final_size_failure')",
                name="ck_quality_search_observations_outcome",
            ),
            sa.CheckConstraint(
                "learning_eligible IN (0, 1)",
                name="ck_quality_search_observations_eligible",
            ),
            sa.CheckConstraint(
                "(learning_eligible = 1 AND exclusion_reason IS NULL AND search_signature_id IS NOT NULL) "
                "OR (learning_eligible = 0 AND exclusion_reason IS NOT NULL)",
                name="ck_quality_search_observations_exclusion",
            ),
            sa.CheckConstraint(
                "selected_crf IS NULL OR (selected_crf >= 0 AND selected_crf <= 63)",
                name="ck_quality_search_observations_crf",
            ),
            sa.CheckConstraint(
                "outcome_kind != 'selected' OR "
                "(selected_crf IS NOT NULL AND selected_target IS NOT NULL AND selected_score IS NOT NULL)",
                name="ck_quality_search_observations_selected",
            ),
            sa.CheckConstraint(
                "candidate_count >= 0 AND (search_duration_seconds IS NULL OR search_duration_seconds >= 0)",
                name="ck_quality_search_observations_counts",
            ),
            sa.ForeignKeyConstraint(
                ["supersedes_observation_id"],
                [f"{TABLE_NAME}.observation_id"],
                ondelete="RESTRICT",
            ),
        )
    _create_indexes()
    _create_triggers()


def downgrade() -> None:
    for trigger_name in (CORRECTION_TRIGGER, NO_DELETE_TRIGGER, NO_UPDATE_TRIGGER):
        op.execute(f'DROP TRIGGER IF EXISTS "{trigger_name}"')
    if _has_table(TABLE_NAME):
        op.drop_table(TABLE_NAME)


def _create_indexes() -> None:
    indexes = {
        "uq_quality_search_observations_run_authority_revision": (
            True,
            ["search_run_id", "authority", "revision"],
            None,
        ),
        "uq_quality_search_observations_supersedes": (
            True,
            ["supersedes_observation_id"],
            sa.text("supersedes_observation_id IS NOT NULL"),
        ),
        "idx_quality_search_observations_lookup": (
            False,
            ["search_signature_id", "prefix", "recorded_at"],
            None,
        ),
        "idx_quality_search_observations_item_time": (
            False,
            ["library_item_id", "recorded_at"],
            None,
        ),
    }
    existing = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(TABLE_NAME)}
    for name, (unique, columns, where) in indexes.items():
        if name in existing:
            continue
        kwargs = {"sqlite_where": where} if where is not None else {}
        op.create_index(name, TABLE_NAME, columns, unique=unique, **kwargs)


def _create_triggers() -> None:
    if not _has_trigger(NO_UPDATE_TRIGGER):
        op.execute(
            f"""
            CREATE TRIGGER "{NO_UPDATE_TRIGGER}"
            BEFORE UPDATE ON "{TABLE_NAME}"
            BEGIN
                SELECT RAISE(ABORT, 'quality_search_observations is append-only');
            END
            """
        )
    if not _has_trigger(NO_DELETE_TRIGGER):
        op.execute(
            f"""
            CREATE TRIGGER "{NO_DELETE_TRIGGER}"
            BEFORE DELETE ON "{TABLE_NAME}"
            BEGIN
                SELECT RAISE(ABORT, 'quality_search_observations is append-only');
            END
            """
        )
    if not _has_trigger(CORRECTION_TRIGGER):
        op.execute(
            f"""
            CREATE TRIGGER "{CORRECTION_TRIGGER}"
            BEFORE INSERT ON "{TABLE_NAME}"
            WHEN NEW.supersedes_observation_id IS NOT NULL
             AND NOT EXISTS (
                 SELECT 1
                 FROM "{TABLE_NAME}" AS previous
                 WHERE previous.observation_id = NEW.supersedes_observation_id
                   AND previous.search_run_id = NEW.search_run_id
                   AND NEW.revision = previous.revision + 1
             )
            BEGIN
                SELECT RAISE(ABORT, 'quality-search correction must extend the same run by one revision');
            END
            """
        )


def _has_table(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def _has_trigger(trigger_name: str) -> bool:
    row = op.get_bind().exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = ?",
        (trigger_name,),
    ).first()
    return row is not None
