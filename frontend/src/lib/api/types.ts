export interface MetricSupport {
	vmaf: boolean;
	xpsnr: boolean;
	ssim: boolean;
}

export interface FolderCard {
	prefix: string;
	title: string;
	subtitle: string;
	scope_label: string;
	item_count: number;
	pending_count: number;
	total_size_bytes: number;
	estimated_savings_bytes: number;
	known_saved_bytes: number;
	projected_reclaim_bytes: number;
	average_age_days: number;
	sort_score: number;
	statuses: Record<string, number>;
	video_codecs: Record<string, number>;
	review_badge_label?: string | null;
	review_badge_tone?: string | null;
	review_badge_detail?: string | null;
	details_loading: boolean;
}

export interface QueueLane {
	running: Array<Record<string, unknown>>;
	queued: Array<Record<string, unknown>>;
	pending_review: Array<Record<string, unknown>>;
	recent_failed?: Array<Record<string, unknown>>;
	running_count: number;
	queued_count: number;
	pending_review_count: number;
	recent_failed_count?: number;
}

export interface EncodeJobProgressTelemetry {
	total_duration_seconds?: number;
	remaining_duration_seconds?: number;
	percent_complete?: number;
	fps?: number | null;
	speed?: number | null;
	eta_seconds?: number | null;
	eta_copy?: string | null;
	current_item_number?: number;
	total_item_count?: number;
	completed_item_count?: number;
	current_item_rel_path?: string;
	progress_state?: string;
	failure_analysis?: EncodeFailureAnalysis | null;
}

export interface EncodeFailureCandidate {
	crf?: number;
	metric?: string;
	score?: number;
	predicted_encode_percent?: number;
	predicted_encode_size_bytes?: number;
	line?: string;
}

export interface EncodeFailureAnalysis {
	kind?: string;
	retry_strategy?: string;
	auto_retry_allowed?: boolean;
	requested_metric?: string;
	target_score?: number;
	min_score?: number;
	max_encoded_percent?: number;
	best_candidate?: EncodeFailureCandidate | null;
	proposed_max_encoded_percent?: number | null;
	summary?: string;
	manifest_indexes?: number[];
	manifest_index?: number;
	item_rel_path?: string;
	item_analyses?: EncodeFailureAnalysis[];
}

export interface EncodeQueueJob {
	job_id: string;
	prefix: string;
	status: string;
	recoverable_item_count?: number;
	host?: Record<string, unknown>;
	error?: string | null;
	last_failure_kind?: string | null;
	last_failure_at?: string | null;
	finished_at?: string | null;
	attempt_summary?: string | null;
	active_hosts?: Array<Record<string, unknown>>;
	running_shard_count?: number;
	queued_shard_count?: number;
	completed_shard_count?: number;
	shard_count?: number;
	queue_position?: number;
	queue_depth?: number;
	schedule_waiting?: boolean;
	scheduler_status_copy?: string;
	telemetry_summary?: string;
	progress?: EncodeJobProgressTelemetry | null;
}

export interface EncodeQueueTelemetry {
	aggregate_speed?: number | null;
	eta_seconds?: number | null;
	eta_copy?: string | null;
	running_jobs?: number;
	queued_jobs?: number;
}

export interface EncodeQueueSummary {
	running_count: number;
	queued_count: number;
	queued_waiting_count?: number;
	needs_attention_count?: number;
	running: EncodeQueueJob[];
	queued: EncodeQueueJob[];
	recent?: EncodeQueueJob[];
	telemetry?: EncodeQueueTelemetry;
	state: {
		is_paused: boolean;
		stop_requested: boolean;
		scheduler_summary?: string;
		scheduler?: Record<string, unknown>;
	};
}

export interface DashboardScanJob {
	job_id: string;
	status: string;
	scope: string;
	prefix: string | null;
	created_at: string | null;
	started_at: string | null;
	last_progress_at?: string | null;
	finished_at: string | null;
	error: string | null;
	stats: {
		items_seen: number;
		updated_paths: number;
		unchanged: number;
	} | null;
}

export interface DashboardSummaryPayload {
	folders_preview: FolderCard[];
	library_colors: Record<string, string>;
	scan_job: DashboardScanJob | null;
	calibration_queue: {
		sample: QueueLane;
		full: QueueLane;
		active_count: number;
		recent_failed_count?: number;
	};
	encode_queue: EncodeQueueSummary;
	archive_cleanup?: {
		archive_root: string;
		file_count: number;
		total_size_bytes: number;
		has_cleanup: boolean;
	};
	catalog_empty: boolean;
	folder_cache_key: string;
	metric_support: MetricSupport;
	metric_status_copy: string;
}

export interface DashboardFoldersPayload {
	folders: FolderCard[];
	catalog_empty: boolean;
	folder_cache_key: string;
}

export interface HostRuntime {
	key: string;
	label: string;
	host?: string;
	available: boolean;
	probe_available?: boolean;
	message: string;
	probe_message?: string;
	media_access?: string;
	source_roots?: Record<string, string>;
	missing_paths: string[];
	issues: string[];
	probe_issues?: string[];
	detail: string | null;
	capabilities: string[];
	priority: number;
	max_parallel_encodes: number;
	allowed_libraries?: string[];
	active_encode_count: number;
	schedule_profile_label: string;
	schedule_detail: string;
	schedule_open?: boolean;
	active_flag: string;
	active_reason: string;
	repo_path?: string;
	queue_active?: boolean;
	setup_supported?: boolean;
	setup_requires_password?: boolean;
	trust_reset_supported?: boolean;
	running_jobs?: EncodeQueueJob[];
	telemetry?: EncodeQueueTelemetry;
}

export interface HostsPayload {
	compact: boolean;
	hosts: HostRuntime[];
}

export interface SettingsLibrary {
	index: string;
	key: string;
	path: string;
	color: string;
}

export interface SettingsHost {
	index: string;
	label: string;
	host: string;
	repo_path: string;
	wake_mac: string;
	start_command: string;
	stop_command: string;
	start_timeout_seconds: string;
	media_access: string;
	priority: string;
	max_parallel_encodes: string;
	schedule_profile: string;
	capabilities: string[];
	allowed_libraries: string[];
	source_roots_json: string;
	staging_root: string;
}

export interface ScheduleProfile {
	index: string;
	key: string;
	label: string;
	days_of_week: string[];
	all_day_days_of_week: string[];
	start_hour: string;
	end_hour: string;
}

export interface SettingsPayload {
	error: string | null;
	saved: boolean;
	libraries: SettingsLibrary[];
	remote_hosts: SettingsHost[];
	transcode_root: string;
	encode_queue_scheduler: {
		mode: string;
		start_hour: number;
		end_hour: number;
		timezone: string;
		summary: string;
	};
	schedule_profiles: ScheduleProfile[];
	schedule_profile_options: Array<{ key: string; label: string }>;
	host_capability_options: Array<{ key: string; label: string; help: string }>;
	archive_root: string;
	archive_cleanup: {
		archive_root: string;
		file_count: number;
		total_size_bytes: number;
		has_cleanup: boolean;
	} | null;
	runtime_settings_path: string;
	repo_config_path: string;
	host_notice: string | null;
	host_notice_kind: string | null;
}

export interface ArchiveCleanupPayload {
	archive_root: string;
	file_count: number;
	total_size_bytes: number;
	has_cleanup: boolean;
}

export interface CompletedFolder {
	prefix: string;
	title: string;
	subtitle: string;
	scope_label: string;
	promoted_item_count: number;
	archived_backup_count: number;
	archived_backup_size_bytes: number;
	total_bytes_saved: number;
	latest_promoted_at: string | null;
}

export interface CompletedPagePayload {
	folders: CompletedFolder[];
	completed_count: number;
	folders_with_backups_count: number;
	archive_cleanup: ArchiveCleanupPayload;
}

export interface CompletedBackupsClearResponse {
	ok: boolean;
	message: string;
	removed_count: number;
	removed_size_bytes: number;
	removed_prefix_count: number;
	completed: CompletedPagePayload;
}

export interface FolderSummary {
	prefix: string;
	item_count: number;
	total_size_bytes: number;
	statuses: Record<string, number>;
	video_codecs: Record<string, number>;
	audio_codecs: Record<string, number>;
	seasons: Record<string, number>;
	resolved_policy: Record<string, unknown>;
	suggested_override?: Record<string, unknown> | null;
}

export interface FolderPayload {
	prefix: string;
	pending: boolean;
	summary?: FolderSummary;
	sample_item?: Record<string, unknown>;
	item_plan?: Record<string, unknown>;
	policy?: Record<string, unknown>;
	hot_spots?: number[];
	calibration?: Record<string, unknown> | null;
	advice?: Record<string, unknown> | null;
	approved_season_shortcut?: Record<string, unknown> | null;
	pending_proposal?: Record<string, unknown> | null;
	review_gate?: Record<string, unknown>;
	calibration_queue?: Record<string, unknown>;
	calibration_job?: Record<string, unknown> | null;
	folder_scan_job?: Record<string, unknown> | null;
	metric_support: MetricSupport;
	metric_status_copy: string;
	resolved_metric?: string;
	recent_tuning_sessions?: Array<Record<string, unknown>>;
	sample_host_key?: string;
	sample_host_options?: Array<Record<string, unknown>>;
	sample_host_help_text?: string;
	encode_job?: EncodeQueueJob | null;
	encode_queue?: EncodeQueueSummary;
	encode_queue_state?: EncodeQueueSummary['state'];
	encode_queue_summary?: string;
	encode_queue_scheduler?: Record<string, unknown>;
}

export interface FolderStatusPayload {
	prefix: string;
	polling_active: boolean;
	calibration_status: string;
	folder_scan_status: string;
	calibration_job: Record<string, unknown> | null;
	retryable_sample_job?: Record<string, unknown> | null;
	folder_scan_job: Record<string, unknown> | null;
}
