import { describe, expect, it } from 'vitest';

import {
	clearStoredWorkbenchFilters,
	parseStoredWorkbenchFilters,
	readStoredWorkbenchFilters,
	workbenchFilterStorageKey,
	writeStoredWorkbenchFilters
} from './home-workbench';

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

	it('scopes persisted filter keys by route mode', () => {
		expect(workbenchFilterStorageKey('queue')).toBe('mediaforce.workbench.filters.v1.queue');
		expect(workbenchFilterStorageKey('folders')).toBe('mediaforce.workbench.filters.v1.folders');
	});

	it('skips storage errors when reading or writing persisted filters', () => {
		const originalWindow = globalThis.window;
		const storage = {
			getItem() {
				throw new Error('blocked');
			},
			setItem() {
				throw new Error('blocked');
			},
			removeItem() {
				throw new Error('blocked');
			}
		} as unknown as Storage;

		Object.defineProperty(globalThis, 'window', {
			configurable: true,
			value: { localStorage: storage }
		});

		expect(readStoredWorkbenchFilters('queue')).toBeNull();
		expect(() =>
			writeStoredWorkbenchFilters('queue', {
				searchQuery: 'hevc',
				libraryFilters: { tv: true },
				stateFilters: { ready: false }
			})
		).not.toThrow();
		expect(() => clearStoredWorkbenchFilters('queue')).not.toThrow();

		Object.defineProperty(globalThis, 'window', {
			configurable: true,
			value: originalWindow
		});
	});
});
