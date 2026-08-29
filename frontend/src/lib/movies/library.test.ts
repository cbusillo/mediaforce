import { describe, expect, it } from 'vitest';

import type { MovieLibraryPayload, MovieTitle } from '$lib/api/types';
import {
	mergeMovieLibraryPayloads,
	movieCompositionDetail,
	movieExpectedOutputBytes,
	moviePrimaryStudioPrefix,
	movieReclaimLowerBound,
	movieReclaimTotalIsLowerBound,
	movieTitleOwnsActiveWork,
	movieTitleNeedsAction,
	movieTitleRuntimeSeconds,
	movieWorkflowIsComplete,
	movieWorkflowLabel,
	selectMovieLeadTitle,
	selectMovieTitle,
	sortMovieTitles
} from './library';

const title: MovieTitle = {
	prefix: 'films/Example',
	root: 'films',
	library_label: 'Movies',
	availability: 'production',
	policy: {},
	title: 'Example',
	scope_mode: 'title_folder',
	item_count: 1,
	feature_count: 1,
	edition_count: 1,
	extra_count: 0,
	uncertain_count: 0,
	included_item_count: 1,
	total_size_bytes: 10,
	included_size_bytes: 10,
	savings_confidence: 'pending',
	promotion_conflicts: [],
	members: [
		{
			item_id: 1,
			prefix: 'films/Example/Example.mkv',
			rel_path: 'films/Example/Example.mkv',
			root: 'films',
			title_prefix: 'films/Example',
			title: 'Example',
			scope_mode: 'title_folder',
			role: 'feature',
			label: 'Example',
			status: 'discovered',
			size_bytes: 10,
			included_by_default: true,
			exact_action_available: true,
			promotion_conflicts: [],
			details_loading: true
		}
	],
	details_loading: true
};

function payload(currentTitle: MovieTitle): MovieLibraryPayload {
	return {
		schema_version: 1,
		libraries: [],
		titles: [currentTitle],
		catalog_empty: false,
		details_loading: currentTitle.details_loading
	};
}

describe('mergeMovieLibraryPayloads', () => {
	it('preserves structure order while hydrating title and member details', () => {
		const merged = mergeMovieLibraryPayloads(
			payload(title),
			payload({
				...title,
				projected_reclaim_bytes: 4,
				savings_confidence: 'estimated',
				details_loading: false,
				members: [
					{
						...title.members[0],
						details_loading: false,
						age: { source: 'plex', timestamp: '2020-01-01T00:00:00+00:00' }
					}
				]
			})
		);

		expect(merged.titles[0]?.projected_reclaim_bytes).toBe(4);
		expect(merged.titles[0]?.members[0]?.age?.source).toBe('plex');
	});

	it('preserves an unavailable reclaim estimate instead of coercing it to zero', () => {
		const merged = mergeMovieLibraryPayloads(
			payload(title),
			payload({
				...title,
				projected_reclaim_bytes: null,
				estimated_savings_bytes: null,
				known_saved_bytes: 0,
				savings_confidence: 'unavailable',
				details_loading: false
			})
		);

		expect(merged.titles[0]?.projected_reclaim_bytes).toBeNull();
		expect(merged.titles[0]?.estimated_savings_bytes).toBeNull();
		expect(merged.titles[0]?.savings_confidence).toBe('unavailable');
	});
});

describe('movieReclaimLowerBound', () => {
	it('uses measured savings when a mixed title has no complete projection', () => {
		expect(
			movieReclaimLowerBound({
				...title,
				projected_reclaim_bytes: null,
				known_saved_bytes: 4,
				savings_confidence: 'unavailable',
				details_loading: false
			})
		).toBe(4);
	});

	it('keeps fully unmeasured titles unavailable', () => {
		expect(
			movieReclaimLowerBound({
				...title,
				projected_reclaim_bytes: null,
				known_saved_bytes: 0,
				savings_confidence: 'unavailable',
				details_loading: false
			})
		).toBeNull();
	});
});

describe('movieReclaimTotalIsLowerBound', () => {
	it('qualifies the total when a title has only a measured lower bound', () => {
		expect(
			movieReclaimTotalIsLowerBound([
				{
					...title,
					projected_reclaim_bytes: null,
					known_saved_bytes: 4,
					savings_confidence: 'unavailable',
					details_loading: false
				}
			])
		).toBe(true);
	});

	it('treats zero as a complete projection when it is explicitly measured', () => {
		expect(
			movieReclaimTotalIsLowerBound([
				{
					...title,
					projected_reclaim_bytes: 0,
					known_saved_bytes: 0,
					savings_confidence: 'measured',
					details_loading: false
				}
			])
		).toBe(false);
	});
});

describe('movie decision facts', () => {
	it('uses the longest feature runtime without adding alternate editions together', () => {
		expect(
			movieTitleRuntimeSeconds({
				...title,
				edition_count: 2,
				members: [
					{ ...title.members[0], duration_seconds: 5400 },
					{
						...title.members[0],
						item_id: 2,
						prefix: 'films/Example/Directors Cut.mkv',
						duration_seconds: 6000
					}
				]
			})
		).toBe(6000);
	});

	it('keeps runtime unknown when only an extra has a measured duration', () => {
		expect(
			movieTitleRuntimeSeconds({
				...title,
				extra_count: 1,
				members: [
					{ ...title.members[0], duration_seconds: undefined },
					{
						...title.members[0],
						item_id: 2,
						prefix: 'films/Example/Behind the Scenes.mkv',
						role: 'extra',
						duration_seconds: 1800
					}
				]
			})
		).toBeNull();
	});

	it('derives expected output from measured or projected savings', () => {
		expect(
			movieExpectedOutputBytes({ ...title, total_size_bytes: 100, projected_reclaim_bytes: 35 })
		).toBe(65);
		expect(movieExpectedOutputBytes({ ...title, projected_reclaim_bytes: null })).toBeNull();
		expect(
			movieExpectedOutputBytes({
				...title,
				total_size_bytes: 100,
				projected_reclaim_bytes: null,
				known_saved_bytes: 35
			})
		).toBeNull();
	});

	it('uses the exact member route for idle one-file titles', () => {
		expect(moviePrimaryStudioPrefix(title)).toBe('films/Example/Example.mkv');
		expect(
			moviePrimaryStudioPrefix({
				...title,
				item_count: 2,
				members: [
					title.members[0],
					{ ...title.members[0], item_id: 2, prefix: 'films/Example/Alternate.mkv' }
				]
			})
		).toBe(title.prefix);
	});

	it.each([
		['proposal', { label: 'Sample plan ready' }, null],
		['pending review', { label: 'Review pending' }, null],
		['accepted encode', { label: 'Approved draft' }, 'encode'],
		['processing', null, 'processing'],
		['validate', null, 'validate'],
		['promote', null, 'promote']
	] as const)(
		'opens the title route when one-file title work is active: %s',
		(_state, reviewBadge, primaryLane) => {
			const activeTitle: MovieTitle = {
				...title,
				review_badge: reviewBadge,
				workflow_state: primaryLane
					? {
							prefix: title.prefix,
							state: `${primaryLane}_candidates`,
							primary_lane: primaryLane,
							label: primaryLane,
							tone: primaryLane === 'processing' ? 'active' : 'ready',
							detail: '',
							counts: {},
							lane_counts: {},
							state_counts: {},
							next_action: { kind: 'none', label: '', enabled: false, target_prefix: title.prefix },
							blockers: []
						}
					: null
			};

			expect(movieTitleOwnsActiveWork(activeTitle)).toBe(true);
			expect(moviePrimaryStudioPrefix(activeTitle)).toBe(title.prefix);
		}
	);

	it('uses the exact member route after one-file title work is complete', () => {
		const completeTitle: MovieTitle = {
			...title,
			workflow_state: {
				prefix: title.prefix,
				state: 'complete',
				primary_lane: 'complete',
				label: 'Finished',
				tone: 'success',
				detail: '',
				counts: {},
				lane_counts: {},
				state_counts: {},
				next_action: { kind: 'none', label: '', enabled: false, target_prefix: title.prefix },
				blockers: []
			}
		};

		expect(movieTitleOwnsActiveWork(completeTitle)).toBe(false);
		expect(moviePrimaryStudioPrefix(completeTitle)).toBe(title.members[0].prefix);
	});

	it('only calls out exceptional file composition', () => {
		expect(movieCompositionDetail(title)).toBeNull();
		expect(movieCompositionDetail({ ...title, edition_count: 2 })).toBe('2 editions');
		expect(movieCompositionDetail({ ...title, extra_count: 1 })).toBe('1 extra');
		expect(movieCompositionDetail({ ...title, uncertain_count: 2 })).toBe('2 files need a choice');
		expect(
			movieCompositionDetail({
				...title,
				item_count: 4,
				edition_count: 2,
				extra_count: 1,
				uncertain_count: 1
			})
		).toBe('2 editions · 1 extra · 1 file needs a choice');
	});
});

describe('movie action discoverability', () => {
	const readyTitle: MovieTitle = {
		...title,
		prefix: 'films/Ready',
		title: 'Ready',
		details_loading: false,
		workflow_state: {
			prefix: 'films/Ready',
			state: 'ready_to_promote',
			primary_lane: 'promote',
			label: 'Ready to promote',
			tone: 'ready',
			detail: 'One validated output is ready.',
			counts: {},
			lane_counts: {},
			state_counts: {},
			next_action: {
				kind: 'promote_outputs',
				label: 'Promote',
				enabled: true,
				target_prefix: 'films/Ready'
			},
			blockers: []
		}
	};

	it('selects the first actionable title for the default priority view', () => {
		expect(movieTitleNeedsAction(readyTitle)).toBe(true);
		expect(movieWorkflowLabel(readyTitle)).toBe('Ready to replace');
		expect(selectMovieLeadTitle([title, readyTitle], 'priority', '')?.prefix).toBe('films/Ready');
	});

	it('labels active movie work as compression', () => {
		expect(
			movieWorkflowLabel({
				...readyTitle,
				workflow_state: {
					...readyTitle.workflow_state!,
					primary_lane: 'processing',
					label: 'Processing'
				}
			})
		).toBe('Compressing');
	});

	it('treats a required file choice as actionable without exposing the backend label', () => {
		const explicitTitle: MovieTitle = {
			...readyTitle,
			workflow_state: {
				...readyTitle.workflow_state!,
				state: 'explicit_selection_required',
				primary_lane: 'none',
				label: 'Explicit selection required',
				next_action: {
					kind: 'review_scope',
					label: 'Review title files',
					enabled: true,
					target_prefix: readyTitle.prefix
				}
			}
		};

		expect(movieTitleNeedsAction(explicitTitle)).toBe(true);
		expect(movieWorkflowLabel(explicitTitle)).toBe('Choose a file');
		expect(selectMovieLeadTitle([title, explicitTitle], 'priority', '')).toBe(explicitTitle);
	});

	it('labels view-only and conflict states truthfully', () => {
		const browseOnlyTitle: MovieTitle = {
			...readyTitle,
			availability: 'browse_only',
			workflow_state: {
				...readyTitle.workflow_state!,
				state: 'browse_only',
				primary_lane: 'none',
				label: 'Browse only'
			}
		};
		const conflictTitle: MovieTitle = {
			...readyTitle,
			promotion_conflicts: [
				{
					kind: 'destination_exists',
					destination_path: '/movies/Ready.mkv',
					member_prefixes: [readyTitle.prefix],
					detail: 'A destination file exists.'
				}
			]
		};

		expect(movieTitleNeedsAction(browseOnlyTitle)).toBe(false);
		expect(movieWorkflowLabel(browseOnlyTitle)).toBe('View only');
		expect(movieTitleNeedsAction(conflictTitle)).toBe(true);
		expect(movieWorkflowLabel(conflictTitle)).toBe('Replacement blocked');
	});

	it('does not show a next-up shortcut while the operator is searching or using another sort', () => {
		expect(selectMovieLeadTitle([readyTitle], 'priority', 'ready')).toBeNull();
		expect(selectMovieLeadTitle([readyTitle], 'name', '')).toBeNull();
	});

	it('ranks work by stage before projected savings', () => {
		const promoteTitle = workflowTitle('Zulu replace', 'promote', 10);
		const validateTitle = workflowTitle('Alpha check', 'validate', 100);
		const mixedTitle = workflowTitle('Several steps', 'mixed', 500);
		const encodeTitle = workflowTitle('Start compression', 'encode', 1000);

		expect(
			sortMovieTitles([encodeTitle, mixedTitle, validateTitle, promoteTitle], 'priority').map(
				(currentTitle) => currentTitle.prefix
			)
		).toEqual([promoteTitle.prefix, validateTitle.prefix, mixedTitle.prefix, encodeTitle.prefix]);
	});

	it('ranks known savings first within a stage without inventing zero', () => {
		const biggest = workflowTitle('Zulu', 'encode', 500);
		const smaller = workflowTitle('Alpha', 'encode', 100);
		const measuredZero = workflowTitle('Measured zero', 'encode', 0);
		const unknown = workflowTitle('Unknown', 'encode', null);

		expect(
			sortMovieTitles([unknown, smaller, measuredZero, biggest], 'priority').map(
				(currentTitle) => currentTitle.title
			)
		).toEqual(['Zulu', 'Alpha', 'Measured zero', 'Unknown']);
	});

	it('uses title A–Z as the stable final priority tie-break', () => {
		const zulu = workflowTitle('Zulu', 'validate', null);
		const alpha = workflowTitle('Alpha', 'validate', null);

		expect(
			sortMovieTitles([zulu, alpha], 'priority').map((currentTitle) => currentTitle.title)
		).toEqual(['Alpha', 'Zulu']);
	});

	it('keeps attention visible but recommends the best safe next action', () => {
		const attentionTitle = workflowTitle('Needs review', 'attention', 1000);
		const processingTitle = workflowTitle('In progress', 'processing', 900);
		const completeTitle = workflowTitle('Finished', 'complete', 800, 'complete');
		const promoteTitle = workflowTitle('Ready', 'promote', 100);
		const ranked = sortMovieTitles(
			[processingTitle, completeTitle, promoteTitle, attentionTitle],
			'priority'
		);

		expect(ranked[0]).toBe(attentionTitle);
		expect(selectMovieLeadTitle(ranked, 'priority', '')).toBe(promoteTitle);
	});

	it('keeps the selected title stable when enrichment changes the ranking order', () => {
		const selected = workflowTitle('Selected', 'encode', null);
		const other = workflowTitle('Other', 'encode', null);
		const initial = sortMovieTitles([selected, other], 'priority');
		const enriched = sortMovieTitles(
			[
				{ ...selected, projected_reclaim_bytes: 10 },
				{ ...other, projected_reclaim_bytes: 100 }
			],
			'priority'
		);

		expect(selectMovieTitle(initial, selected.prefix)?.prefix).toBe(selected.prefix);
		expect(enriched[0]?.prefix).toBe(other.prefix);
		expect(selectMovieTitle(enriched, selected.prefix)?.prefix).toBe(selected.prefix);
	});

	it('lets completion override stale actionable lane data', () => {
		const completedTitle: MovieTitle = {
			...readyTitle,
			workflow_state: {
				...readyTitle.workflow_state!,
				state: 'complete',
				primary_lane: 'encode',
				label: 'Stale encode label'
			}
		};

		expect(movieWorkflowIsComplete(completedTitle.workflow_state)).toBe(true);
		expect(movieTitleNeedsAction(completedTitle)).toBe(false);
		expect(movieWorkflowLabel(completedTitle)).toBe('Finished');
	});

	it('recognizes a complete lane when the aggregate state is stale', () => {
		const completedTitle: MovieTitle = {
			...readyTitle,
			workflow_state: {
				...readyTitle.workflow_state!,
				state: 'ready_to_encode',
				primary_lane: 'complete',
				label: 'Stale state label'
			}
		};

		expect(movieWorkflowIsComplete(completedTitle.workflow_state)).toBe(true);
		expect(movieTitleNeedsAction(completedTitle)).toBe(false);
		expect(movieWorkflowLabel(completedTitle)).toBe('Finished');
	});
});

function workflowTitle(
	name: string,
	lane: NonNullable<MovieTitle['workflow_state']>['primary_lane'],
	projectedReclaim: number | null,
	state = `ready_to_${lane}`
): MovieTitle {
	return {
		...title,
		prefix: `films/${name}`,
		title: name,
		projected_reclaim_bytes: projectedReclaim,
		details_loading: false,
		savings_confidence: projectedReclaim == null ? 'unavailable' : 'estimated',
		workflow_state: {
			prefix: `films/${name}`,
			state,
			primary_lane: lane,
			label: name,
			tone: 'ready',
			detail: name,
			counts: {},
			lane_counts: {},
			state_counts: {},
			next_action: {
				kind: 'review_scope',
				label: 'Open movie',
				enabled: true,
				target_prefix: `films/${name}`
			},
			blockers: []
		}
	};
}
