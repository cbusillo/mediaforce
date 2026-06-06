import { describe, expect, it } from 'vitest';
import type { FolderCard } from '$lib/api/types';
import {
	folderStatusCopy,
	isQueueActionableFolder,
	queuePrimaryActionLabel,
	queueFolderState,
	queueFolderTone,
	queueStateLabel,
	buildQueueStatusTiles,
	totalPendingItems
} from './queue-workstation';

const emptyQueueLane = {
	running: [],
	queued: [],
	pending_review: [],
	running_count: 0,
	queued_count: 0,
	pending_review_count: 0
};

function folder(overrides: Partial<FolderCard>): FolderCard {
	return {
		prefix: 'tv/show',
		title: 'Show',
		subtitle: 'TV',
		scope_label: 'tv',
		item_count: 4,
		pending_count: 0,
		total_size_bytes: 1024,
		estimated_savings_bytes: 0,
		known_saved_bytes: 0,
		projected_reclaim_bytes: 512,
		average_age_days: 12,
		sort_score: 1,
		statuses: {},
		video_codecs: {},
		details_loading: false,
		...overrides
	};
}

function workflowState(overrides: Partial<NonNullable<FolderCard['workflow_state']>> = {}) {
	return {
		prefix: 'tv/show',
		state: 'ready_to_validate',
		primary_lane: 'validate' as const,
		label: 'Ready to validate',
		tone: 'ready' as const,
		detail: '2 encoded output(s) need validation.',
		counts: {
			items: 4,
			encode_candidates: 0,
			ready_to_validate: 2,
			ready_to_promote: 0,
			processing: 0,
			complete: 2,
			blocked: 0
		},
		lane_counts: {},
		state_counts: {},
		next_action: {
			kind: 'validate_outputs' as const,
			label: 'Validate outputs',
			enabled: true,
			target_prefix: 'tv/show'
		},
		blockers: [],
		...overrides
	};
}

describe('Queue workstation labels', () => {
	const dashboard = {
		folders_preview: [],
		library_colors: {},
		scan_job: null,
		calibration_queue: {
			folders_preview: [],
			sample: emptyQueueLane,
			full: emptyQueueLane,
			active_count: 0
		},
		encode_queue: {
			running_count: 0,
			queued_count: 0,
			running: [],
			queued: [],
			state: { stop_requested: false, is_paused: false, scheduler_summary: 'idle' }
		},
		catalog_empty: false,
		folder_cache_key: 'test',
		metric_support: { vmaf: true, xpsnr: false, ssim: false },
		metric_status_copy: 'vmaf'
	};
	const hosts = { compact: true, hosts: [] };

	it('normalizes workflow state labels into basic-user actions', () => {
		expect(queueStateLabel('Ready to start')).toBe('Needs sample');
		expect(queueStateLabel('Sample queued')).toBe('Sample waiting');
		expect(queueStateLabel('Review media')).toBe('Ready to review');
		expect(queueStateLabel('Encode failed')).toBe('Processing needs attention');
	});

	it('uses action-ready fallback labels when backend state is absent', () => {
		expect(queueFolderState(folder({ pending_count: 3 }))).toBe('Needs sample');
		expect(queueFolderState(folder({ known_saved_bytes: 2048 }))).toBe('Completed');
		expect(queueFolderState(folder({}))).toBe('Cataloged');
	});

	it('prefers canonical workflow state over pending-count fallbacks', () => {
		const readyToValidate = folder({ pending_count: 3, workflow_state: workflowState() });

		expect(queueFolderState(readyToValidate)).toBe('Ready to validate');
		expect(queueFolderTone(readyToValidate)).toBe('ready');
		expect(folderStatusCopy(readyToValidate)).toBe('2 encoded output(s) need validation.');
		expect(totalPendingItems([readyToValidate])).toBe(2);
	});

	it('keeps the queue limited to folders with actionable workflow', () => {
		const readyToValidate = folder({ workflow_state: workflowState() });

		expect(isQueueActionableFolder(readyToValidate)).toBe(true);
		expect(queuePrimaryActionLabel(readyToValidate)).toBe('Validate 2 outputs');
		expect(
			isQueueActionableFolder(
				folder({
					workflow_state: workflowState({
						state: 'complete',
						primary_lane: 'complete',
						label: 'Complete',
						tone: 'success',
						detail: 'No remaining workflow action is available for this scope.',
						counts: {
							items: 4,
							encode_candidates: 0,
							ready_to_validate: 0,
							ready_to_promote: 0,
							processing: 0,
							complete: 4,
							blocked: 0
						},
						next_action: {
							kind: 'none',
							label: 'No action',
							enabled: false,
							target_prefix: 'tv/show'
						}
					})
				})
			)
		).toBe(false);
		expect(isQueueActionableFolder(folder({ pending_count: 3 }))).toBe(true);
		expect(isQueueActionableFolder(folder({ pending_count: 0 }))).toBe(false);
	});

	it('uses safe queue action labels when no workflow action is enabled', () => {
		expect(queuePrimaryActionLabel(folder({ pending_count: 3 }))).toBe('Open Folder Studio');
		expect(
			queuePrimaryActionLabel(
				folder({
					workflow_state: workflowState({
						state: 'complete',
						primary_lane: 'complete',
						label: 'Complete',
						tone: 'success',
						next_action: {
							kind: 'none',
							label: 'No action',
							enabled: false,
							target_prefix: 'tv/show'
						}
					})
				})
			)
		).toBe('Review completed scope');
	});

	it('maps workflow attention to a fail tone for workbench scanning', () => {
		expect(queueFolderTone(folder({ workflow_state: workflowState({ tone: 'attention' }) }))).toBe(
			'fail'
		);
	});

	it('describes status tiles with the active folder scope', () => {
		const tiles = buildQueueStatusTiles(
			dashboard,
			{
				folders: [folder({ workflow_state: workflowState() })],
				catalog_empty: false,
				folder_cache_key: 'test'
			},
			hosts,
			'Whole shows'
		);

		expect(tiles[0].value).toBe('1 whole shows');
		expect(tiles[0].detail).toBe('2 open work');
		expect(tiles[1].detail).toBe('estimated from visible whole shows');
	});
});
