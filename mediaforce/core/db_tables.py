from sqlalchemy import Column
from sqlalchemy import CheckConstraint
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
    Column("status", Text, nullable=False, server_default="running"),
    Column("error", Text),
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
    Column("heartbeat_at", Text),
    Column("progress_json", Text),
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
Index(
    "idx_calibration_jobs_lane_status_finished",
    calibration_jobs.c.lane,
    calibration_jobs.c.status,
    calibration_jobs.c.finished_at.desc(),
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
    Column("schedule_close_deadline_at", Text),
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

quality_search_observations = Table(
    "quality_search_observations",
    metadata,
    Column("observation_id", Text, primary_key=True),
    Column("search_run_id", Text, nullable=False),
    Column("revision", Integer, nullable=False, server_default="0"),
    Column(
        "supersedes_observation_id",
        Text,
        ForeignKey("quality_search_observations.observation_id", ondelete="RESTRICT"),
    ),
    Column("supersession_reason", Text),
    Column("schema_version", Integer, nullable=False, server_default="1"),
    Column("library_item_id", Integer, nullable=False),
    Column("prefix", Text, nullable=False),
    Column("source_rel_path", Text, nullable=False),
    Column("source_fingerprint", Text),
    Column("search_signature_id", Text),
    Column("policy_hash", Text),
    Column("search_objective", Text, nullable=False),
    Column("quality_metric", Text, nullable=False),
    Column("outcome_kind", Text, nullable=False),
    Column("authority", Text, nullable=False),
    Column("learning_eligible", Integer, nullable=False),
    Column("exclusion_reason", Text),
    Column("selected_crf", REAL),
    Column("selected_target", REAL),
    Column("selected_score", REAL),
    Column("actual_output_bytes", Integer),
    Column("candidate_count", Integer, nullable=False, server_default="0"),
    Column("search_duration_seconds", REAL),
    Column("context_json", Text, nullable=False),
    Column("bounds_json", Text, nullable=False),
    Column("candidate_trace_json", Text, nullable=False),
    Column("outcome_json", Text, nullable=False),
    Column("timing_json", Text, nullable=False),
    Column("provenance_json", Text, nullable=False),
    Column("shadow_json", Text),
    Column("payload_sha256", Text, nullable=False),
    Column("recorded_at", Text, nullable=False),
    CheckConstraint("revision >= 0", name="ck_quality_search_observations_revision"),
    CheckConstraint(
        "(supersedes_observation_id IS NULL AND revision = 0 AND supersession_reason IS NULL) "
        "OR (supersedes_observation_id IS NOT NULL AND revision > 0 AND supersession_reason IS NOT NULL)",
        name="ck_quality_search_observations_revision_chain",
    ),
    CheckConstraint(
        "authority IN ('staged_backfill', 'runtime_native', 'correction')",
        name="ck_quality_search_observations_authority",
    ),
    CheckConstraint(
        "outcome_kind IN ('selected', 'deterministic_search_failure', 'final_size_failure')",
        name="ck_quality_search_observations_outcome",
    ),
    CheckConstraint(
        "learning_eligible IN (0, 1)",
        name="ck_quality_search_observations_eligible",
    ),
    CheckConstraint(
        "(learning_eligible = 1 AND exclusion_reason IS NULL AND search_signature_id IS NOT NULL) "
        "OR (learning_eligible = 0 AND exclusion_reason IS NOT NULL)",
        name="ck_quality_search_observations_exclusion",
    ),
    CheckConstraint(
        "selected_crf IS NULL OR (selected_crf >= 0 AND selected_crf <= 63)",
        name="ck_quality_search_observations_crf",
    ),
    CheckConstraint(
        "outcome_kind != 'selected' OR "
        "(selected_crf IS NOT NULL AND selected_target IS NOT NULL AND selected_score IS NOT NULL)",
        name="ck_quality_search_observations_selected",
    ),
    CheckConstraint(
        "candidate_count >= 0 AND (search_duration_seconds IS NULL OR search_duration_seconds >= 0)",
        name="ck_quality_search_observations_counts",
    ),
)
Index(
    "uq_quality_search_observations_run_authority_revision",
    quality_search_observations.c.search_run_id,
    quality_search_observations.c.authority,
    quality_search_observations.c.revision,
    unique=True,
)
Index(
    "uq_quality_search_observations_supersedes",
    quality_search_observations.c.supersedes_observation_id,
    unique=True,
    sqlite_where=quality_search_observations.c.supersedes_observation_id.is_not(None),
)
Index(
    "idx_quality_search_observations_lookup",
    quality_search_observations.c.search_signature_id,
    quality_search_observations.c.prefix,
    quality_search_observations.c.recorded_at.desc(),
)
Index(
    "idx_quality_search_observations_item_time",
    quality_search_observations.c.library_item_id,
    quality_search_observations.c.recorded_at.desc(),
)

content_intent_boundary_observations = Table(
    "content_intent_boundary_observations",
    metadata,
    Column("observation_id", Text, primary_key=True),
    Column("series_id", Text, nullable=False),
    Column("boundary_group_id", Text, nullable=False),
    Column("schema_version", Integer, nullable=False, server_default="1"),
    Column("revision", Integer, nullable=False, server_default="0"),
    Column(
        "supersedes_observation_id",
        Text,
        ForeignKey("content_intent_boundary_observations.observation_id", ondelete="RESTRICT"),
    ),
    Column("supersession_reason", Text),
    Column("authority", Text, nullable=False),
    Column("disposition", Text, nullable=False, server_default="active"),
    Column("personalization_eligible", Integer, nullable=False),
    Column("exclusion_reason", Text),
    Column("library_item_id", Integer, nullable=False),
    Column("prefix", Text, nullable=False),
    Column("source_rel_path", Text, nullable=False),
    Column("source_id", Text, nullable=False),
    Column("source_fingerprint", Text),
    Column("content_fingerprint_kind", Text, nullable=False),
    Column("content_fingerprint", Text, nullable=False),
    Column("content_id", Text, nullable=False),
    Column("content_profile_id", Text, nullable=False),
    Column("content_traits_json", Text, nullable=False),
    Column("intent_semantic_id", Text, nullable=False),
    Column("intent_snapshot_id", Text, nullable=False),
    Column("intent_level", Text, nullable=False),
    Column("compatibility_key", Text, nullable=False),
    Column("compatibility_json", Text, nullable=False),
    Column("policy_hash", Text, nullable=False),
    Column("source_event_kind", Text, nullable=False),
    Column("source_event_id", Text, nullable=False),
    Column("job_id", Text, nullable=False),
    Column("artifact_fingerprint", Text, nullable=False),
    Column("source_evidence_ids_json", Text, nullable=False),
    Column("observation_kind", Text, nullable=False),
    Column("verdict", Text, nullable=False),
    Column("boundary_kind", Text, nullable=False),
    Column("authoritative_anchor_bytes", Integer, nullable=False),
    Column("boundary_size_bytes", Integer, nullable=False),
    Column("actual_output_bytes", Integer),
    Column("sampled_clip_bytes", Integer),
    Column("duration_seconds", REAL, nullable=False),
    Column("boundary_bitrate_bps", Integer, nullable=False),
    Column("direction", Text, nullable=False),
    Column("quality_metric", Text, nullable=False),
    Column("quality_target", REAL, nullable=False),
    Column("minimum_quality_score", REAL, nullable=False),
    Column("measured_quality_score", REAL, nullable=False),
    Column("quality_floor_met", Integer, nullable=False),
    Column("assessment_json", Text, nullable=False),
    Column("provenance_json", Text, nullable=False),
    Column("payload_sha256", Text, nullable=False),
    Column("recorded_at", Text, nullable=False),
    CheckConstraint(
        "schema_version = 1",
        name="ck_content_intent_boundary_schema_version",
    ),
    CheckConstraint("revision >= 0", name="ck_content_intent_boundary_revision"),
    CheckConstraint(
        "(revision = 0 AND supersedes_observation_id IS NULL AND supersession_reason IS NULL "
        "AND authority = 'runtime_native') OR "
        "(revision > 0 AND supersedes_observation_id IS NOT NULL "
        "AND length(trim(supersession_reason)) > 0 AND authority = 'correction')",
        name="ck_content_intent_boundary_revision_chain",
    ),
    CheckConstraint(
        "disposition IN ('active', 'withdrawn')",
        name="ck_content_intent_boundary_disposition",
    ),
    CheckConstraint(
        "personalization_eligible IN (0, 1)",
        name="ck_content_intent_boundary_eligible",
    ),
    CheckConstraint(
        "(personalization_eligible = 1 AND disposition = 'active' AND exclusion_reason IS NULL) "
        "OR (personalization_eligible = 0 AND exclusion_reason IS NOT NULL)",
        name="ck_content_intent_boundary_exclusion",
    ),
    CheckConstraint(
        "disposition != 'withdrawn' OR (revision > 0 AND personalization_eligible = 0)",
        name="ck_content_intent_boundary_withdrawal",
    ),
    CheckConstraint(
        "intent_level IN ('reference', 'transparent', 'balanced', 'perceptual_floor')",
        name="ck_content_intent_boundary_intent",
    ),
    CheckConstraint(
        "content_fingerprint_kind = 'mediaforce_content_version_v1'",
        name="ck_content_intent_boundary_fingerprint_kind",
    ),
    CheckConstraint(
        "source_event_kind IN ('post_test_review')",
        name="ck_content_intent_boundary_event",
    ),
    CheckConstraint(
        "(observation_kind = 'visual_approval' AND verdict = 'acceptable' AND boundary_kind = 'upper_bound') "
        "OR (observation_kind = 'visual_rejection' AND verdict = 'unacceptable' "
        "AND boundary_kind = 'lower_bound')",
        name="ck_content_intent_boundary_verdict",
    ),
    CheckConstraint(
        "authoritative_anchor_bytes > 0 AND boundary_size_bytes > 0 "
        "AND (actual_output_bytes IS NULL OR actual_output_bytes > 0) "
        "AND (sampled_clip_bytes IS NULL OR sampled_clip_bytes > 0) "
        "AND duration_seconds > 0 AND boundary_bitrate_bps > 0",
        name="ck_content_intent_boundary_measurements",
    ),
    CheckConstraint(
        "(direction = 'smaller' AND boundary_size_bytes < authoritative_anchor_bytes) "
        "OR (direction = 'same' AND boundary_size_bytes = authoritative_anchor_bytes) "
        "OR (direction = 'larger' AND boundary_size_bytes > authoritative_anchor_bytes)",
        name="ck_content_intent_boundary_direction",
    ),
    CheckConstraint(
        "quality_target >= 0 AND minimum_quality_score >= 0 "
        "AND minimum_quality_score <= quality_target AND measured_quality_score >= 0",
        name="ck_content_intent_boundary_quality",
    ),
    CheckConstraint(
        "quality_floor_met IN (0, 1) AND "
        "((quality_floor_met = 1 AND measured_quality_score >= minimum_quality_score) "
        "OR (quality_floor_met = 0 AND measured_quality_score < minimum_quality_score))",
        name="ck_content_intent_boundary_quality_floor",
    ),
)
Index(
    "uq_content_intent_boundary_series_revision",
    content_intent_boundary_observations.c.series_id,
    content_intent_boundary_observations.c.revision,
    unique=True,
)
Index(
    "uq_content_intent_boundary_supersedes",
    content_intent_boundary_observations.c.supersedes_observation_id,
    unique=True,
    sqlite_where=content_intent_boundary_observations.c.supersedes_observation_id.is_not(None),
)
Index(
    "idx_content_intent_boundary_group_time",
    content_intent_boundary_observations.c.boundary_group_id,
    content_intent_boundary_observations.c.recorded_at.desc(),
)
Index(
    "idx_content_intent_boundary_compatibility_time",
    content_intent_boundary_observations.c.intent_semantic_id,
    content_intent_boundary_observations.c.compatibility_key,
    content_intent_boundary_observations.c.recorded_at.desc(),
)
Index(
    "idx_content_intent_boundary_model_time",
    content_intent_boundary_observations.c.content_profile_id,
    content_intent_boundary_observations.c.intent_semantic_id,
    content_intent_boundary_observations.c.compatibility_key,
    content_intent_boundary_observations.c.recorded_at.desc(),
)
Index(
    "idx_content_intent_boundary_item_time",
    content_intent_boundary_observations.c.library_item_id,
    content_intent_boundary_observations.c.recorded_at.desc(),
)
Index(
    "idx_content_intent_boundary_job_time",
    content_intent_boundary_observations.c.job_id,
    content_intent_boundary_observations.c.recorded_at.desc(),
)

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
