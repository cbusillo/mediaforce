import { describe, expect, it } from 'vitest';

import type { FolderWorkflowState, OtherLibraryPayload, OtherWorkUnit } from '$lib/api/types';
import {
	mergeOtherLibraryPayloads,
	otherActionFileCount,
	otherReadinessBlockerCopy,
	otherSampleSetupResult,
	otherScopeSummary,
	otherWorkflowDetail,
	otherWorkflowLabel
} from './library';

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

function workflow(
	primaryLane: FolderWorkflowState['primary_lane'],
	laneCounts: Record<string, number> = {}
): FolderWorkflowState {
	return {
		prefix: workUnit.prefix,
		state: `${primaryLane}_state`,
		primary_lane: primaryLane,
		label: `Raw ${primaryLane} label`,
		tone: primaryLane === 'processing' ? 'active' : 'ready',
		detail: `/raw/path with ${primaryLane} status`,
		counts: { total: 2 },
		lane_counts: laneCounts,
		state_counts: {},
		next_action: {
			kind: 'none',
			label: 'Raw action',
			enabled: false,
			target_prefix: workUnit.prefix
		},
		blockers: []
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

describe('Other workflow presentation', () => {
	it.each([
		['encode', 'Ready to compress'],
		['validate', 'Ready to check'],
		['promote', 'Ready to replace'],
		['processing', 'Compressing'],
		['attention', 'Needs attention'],
		['blocked', 'Needs attention'],
		['complete', 'Finished'],
		['mixed', 'Several steps'],
		['none', 'No work needed']
	] as const)('maps %s without exposing backend vocabulary', (lane, expected) => {
		expect(otherWorkflowLabel(workflow(lane))).toBe(expected);
	});

	it('keeps lifecycle-held scopes protected', () => {
		expect(otherWorkflowLabel({ ...workflow('none'), state: 'held' })).toBe('Protected');
	});

	it('builds truthful singular and plural phase details', () => {
		expect(otherWorkflowDetail(workflow('validate', { validate: 1 }), 3)).toBe(
			'1 compressed file needs a final safety check.'
		);
		expect(otherWorkflowDetail(workflow('promote', { promote: 2 }), 3)).toBe(
			'2 checked files can replace their originals.'
		);
		expect(otherWorkflowDetail(workflow('processing'), 1)).toBe(
			'Mediaforce is compressing this file now.'
		);
	});

	it('uses current-action membership rather than the encode lane total', () => {
		expect(otherWorkflowDetail(workflow('encode', { encode: 5 }), 2)).toBe(
			'2 files are ready to compress.'
		);
		expect(otherWorkflowDetail(workflow('encode', { encode: 5 }), 0, false)).toBe(
			'Narrow the folder before Mediaforce can confirm which files are ready to compress.'
		);
	});

	it('keeps banned primary terms and raw paths out of mapped copy', () => {
		const copy = [
			'encode',
			'validate',
			'promote',
			'processing',
			'attention',
			'blocked',
			'complete',
			'mixed'
		]
			.map((lane) => {
				const state = workflow(lane as FolderWorkflowState['primary_lane'], {
					encode: 1,
					validate: 1,
					promote: 1
				});
				return `${otherWorkflowLabel(state)} ${otherWorkflowDetail(state, 2)}`;
			})
			.join(' ');

		expect(copy).not.toMatch(
			/\b(?:validate|validated|promote|promoted|publish|deliver|encode|work units?|semantics|inference|workers?|hosts?)\b/i
		);
		expect(copy).not.toContain('/raw/path');
	});
});

describe('Other scope presentation', () => {
	it('states included and untouched file counts', () => {
		expect(otherScopeSummary(5, 3, true, 250)).toEqual({
			included: '3 of 5',
			untouched: '2 files',
			confirmation: '3 files will be compressed. 2 files stay untouched.'
		});
		expect(otherScopeSummary(1, 1, true, 250)).toEqual({
			included: '1 of 1',
			untouched: 'None',
			confirmation: '1 file will be compressed. No files are left out.'
		});
	});

	it('does not claim complete membership above the safe limit', () => {
		expect(otherScopeSummary(300, 0, false, 250)).toEqual({
			included: 'More than 250 files',
			untouched: 'Not known until the folder is smaller',
			confirmation: 'Use one file at a time or split the folder before starting work.'
		});
	});

	it('uses the current phase count instead of the whole folder count', () => {
		expect(otherActionFileCount(workflow('encode', { encode: 4 }), 3, 5)).toBe(3);
		expect(otherActionFileCount(workflow('validate', { validate: 2 }), 0, 5)).toBe(2);
		expect(otherActionFileCount(workflow('promote', { promote: 1 }), 0, 5)).toBe(1);
	});

	it('translates backend readiness recovery into primary operator language', () => {
		expect(
			otherReadinessBlockerCopy(
				'The complete membership cannot be reviewed as one bounded work unit before sampling or queueing this scope.'
			)
		).toBe(
			'The complete membership cannot be reviewed as one folder selection before creating a sample or starting work on this scope.'
		);
		expect(otherReadinessBlockerCopy('Choose exact-file grouping before processing.')).toBe(
			'Choose one-file-at-a-time before compression.'
		);
		expect(otherReadinessBlockerCopy('Processing cannot start for this work unit.')).toBe(
			'Compression cannot start for this folder or file.'
		);
		expect(otherReadinessBlockerCopy('Bounded work units need processing.')).toBe(
			'Folder selections need compression.'
		);
		expect(otherReadinessBlockerCopy('Use work units before processing.')).toBe(
			'Use folders or files before compression.'
		);
	});
});

describe('Other sample setup result', () => {
	it('distinguishes a queueable setup from a nonqueueable request', () => {
		expect(otherSampleSetupResult(true)).toEqual({
			message: 'Sample setup is ready. Choose Create sample when you are ready.',
			attention: false
		});
		expect(otherSampleSetupResult(false)).toEqual({
			message: 'The sample setup needs another request. Nothing was queued.',
			attention: true,
			attentionTitle: 'Sample setup needs attention'
		});
	});
});
