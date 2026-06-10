export const WORKBENCH_FILTER_STORAGE_KEY = 'mediaforce.workbench.filters.v1';
export const LEGACY_WORKBENCH_FILTER_STORAGE_KEY = 'mediaforce.queue.filters.v1';

export type WorkbenchFilterState = {
	searchQuery: string;
	libraryFilters: Record<string, boolean>;
	stateFilters: Record<string, boolean>;
};

export type WorkbenchFilterMode = 'queue' | 'folders';

export const WORKBENCH_PAGE_SIZE = 32;

export type WorkbenchPage<T> = {
	page: number;
	pageSize: number;
	totalRows: number;
	totalPages: number;
	startIndex: number;
	endIndex: number;
	rows: T[];
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

export function workbenchFilterStorageKey(mode: WorkbenchFilterMode): string {
	return `${WORKBENCH_FILTER_STORAGE_KEY}.${mode}`;
}

export function readStoredWorkbenchFilters(mode: WorkbenchFilterMode): WorkbenchFilterState | null {
	if (typeof window === 'undefined') return null;
	try {
		const storedFilters = parseStoredWorkbenchFilters(
			window.localStorage.getItem(workbenchFilterStorageKey(mode))
		);
		if (storedFilters || mode !== 'queue') return storedFilters;
		const legacyFilters = parseStoredWorkbenchFilters(
			window.localStorage.getItem(LEGACY_WORKBENCH_FILTER_STORAGE_KEY)
		);
		if (legacyFilters) {
			writeStoredWorkbenchFilters(mode, legacyFilters);
			window.localStorage.removeItem(LEGACY_WORKBENCH_FILTER_STORAGE_KEY);
		}
		return legacyFilters;
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

export function normalizeWorkbenchPage(
	requestedPage: number,
	totalRows: number,
	pageSize = WORKBENCH_PAGE_SIZE
): number {
	const normalizedTotal = Math.max(Math.floor(totalRows), 0);
	const normalizedPageSize = Math.max(Math.floor(pageSize), 1);
	const totalPages = Math.max(Math.ceil(normalizedTotal / normalizedPageSize), 1);
	if (!Number.isFinite(requestedPage)) return 1;
	return Math.min(Math.max(Math.floor(requestedPage), 1), totalPages);
}

export function paginateWorkbenchRows<T>(
	rows: T[],
	requestedPage: number,
	pageSize = WORKBENCH_PAGE_SIZE
): WorkbenchPage<T> {
	const normalizedPageSize = Math.max(Math.floor(pageSize), 1);
	const totalRows = rows.length;
	const totalPages = Math.max(Math.ceil(totalRows / normalizedPageSize), 1);
	const page = normalizeWorkbenchPage(requestedPage, totalRows, normalizedPageSize);
	const startIndex = totalRows === 0 ? 0 : (page - 1) * normalizedPageSize;
	const endIndex = Math.min(startIndex + normalizedPageSize, totalRows);
	return {
		page,
		pageSize: normalizedPageSize,
		totalRows,
		totalPages,
		startIndex,
		endIndex,
		rows: rows.slice(startIndex, endIndex)
	};
}
