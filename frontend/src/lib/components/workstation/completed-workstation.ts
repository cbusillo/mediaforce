import type {
	ArchiveCleanupSummary,
	CompletedFolderRow,
	CompletedHistoryEvent,
	CompletedPayload
} from '$lib/api/types';
import type { FooterSignal, ShellTone, StatusTile } from './shell-types';
import { formatBytes } from './folder-studio-view';

export type CleanupState = 'ready' | 'blocked' | 'unknown' | 'cleaned';

export type CompletedHistoryRow = CompletedHistoryEvent & {
	source: 'api' | 'folder';
};

export type CompletedFilterOption = {
	key: string;
	label: string;
	folders: number;
	backups: number;
	size: number;
};

export type CompletedReadinessSummary = {
	tone: ShellTone;
	title: string;
	detail: string;
	metricLabel: string;
	metricValue: string;
};

export function cleanupState(
	folder: CompletedFolderRow,
	archive: ArchiveCleanupSummary
): CleanupState {
	const explicit = String(folder.cleanup_state ?? '').toLowerCase();
	if (
		explicit === 'ready' ||
		explicit === 'blocked' ||
		explicit === 'unknown' ||
		explicit === 'cleaned'
	) {
		return explicit;
	}
	if (!archive.archive_root && folder.promoted_item_count > 0) return 'blocked';
	if ((folder.outside_archive_root_count ?? 0) > 0) return 'blocked';
	if ((folder.missing_backup_count ?? 0) > 0) return 'unknown';
	if (folder.archived_backup_count > 0) return 'ready';
	return 'cleaned';
}

export function cleanupTone(state: CleanupState): ShellTone {
	if (state === 'ready') return 'ready';
	if (state === 'blocked') return 'fail';
	if (state === 'unknown') return 'wait';
	return 'idle';
}

export function cleanupLabel(state: CleanupState): string {
	if (state === 'ready') return 'Originals waiting';
	if (state === 'blocked') return 'Check settings';
	if (state === 'unknown') return 'Originals already gone';
	return 'Finished';
}

export function completedHistoryLabel(value: string): string {
	const normalized = value.toLowerCase();
	if (normalized.includes('encoding started')) return 'Season started';
	if (normalized.includes('encoding stopped')) return 'Season stopped';
	if (normalized.includes('encoding failed')) return 'Season failed';
	if (normalized.includes('promotion completed')) return 'Season finished';
	if (normalized.includes('cleanup')) return 'Originals removed';
	return value.replace(/encode/gi, 'season').replace(/promotion/gi, 'finish');
}

export function completedHistoryDetail(value: string): string {
	return value
		.replace(/encode worker/gi, 'A computer')
		.replace(/processing an item/gi, 'making an episode')
		.replace(/promoted items?/gi, 'finished episodes')
		.replace(/GiB/g, 'GB')
		.replace(/MiB/g, 'MB');
}

export function completedHistorySearchText(event: CompletedHistoryEvent): string {
	return [
		completedHistoryLabel(event.label),
		completedHistoryDetail(event.detail),
		event.label,
		event.prefix,
		event.title,
		event.subtitle,
		event.detail,
		event.event_type
	]
		.join(' ')
		.toLowerCase();
}

export function cleanupDetail(folder: CompletedFolderRow, archive: ArchiveCleanupSummary): string {
	if (folder.cleanup_detail?.trim()) return folder.cleanup_detail.trim();
	const state = cleanupState(folder, archive);
	if (state === 'ready') return 'Originals are waiting and can be removed when you are ready.';
	if (state === 'blocked')
		return 'Mediaforce cannot check the originals with the current settings.';
	if (state === 'unknown')
		return 'The originals are already gone; confirm after checking the new files.';
	return 'No originals are waiting to be removed.';
}

export function folderCanBeMarkedHandled(
	folder: CompletedFolderRow,
	archive: ArchiveCleanupSummary
): boolean {
	return cleanupState(folder, archive) === 'unknown' && (folder.missing_backup_count ?? 0) > 0;
}

export function folderCanBeSelected(
	folder: CompletedFolderRow,
	archive: ArchiveCleanupSummary
): boolean {
	return cleanupState(folder, archive) === 'ready' && folder.archived_backup_count > 0;
}

export function readyFolders(
	folders: CompletedFolderRow[],
	archive: ArchiveCleanupSummary
): CompletedFolderRow[] {
	return folders.filter((folder) => folderCanBeSelected(folder, archive));
}

export function totalArchivedBackupSize(folders: CompletedFolderRow[]): number {
	return folders.reduce(
		(total, folder) => total + Math.max(folder.archived_backup_size_bytes ?? 0, 0),
		0
	);
}

export function totalSavedSize(folders: CompletedFolderRow[]): number {
	return folders.reduce((total, folder) => total + Math.max(folder.total_bytes_saved ?? 0, 0), 0);
}

export function cleanupStateCounts(folders: CompletedFolderRow[], archive: ArchiveCleanupSummary) {
	return folders.reduce(
		(counts, folder) => {
			counts[cleanupState(folder, archive)] += 1;
			return counts;
		},
		{ ready: 0, blocked: 0, unknown: 0, cleaned: 0 } satisfies Record<CleanupState, number>
	);
}

export function buildCompletedReadinessSummary(
	payload: CompletedPayload | null,
	loadError: string | null
): CompletedReadinessSummary {
	if (!payload) {
		return {
			tone: loadError ? 'fail' : 'idle',
			title: loadError ? 'Finished seasons are unavailable' : 'Finished seasons are loading',
			detail: loadError ?? 'Opening finished seasons and their original-file status.',
			metricLabel: 'Finished',
			metricValue: loadError ? 'offline' : 'loading'
		};
	}
	const folders = payload.folders;
	const archive = payload.archive_cleanup;
	const counts = cleanupStateCounts(folders, archive);
	const reviewCount = counts.blocked + counts.unknown;
	if (counts.ready > 0) {
		return {
			tone: 'ready',
			title: 'Originals are waiting',
			detail: `${counts.ready.toLocaleString('en-US')} finished ${counts.ready === 1 ? 'season has' : 'seasons have'} originals waiting for your decision.`,
			metricLabel: 'Waiting',
			metricValue: String(counts.ready)
		};
	}
	if (reviewCount > 0) {
		return {
			tone: counts.blocked > 0 ? 'fail' : 'wait',
			title:
				counts.blocked > 0 ? 'Check before deleting' : 'Confirm originals that are already gone',
			detail:
				counts.blocked > 0
					? `${counts.blocked.toLocaleString('en-US')} finished ${counts.blocked === 1 ? 'season needs' : 'seasons need'} settings checked before originals can be removed.`
					: `${counts.unknown.toLocaleString('en-US')} finished ${counts.unknown === 1 ? 'season has' : 'seasons have'} originals already gone; confirm after checking the new files.`,
			metricLabel: counts.blocked > 0 ? 'Check' : 'Confirm',
			metricValue: String(reviewCount)
		};
	}
	return {
		tone: 'idle',
		title: 'Everything is settled',
		detail: `${folders.length.toLocaleString('en-US')} finished ${folders.length === 1 ? 'season is' : 'seasons are'} settled. Recent changes remain available in History.`,
		metricLabel: 'Finished',
		metricValue: String(counts.cleaned)
	};
}

export function buildCompletedStatusTiles(
	payload: CompletedPayload | null,
	loadError: string | null
): StatusTile[] {
	if (!payload) {
		return [
			{
				label: 'Completed',
				value: loadError ? 'Unavailable' : 'Loading',
				detail: loadError ?? 'waiting for completed work',
				tone: loadError ? 'fail' : 'idle'
			}
		];
	}
	const folders = payload.folders;
	const archive = payload.archive_cleanup;
	const counts = cleanupStateCounts(folders, archive);
	return [
		{
			label: 'Completed folders',
			value: payload.completed_count.toLocaleString('en-US'),
			detail: `${folders.reduce((total, folder) => total + folder.promoted_item_count, 0).toLocaleString('en-US')} promoted items`,
			tone: payload.completed_count > 0 ? 'ready' : 'idle'
		},
		{
			label: 'Delete queue',
			value: payload.folders_with_backups_count.toLocaleString('en-US'),
			detail: `${archive.file_count.toLocaleString('en-US')} files ready to remove`,
			tone: payload.folders_with_backups_count > 0 ? 'wait' : 'idle'
		},
		{
			label: 'Space ready',
			value: formatBytes(archive.total_size_bytes),
			detail: archive.has_cleanup ? 'cleanup available' : 'no cleanup files',
			tone: archive.has_cleanup ? 'ready' : 'idle',
			mono: true
		},
		{
			label: 'Review needed',
			value: `${counts.ready} / ${counts.blocked + counts.unknown}`,
			detail: 'ready to delete versus needing review',
			tone:
				counts.blocked > 0
					? 'fail'
					: counts.unknown > 0
						? 'wait'
						: counts.ready > 0
							? 'ready'
							: 'idle'
		},
		{
			label: 'Cleanup folder',
			value: archive.archive_root ? 'Configured' : 'Missing',
			detail: archive.archive_root || 'cleanup folder unavailable',
			tone: archive.archive_root ? 'ready' : 'fail'
		}
	];
}

export function buildCompletedFooterSignals(payload: CompletedPayload | null): FooterSignal[] {
	if (!payload) return [{ label: 'Completed', value: 'unavailable', tone: 'fail' }];
	const counts = cleanupStateCounts(payload.folders, payload.archive_cleanup);
	return [
		{ label: 'Completed', value: String(payload.completed_count), tone: 'ready' },
		{ label: 'Originals', value: String(payload.folders_with_backups_count), tone: 'wait' },
		{ label: 'Waiting', value: String(counts.ready), tone: counts.ready > 0 ? 'ready' : 'idle' },
		{
			label: 'Review',
			value: String(counts.blocked + counts.unknown),
			tone: counts.blocked > 0 ? 'fail' : 'wait'
		},
		{
			label: 'Reclaimable',
			value: formatBytes(payload.archive_cleanup.total_size_bytes),
			tone: 'ready'
		}
	];
}

export function completedLibraryOptions(folders: CompletedFolderRow[]): CompletedFilterOption[] {
	const byLibrary = new Map<string, CompletedFilterOption>();
	for (const folder of folders) {
		const label = folder.scope_label || folder.prefix.split('/')[0] || 'Library';
		const key = label.trim().toLowerCase() || 'library';
		const existing = byLibrary.get(key) ?? {
			key,
			label,
			folders: 0,
			backups: 0,
			size: 0
		};
		existing.folders += 1;
		existing.backups += Math.max(folder.archived_backup_count ?? 0, 0);
		existing.size += Math.max(folder.archived_backup_size_bytes ?? 0, 0);
		byLibrary.set(key, existing);
	}
	return [...byLibrary.values()].sort(
		(left, right) => right.size - left.size || left.label.localeCompare(right.label)
	);
}

export function completedStateOptions(
	folders: CompletedFolderRow[],
	archive: ArchiveCleanupSummary
): CompletedFilterOption[] {
	const byState = new Map<string, CompletedFilterOption>();
	for (const folder of folders) {
		const state = cleanupState(folder, archive);
		const label = cleanupLabel(state);
		const existing = byState.get(state) ?? {
			key: state,
			label,
			folders: 0,
			backups: 0,
			size: 0
		};
		existing.folders += 1;
		existing.backups += Math.max(folder.archived_backup_count ?? 0, 0);
		existing.size += Math.max(folder.archived_backup_size_bytes ?? 0, 0);
		byState.set(state, existing);
	}
	const order: CleanupState[] = ['ready', 'blocked', 'unknown', 'cleaned'];
	return [...byState.values()].sort(
		(left, right) =>
			order.indexOf(left.key as CleanupState) - order.indexOf(right.key as CleanupState)
	);
}

export function folderSearchText(
	folder: CompletedFolderRow,
	archive: ArchiveCleanupSummary
): string {
	return [
		folder.title,
		folder.subtitle,
		folder.prefix,
		folder.scope_label,
		cleanupLabel(cleanupState(folder, archive)),
		cleanupDetail(folder, archive)
	]
		.join(' ')
		.toLowerCase();
}

export function buildCompletedHistoryRows(payload: CompletedPayload): CompletedHistoryRow[] {
	if (payload.history?.length) {
		return payload.history.map((event) => ({ ...event, source: 'api' }));
	}
	return payload.folders
		.filter((folder) => folder.latest_promoted_at)
		.slice(0, 80)
		.map((folder, index) => ({
			id: index,
			event_type: 'promotion_completed',
			label: 'Promotion completed',
			tone: 'ready',
			prefix: folder.prefix,
			title: folder.title,
			subtitle: folder.subtitle,
			scope_label: folder.scope_label,
			created_at: folder.latest_promoted_at ?? '',
			detail: `${folder.promoted_item_count.toLocaleString('en-US')} promoted items · ${formatBytes(folder.total_bytes_saved)} saved`,
			size_bytes: folder.total_bytes_saved,
			source: 'folder'
		}));
}
