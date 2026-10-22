import { describe, expect, it } from 'vitest';
import type { CompletedFolderRow, CompletedPayload } from '$lib/api/types';
import { formatFileSize } from '$lib/format';
import {
	buildCompletedHistoryRows,
	buildCompletedStatusTiles,
	buildDeleteConfirmCopy,
	buildMarkHandledConfirmCopy,
	cleanupActionBlockers,
	cleanupDetail,
	cleanupState,
	completedHistorySearchText,
	completedStateOptions,
	finishedCopy,
	folderCanBeSelected
} from './completed-workstation';

const archive = {
	archive_root: '/runtime/archive',
	file_count: 4,
	total_size_bytes: 4096,
	has_cleanup: true
};

function folder(overrides: Partial<CompletedFolderRow>): CompletedFolderRow {
	return {
		prefix: 'tv/show/season 1',
		title: 'Season 1',
		subtitle: 'Show',
		scope_label: 'tv',
		promoted_item_count: 2,
		archived_backup_count: 0,
		archived_backup_size_bytes: 0,
		total_bytes_saved: 2048,
		latest_promoted_at: '2026-04-10T10:00:00+00:00',
		...overrides
	};
}

function payload(folders: CompletedFolderRow[]): CompletedPayload {
	return {
		folders,
		completed_count: folders.length,
		folders_with_backups_count: folders.filter((item) => item.archived_backup_count > 0).length,
		archive_cleanup: archive,
		history: []
	};
}

describe('Completed workstation mapping', () => {
	it('maps ready, blocked, unknown, and cleaned states from folder evidence', () => {
		const ready = folder({ archived_backup_count: 2, archived_backup_size_bytes: 1024 });
		const blocked = folder({ prefix: 'tv/show/season 2', outside_archive_root_count: 1 });
		const unknown = folder({ prefix: 'tv/show/season 3', missing_backup_count: 1 });
		const cleaned = folder({ prefix: 'tv/show/season 4' });

		expect(cleanupState(ready, archive)).toBe('ready');
		expect(cleanupState(blocked, archive)).toBe('blocked');
		expect(cleanupState(unknown, archive)).toBe('unknown');
		expect(cleanupState(cleaned, archive)).toBe('cleaned');
		expect(folderCanBeSelected(ready, archive)).toBe(true);
		expect(folderCanBeSelected(unknown, archive)).toBe(false);
	});

	it('builds status tiles around cleanup readiness and archive health', () => {
		const tiles = buildCompletedStatusTiles(
			payload([
				folder({ archived_backup_count: 1, archived_backup_size_bytes: 1024 }),
				folder({ prefix: 'tv/show/season 2', outside_archive_root_count: 1 })
			]),
			null
		);

		expect(tiles).toMatchObject([
			{ label: 'Finished folders', tone: 'ready' },
			{ label: 'Backups to delete', tone: 'wait' },
			{ label: finishedCopy.spaceToFree, tone: 'ready' },
			{ label: finishedCopy.needsLook, value: '1 / 1', tone: 'fail' },
			{ label: 'Cleanup folder', tone: 'ready' }
		]);
	});

	it('orders cleanup state filter options by operator risk sequence', () => {
		const options = completedStateOptions(
			[
				folder({ prefix: 'tv/show/season 4' }),
				folder({ prefix: 'tv/show/season 3', missing_backup_count: 1 }),
				folder({ prefix: 'tv/show/season 1', archived_backup_count: 2 }),
				folder({ prefix: 'tv/show/season 2', outside_archive_root_count: 1 })
			],
			archive
		);

		expect(options.map((option) => option.label)).toEqual([
			'Backups ready to delete',
			'Check before deleting',
			'Backups already gone',
			'Nothing to delete'
		]);
		expect(options.map((option) => option.key)).toEqual(['ready', 'blocked', 'unknown', 'cleaned']);
	});

	it('falls back to plain promotion rows when the API has not supplied history events', () => {
		const rows = buildCompletedHistoryRows(
			payload([folder({ title: 'Season 1', promoted_item_count: 2, total_bytes_saved: 2048 })])
		);

		expect(rows).toHaveLength(1);
		expect(rows[0]).toMatchObject({ source: 'folder', title: 'Season 1' });
		const visible = `${rows[0].label} ${rows[0].detail}`.toLowerCase();
		expect(visible).toContain(formatFileSize(2048).toLowerCase());
		for (const internal of ['promot', 'item', 'bytes']) {
			expect(visible).not.toContain(internal);
		}
	});

	it('shows backend history copy as sent and keeps technical text searchable', () => {
		const event = {
			id: 7083,
			event_type: 'encoding_failed',
			label: 'Failed',
			tone: 'fail',
			prefix: 'tv/Futurama/Season 3',
			title: 'Futurama · Season 3',
			subtitle: 'Futurama',
			scope_label: 'Season',
			domain: 'tv' as const,
			created_at: '2026-10-21T02:58:59+00:00',
			detail: 'Compressing this episode did not finish.',
			size_bytes: null,
			file_name: 'Futurama.S03E08.1080p.mkv',
			technical_detail: 'encoder banner line one\n  configuration: --prefix=/opt/encoder'
		};
		const rows = buildCompletedHistoryRows({ ...payload([]), history: [event] });

		expect(rows).toEqual([{ ...event, source: 'api' }]);
		const search = completedHistorySearchText(rows[0]);
		for (const text of [event.label, event.detail, event.title, event.file_name, 'configuration']) {
			expect(search).toContain(text.toLowerCase());
		}
	});

	it('keeps delete and mark-handled confirmations semantically separate', () => {
		expect(
			buildDeleteConfirmCopy('selected', {
				folderCount: 2,
				backupCount: 3,
				backupSizeBytes: 4096,
				archiveRoot: '/runtime/archive'
			})
		).toEqual({
			title: 'Delete original backups for 2 selected folders?',
			scope: 'Deletes 3 files (4.1 KB) from /runtime/archive.',
			safety:
				'Your finished files are not touched. Only the original backups in the Cleanup folder are deleted.',
			warning: 'This cannot be undone.',
			confirmLabel: 'Delete 3 original backups'
		});
		expect(
			buildDeleteConfirmCopy('global', {
				folderCount: 2,
				backupCount: 4,
				backupSizeBytes: 4096,
				archiveRoot: '/runtime/archive'
			}).scope
		).toContain('including folders hidden by your current filters');
		expect(
			buildMarkHandledConfirmCopy({
				folderCount: 1,
				backupCount: 2,
				archiveRoot: '/runtime/archive'
			})
		).toEqual({
			title: 'Mark 1 folder as handled?',
			scope: '2 original backups are already gone from /runtime/archive.',
			safety: 'Nothing is deleted. This only clears these folders from review.',
			confirmLabel: 'Mark handled'
		});
	});

	it('provides visible reasons for disabled cleanup actions', () => {
		expect(
			cleanupActionBlockers({
				completedAvailable: true,
				archive,
				selectedFolderCount: 0,
				reviewFolderCount: 0,
				cleanupPending: false,
				reviewPending: false
			})
		).toEqual({
			selected: 'Select at least one folder with original backups.',
			global: null,
			review: 'Select at least one folder whose original backups are already gone.'
		});
		expect(
			cleanupActionBlockers({
				completedAvailable: true,
				archive: { ...archive, archive_root: '', has_cleanup: false, file_count: 0 },
				selectedFolderCount: 1,
				reviewFolderCount: 1,
				cleanupPending: false,
				reviewPending: false
			}).global
		).toBe('Set a Cleanup folder in Settings before deleting original backups.');
	});

	it('explains each cleanup state with canonical backup language', () => {
		expect(cleanupDetail(folder({ archived_backup_count: 1 }), archive)).toContain(
			'Original backups are in the Cleanup folder'
		);
		expect(cleanupDetail(folder({ missing_backup_count: 1 }), archive)).toContain(
			'Nothing will be deleted'
		);
		expect(cleanupDetail(folder({ outside_archive_root_count: 2 }), archive)).toBe(
			'2 original backups sit outside the Cleanup folder, so Mediaforce will not delete them here.'
		);
	});
});
