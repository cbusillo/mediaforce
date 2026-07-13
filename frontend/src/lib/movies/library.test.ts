import { describe, expect, it } from 'vitest';

import type { MovieLibraryPayload, MovieTitle } from '$lib/api/types';
import {
	mergeMovieLibraryPayloads,
	movieReclaimLowerBound,
	movieReclaimTotalIsLowerBound
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
