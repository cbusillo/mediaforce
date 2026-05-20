import { describe, expect, it } from 'vitest';
import type { CompletedFolderRow, CompletedPayload } from '$lib/api/types';
import {
	buildCompletedHistoryRows,
	buildCompletedReadinessSummary,
	buildCompletedStatusTiles,
	cleanupState,
	completedStateOptions,
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
			{ label: 'Completed folders', tone: 'ready' },
			{ label: 'Delete queue', tone: 'wait' },
			{ label: 'Space ready', tone: 'ready' },
			{ label: 'Review needed', value: '1 / 1', tone: 'fail' },
			{ label: 'Cleanup folder', tone: 'ready' }
		]);
	});

	it('summarizes completed cleanup state for the page headline', () => {
		expect(
			buildCompletedReadinessSummary(
				payload([folder({ archived_backup_count: 1, archived_backup_size_bytes: 1024 })]),
				null
			)
		).toMatchObject({
			tone: 'ready',
			title: 'Originals are waiting',
			metricLabel: 'Waiting',
			metricValue: '1'
		});

		expect(buildCompletedReadinessSummary(payload([folder({})]), null)).toMatchObject({
			tone: 'idle',
			title: 'Completed work is handled',
			metricLabel: 'Handled',
			metricValue: '1'
		});
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
			'Originals waiting',
			'Check settings',
			'Originals already removed',
			'Handled'
		]);
		expect(options.map((option) => option.key)).toEqual(['ready', 'blocked', 'unknown', 'cleaned']);
	});

	it('falls back to promotion rows when the API has not supplied history events', () => {
		const rows = buildCompletedHistoryRows(
			payload([folder({ title: 'Season 1', promoted_item_count: 2, total_bytes_saved: 2048 })])
		);

		expect(rows).toHaveLength(1);
		expect(rows[0]).toMatchObject({
			source: 'folder',
			label: 'Promotion completed',
			title: 'Season 1'
		});
	});
});
