"""Add append-only content-intent boundary observations.

Revision ID: 20260726_0019
Revises: 20260724_0018
Create Date: 2026-07-26 21:00:00
"""

import sqlalchemy as sa
# noinspection PyPackageRequirements
from alembic import op

revision = "20260726_0019"
down_revision = "20260724_0018"
branch_labels = None
depends_on = None

TABLE_NAME = "content_intent_boundary_observations"
NO_UPDATE_TRIGGER = "content_intent_boundary_observations_no_update"
NO_DELETE_TRIGGER = "content_intent_boundary_observations_no_delete"
CORRECTION_TRIGGER = "content_intent_boundary_observations_validate_correction"


def upgrade() -> None:
    if not _has_table(TABLE_NAME):
        op.create_table(
            TABLE_NAME,
            sa.Column("observation_id", sa.Text(), primary_key=True),
            sa.Column("series_id", sa.Text(), nullable=False),
            sa.Column("boundary_group_id", sa.Text(), nullable=False),
            sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("supersedes_observation_id", sa.Text(), nullable=True),
            sa.Column("supersession_reason", sa.Text(), nullable=True),
            sa.Column("authority", sa.Text(), nullable=False),
            sa.Column("disposition", sa.Text(), nullable=False, server_default="active"),
            sa.Column("personalization_eligible", sa.Integer(), nullable=False),
            sa.Column("exclusion_reason", sa.Text(), nullable=True),
            sa.Column("library_item_id", sa.Integer(), nullable=False),
            sa.Column("prefix", sa.Text(), nullable=False),
            sa.Column("source_rel_path", sa.Text(), nullable=False),
            sa.Column("source_id", sa.Text(), nullable=False),
            sa.Column("source_fingerprint", sa.Text(), nullable=True),
            sa.Column("content_fingerprint_kind", sa.Text(), nullable=False),
            sa.Column("content_fingerprint", sa.Text(), nullable=False),
            sa.Column("content_id", sa.Text(), nullable=False),
            sa.Column("content_profile_id", sa.Text(), nullable=False),
            sa.Column("content_traits_json", sa.Text(), nullable=False),
            sa.Column("intent_semantic_id", sa.Text(), nullable=False),
            sa.Column("intent_snapshot_id", sa.Text(), nullable=False),
            sa.Column("intent_level", sa.Text(), nullable=False),
            sa.Column("compatibility_key", sa.Text(), nullable=False),
            sa.Column("compatibility_json", sa.Text(), nullable=False),
            sa.Column("policy_hash", sa.Text(), nullable=False),
            sa.Column("source_event_kind", sa.Text(), nullable=False),
            sa.Column("source_event_id", sa.Text(), nullable=False),
            sa.Column("job_id", sa.Text(), nullable=False),
            sa.Column("artifact_fingerprint", sa.Text(), nullable=False),
            sa.Column("source_evidence_ids_json", sa.Text(), nullable=False),
            sa.Column("observation_kind", sa.Text(), nullable=False),
            sa.Column("verdict", sa.Text(), nullable=False),
            sa.Column("boundary_kind", sa.Text(), nullable=False),
            sa.Column("authoritative_anchor_bytes", sa.Integer(), nullable=False),
            sa.Column("boundary_size_bytes", sa.Integer(), nullable=False),
            sa.Column("actual_output_bytes", sa.Integer(), nullable=True),
            sa.Column("sampled_clip_bytes", sa.Integer(), nullable=True),
            sa.Column("duration_seconds", sa.REAL(), nullable=False),
            sa.Column("boundary_bitrate_bps", sa.Integer(), nullable=False),
            sa.Column("direction", sa.Text(), nullable=False),
            sa.Column("quality_metric", sa.Text(), nullable=False),
            sa.Column("quality_target", sa.REAL(), nullable=False),
            sa.Column("minimum_quality_score", sa.REAL(), nullable=False),
            sa.Column("measured_quality_score", sa.REAL(), nullable=False),
            sa.Column("quality_floor_met", sa.Integer(), nullable=False),
            sa.Column("assessment_json", sa.Text(), nullable=False),
            sa.Column("provenance_json", sa.Text(), nullable=False),
            sa.Column("payload_sha256", sa.Text(), nullable=False),
            sa.Column("recorded_at", sa.Text(), nullable=False),
            sa.CheckConstraint(
                "schema_version = 1",
                name="ck_content_intent_boundary_schema_version",
            ),
            sa.CheckConstraint("revision >= 0", name="ck_content_intent_boundary_revision"),
            sa.CheckConstraint(
                "(revision = 0 AND supersedes_observation_id IS NULL AND supersession_reason IS NULL "
                "AND authority = 'runtime_native') OR "
                "(revision > 0 AND supersedes_observation_id IS NOT NULL "
                "AND length(trim(supersession_reason)) > 0 AND authority = 'correction')",
                name="ck_content_intent_boundary_revision_chain",
            ),
            sa.CheckConstraint(
                "disposition IN ('active', 'withdrawn')",
                name="ck_content_intent_boundary_disposition",
            ),
            sa.CheckConstraint(
                "personalization_eligible IN (0, 1)",
                name="ck_content_intent_boundary_eligible",
            ),
            sa.CheckConstraint(
                "(personalization_eligible = 1 AND disposition = 'active' AND exclusion_reason IS NULL) "
                "OR (personalization_eligible = 0 AND exclusion_reason IS NOT NULL)",
                name="ck_content_intent_boundary_exclusion",
            ),
            sa.CheckConstraint(
                "disposition != 'withdrawn' OR (revision > 0 AND personalization_eligible = 0)",
                name="ck_content_intent_boundary_withdrawal",
            ),
            sa.CheckConstraint(
                "intent_level IN ('reference', 'transparent', 'balanced', 'perceptual_floor')",
                name="ck_content_intent_boundary_intent",
            ),
            sa.CheckConstraint(
                "content_fingerprint_kind = 'mediaforce_content_version_v1'",
                name="ck_content_intent_boundary_fingerprint_kind",
            ),
            sa.CheckConstraint(
                "source_event_kind IN ('post_test_review')",
                name="ck_content_intent_boundary_event",
            ),
            sa.CheckConstraint(
                "(observation_kind = 'visual_approval' AND verdict = 'acceptable' "
                "AND boundary_kind = 'upper_bound') OR "
                "(observation_kind = 'visual_rejection' AND verdict = 'unacceptable' "
                "AND boundary_kind = 'lower_bound')",
                name="ck_content_intent_boundary_verdict",
            ),
            sa.CheckConstraint(
                "authoritative_anchor_bytes > 0 AND boundary_size_bytes > 0 "
                "AND (actual_output_bytes IS NULL OR actual_output_bytes > 0) "
                "AND (sampled_clip_bytes IS NULL OR sampled_clip_bytes > 0) "
                "AND duration_seconds > 0 AND boundary_bitrate_bps > 0",
                name="ck_content_intent_boundary_measurements",
            ),
            sa.CheckConstraint(
                "(direction = 'smaller' AND boundary_size_bytes < authoritative_anchor_bytes) "
                "OR (direction = 'same' AND boundary_size_bytes = authoritative_anchor_bytes) "
                "OR (direction = 'larger' AND boundary_size_bytes > authoritative_anchor_bytes)",
                name="ck_content_intent_boundary_direction",
            ),
            sa.CheckConstraint(
                "quality_target >= 0 AND minimum_quality_score >= 0 "
                "AND minimum_quality_score <= quality_target AND measured_quality_score >= 0",
                name="ck_content_intent_boundary_quality",
            ),
            sa.CheckConstraint(
                "quality_floor_met IN (0, 1) AND "
                "((quality_floor_met = 1 AND measured_quality_score >= minimum_quality_score) "
                "OR (quality_floor_met = 0 AND measured_quality_score < minimum_quality_score))",
                name="ck_content_intent_boundary_quality_floor",
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
    if _has_table(TABLE_NAME) and op.get_bind().exec_driver_sql(
        f'SELECT 1 FROM "{TABLE_NAME}" LIMIT 1'
    ).first() is not None:
        raise RuntimeError("Cannot remove immutable content-intent boundary observations")
    for trigger_name in (CORRECTION_TRIGGER, NO_DELETE_TRIGGER, NO_UPDATE_TRIGGER):
        op.execute(f'DROP TRIGGER IF EXISTS "{trigger_name}"')
    if _has_table(TABLE_NAME):
        op.drop_table(TABLE_NAME)


def _create_indexes() -> None:
    indexes = {
        "uq_content_intent_boundary_series_revision": (
            True,
            ["series_id", "revision"],
            None,
        ),
        "uq_content_intent_boundary_supersedes": (
            True,
            ["supersedes_observation_id"],
            sa.text("supersedes_observation_id IS NOT NULL"),
        ),
        "idx_content_intent_boundary_group_time": (
            False,
            ["boundary_group_id", sa.text("recorded_at DESC")],
            None,
        ),
        "idx_content_intent_boundary_compatibility_time": (
            False,
            ["intent_semantic_id", "compatibility_key", sa.text("recorded_at DESC")],
            None,
        ),
        "idx_content_intent_boundary_model_time": (
            False,
            [
                "content_profile_id",
                "intent_semantic_id",
                "compatibility_key",
                sa.text("recorded_at DESC"),
            ],
            None,
        ),
        "idx_content_intent_boundary_item_time": (
            False,
            ["library_item_id", sa.text("recorded_at DESC")],
            None,
        ),
        "idx_content_intent_boundary_job_time": (
            False,
            ["job_id", sa.text("recorded_at DESC")],
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
                SELECT RAISE(ABORT, 'content_intent_boundary_observations is append-only');
            END
            """
        )
    if not _has_trigger(NO_DELETE_TRIGGER):
        op.execute(
            f"""
            CREATE TRIGGER "{NO_DELETE_TRIGGER}"
            BEFORE DELETE ON "{TABLE_NAME}"
            BEGIN
                SELECT RAISE(ABORT, 'content_intent_boundary_observations is append-only');
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
                   AND previous.series_id = NEW.series_id
                   AND NEW.revision = previous.revision + 1
                   AND previous.disposition = 'active'
                   AND previous.schema_version = NEW.schema_version
                   AND previous.boundary_group_id = NEW.boundary_group_id
                   AND previous.library_item_id = NEW.library_item_id
                   AND previous.prefix = NEW.prefix
                   AND previous.source_rel_path = NEW.source_rel_path
                   AND previous.source_id = NEW.source_id
                   AND ifnull(previous.source_fingerprint, '') = ifnull(NEW.source_fingerprint, '')
                   AND previous.content_fingerprint_kind = NEW.content_fingerprint_kind
                   AND previous.content_fingerprint = NEW.content_fingerprint
                   AND previous.content_id = NEW.content_id
                   AND previous.content_profile_id = NEW.content_profile_id
                   AND previous.content_traits_json = NEW.content_traits_json
                   AND previous.intent_semantic_id = NEW.intent_semantic_id
                   AND previous.intent_snapshot_id = NEW.intent_snapshot_id
                   AND previous.intent_level = NEW.intent_level
                   AND previous.compatibility_key = NEW.compatibility_key
                   AND previous.compatibility_json = NEW.compatibility_json
                   AND previous.policy_hash = NEW.policy_hash
                   AND previous.source_event_kind = NEW.source_event_kind
                   AND previous.source_event_id = NEW.source_event_id
                   AND previous.job_id = NEW.job_id
                   AND previous.artifact_fingerprint = NEW.artifact_fingerprint
                   AND previous.source_evidence_ids_json = NEW.source_evidence_ids_json
                   AND previous.authoritative_anchor_bytes = NEW.authoritative_anchor_bytes
                   AND previous.boundary_size_bytes = NEW.boundary_size_bytes
                   AND ifnull(previous.actual_output_bytes, -1) = ifnull(NEW.actual_output_bytes, -1)
                   AND ifnull(previous.sampled_clip_bytes, -1) = ifnull(NEW.sampled_clip_bytes, -1)
                   AND previous.duration_seconds = NEW.duration_seconds
                   AND previous.boundary_bitrate_bps = NEW.boundary_bitrate_bps
                   AND previous.direction = NEW.direction
                   AND previous.quality_metric = NEW.quality_metric
                   AND previous.quality_target = NEW.quality_target
                   AND previous.minimum_quality_score = NEW.minimum_quality_score
                   AND previous.measured_quality_score = NEW.measured_quality_score
                   AND previous.quality_floor_met = NEW.quality_floor_met
                   AND json_extract(previous.assessment_json, '$.measurement_basis')
                       = json_extract(NEW.assessment_json, '$.measurement_basis')
                   AND json_extract(previous.assessment_json, '$.predicted_total_size_bytes')
                       = json_extract(NEW.assessment_json, '$.predicted_total_size_bytes')
                   AND ifnull(json_extract(previous.assessment_json, '$.predicted_video_size_bytes'), -1)
                       = ifnull(json_extract(NEW.assessment_json, '$.predicted_video_size_bytes'), -1)
                   AND ifnull(json_extract(previous.assessment_json, '$.sampled_clip_bytes'), -1)
                       = ifnull(json_extract(NEW.assessment_json, '$.sampled_clip_bytes'), -1)
                   AND ifnull(json_extract(previous.assessment_json, '$.review_artifact_size_bytes'), -1)
                       = ifnull(json_extract(NEW.assessment_json, '$.review_artifact_size_bytes'), -1)
                   AND ifnull(json_extract(previous.assessment_json, '$.chosen_crf'), -1)
                       = ifnull(json_extract(NEW.assessment_json, '$.chosen_crf'), -1)
                   AND json_extract(previous.assessment_json, '$.quality_floor_met')
                       = json_extract(NEW.assessment_json, '$.quality_floor_met')
                   AND json_extract(previous.assessment_json, '$.content_traits')
                       = json_extract(NEW.assessment_json, '$.content_traits')
                   AND ifnull(json_extract(previous.assessment_json, '$.content_fingerprint_evidence_id'), '')
                       = ifnull(json_extract(NEW.assessment_json, '$.content_fingerprint_evidence_id'), '')
                   AND json_extract(previous.assessment_json, '$.evidence_ids')
                       = json_extract(NEW.assessment_json, '$.evidence_ids')
                   AND json_extract(previous.provenance_json, '$.schema_version')
                       = json_extract(NEW.provenance_json, '$.schema_version')
                   AND json_extract(previous.provenance_json, '$.source')
                       = json_extract(NEW.provenance_json, '$.source')
                   AND json_extract(previous.provenance_json, '$.calibration_action')
                       = json_extract(NEW.provenance_json, '$.calibration_action')
                   AND json_extract(previous.provenance_json, '$.sample_job_id')
                       = json_extract(NEW.provenance_json, '$.sample_job_id')
                   AND json_extract(previous.provenance_json, '$.policy_hash')
                       = json_extract(NEW.provenance_json, '$.policy_hash')
                   AND json_extract(previous.provenance_json, '$.intent_snapshot_id')
                       = json_extract(NEW.provenance_json, '$.intent_snapshot_id')
                   AND json_extract(previous.provenance_json, '$.content_fingerprint_kind')
                       = json_extract(NEW.provenance_json, '$.content_fingerprint_kind')
                   AND json_extract(previous.provenance_json, '$.artifact_fingerprint')
                       = json_extract(NEW.provenance_json, '$.artifact_fingerprint')
                   AND julianday(NEW.recorded_at) > julianday(previous.recorded_at)
             )
            BEGIN
                SELECT RAISE(ABORT, 'content-intent correction must extend one immutable series by one revision');
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
