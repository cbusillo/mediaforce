import { describe, expect, it } from 'vitest';

import {
	clearStoredWorkbenchFilters,
	LEGACY_WORKBENCH_FILTER_STORAGE_KEY,
	normalizeWorkbenchPage,
	paginateWorkbenchRows,
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

	it('migrates legacy queue filters to the scoped queue key', () => {
		const originalWindow = globalThis.window;
		const values = new Map<string, string>();
		values.set(
			LEGACY_WORKBENCH_FILTER_STORAGE_KEY,
			JSON.stringify({
				searchQuery: 'terminator',
				libraryFilters: { tv: false },
				stateFilters: { ready: true }
			})
		);
		const storage = {
			getItem(key: string) {
				return values.get(key) ?? null;
			},
			setItem(key: string, value: string) {
				values.set(key, value);
			},
			removeItem(key: string) {
				values.delete(key);
			}
		} as unknown as Storage;

		Object.defineProperty(globalThis, 'window', {
			configurable: true,
			value: { localStorage: storage }
		});

		expect(readStoredWorkbenchFilters('queue')).toEqual({
			searchQuery: 'terminator',
			libraryFilters: { tv: false },
			stateFilters: { ready: true }
		});
		expect(values.has(LEGACY_WORKBENCH_FILTER_STORAGE_KEY)).toBe(false);
		expect(
			parseStoredWorkbenchFilters(values.get(workbenchFilterStorageKey('queue')) ?? null)
		).toEqual({
			searchQuery: 'terminator',
			libraryFilters: { tv: false },
			stateFilters: { ready: true }
		});

		Object.defineProperty(globalThis, 'window', {
			configurable: true,
			value: originalWindow
		});
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

describe('home workbench row reachability', () => {
	it('normalizes requested pages into the reachable range', () => {
		expect(normalizeWorkbenchPage(1, 64, 32)).toBe(1);
		expect(normalizeWorkbenchPage(2, 64, 32)).toBe(2);
		expect(normalizeWorkbenchPage(9, 64, 32)).toBe(2);
		expect(normalizeWorkbenchPage(0, 64, 32)).toBe(1);
		expect(normalizeWorkbenchPage(Number.NaN, 64, 32)).toBe(1);
		expect(normalizeWorkbenchPage(5, 0, 32)).toBe(1);
	});

	it('returns the visible page rows with explicit range metadata', () => {
		const rows = Array.from({ length: 85 }, (_, index) => `row-${index + 1}`);

		expect(paginateWorkbenchRows(rows, 1, 32)).toEqual({
			page: 1,
			pageSize: 32,
			totalRows: 85,
			totalPages: 3,
			startIndex: 0,
			endIndex: 32,
			rows: rows.slice(0, 32)
		});
		expect(paginateWorkbenchRows(rows, 3, 32)).toEqual({
			page: 3,
			pageSize: 32,
			totalRows: 85,
			totalPages: 3,
			startIndex: 64,
			endIndex: 85,
			rows: rows.slice(64, 85)
		});
	});

	it('clamps an out-of-range page without hiding rows from the total', () => {
		const rows = Array.from({ length: 33 }, (_, index) => index);
		const page = paginateWorkbenchRows(rows, 99, 32);

		expect(page.page).toBe(2);
		expect(page.totalRows).toBe(33);
		expect(page.totalPages).toBe(2);
		expect(page.rows).toEqual([32]);
	});
});
