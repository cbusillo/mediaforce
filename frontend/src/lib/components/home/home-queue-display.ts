import type { DashboardSummaryPayload, FolderCard as FolderCardData } from '$lib/api/types';
import { folderLibraryKey, folderLibraryLabel } from '$lib/folder-display';

export type FolderSortKey =
	| 'priority'
	| 'title'
	| 'library'
	| 'pending'
	| 'reclaim'
	| 'age'
	| 'review';
export type FolderSortDirection = 'asc' | 'desc';

export type RankedFolderRow = {
	folder: FolderCardData;
	index: number;
	priority: number;
	libraryLabel: string;
	reviewLabel: string;
};

export function deriveFolderLibraries(folders: FolderCardData[]) {
	const counts: Record<string, { key: string; label: string; count: number }> = {};

	for (const folder of folders) {
		const key = folderLibraryKey(folder.prefix);
		const existing = counts[key];
		if (existing) {
			existing.count += 1;
			continue;
		}
		counts[key] = { key, label: folderLibraryLabel(key), count: 1 };
	}

	return Object.values(counts).sort((left, right) => left.label.localeCompare(right.label));
}

export function defaultFolderSortDirection(key: FolderSortKey): FolderSortDirection {
	switch (key) {
		case 'priority':
		case 'title':
		case 'library':
		case 'review':
			return 'asc';
		case 'pending':
		case 'reclaim':
		case 'age':
			return 'desc';
	}
}

export function rankVisibleFolders(
	visibleFolders: FolderCardData[],
	folderSortKey: FolderSortKey,
	folderSortDirection: FolderSortDirection
): RankedFolderRow[] {
	const collator = new Intl.Collator('en-US', { sensitivity: 'base', numeric: true });
	const rankedFolders = visibleFolders.map((folder, index) => ({
		folder,
		index,
		priority: index + 1,
		libraryLabel: folderLibraryLabel(folderLibraryKey(folder.prefix)),
		reviewLabel: String(folder.review_badge_label ?? '')
	}));

	rankedFolders.sort((left, right) => {
		const direction = folderSortDirection === 'asc' ? 1 : -1;
		let comparison = 0;

		switch (folderSortKey) {
			case 'priority':
				comparison = left.index - right.index;
				break;
			case 'title':
				comparison = collator.compare(left.folder.title, right.folder.title);
				break;
			case 'library':
				comparison = collator.compare(left.libraryLabel, right.libraryLabel);
				break;
			case 'pending':
				comparison = left.folder.pending_count - right.folder.pending_count;
				break;
			case 'reclaim':
				comparison = left.folder.projected_reclaim_bytes - right.folder.projected_reclaim_bytes;
				break;
			case 'age':
				comparison = left.folder.average_age_days - right.folder.average_age_days;
				break;
			case 'review':
				comparison = collator.compare(left.reviewLabel, right.reviewLabel);
				break;
		}

		if (comparison === 0) {
			comparison = left.index - right.index;
		}

		return comparison * direction;
	});

	return rankedFolders;
}

export function parseIsoDate(value: string | null | undefined): Date | null {
	if (!value) {
		return null;
	}
	const parsed = new Date(value);
	return Number.isNaN(parsed.getTime()) ? null : parsed;
}

export function formatCount(value: number): string {
	return new Intl.NumberFormat('en-US').format(value);
}

export function formatRelativeTime(value: string | null | undefined, now: number): string {
	const parsed = parseIsoDate(value);
	if (!parsed) {
		return 'just now';
	}
	const seconds = Math.max(0, Math.round((now - parsed.getTime()) / 1000));
	if (seconds < 10) {
		return 'moments ago';
	}
	if (seconds < 60) {
		return `${seconds}s ago`;
	}
	const minutes = Math.round(seconds / 60);
	if (minutes < 60) {
		return `${minutes}m ago`;
	}
	const hours = Math.round(minutes / 60);
	return `${hours}h ago`;
}

export function formatTopCounts(
	mapping: Record<string, number> | null | undefined,
	limit = 3,
	emptyCopy = 'No signal yet'
): string {
	if (!mapping) {
		return emptyCopy;
	}
	const entries = Object.entries(mapping)
		.filter(([, value]) => Number(value) > 0)
		.sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]))
		.slice(0, limit);

	if (!entries.length) {
		return emptyCopy;
	}

	return entries.map(([key, value]) => `${value} ${key}`).join(' · ');
}

export function buildFolderLookup(
	previewFolders: FolderCardData[],
	fullFolders: FolderCardData[]
): Record<string, FolderCardData> {
	const lookup: Record<string, FolderCardData> = {};
	for (const folder of [...previewFolders, ...fullFolders]) {
		if (!lookup[folder.prefix]) {
			lookup[folder.prefix] = folder;
		}
	}
	return lookup;
}

export function folderTitleForPrefix(
	prefix: string,
	folderLookup: Record<string, FolderCardData>
): string {
	return folderLookup[prefix]?.title || prefix.split('/').filter(Boolean).at(-1) || prefix;
}

export function folderSubtitleForPrefix(
	prefix: string,
	folderLookup: Record<string, FolderCardData>
): string {
	return folderLookup[prefix]?.subtitle || prefix;
}

export function queueJobDetailCopy(
	job: DashboardSummaryPayload['encode_queue']['running'][number]
): string {
	const progressState = String(job.progress?.progress_state ?? '').trim();
	if (progressState) {
		return progressState;
	}
	const telemetrySummary = String(job.telemetry_summary ?? '').trim();
	if (telemetrySummary) {
		return telemetrySummary;
	}
	const schedulerCopy = String(job.scheduler_status_copy ?? '').trim();
	if (schedulerCopy) {
		return schedulerCopy;
	}
	const attemptSummary = String(job.attempt_summary ?? '').trim();
	if (attemptSummary) {
		return attemptSummary;
	}
	return 'Waiting for the next queue event.';
}
