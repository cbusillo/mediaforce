import { describe, expect, it } from 'vitest';
import type { OperatorEvidenceBacklogRow, OperatorEvidenceState } from '$lib/api/types';
import {
	catalogProviderNotices,
	catalogStateView,
	evidenceKindLabel,
	evidenceStateView,
	evidenceWorkReasonView,
	evidenceWorkStatusView,
	libraryCheckLabels,
	operatorRefreshInterval,
	scopeLabel
} from './operator-work';
import { operatorStateCopy } from '$lib/operator-copy';

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
	it('warns only for enabled metadata providers that are not configured', () => {
		const notices = catalogProviderNotices({
			status: 'idle',
			freshness: 'current',
			item_count: 20,
			last_completed_at: null,
			job: null,
			source_roots: [],
			warnings: [],
			providers: {
				plex: {
					enabled: true,
					base_url: 'http://plex.local:32400',
					token_env: 'MEDIAFORCE_PLEX_TOKEN',
					token_configured: false
				},
				tmdb: {
					enabled: false,
					token_env: 'MEDIAFORCE_TMDB_TOKEN',
					token_configured: false
				}
			},
			can_refresh: true
		});

		expect(notices).toEqual([
			{
				title: 'Plex metadata is not updating',
				detail:
					'Set MEDIAFORCE_PLEX_TOKEN for the Mediaforce service so refreshes can preserve original arrival dates.'
			}
		]);
	});

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
		).toBe(libraryCheckLabels.catalogStale);
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
		expect(
			catalogStateView({
				status: 'failed',
				freshness: 'unknown',
				item_count: 20,
				last_completed_at: null,
				job: { error: 'ProcessCancelledError: Operation was cancelled.' } as never,
				source_roots: [],
				warnings: [],
				can_refresh: true
			}).detail
		).toBe('The operation was stopped before it finished.');
	});

	it('keeps internal analysis words out of Activity disclosures and rows', () => {
		const internalWords =
			/\b(cadence|fingerprint|bounded|operator|scope|analyzer|encode|catalog policy)\b/i;
		const row = { work_status: 'running', last_error: null } as OperatorEvidenceBacklogRow;
		const shown = [
			catalogStateView({
				status: 'idle',
				freshness: 'current',
				item_count: 20,
				last_completed_at: null,
				job: null,
				source_roots: [],
				warnings: [],
				can_refresh: true
			}).detail,
			evidenceKindLabel('cadence_analysis'),
			evidenceKindLabel('media_fingerprint'),
			evidenceWorkStatusView(row).detail,
			evidenceWorkStatusView({ ...row, work_status: 'waiting_host' }).detail,
			scopeLabel(null),
			...[
				'representative_technical_coverage',
				'representative_uncertainty',
				'operator_scope',
				'original_source_measurement'
			].flatMap((reason) => {
				const view = evidenceWorkReasonView(reason);
				return [view?.label, view?.detail];
			}),
			...(['cancel_requested', 'paused', 'queued'] as const).map(
				(status) => evidenceStateView(evidenceFixture(status)).detail
			)
		];

		for (const copy of shown) expect(copy).not.toMatch(internalWords);
		expect(evidenceKindLabel('media_fingerprint')).not.toBe(evidenceKindLabel('cadence_analysis'));
	});

	it('uses prepared, running, and terminal analysis language', () => {
		expect(evidenceStateView(evidenceFixture('paused')).label).toBe(
			libraryCheckLabels.analysisReady
		);
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
		for (const status of ['cancelled']) {
			expect(
				evidenceWorkStatusView({
					...row,
					work_status: status,
					work_reason: 'original_source_measurement',
					retry_not_before: '2999-01-01T00:00:00Z'
				}).tone
			).toBe('wait');
		}
		expect(
			evidenceWorkStatusView({
				...row,
				work_status: 'failed',
				work_reason: 'original_source_measurement'
			}).tone
		).toBe('fail');
		expect(evidenceWorkStatusView({ ...row, work_status: 'waiting_source' }).label).toBe(
			'Source unavailable'
		);
		const waitingForComputer = evidenceWorkStatusView({ ...row, work_status: 'waiting_host' });
		expect(operatorStateCopy('waiting_host', {}, '')).not.toBe('');
		expect(waitingForComputer.label).toBe(operatorStateCopy('waiting_host'));
		expect(waitingForComputer.label).not.toBe(
			evidenceWorkStatusView({ ...row, work_status: 'waiting_source' }).label
		);
		expect(waitingForComputer.tone).toBe('wait');
		expect(
			evidenceWorkStatusView({
				...row,
				work_status: 'failed',
				last_error: 'Evidence analysis completed with non-current result: unknown.'
			}).detail
		).toBe('Analysis did not produce a usable result.');
	});

	it('explains decision-priority work in operator language', () => {
		expect(evidenceWorkReasonView('sample_safety')).toEqual({
			label: 'Needed for sample',
			detail: 'Complete this before starting the sample.'
		});
		expect(evidenceWorkReasonView('encode_safety')?.label).toBe('Needed for production');
		expect(evidenceWorkReasonView('policy_reclassification')?.label).toBe('No media read');
		const reasons = [
			'representative_technical_coverage',
			'representative_uncertainty',
			'operator_scope'
		].map((reason) => evidenceWorkReasonView(reason));
		for (const view of reasons) expect(view?.label).toBeTruthy();
		expect(new Set(reasons.map((view) => view?.label)).size).toBe(reasons.length);
		expect(evidenceWorkReasonView('future_reason')).toEqual({
			label: 'Priority work',
			detail: 'Prepared for a decision that needs current evidence.'
		});
		expect(evidenceWorkReasonView(null)).toBeNull();
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
