"""Initial Mediaforce schema.

Revision ID: 20260401_0001
Revises:
Create Date: 2026-04-01 00:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20260401_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scan_runs",
        sa.Column("scan_id", sa.Text(), nullable=False),
        sa.Column("started_at", sa.Text(), nullable=False),
        sa.Column("completed_at", sa.Text(), nullable=True),
        sa.Column("owner_pid", sa.Integer(), nullable=True),
        sa.Column("last_progress_at", sa.Text(), nullable=True),
        sa.Column("roots_json", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False, server_default="unknown"),
        sa.Column("prefixes_json", sa.Text(), nullable=True),
        sa.Column("file_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reprobed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unchanged_count", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("scan_id"),
    )
    op.create_table(
        "library_items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_path", sa.Text(), nullable=False),
        sa.Column("rel_path", sa.Text(), nullable=False),
        sa.Column("media_root", sa.Text(), nullable=False),
        sa.Column("parent_dir", sa.Text(), nullable=False),
        sa.Column("file_name", sa.Text(), nullable=False),
        sa.Column("container", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("mtime_ns", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("duration_seconds", sa.REAL(), nullable=True),
        sa.Column("video_codec", sa.Text(), nullable=True),
        sa.Column("video_bitrate", sa.Integer(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("pix_fmt", sa.Text(), nullable=True),
        sa.Column("audio_track_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("subtitle_track_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("english_audio_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("english_subtitle_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("default_audio_language", sa.Text(), nullable=True),
        sa.Column("default_subtitle_language", sa.Text(), nullable=True),
        sa.Column("audio_summary_json", sa.Text(), nullable=False),
        sa.Column("subtitle_summary_json", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="discovered"),
        sa.Column("priority_score", sa.REAL(), nullable=False, server_default="0"),
        sa.Column("recommendation", sa.Text(), nullable=True),
        sa.Column("recommendation_reason", sa.Text(), nullable=True),
        sa.Column("last_scan_id", sa.Text(), nullable=False),
        sa.Column("discovered_at", sa.Text(), nullable=False),
        sa.Column("last_seen_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_path"),
    )
    op.create_index("idx_library_items_rel_path", "library_items", ["rel_path"])
    op.create_index("idx_library_items_status_score", "library_items", ["status", sa.text("priority_score DESC")])
    op.create_table(
        "run_manifests",
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("output_path", sa.Text(), nullable=False),
        sa.Column("selection_json", sa.Text(), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_table(
        "calibration_jobs",
        sa.Column("job_id", sa.Text(), nullable=False),
        sa.Column("prefix", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("lane", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("host_json", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("policy_json", sa.Text(), nullable=False),
        sa.Column("sample_item_json", sa.Text(), nullable=False),
        sa.Column("seed_source", sa.Text(), nullable=True),
        sa.Column("seed_summary", sa.Text(), nullable=True),
        sa.Column("seed_prompt_version", sa.Text(), nullable=True),
        sa.Column("seed_raw_response", sa.Text(), nullable=True),
        sa.Column("seed_proposed_policy_json", sa.Text(), nullable=True),
        sa.Column("seed_applied_policy_json", sa.Text(), nullable=True),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("owner_pid", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("started_at", sa.Text(), nullable=True),
        sa.Column("finished_at", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("job_id"),
    )
    op.create_index("idx_calibration_jobs_prefix_created", "calibration_jobs", ["prefix", sa.text("created_at DESC")])
    op.create_index("idx_calibration_jobs_lane_status_created", "calibration_jobs",
                    ["lane", "status", sa.text("created_at ASC")])
    op.create_table(
        "encode_queue_state",
        sa.Column("queue_name", sa.Text(), nullable=False),
        sa.Column("is_paused", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stop_requested", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active_job_id", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("queue_name"),
    )
    op.create_table(
        "encode_jobs",
        sa.Column("job_id", sa.Text(), nullable=False),
        sa.Column("prefix", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("manifest_path", sa.Text(), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("saved_profile_path", sa.Text(), nullable=True),
        sa.Column("host_json", sa.Text(), nullable=False),
        sa.Column("last_host_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("process_pid", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("bypass_schedule", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("leased_at", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.Text(), nullable=True),
        sa.Column("heartbeat_at", sa.Text(), nullable=True),
        sa.Column("worker_id", sa.Text(), nullable=True),
        sa.Column("retry_not_before", sa.Text(), nullable=True),
        sa.Column("waiting_reason", sa.Text(), nullable=True),
        sa.Column("terminal_reason", sa.Text(), nullable=True),
        sa.Column("last_failure_kind", sa.Text(), nullable=True),
        sa.Column("last_failure_at", sa.Text(), nullable=True),
        sa.Column("host_cooldown_until", sa.Text(), nullable=True),
        sa.Column("progress_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("started_at", sa.Text(), nullable=True),
        sa.Column("finished_at", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("job_id"),
    )
    op.create_index("idx_encode_jobs_status_created", "encode_jobs", ["status", sa.text("created_at ASC")])
    op.create_index("idx_encode_jobs_prefix_created", "encode_jobs", ["prefix", sa.text("created_at DESC")])
    op.create_table(
        "tuning_sessions",
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("prefix", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("diagnosis", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Text(), nullable=True),
        sa.Column("evidence_checked_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("suggested_follow_up", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.Text(), nullable=True),
        sa.Column("proposed_policy_json", sa.Text(), nullable=True),
        sa.Column("applied_policy_json", sa.Text(), nullable=True),
        sa.Column("toolbelt_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("self_check_json", sa.Text(), nullable=True),
        sa.Column("raw_response", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("session_id"),
    )
    op.create_index("idx_tuning_sessions_prefix_created", "tuning_sessions", ["prefix", sa.text("created_at DESC")])
    op.create_table(
        "learning_artifacts",
        sa.Column("artifact_id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("prefix", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("artifact_path", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("tags_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["tuning_sessions.session_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("artifact_id"),
    )
    op.create_index("idx_learning_artifacts_prefix_updated", "learning_artifacts",
                    ["prefix", sa.text("updated_at DESC")])
    op.create_table(
        "staged_artifacts",
        sa.Column("library_item_id", sa.Integer(), nullable=False),
        sa.Column("manifest_run_id", sa.Text(), nullable=True),
        sa.Column("manifest_path", sa.Text(), nullable=True),
        sa.Column("item_index", sa.Integer(), nullable=True),
        sa.Column("source_fingerprint", sa.Text(), nullable=True),
        sa.Column("staging_path", sa.Text(), nullable=False),
        sa.Column("staging_size_bytes", sa.Integer(), nullable=True),
        sa.Column("staging_mtime_ns", sa.Integer(), nullable=True),
        sa.Column("staging_fingerprint", sa.Text(), nullable=True),
        sa.Column("chosen_crf", sa.REAL(), nullable=True),
        sa.Column("quality_metric", sa.Text(), nullable=True),
        sa.Column("quality_target", sa.REAL(), nullable=True),
        sa.Column("quality_score", sa.REAL(), nullable=True),
        sa.Column("encode_command_json", sa.Text(), nullable=True),
        sa.Column("audio_summary_json", sa.Text(), nullable=True),
        sa.Column("subtitle_summary_json", sa.Text(), nullable=True),
        sa.Column("validation_json", sa.Text(), nullable=True),
        sa.Column("staged_at", sa.Text(), nullable=True),
        sa.Column("validated_at", sa.Text(), nullable=True),
        sa.Column("promoted_at", sa.Text(), nullable=True),
        sa.Column("promoted_path", sa.Text(), nullable=True),
        sa.Column("archived_source_path", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["library_item_id"], ["library_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("library_item_id"),
    )
    op.create_table(
        "item_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("library_item_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("details_json", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["library_item_id"], ["library_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_item_events_item_time", "item_events", ["library_item_id", sa.text("created_at DESC")])


def downgrade() -> None:
    op.drop_index("idx_item_events_item_time", table_name="item_events")
    op.drop_table("item_events")
    op.drop_table("staged_artifacts")
    op.drop_index("idx_learning_artifacts_prefix_updated", table_name="learning_artifacts")
    op.drop_table("learning_artifacts")
    op.drop_index("idx_tuning_sessions_prefix_created", table_name="tuning_sessions")
    op.drop_table("tuning_sessions")
    op.drop_index("idx_encode_jobs_prefix_created", table_name="encode_jobs")
    op.drop_index("idx_encode_jobs_status_created", table_name="encode_jobs")
    op.drop_table("encode_jobs")
    op.drop_table("encode_queue_state")
    op.drop_index("idx_calibration_jobs_lane_status_created", table_name="calibration_jobs")
    op.drop_index("idx_calibration_jobs_prefix_created", table_name="calibration_jobs")
    op.drop_table("calibration_jobs")
    op.drop_table("run_manifests")
    op.drop_index("idx_library_items_status_score", table_name="library_items")
    op.drop_index("idx_library_items_rel_path", table_name="library_items")
    op.drop_table("library_items")
    op.drop_table("scan_runs")
