import { describe, expect, it } from 'vitest';

import { parseStoredWorkbenchFilters } from './home-workbench';

describe('home workbench filter storage', () => {
	it('restores only valid persisted filter values', () => {
		expect(
			parseStoredWorkbenchFilters(
				JSON.stringify({
					searchQuery: 'hevc',
					libraryFilters: { tv: false, movies: true, stale: 'nope' },
					stateFilters: { pending: false, ready: true, broken: null }
				})
			)
		).toEqual({
			searchQuery: 'hevc',
			libraryFilters: { tv: false, movies: true },
			stateFilters: { pending: false, ready: true }
		});
	});

	it('ignores missing or invalid stored filters', () => {
		expect(parseStoredWorkbenchFilters(null)).toBeNull();
		expect(parseStoredWorkbenchFilters('{')).toBeNull();
	});
});
