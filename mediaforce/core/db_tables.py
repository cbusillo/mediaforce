from sqlalchemy import Column
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import MetaData
from sqlalchemy import REAL
from sqlalchemy import String
from sqlalchemy import Table
from sqlalchemy import Text

metadata = MetaData()

alembic_version = Table(
    "alembic_version",
    metadata,
    Column("version_num", String(32), primary_key=True),
)

scan_runs = Table(
    "scan_runs",
    metadata,
    Column("scan_id", Text, primary_key=True),
    Column("started_at", Text, nullable=False),
    Column("completed_at", Text),
    Column("owner_pid", Integer),
    Column("last_progress_at", Text),
    Column("roots_json", Text, nullable=False),
    Column("scope", Text, nullable=False, server_default="unknown"),
    Column("prefixes_json", Text),
    Column("file_count", Integer, nullable=False, server_default="0"),
    Column("reprobed_count", Integer, nullable=False, server_default="0"),
    Column("unchanged_count", Integer, nullable=False, server_default="0"),
)

library_items = Table(
    "library_items",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("source_path", Text, nullable=False, unique=True),
    Column("rel_path", Text, nullable=False),
    Column("media_root", Text, nullable=False),
    Column("parent_dir", Text, nullable=False),
    Column("file_name", Text, nullable=False),
    Column("container", Text, nullable=False),
    Column("size_bytes", Integer, nullable=False),
    Column("mtime_ns", Integer, nullable=False),
    Column("fingerprint", Text, nullable=False),
    Column("duration_seconds", REAL),
    Column("video_codec", Text),
    Column("video_bitrate", Integer),
    Column("width", Integer),
    Column("height", Integer),
    Column("pix_fmt", Text),
    Column("audio_track_count", Integer, nullable=False, server_default="0"),
    Column("subtitle_track_count", Integer, nullable=False, server_default="0"),
    Column("english_audio_count", Integer, nullable=False, server_default="0"),
    Column("english_subtitle_count", Integer, nullable=False, server_default="0"),
    Column("default_audio_language", Text),
    Column("default_subtitle_language", Text),
    Column("audio_summary_json", Text, nullable=False),
    Column("subtitle_summary_json", Text, nullable=False),
    Column("attachment_summary_json", Text),
    Column("cadence_summary_json", Text),
    Column("media_fingerprint_json", Text),
    Column("content_version_changed_at", Text),
    Column("content_version_fingerprint", Text),
    Column("status", Text, nullable=False, server_default="discovered"),
    Column("priority_score", REAL, nullable=False, server_default="0"),
    Column("recommendation", Text),
    Column("recommendation_reason", Text),
    Column("last_scan_id", Text, nullable=False),
    Column("discovered_at", Text, nullable=False),
    Column("last_seen_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
)
Index("idx_library_items_rel_path", library_items.c.rel_path)
Index("idx_library_items_status_score", library_items.c.status, library_items.c.priority_score.desc())

library_item_evidence_state = Table(
    "library_item_evidence_state",
    metadata,
    Column(
        "library_item_id",
        Integer,
        ForeignKey("library_items.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("evidence_kind", Text, primary_key=True),
    Column("state", Text, nullable=False),
    Column("reason", Text),
    Column("summary_sha256", Text),
    Column("source_fingerprint", Text),
    Column("summary_schema_version", Integer),
    Column("analyzer_name", Text),
    Column("analyzer_version", Text),
    Column("analyzer_runtime_version", Text),
    Column("policy_hash", Text, nullable=False),
    Column("decision_status", Text),
    Column("attempt_count", Integer, nullable=False, server_default="0"),
    Column("retry_not_before", Text),
    Column("last_attempt_at", Text),
    Column("last_error", Text),
    Column("work_batch_id", Text),
    Column("work_status", Text),
    Column("work_priority", Integer, nullable=False, server_default="100"),
    Column("work_reason", Text),
    Column("work_source_fingerprint", Text),
    Column("leased_at", Text),
    Column("lease_expires_at", Text),
    Column("heartbeat_at", Text),
    Column("worker_id", Text),
    Column("process_pid", Integer),
    Column("updated_at", Text, nullable=False),
)
Index(
    "idx_library_item_evidence_state_kind_state",
    library_item_evidence_state.c.evidence_kind,
    library_item_evidence_state.c.state,
    library_item_evidence_state.c.reason,
)
Index(
    "idx_library_item_evidence_state_work_ready",
    library_item_evidence_state.c.state,
    library_item_evidence_state.c.retry_not_before,
    library_item_evidence_state.c.evidence_kind,
)
Index(
    "idx_library_item_evidence_state_work_claim",
    library_item_evidence_state.c.work_batch_id,
    library_item_evidence_state.c.work_status,
    library_item_evidence_state.c.retry_not_before,
    library_item_evidence_state.c.work_priority,
    library_item_evidence_state.c.evidence_kind,
    library_item_evidence_state.c.library_item_id,
)

evidence_queue_state = Table(
    "evidence_queue_state",
    metadata,
    Column("queue_name", Text, primary_key=True),
    Column("batch_id", Text),
    Column("status", Text, nullable=False, server_default="idle"),
    Column("scope_json", Text),
    Column("evidence_kinds_json", Text, nullable=False, server_default="[]"),
    Column("is_paused", Integer, nullable=False, server_default="0"),
    Column("cancel_requested", Integer, nullable=False, server_default="0"),
    Column("item_count", Integer, nullable=False, server_default="0"),
    Column("completed_count", Integer, nullable=False, server_default="0"),
    Column("failed_count", Integer, nullable=False, server_default="0"),
    Column("cancelled_count", Integer, nullable=False, server_default="0"),
    Column("created_at", Text),
    Column("started_at", Text),
    Column("finished_at", Text),
    Column("updated_at", Text, nullable=False),
)

background_work_state = Table(
    "background_work_state",
    metadata,
    Column("work_area", Text, primary_key=True),
    Column("is_paused", Integer, nullable=False, server_default="0"),
    Column("updated_at", Text, nullable=False),
)

plex_item_metadata = Table(
    "plex_item_metadata",
    metadata,
    Column("library_item_id", Integer, ForeignKey("library_items.id", ondelete="CASCADE"), primary_key=True),
    Column("plex_server_id", Text, nullable=False),
    Column("plex_item_rating_key", Text, nullable=False),
    Column("plex_part_id", Text),
    Column("plex_show_rating_key", Text),
    Column("plex_season_index", Integer),
    Column("plex_added_at", Text),
    Column("plex_part_path", Text, nullable=False),
    Column("observed_at", Text, nullable=False),
)
Index("idx_plex_item_metadata_show", plex_item_metadata.c.plex_show_rating_key)

series_metadata = Table(
    "series_metadata",
    metadata,
    Column("series_prefix", Text, primary_key=True),
    Column("plex_server_id", Text),
    Column("plex_show_rating_key", Text),
    Column("plex_guids_json", Text, nullable=False, server_default="[]"),
    Column("plex_observed_at", Text),
    Column("tmdb_series_id", Integer),
    Column("tmdb_status", Text),
    Column("tmdb_in_production", Integer),
    Column("tmdb_observed_at", Text),
    Column("updated_at", Text, nullable=False),
)
Index("idx_series_metadata_tmdb", series_metadata.c.tmdb_series_id)

metadata_sync_state = Table(
    "metadata_sync_state",
    metadata,
    Column("provider", Text, primary_key=True),
    Column("last_success_at", Text),
    Column("updated_at", Text, nullable=False),
)

run_manifests = Table(
    "run_manifests",
    metadata,
    Column("run_id", Text, primary_key=True),
    Column("created_at", Text, nullable=False),
    Column("output_path", Text, nullable=False),
    Column("selection_json", Text, nullable=False),
    Column("item_count", Integer, nullable=False),
)

calibration_jobs = Table(
    "calibration_jobs",
    metadata,
    Column("job_id", Text, primary_key=True),
    Column("prefix", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("lane", Text, nullable=False),
    Column("action", Text, nullable=False),
    Column("host_json", Text, nullable=False),
    Column("notes", Text),
    Column("policy_json", Text, nullable=False),
    Column("sample_item_json", Text, nullable=False),
    Column("seed_source", Text),
    Column("seed_summary", Text),
    Column("seed_prompt_version", Text),
    Column("seed_raw_response", Text),
    Column("seed_proposed_policy_json", Text),
    Column("seed_applied_policy_json", Text),
    Column("result_json", Text),
    Column("error", Text),
    Column("owner_pid", Integer),
    Column("created_at", Text, nullable=False),
    Column("started_at", Text),
    Column("finished_at", Text),
    Column("updated_at", Text, nullable=False),
)
Index("idx_calibration_jobs_prefix_created", calibration_jobs.c.prefix, calibration_jobs.c.created_at.desc())
Index(
    "idx_calibration_jobs_lane_status_created",
    calibration_jobs.c.lane,
    calibration_jobs.c.status,
    calibration_jobs.c.created_at.asc(),
)

encode_queue_state = Table(
    "encode_queue_state",
    metadata,
    Column("queue_name", Text, primary_key=True),
    Column("is_paused", Integer, nullable=False, server_default="0"),
    Column("stop_requested", Integer, nullable=False, server_default="0"),
    Column("active_job_id", Text),
    Column("updated_at", Text, nullable=False),
)

encode_jobs = Table(
    "encode_jobs",
    metadata,
    Column("job_id", Text, primary_key=True),
    Column("prefix", Text, nullable=False),
    Column("job_kind", Text, nullable=False, server_default="single"),
    Column("parent_job_id", Text),
    Column("status", Text, nullable=False),
    Column("manifest_path", Text, nullable=False),
    Column("manifest_indexes_json", Text),
    Column("item_count", Integer, nullable=False, server_default="0"),
    Column("saved_profile_path", Text),
    Column("host_json", Text, nullable=False),
    Column("last_host_json", Text, nullable=False, server_default="{}"),
    Column("notes", Text),
    Column("process_pid", Integer),
    Column("error", Text),
    Column("bypass_schedule", Integer, nullable=False, server_default="0"),
    Column("attempt_count", Integer, nullable=False, server_default="0"),
    Column("leased_at", Text),
    Column("lease_expires_at", Text),
    Column("heartbeat_at", Text),
    Column("worker_id", Text),
    Column("retry_not_before", Text),
    Column("waiting_reason", Text),
    Column("terminal_reason", Text),
    Column("last_failure_kind", Text),
    Column("last_failure_at", Text),
    Column("host_cooldown_until", Text),
    Column("progress_json", Text),
    Column("created_at", Text, nullable=False),
    Column("started_at", Text),
    Column("finished_at", Text),
    Column("updated_at", Text, nullable=False),
)
Index("idx_encode_jobs_status_created", encode_jobs.c.status, encode_jobs.c.created_at.asc())
Index("idx_encode_jobs_prefix_created", encode_jobs.c.prefix, encode_jobs.c.created_at.desc())
Index("idx_encode_jobs_kind_status_created", encode_jobs.c.job_kind, encode_jobs.c.status, encode_jobs.c.created_at.asc())
Index("idx_encode_jobs_parent_created", encode_jobs.c.parent_job_id, encode_jobs.c.created_at.asc())
Index(
    "idx_encode_jobs_status_retry_ready",
    encode_jobs.c.status,
    encode_jobs.c.retry_not_before,
    encode_jobs.c.created_at.asc(),
)

tuning_sessions = Table(
    "tuning_sessions",
    metadata,
    Column("session_id", Text, primary_key=True),
    Column("prefix", Text, nullable=False),
    Column("note", Text, nullable=False),
    Column("summary", Text),
    Column("diagnosis", Text),
    Column("confidence", Text),
    Column("evidence_checked_json", Text, nullable=False, server_default="[]"),
    Column("suggested_follow_up", Text),
    Column("prompt_version", Text),
    Column("proposed_policy_json", Text),
    Column("applied_policy_json", Text),
    Column("toolbelt_json", Text, nullable=False, server_default="{}"),
    Column("self_check_json", Text),
    Column("raw_response", Text),
    Column("created_at", Text, nullable=False),
)
Index("idx_tuning_sessions_prefix_created", tuning_sessions.c.prefix, tuning_sessions.c.created_at.desc())

learning_artifacts = Table(
    "learning_artifacts",
    metadata,
    Column("artifact_id", Text, primary_key=True),
    Column("session_id", Text, ForeignKey("tuning_sessions.session_id", ondelete="CASCADE"), nullable=False),
    Column("prefix", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("artifact_path", Text, nullable=False),
    Column("summary", Text),
    Column("tags_json", Text, nullable=False, server_default="[]"),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
)
Index("idx_learning_artifacts_prefix_updated", learning_artifacts.c.prefix, learning_artifacts.c.updated_at.desc())

staged_artifacts = Table(
    "staged_artifacts",
    metadata,
    Column("library_item_id", Integer, ForeignKey("library_items.id", ondelete="CASCADE"), primary_key=True),
    Column("manifest_run_id", Text),
    Column("manifest_path", Text),
    Column("item_index", Integer),
    Column("encode_origin", Text),
    Column("encode_job_id", Text),
    Column("encode_worker_id", Text),
    Column("encode_host_key", Text),
    Column("encode_host_label", Text),
    Column("encode_host_mode", Text),
    Column("encode_media_access", Text),
    Column("source_path", Text),
    Column("source_rel_path", Text),
    Column("source_size_bytes", Integer),
    Column("source_duration_seconds", REAL),
    Column("source_video_codec", Text),
    Column("source_fingerprint", Text),
    Column("encode_started_at", Text),
    Column("encode_completed_at", Text),
    Column("encode_duration_seconds", REAL),
    Column("staging_path", Text, nullable=False),
    Column("staging_size_bytes", Integer),
    Column("staging_mtime_ns", Integer),
    Column("staging_fingerprint", Text),
    Column("bytes_saved", Integer),
    Column("size_ratio", REAL),
    Column("chosen_crf", REAL),
    Column("quality_metric", Text),
    Column("quality_target", REAL),
    Column("quality_score", REAL),
    Column("encode_command_json", Text),
    Column("audio_summary_json", Text),
    Column("subtitle_summary_json", Text),
    Column("attachment_summary_json", Text),
    Column("validation_json", Text),
    Column("staged_at", Text),
    Column("validated_at", Text),
    Column("promoted_at", Text),
    Column("promoted_path", Text),
    Column("archived_source_path", Text),
    Column("updated_at", Text, nullable=False),
)

item_events = Table(
    "item_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("library_item_id", Integer, ForeignKey("library_items.id", ondelete="CASCADE"), nullable=False),
    Column("created_at", Text, nullable=False),
    Column("event_type", Text, nullable=False),
    Column("details_json", Text, nullable=False),
)
Index("idx_item_events_item_time", item_events.c.library_item_id, item_events.c.created_at.desc())

all_tables = metadata.sorted_tables
