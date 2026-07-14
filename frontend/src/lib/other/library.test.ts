import { describe, expect, it } from 'vitest';

import type { OtherLibraryPayload, OtherWorkUnit } from '$lib/api/types';
import { mergeOtherLibraryPayloads } from './library';

const workUnit: OtherWorkUnit = {
	prefix: 'other/Collection',
	root: 'other',
	library_label: 'Other',
	availability: 'production',
	default_profile: 'other_conservative',
	policy: { grouping: 'folder' },
	title: 'Collection',
	subtitle: 'Other',
	scope_label: 'Folder',
	scope_mode: 'folder',
	media_scope: {
		schema_version: 1,
		prefix: 'other/Collection',
		root: 'other',
		domain: 'other',
		kind: 'media_folder',
		match: 'descendants',
		title: 'Collection',
		subtitle: 'Other',
		scope_label: 'Folder',
		parent: { prefix: 'other', title: 'Other' }
	},
	item_count: 2,
	total_size_bytes: 100,
	blocked_item_count: 0,
	statuses: { discovered: 2 },
	video_codecs: { h264: 2 },
	profile_readiness: {
		state: 'ready',
		label: 'Profile ready',
		detail: 'All files are ready.',
		profile: 'other_conservative',
		profile_label: 'Conservative',
		blockers: []
	},
	membership_requires_confirmation: true,
	details_loading: true
};

function payload(unit: OtherWorkUnit): OtherLibraryPayload {
	return {
		schema_version: 1,
		libraries: [],
		work_units: [unit],
		catalog_empty: false,
		catalog_truncated: false,
		catalog_item_limit: 5000,
		catalog_work_unit_limit: 500,
		details_loading: unit.details_loading
	};
}

describe('mergeOtherLibraryPayloads', () => {
	it('keeps structure order while hydrating workflow and reclaim details', () => {
		const merged = mergeOtherLibraryPayloads(
			payload(workUnit),
			payload({
				...workUnit,
				projected_reclaim_bytes: 40,
				workflow_state: {
					prefix: workUnit.prefix,
					state: 'encode_candidates',
					primary_lane: 'encode',
					label: 'Ready to encode',
					tone: 'ready',
					detail: '2 files are ready.',
					counts: { total: 2 },
					lane_counts: { encode: 2 },
					state_counts: { encode_candidate: 2 },
					next_action: {
						kind: 'queue_encode',
						label: 'Queue encode',
						enabled: true,
						target_prefix: workUnit.prefix
					},
					blockers: []
				},
				details_loading: false
			})
		);

		expect(merged.work_units[0]?.projected_reclaim_bytes).toBe(40);
		expect(merged.work_units[0]?.workflow_state?.primary_lane).toBe('encode');
		expect(merged.work_units[0]?.details_loading).toBe(false);
	});
});
