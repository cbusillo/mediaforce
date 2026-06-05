import { describe, expect, it } from 'vitest';
import type { FolderCard } from '$lib/api/types';
import {
	folderStatusCopy,
	queueFolderState,
	queueFolderTone,
	queueStateLabel,
	totalPendingItems
} from './queue-workstation';

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

	it('maps workflow attention to a fail tone for workbench scanning', () => {
		expect(queueFolderTone(folder({ workflow_state: workflowState({ tone: 'attention' }) }))).toBe(
			'fail'
		);
	});
});
