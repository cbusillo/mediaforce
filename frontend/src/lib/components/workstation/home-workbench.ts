export const WORKBENCH_FILTER_STORAGE_KEY = 'mediaforce.queue.filters.v1';

export type WorkbenchFilterState = {
	searchQuery: string;
	libraryFilters: Record<string, boolean>;
	stateFilters: Record<string, boolean>;
};

type StoredFilters = {
	searchQuery?: unknown;
	libraryFilters?: unknown;
	stateFilters?: unknown;
};

export function normalizeStoredFilterRecord(value: unknown): Record<string, boolean> {
	if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
	return Object.fromEntries(
		Object.entries(value).filter(
			(entry): entry is [string, boolean] => typeof entry[1] === 'boolean'
		)
	);
}

export function parseStoredWorkbenchFilters(
	rawFilters: string | null
): WorkbenchFilterState | null {
	if (!rawFilters) return null;
	try {
		const stored = JSON.parse(rawFilters) as StoredFilters;
		return {
			searchQuery: typeof stored.searchQuery === 'string' ? stored.searchQuery : '',
			libraryFilters: normalizeStoredFilterRecord(stored.libraryFilters),
			stateFilters: normalizeStoredFilterRecord(stored.stateFilters)
		};
	} catch {
		return null;
	}
}
