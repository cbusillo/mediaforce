import { describe, expect, it } from 'vitest';
import type { FolderCard } from '$lib/api/types';
import { queueFolderState, queueStateLabel } from './queue-workstation';

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
});
