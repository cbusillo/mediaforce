import type {
	CompletedPayload,
	DashboardFoldersPayload,
	DashboardSummaryPayload,
	HostsPayload,
	QueueLane
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
