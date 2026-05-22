export const WORKBENCH_FILTER_STORAGE_KEY = 'mediaforce.workbench.filters.v1';

export type WorkbenchFilterState = {
	searchQuery: string;
	libraryFilters: Record<string, boolean>;
	stateFilters: Record<string, boolean>;
};

export type WorkbenchFilterMode = 'queue' | 'folders';

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

export function workbenchFilterStorageKey(mode: WorkbenchFilterMode): string {
	return `${WORKBENCH_FILTER_STORAGE_KEY}.${mode}`;
}

export function readStoredWorkbenchFilters(mode: WorkbenchFilterMode): WorkbenchFilterState | null {
	if (typeof window === 'undefined') return null;
	try {
		return parseStoredWorkbenchFilters(
			window.localStorage.getItem(workbenchFilterStorageKey(mode))
		);
	} catch {
		return null;
	}
}

export function writeStoredWorkbenchFilters(
	mode: WorkbenchFilterMode,
	filters: WorkbenchFilterState
): void {
	if (typeof window === 'undefined') return;
	try {
		window.localStorage.setItem(workbenchFilterStorageKey(mode), JSON.stringify(filters));
	} catch {
		// Ignore unavailable or quota-limited storage so the view still works.
	}
}

export function clearStoredWorkbenchFilters(mode: WorkbenchFilterMode): void {
	if (typeof window === 'undefined') return;
	try {
		window.localStorage.removeItem(workbenchFilterStorageKey(mode));
	} catch {
		// Ignore unavailable storage.
	}
}
