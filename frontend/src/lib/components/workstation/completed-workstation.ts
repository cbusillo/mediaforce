import type {
	ArchiveCleanupSummary,
	CompletedFolderRow,
	CompletedHistoryEvent,
	CompletedPayload
} from '$lib/api/types';
import { normalizeFileSizeCopy } from '$lib/format';
import type { FooterSignal, ShellTone, StatusTile } from './shell-types';
import { formatBytes } from './folder-studio-view';

export type CleanupState = 'ready' | 'blocked' | 'unknown' | 'cleaned';
export type CompletedCleanupScope = 'selected' | 'global';

export type DeleteConfirmCopy = {
	title: string;
	scope: string;
	safety: string;
	warning: string;
	confirmLabel: string;
};

export type MarkHandledConfirmCopy = {
	title: string;
	scope: string;
	safety: string;
	confirmLabel: string;
};

export type CleanupActionBlockers = {
	selected: string | null;
	global: string | null;
	review: string | null;
};

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
	if (state === 'ready') return 'Backups ready to delete';
	if (state === 'blocked') return 'Check before deleting';
	if (state === 'unknown') return 'Backups already gone';
	return 'Nothing to delete';
}

function completedHistoryNoun(event: CompletedHistoryRow): string {
	if (event.domain === 'movie') return 'Movie';
	if (event.domain === 'tv') return event.source === 'folder' ? 'Season' : 'Episode';
	if (event.domain === 'other') return 'File';
	if (event.source === 'folder' && event.scope_label === 'Title') return 'Movie';
	return event.scope_label || 'Media item';
}

export function completedHistoryLabel(event: CompletedHistoryRow): string {
	const normalized = event.label.toLowerCase();
	const noun = completedHistoryNoun(event);
	if (normalized.includes('encoding started')) return `${noun} started`;
	if (normalized.includes('encoding completed')) return `${noun} made`;
	if (normalized.includes('encoding stopped')) return `${noun} stopped`;
	if (normalized.includes('encoding failed')) return `${noun} failed`;
	if (normalized.includes('validation completed')) return `${noun} checked`;
	if (normalized.includes('promotion completed')) return `${noun} finished`;
	if (event.event_type === 'originals_removed_confirmed') return 'Marked handled';
	if (normalized.includes('cleanup') || normalized.includes('backup'))
		return 'Original backups deleted';
	return event.label.replace(/encode/gi, noun.toLowerCase()).replace(/promotion/gi, 'finish');
}

export function completedHistoryDetail(value: string, event?: CompletedHistoryRow): string {
	const noun = event ? completedHistoryNoun(event).toLowerCase() : 'media item';
	const article = noun === 'episode' ? 'an' : 'a';
	return normalizeFileSizeCopy(
		value
			.replace(/encode worker/gi, 'A computer')
			.replace(/processing an item/gi, `making ${article} ${noun}`)
			.replace(/promoted items/gi, `finished ${noun}s`)
			.replace(/promoted item/gi, `finished ${noun}`)
	);
}

export function completedHistorySearchText(event: CompletedHistoryRow): string {
	return [
		completedHistoryLabel(event),
		completedHistoryDetail(event.detail, event),
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
	if (state === 'ready')
		return 'Original backups are in the Cleanup folder and can be deleted when you are ready.';
	if (state === 'blocked') return cleanupBlockedReason(folder, archive);
	if (state === 'unknown')
		return 'The original backups are already gone from the Cleanup folder. Nothing will be deleted; you can mark this handled.';
	return 'No original backups are waiting in the Cleanup folder.';
}

export function cleanupBlockedReason(
	folder: CompletedFolderRow,
	archive: ArchiveCleanupSummary
): string {
	if (!archive.archive_root) {
		return 'Cleanup folder is not set, so Mediaforce cannot find the original backups.';
	}
	const outsideCount = Math.max(folder.outside_archive_root_count ?? 0, 0);
	if (outsideCount > 0) {
		return `${outsideCount.toLocaleString('en-US')} original ${outsideCount === 1 ? 'backup sits' : 'backups sit'} outside the Cleanup folder, so Mediaforce will not delete ${outsideCount === 1 ? 'it' : 'them'} here.`;
	}
	return 'Mediaforce cannot verify the original backups with the current settings.';
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

export function buildDeleteConfirmCopy(
	scope: CompletedCleanupScope,
	input: {
		folderCount: number;
		backupCount: number;
		backupSizeBytes: number;
		archiveRoot: string;
	}
): DeleteConfirmCopy {
	const folderCount = Math.max(input.folderCount, 0);
	const backupCount = Math.max(input.backupCount, 0);
	const folderNoun = folderCount === 1 ? 'folder' : 'folders';
	const backupNoun = backupCount === 1 ? 'original backup' : 'original backups';
	const fileNoun = backupCount === 1 ? 'file' : 'files';
	const location = input.archiveRoot || 'the Cleanup folder';
	return scope === 'selected'
		? {
				title: `Delete original backups for ${folderCount.toLocaleString('en-US')} selected ${folderNoun}?`,
				scope: `Deletes ${backupCount.toLocaleString('en-US')} ${fileNoun} (${formatBytes(input.backupSizeBytes)}) from ${location}.`,
				safety:
					'Your finished files are not touched. Only the original backups in the Cleanup folder are deleted.',
				warning: 'This cannot be undone.',
				confirmLabel: `Delete ${backupCount.toLocaleString('en-US')} ${backupNoun}`
			}
		: {
				title: 'Delete all original backups in the Cleanup folder?',
				scope: `Deletes all ${backupCount.toLocaleString('en-US')} ${fileNoun} (${formatBytes(input.backupSizeBytes)}) in ${location}, including folders hidden by your current filters.`,
				safety:
					'Your finished files are not touched. Only the original backups in the Cleanup folder are deleted.',
				warning: 'This cannot be undone.',
				confirmLabel: `Delete all ${backupCount.toLocaleString('en-US')} ${backupNoun}`
			};
}

export function buildMarkHandledConfirmCopy(input: {
	folderCount: number;
	backupCount: number;
	archiveRoot: string;
}): MarkHandledConfirmCopy {
	const folderCount = Math.max(input.folderCount, 0);
	const backupCount = Math.max(input.backupCount, 0);
	return {
		title: `Mark ${folderCount.toLocaleString('en-US')} ${folderCount === 1 ? 'folder' : 'folders'} as handled?`,
		scope: `${backupCount.toLocaleString('en-US')} original ${backupCount === 1 ? 'backup is' : 'backups are'} already gone from ${input.archiveRoot || 'the Cleanup folder'}.`,
		safety: 'Nothing is deleted. This only clears these folders from review.',
		confirmLabel: 'Mark handled'
	};
}

export function cleanupActionBlockers(input: {
	completedAvailable: boolean;
	archive: ArchiveCleanupSummary;
	selectedFolderCount: number;
	reviewFolderCount: number;
	cleanupPending: boolean;
	reviewPending: boolean;
}): CleanupActionBlockers {
	if (!input.completedAvailable) {
		return {
			selected: 'Finished media is unavailable.',
			global: 'Finished media is unavailable.',
			review: 'Finished media is unavailable.'
		};
	}
	return {
		selected: input.cleanupPending
			? 'Deleting original backups…'
			: input.selectedFolderCount === 0
				? 'Select at least one folder with original backups.'
				: null,
		global: input.cleanupPending
			? 'Deleting original backups…'
			: !input.archive.archive_root
				? 'Set a Cleanup folder in Settings before deleting original backups.'
				: !input.archive.has_cleanup || input.archive.file_count <= 0
					? 'No original backups are waiting in the Cleanup folder.'
					: null,
		review: input.reviewPending
			? 'Marking folders handled…'
			: input.reviewFolderCount === 0
				? 'Select at least one folder whose original backups are already gone.'
				: null
	};
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

export function buildCompletedStatusTiles(
	payload: CompletedPayload | null,
	loadError: string | null
): StatusTile[] {
	if (!payload) {
		return [
			{
				label: 'Finished',
				value: loadError ? 'Unavailable' : 'Loading',
				detail: loadError ?? 'waiting for finished work',
				tone: loadError ? 'fail' : 'idle'
			}
		];
	}
	const folders = payload.folders;
	const archive = payload.archive_cleanup;
	const counts = cleanupStateCounts(folders, archive);
	return [
		{
			label: 'Finished folders',
			value: payload.completed_count.toLocaleString('en-US'),
			detail: `${folders.reduce((total, folder) => total + folder.promoted_item_count, 0).toLocaleString('en-US')} promoted items`,
			tone: payload.completed_count > 0 ? 'ready' : 'idle'
		},
		{
			label: 'Backups to delete',
			value: payload.folders_with_backups_count.toLocaleString('en-US'),
			detail: `${archive.file_count.toLocaleString('en-US')} files in the Cleanup folder`,
			tone: payload.folders_with_backups_count > 0 ? 'wait' : 'idle'
		},
		{
			label: 'Space to reclaim',
			value: formatBytes(archive.total_size_bytes),
			detail: archive.has_cleanup ? 'in the Cleanup folder' : 'nothing to delete',
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
			value: archive.archive_root ? 'Set' : 'Not set',
			detail: archive.archive_root || 'no Cleanup folder set',
			tone: archive.archive_root ? 'ready' : 'fail'
		}
	];
}

export function buildCompletedFooterSignals(payload: CompletedPayload | null): FooterSignal[] {
	if (!payload) return [{ label: 'Finished', value: 'unavailable', tone: 'fail' }];
	const counts = cleanupStateCounts(payload.folders, payload.archive_cleanup);
	return [
		{ label: 'Finished', value: String(payload.completed_count), tone: 'ready' },
		{ label: 'Backups', value: String(payload.folders_with_backups_count), tone: 'wait' },
		{ label: 'Ready', value: String(counts.ready), tone: counts.ready > 0 ? 'ready' : 'idle' },
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
