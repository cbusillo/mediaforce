import { describe, expect, it } from 'vitest';
import type { OperatorEvidenceBacklogRow, OperatorEvidenceState } from '$lib/api/types';
import {
	catalogStateView,
	evidenceStateView,
	evidenceWorkStatusView,
	operatorRefreshInterval
} from './operator-work';

function evidenceFixture(status: string, runnerActive = false): OperatorEvidenceState {
	return {
		live_status: status,
		terminal_status: null,
		runner_active: runnerActive,
		queue: {
			queue_name: 'evidence',
			batch_id: 'batch-1',
			status,
			scope: null,
			evidence_kinds: ['cadence_analysis'],
			is_paused: status === 'paused',
			cancel_requested: false,
			item_count: 4,
			completed_count: 0,
			failed_count: 0,
			cancelled_count: 0,
			created_at: '2026-07-19T12:00:00+00:00',
			started_at: null,
			finished_at: null,
			updated_at: '2026-07-19T12:00:00+00:00',
			counts: { queued: 4 },
			running: null,
			remaining_count: 4
		},
		progress: {
			total_count: 4,
			remaining_count: 4,
			done_count: 0,
			percent: 0,
			counts: { queued: 4 },
			running: null
		},
		inventory: { total: 10, backlog_total: 4, by_kind: {} },
		backlog: {
			offset: 0,
			limit: 25,
			total: 4,
			range_start: 1,
			range_end: 4,
			has_previous: false,
			has_next: false,
			filters: { evidence_kind: null, state: null, media_root: null, work_status: null },
			rows: []
		},
		can_prepare: false,
		can_pause: false,
		can_resume: true,
		can_cancel: true
	};
}

describe('operator work copy', () => {
	it('distinguishes current, stale, and failed catalog states', () => {
		expect(
			catalogStateView({
				status: 'idle',
				freshness: 'current',
				item_count: 20,
				last_completed_at: null,
				job: null,
				source_roots: [],
				warnings: [],
				can_refresh: true
			}).label
		).toBe('Current');
		expect(
			catalogStateView({
				status: 'idle',
				freshness: 'stale',
				item_count: 20,
				last_completed_at: null,
				job: null,
				source_roots: [],
				warnings: [],
				can_refresh: true
			}).label
		).toBe('Refresh suggested');
		expect(
			catalogStateView({
				status: 'failed',
				freshness: 'unknown',
				item_count: 20,
				last_completed_at: null,
				job: { error: 'Source offline' } as never,
				source_roots: [],
				warnings: [],
				can_refresh: true
			}).detail
		).toBe('Source offline');
	});

	it('uses prepared, running, and terminal analysis language', () => {
		expect(evidenceStateView(evidenceFixture('paused')).label).toBe('Prepared');
		expect(evidenceStateView(evidenceFixture('queued', true)).label).toBe('Analyzing');
		expect(evidenceStateView(evidenceFixture('completed_with_errors')).label).toBe(
			'Needs attention'
		);
	});

	it('labels retry and source waits without raw state names', () => {
		const row = {
			work_status: 'retry_wait',
			last_error: 'temporary failure'
		} as OperatorEvidenceBacklogRow;
		expect(evidenceWorkStatusView(row).label).toBe('Retry scheduled');
		expect(evidenceWorkStatusView({ ...row, work_status: 'waiting_source' }).label).toBe(
			'Source unavailable'
		);
	});

	it('polls only while the server reports active work', () => {
		expect(
			operatorRefreshInterval({ refresh: { mode: 'manual', interval_ms: null } } as never)
		).toBeNull();
		expect(
			operatorRefreshInterval({ refresh: { mode: 'active', interval_ms: 2500 } } as never)
		).toBe(2500);
	});
});
