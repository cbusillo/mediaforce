import type {
	CompletedPayload,
	DashboardFoldersPayload,
	DashboardSummaryPayload,
	FolderPayload,
	FolderStatusPayload,
	HostsPayload,
	MediaScopePayload,
	MovieLibraryPayload,
	OtherLibraryPayload,
	QueueLane,
	StagedIntegrityPayload
} from './types';

const emptyQueueLane: QueueLane = {
	running: [],
	queued: [],
	pending_review: [],
	running_count: 0,
	queued_count: 0,
	pending_review_count: 0
};

export const initialDashboard: DashboardSummaryPayload = {
	folders_preview: [],
	library_colors: {},
	scan_job: null,
	calibration_queue: {
		sample: emptyQueueLane,
		full: emptyQueueLane,
		active_count: 0
	},
	encode_queue: {
		running_count: 0,
		queued_count: 0,
		running: [],
		queued: [],
		state: { is_paused: false, stop_requested: false, scheduler_summary: 'loading' }
	},
	catalog_empty: false,
	folder_cache_key: 'loading',
	metric_support: { vmaf: false, xpsnr: false, ssim: false },
	metric_status_copy: 'loading'
};

export const initialFoldersPayload: DashboardFoldersPayload = {
	folders: [],
	series_folders: [],
	folder_cache_key: 'loading',
	catalog_empty: false
};

export const initialHosts: HostsPayload = { compact: true, hosts: [] };

export const initialMovieLibrary: MovieLibraryPayload = {
	schema_version: 1,
	libraries: [],
	titles: [],
	catalog_empty: false,
	details_loading: true
};

export const initialOtherLibrary: OtherLibraryPayload = {
	schema_version: 1,
	libraries: [],
	work_units: [],
	catalog_empty: false,
	catalog_truncated: false,
	catalog_item_limit: 5000,
	catalog_work_unit_limit: 500,
	details_loading: true
};

function loadingMediaScope(prefix: string): MediaScopePayload {
	return {
		schema_version: 1,
		prefix,
		root: prefix.split('/')[0] ?? '',
		domain: 'other',
		kind: 'media_folder',
		match: 'descendants',
		title: prefix.split('/').at(-1) || 'Library',
		subtitle: 'Library',
		scope_label: 'Folder',
		parent: null
	};
}

export function initialFolderPayload(prefix: string): FolderPayload {
	return {
		prefix,
		media_scope: loadingMediaScope(prefix),
		pending: true,
		metric_support: { vmaf: false, xpsnr: false, ssim: false },
		metric_status_copy: 'loading',
		workflow_state: {
			prefix,
			state: 'loading',
			primary_lane: 'none',
			label: 'Loading',
			tone: 'active',
			detail: 'Fetching folder state, worker readiness, and workflow status.',
			counts: {},
			lane_counts: {},
			state_counts: {},
			next_action: {
				kind: 'review_scope',
				label: 'Open folders',
				enabled: true,
				target_prefix: prefix
			},
			blockers: []
		}
	};
}

export function initialFolderStatusPayload(prefix: string): FolderStatusPayload {
	return {
		prefix,
		media_scope: loadingMediaScope(prefix),
		polling_active: false,
		calibration_status: 'loading',
		folder_scan_status: 'loading',
		calibration_job: null,
		folder_scan_job: null,
		staged_integrity: initialStagedIntegrityPayload(prefix),
		workflow_state: {
			prefix,
			state: 'loading',
			primary_lane: 'none',
			label: 'Loading',
			tone: 'active',
			detail: 'Fetching folder state, worker readiness, and workflow status.',
			counts: {},
			lane_counts: {},
			state_counts: {},
			next_action: {
				kind: 'review_scope',
				label: 'Open folders',
				enabled: true,
				target_prefix: prefix
			},
			blockers: []
		}
	};
}

export function initialStagedIntegrityPayload(
	prefix: string,
	loadError?: string
): StagedIntegrityPayload {
	return {
		scope: loadingMediaScope(prefix),
		counts: {},
		blocker_count: 0,
		blockers: [],
		database_truncated: false,
		discovery: {
			requested: false,
			truncated: false,
			entries_scanned: 0
		},
		offset: 0,
		limit: 100,
		records: [],
		next_offset: null,
		load_error: loadError
	};
}

export const initialCompleted: CompletedPayload = {
	folders: [],
	completed_count: 0,
	folders_with_backups_count: 0,
	archive_cleanup: {
		archive_root: '',
		file_count: 0,
		total_size_bytes: 0,
		has_cleanup: false
	},
	history: []
};
