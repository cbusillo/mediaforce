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
	average_age_days: number;
	sort_score: number;
	statuses: Record<string, number>;
	video_codecs: Record<string, number>;
}

export interface QueueLane {
	running: Array<Record<string, unknown>>;
	queued: Array<Record<string, unknown>>;
	pending_review: Array<Record<string, unknown>>;
	running_count: number;
	queued_count: number;
	pending_review_count: number;
}

export interface EncodeQueueSummary {
	running_count: number;
	queued_count: number;
	queued_waiting_count?: number;
	needs_attention_count?: number;
	running: Array<Record<string, unknown>>;
	queued: Array<Record<string, unknown>>;
	recent?: Array<Record<string, unknown>>;
	state: {
		is_paused: boolean;
		stop_requested: boolean;
		scheduler_summary?: string;
		scheduler?: Record<string, unknown>;
	};
}

export interface DashboardPayload {
	folders: FolderCard[];
	scan_job: Record<string, unknown> | null;
	calibration_queue: {
		sample: QueueLane;
		full: QueueLane;
		active_count: number;
	};
	encode_queue: EncodeQueueSummary;
	catalog_empty: boolean;
	metric_support: MetricSupport;
	metric_status_copy: string;
}

export interface HostRuntime {
	key: string;
	label: string;
	available: boolean;
	message: string;
	missing_paths: string[];
	issues: string[];
	detail: string | null;
	capabilities: string[];
	priority: number;
	max_parallel_encodes: number;
	active_encode_count: number;
	schedule_profile_label: string;
	schedule_detail: string;
	schedule_open?: boolean;
	active_flag: string;
	active_reason: string;
	repo_path?: string;
	queue_active?: boolean;
}

export interface HostsPayload {
	compact: boolean;
	hosts: HostRuntime[];
}

export interface SettingsLibrary {
	index: string;
	key: string;
	path: string;
}

export interface SettingsHost {
	index: string;
	label: string;
	host: string;
	repo_path: string;
	wake_mac: string;
	priority: string;
	max_parallel_encodes: string;
	schedule_profile: string;
	capabilities: string[];
}

export interface ScheduleProfile {
	index: string;
	key: string;
	label: string;
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
	runtime_settings_path: string;
	repo_config_path: string;
	host_notice: string | null;
	host_notice_kind: string | null;
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
	review_gate?: Record<string, unknown>;
	calibration_queue?: Record<string, unknown>;
	calibration_job?: Record<string, unknown> | null;
	folder_scan_job?: Record<string, unknown> | null;
	metric_support: MetricSupport;
	metric_status_copy: string;
	resolved_metric?: string;
	sample_host_key?: string;
	sample_host_options?: Array<Record<string, unknown>>;
	sample_host_help_text?: string;
	encode_job?: Record<string, unknown> | null;
	encode_queue?: Record<string, unknown>;
	encode_queue_state?: Record<string, unknown>;
	encode_queue_summary?: string;
	encode_queue_scheduler?: Record<string, unknown>;
}

export interface FolderStatusPayload {
	prefix: string;
	polling_active: boolean;
	calibration_status: string;
	folder_scan_status: string;
	calibration_job: Record<string, unknown> | null;
	folder_scan_job: Record<string, unknown> | null;
}
