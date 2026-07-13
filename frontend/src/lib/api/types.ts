export interface MetricSupport {
	vmaf: boolean;
	xpsnr: boolean;
	ssim: boolean;
}

export type WorkflowTone = 'idle' | 'ready' | 'active' | 'attention' | 'success';
export type WorkflowActionKind =
	| 'none'
	| 'queue_encode'
	| 'validate_outputs'
	| 'promote_outputs'
	| 'monitor_encode'
	| 'open_ops'
	| 'review_scope';
export type WorkflowLane =
	| 'none'
	| 'encode'
	| 'validate'
	| 'promote'
	| 'processing'
	| 'attention'
	| 'complete'
	| 'blocked'
	| 'mixed';

export interface WorkflowNextAction {
	kind: WorkflowActionKind;
	label: string;
	enabled: boolean;
	target_prefix: string;
}

export interface FolderWorkflowState {
	prefix: string;
	state: string;
	primary_lane: WorkflowLane;
	label: string;
	tone: WorkflowTone;
	detail: string;
	counts: Record<string, number>;
	lane_counts: Record<string, number>;
	state_counts: Record<string, number>;
	next_action: WorkflowNextAction;
	blockers: string[];
}

export interface LifecycleHoldReason {
	code: string;
	label: string;
	detail: string;
	release_at?: string | null;
}

export interface SeasonLifecycleState {
	prefix: string;
	label: string;
	season_number?: number | null;
	is_special: boolean;
	ambiguous: boolean;
	is_current_season: boolean;
	candidate_count: number;
	eligible_candidate_count: number;
	held_candidate_count: number;
	hold_reasons: LifecycleHoldReason[];
	activity_at?: string | null;
	ranking_added_at?: string | null;
	ranking_added_source?: string;
}

export interface LifecycleState {
	schema_version: number;
	prefix: string;
	series_prefix?: string | null;
	policy_mode?: 'auto' | 'on' | 'off';
	provider_state?: 'active' | 'ended' | 'unknown' | 'stale';
	provider_status?: string | null;
	provider_observed_at?: string | null;
	current_season_number?: number | null;
	candidate_count: number;
	eligible_candidate_count: number;
	held_candidate_count: number;
	hold_reason_counts?: Record<string, number>;
	ranking_added_at?: string | null;
	can_override_holds?: boolean;
	seasons: SeasonLifecycleState[];
}

export type MediaScopeDomain = 'tv' | 'movie' | 'other';
export type MediaScopeKind =
	| 'library_root'
	| 'tv_series'
	| 'tv_season'
	| 'movie_title'
	| 'movie_file'
	| 'media_folder'
	| 'media_file';
export type MediaScopeMatch = 'exact_item' | 'descendants';

export interface MediaScopePayload {
	schema_version: number;
	prefix: string;
	root: string;
	domain: MediaScopeDomain;
	kind: MediaScopeKind;
	match: MediaScopeMatch;
	title: string;
	subtitle: string;
	scope_label: string;
	parent?: { prefix: string; title: string } | null;
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
	workflow_state?: FolderWorkflowState | null;
	lifecycle?: LifecycleState | null;
	media_scope?: MediaScopePayload | null;
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
	active_host_labels?: string[];
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
	created_at?: string | null;
	finished_at?: string | null;
	updated_at?: string | null;
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

export interface ArchiveCleanupSummary {
	archive_root: string;
	file_count: number;
	total_size_bytes: number;
	has_cleanup: boolean;
}

export interface CompletedFolderRow {
	prefix: string;
	title: string;
	subtitle: string;
	scope_label: string;
	promoted_item_count: number;
	archived_backup_count: number;
	archived_backup_size_bytes: number;
	total_bytes_saved: number;
	latest_promoted_at: string | null;
	cleanup_state?: 'ready' | 'blocked' | 'unknown' | 'cleaned' | string;
	cleanup_detail?: string;
	missing_backup_count?: number;
	outside_archive_root_count?: number;
	originals_removed_count?: number;
}

export interface CompletedHistoryEvent {
	id: number;
	event_type: string;
	label: string;
	tone: 'active' | 'ready' | 'wait' | 'fail' | 'idle' | string;
	prefix: string;
	title: string;
	subtitle: string;
	scope_label: string;
	created_at: string;
	detail: string;
	size_bytes?: number | null;
}

export interface CompletedPayload {
	folders: CompletedFolderRow[];
	completed_count: number;
	folders_with_backups_count: number;
	archive_cleanup: ArchiveCleanupSummary;
	history?: CompletedHistoryEvent[];
}

export interface CompletedCleanupResult {
	ok: boolean;
	message: string;
	removed_count: number;
	removed_size_bytes: number;
	removed_prefix_count: number;
	completed?: CompletedPayload;
}

export interface CompletedOriginalsResolvedResult {
	ok: boolean;
	message: string;
	resolved_count: number;
	resolved_prefix_count: number;
	completed?: CompletedPayload;
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
	catalog_empty: boolean;
	folder_cache_key: string;
	metric_support: MetricSupport;
	metric_status_copy: string;
}

export interface DashboardFoldersPayload {
	folders: FolderCard[];
	series_folders?: FolderCard[];
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
	storage_recovery_available?: boolean;
	missing_paths: string[];
	missing_mounts?: string[];
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

export type LibraryType = 'tv' | 'movie' | 'spatial' | 'other';
export type LibraryAvailability = 'production' | 'browse_only' | 'disabled';

export interface LibraryPolicy {
	series_lifecycle_mode?: 'auto' | 'on' | 'off';
	current_season_inactive_days?: number;
	season_acquisition_hold_days?: number;
	series_metadata_stale_days?: number;
	grouping?: 'title' | 'folder' | 'file';
	editions?: 'separate';
	extras?: 'exclude' | 'include';
	ranking?: 'oldest_added_first' | 'largest_first';
	playback_target?: string;
	stereo_layout?: 'unknown' | 'side_by_side' | 'top_bottom' | 'frame_sequential';
	projection?: 'unknown' | 'flat' | 'equirectangular_180' | 'equirectangular_360';
	geometry_policy?: 'preserve';
	container_profile?: 'unqualified';
}

export interface LibraryReadiness {
	state: 'ready' | 'incomplete' | 'blocked' | 'browse_only' | 'disabled';
	label: string;
	detail: string;
}

export interface SettingsLibrary {
	index: string;
	key: string;
	label: string;
	path: string;
	color: string;
	library_type: LibraryType;
	availability: LibraryAvailability;
	default_profile: string;
	plex_path: string;
	policy: LibraryPolicy;
	readiness: LibraryReadiness;
	type_change_confirmation: string;
}

export interface LibraryTypeChangePreview {
	key: string;
	from_type: LibraryType;
	to_type: LibraryType;
	item_count: number;
	requires_rescan: boolean;
	clears_saved_profiles: boolean;
	acknowledgement: string;
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
	library_type_options: Array<{ key: LibraryType; label: string }>;
	library_profile_options: Record<LibraryType, Array<{ key: string; label: string }>>;
	remote_hosts: SettingsHost[];
	transcode_root: string;
	video_defaults: {
		quality_metric: string;
		target_vmaf: string;
		min_target_vmaf: string;
		target_xpsnr: string;
		min_target_xpsnr: string;
		target_size_mb: string;
		target_runtime_minutes: string;
		size_goal_mode: string;
		sample_projection_tolerance_percent: string;
		final_output_tolerance_percent: string;
		decision_model: string;
		quality_engine: string;
		max_height: string;
		default_grain: string;
		max_encoded_percent: string;
	};
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
	metadata: {
		plex: {
			enabled: boolean;
			base_url: string;
			library_roots: Record<string, string>;
			token_env: string;
			token_configured: boolean;
			refresh_interval_hours: number;
		};
		tmdb: {
			enabled: boolean;
			base_url: string;
			token_env: string;
			token_configured: boolean;
			refresh_interval_hours: number;
		};
	};
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

export type SizeGoalMode = 'normalized' | 'absolute';

export interface SizeGoalRequestPayload {
	mode: SizeGoalMode;
	value_mb: number;
	reference_runtime_minutes?: number;
	sample_projection_tolerance_percent: number;
	final_output_tolerance_percent: number;
}

export interface ResolutionIntentRequestPayload {
	mode: 'source' | 'max_height';
	max_height?: number | null;
}

export interface OperatorIntentRequestPayload {
	schema_version: 1;
	size_goal: SizeGoalRequestPayload;
	resolution: ResolutionIntentRequestPayload;
	quality?: Record<string, unknown>;
	streams?: {
		audio?: Record<string, unknown>;
		subtitle?: Record<string, unknown>;
	};
	quality_risk_tags?: QualityRiskTag[];
	quality_risk_details?: string;
	evidence_authority?:
		'none' | 'operator_observed' | 'approved_visual_result' | 'rejected_visual_result';
}

export interface ResolvedSizeGoalPayload {
	schema_version: 1;
	mode: SizeGoalMode | 'ambiguous';
	source: string;
	status: string;
	requires_confirmation: boolean;
	reference_size_mb?: number | null;
	reference_runtime_minutes?: number | null;
	item_runtime_seconds?: number | null;
	target_size_bytes?: number | null;
	target_size_mb?: number | null;
	sample_projection_tolerance_percent: number;
	final_output_tolerance_percent: number;
	sample_lower_bound_bytes?: number | null;
	sample_upper_bound_bytes?: number | null;
	final_lower_bound_bytes?: number | null;
	final_upper_bound_bytes?: number | null;
	rationale: string;
}

export interface ResolvedOperatorIntentPayload {
	schema_version: 1;
	requires_confirmation: boolean;
	size_goal: ResolvedSizeGoalPayload;
	resolution: Record<string, unknown>;
	request: OperatorIntentRequestPayload | null;
	quality?: Record<string, unknown>;
	streams?: Record<string, unknown>;
}

export interface SizeGoalOptionPayload {
	key: string;
	title: string;
	detail: string;
	requires_explicit_selection: boolean;
	operator_intent: OperatorIntentRequestPayload;
	resolved_size_goal: ResolvedSizeGoalPayload;
}

export type StreamBudgetFeasibilityStatus =
	'feasible' | 'aggressive_but_measurable' | 'arithmetically_infeasible' | 'requires_measurement';

export interface StreamBudgetEntryPayload {
	category: 'audio' | 'subtitle' | 'attachment' | 'container';
	source_index: number | null;
	action: string;
	source_codec: string | null;
	output_codec: string | null;
	budget_bytes: number | null;
	lower_bound_bytes: number | null;
	upper_bound_bytes: number | null;
	bitrate_bps: number | null;
	provenance: string;
	confidence: 'exact' | 'high' | 'medium' | 'low' | 'unknown';
	requires_measurement: boolean;
	rationale: string;
}

export interface PlannedStreamPayload {
	kind: 'audio' | 'subtitle' | 'attachment';
	source_index: number;
	source_codec: string;
	action: 'copy' | 'transcode' | 'drop';
	output_codec: string | null;
	codec_argument: string | null;
	output_bitrate_bps: number | null;
	output_bitrate_text: string | null;
	channels: number | null;
	language: string | null;
	default: boolean;
	forced: boolean;
	source_bitrate_bps: number | null;
	source_duration_seconds: number | null;
	source_size_bytes: number | null;
	file_name: string | null;
	mime_type: string | null;
}

export interface ProductionStreamPlanPayload {
	schema_version: 1;
	plan_id: string;
	source_id: string;
	source_fingerprint: string | null;
	policy_hash: string;
	output_container: string;
	attachments_known: boolean;
	copy_unknown_attachments: boolean;
	streams: PlannedStreamPayload[];
}

export interface StreamBudgetLedgerPayload {
	schema_version: 1;
	ledger_id: string;
	source: {
		source_id: string;
		source_fingerprint: string | null;
		source_size_bytes: number | null;
		source_video_bitrate_bps: number | null;
		duration_seconds: number | null;
	};
	policy_hash: string;
	size_goal: ResolvedSizeGoalPayload;
	stream_plan: ProductionStreamPlanPayload;
	entries: StreamBudgetEntryPayload[];
	totals: {
		total_target_bytes: number | null;
		audio_bytes: number | null;
		subtitle_bytes: number | null;
		attachment_bytes: number | null;
		container_bytes: number | null;
		non_video_bytes: number | null;
		minimum_non_video_bytes: number;
		maximum_non_video_bytes: number | null;
		remaining_video_bytes: number | null;
		remaining_video_bitrate_bps: number | null;
	};
	source_relative_cap: {
		configured_total_percent: number | null;
		total_cap_bytes: number | null;
		video_cap_bytes: number | null;
		video_cap_bitrate_bps: number | null;
		video_cap_percent: number | null;
		status: 'available' | 'arithmetically_infeasible' | 'requires_measurement' | 'unavailable';
	};
	feasibility: {
		status: StreamBudgetFeasibilityStatus;
		reasons: string[];
		arithmetic_infeasible: boolean;
		aggressive: boolean;
		requires_measurement: boolean;
	};
	uncertainty: {
		confidence: 'exact' | 'high' | 'medium' | 'low' | 'unknown';
		requires_measurement: boolean;
		minimum_non_video_bytes: number;
		maximum_non_video_bytes: number | null;
	};
}

export type QualityRiskTag =
	| 'softness_detail_loss'
	| 'motion_breakup'
	| 'banding_dark_scene_damage'
	| 'grain_noise_treatment'
	| 'cadence_interlace_artifacts'
	| 'audio_quality_layout'
	| 'other';

export type QualityRiskLevel = 'low' | 'medium' | 'high' | 'unknown';
export type QualityRiskOperatorStatus =
	'pending' | 'approved' | 'rejected' | 'needs_operator_review';

export type QualityRiskVerdict =
	'safe_to_sample' | 'needs_operator_review' | 'request_comparison' | 'blocked';

export interface QualityRiskItem {
	tag: QualityRiskTag;
	label: string;
	level: QualityRiskLevel;
	rationale: string;
	evidence_ids?: string[];
	moment_indexes?: number[];
}

export interface QualityRiskMomentRef {
	moment: number;
	timestamp_seconds: number;
	role: string;
	risk_tags: QualityRiskTag[];
	evidence_id?: string | null;
	rationale?: string | null;
}

export interface QualityRiskOperatorRecord {
	kind?: string;
	created_at?: string | null;
	verdict?: string;
	tags?: QualityRiskTag[];
	details?: string;
	evidence_id?: string | null;
	evidence_ids?: string[];
	moment_indexes?: number[];
	sample_job_id?: string | null;
	policy_hash?: string | null;
	source_id?: string | null;
	prefix?: string | null;
	authoritative?: boolean;
}

export interface QualityRiskTargetSizeSearch {
	trace_id?: string;
	schema_version?: number;
	status?: 'selected' | 'infeasible' | 'quality_conflict' | 'needs_review';
	selection_reason?: string | null;
	curve_shape?: 'single_point' | 'monotonic' | 'non_monotonic' | null;
	candidate_count?: number;
	max_candidates?: number;
	target_bytes?: number | null;
	lower_bound_bytes?: number | null;
	upper_bound_bytes?: number | null;
	selected_crf?: number | null;
	selected_metric?: string | null;
	selected_metric_score?: number | null;
	minimum_metric_score?: number | null;
	quality_floor_met?: boolean | null;
	predicted_whole_episode_bytes?: number | null;
	within_sample_band?: boolean | null;
	transform_plan_id?: string | null;
}

export interface QualityRiskPreTestInstruction {
	prefix?: string;
	source_id?: string;
	policy_hash?: string;
	sample_job_id?: string | null;
	focus_tags?: QualityRiskTag[];
	focus_labels?: string[];
	moments?: QualityRiskMomentRef[];
}

export interface QualityRiskPayload {
	schema?: string;
	version?: number;
	verdict?: QualityRiskVerdict;
	source_id?: string | null;
	source_fingerprint?: string | null;
	sample_job_id?: string | null;
	evidence_ids?: string[];
	policy_hash?: string | null;
	current_policy_hash?: string | null;
	preview_policy_hash?: string | null;
	blocked?: boolean;
	blocking_reasons?: string[];
	request_comparison?: boolean;
	comparison_reason?: string | null;
	typed_risks?: QualityRiskItem[];
	operator_decision?: {
		status?: QualityRiskOperatorStatus;
		current_rejection_outranks_approval?: boolean;
		reviewed_policy_hash?: string;
		records?: QualityRiskOperatorRecord[];
		effective_record?: QualityRiskOperatorRecord | null;
	} | null;
	interpretation?: {
		summary?: string;
		verdict?: QualityRiskVerdict;
		confidence?: QualityRiskLevel;
		risks?: QualityRiskItem[];
		suggested_actions?: string[];
	} | null;
	moment_count?: number;
	review_moment_ids?: string[];
	target_size_search?: QualityRiskTargetSizeSearch | null;
	pre_test_instruction?: QualityRiskPreTestInstruction | null;
}

export interface FolderPayload {
	prefix: string;
	media_scope: MediaScopePayload;
	pending: boolean;
	summary?: FolderSummary;
	sample_item?: Record<string, unknown>;
	item_plan?: Record<string, unknown>;
	policy?: Record<string, unknown>;
	hot_spots?: number[];
	calibration?: Record<string, unknown> | null;
	advice?: Record<string, unknown> | null;
	size_target_analysis?: Record<string, unknown> | null;
	resolved_operator_intent?: ResolvedOperatorIntentPayload;
	stream_budget_ledger?: StreamBudgetLedgerPayload;
	size_goal_options?: SizeGoalOptionPayload[];
	quality_risk?: QualityRiskPayload | null;
	approved_season_shortcut?: Record<string, unknown> | null;
	series_context?: { prefix: string; title: string } | null;
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
	encode_candidate_count?: number;
	workflow_state?: FolderWorkflowState | null;
	lifecycle?: LifecycleState | null;
}

export interface FolderBenchPreviewResponse {
	ok: boolean;
	message?: string;
	proposal?: Record<string, unknown> | null;
}

export interface FolderBenchConfirmResponse {
	ok: boolean;
	message?: string;
	job?: Record<string, unknown> | null;
	advice?: Record<string, unknown> | null;
}

export interface FolderStatusPayload {
	prefix: string;
	media_scope: MediaScopePayload;
	polling_active: boolean;
	calibration_status: string;
	folder_scan_status: string;
	calibration_job: Record<string, unknown> | null;
	retryable_sample_job?: Record<string, unknown> | null;
	folder_scan_job: Record<string, unknown> | null;
	workflow_state?: FolderWorkflowState | null;
}
