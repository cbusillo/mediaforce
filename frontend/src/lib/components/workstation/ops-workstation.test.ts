import { describe, expect, it } from 'vitest';
import type { DashboardSummaryPayload, HostsPayload } from '$lib/api/types';
import {
	buildOpsBlockers,
	buildOpsHistoryRows,
	buildOpsQueueRows,
	buildOpsReadinessSummary,
	buildOpsStatusTiles,
	hostPrepareDisabled,
	hostPrepareTitle,
	hostStateCopy,
	hostTone,
	rowRecoveryLabel,
	rowRecoveryTitle,
	retryableEncodeJobIds
} from './ops-workstation';

function dashboardFixture(): DashboardSummaryPayload {
	return {
		folders_preview: [],
		library_colors: {},
		scan_job: null,
		calibration_queue: {
			sample: {
				running: [
					{
						job_id: 'sample-1',
						prefix: 'tv/show/season 1',
						status: 'running',
						host: { label: 'studio-mini' }
					}
				],
				queued: [],
				pending_review: [],
				running_count: 1,
				queued_count: 0,
				pending_review_count: 0,
				recent_failed: [
					{
						job_id: 'sample-failed-1',
						prefix: 'tv/show/season 4',
						status: 'failed',
						error: 'could not find a viable sample',
						host: { label: 'studio-mini' }
					}
				],
				recent_failed_count: 1
			},
			full: {
				running: [],
				queued: [],
				pending_review: [],
				running_count: 0,
				queued_count: 0,
				pending_review_count: 0,
				recent_failed: [],
				recent_failed_count: 0
			},
			active_count: 1,
			recent_failed_count: 1
		},
		encode_queue: {
			running_count: 1,
			queued_count: 1,
			queued_waiting_count: 1,
			needs_attention_count: 1,
			running: [
				{
					job_id: 'encode-1',
					prefix: 'movies/feature',
					status: 'running',
					host: { label: 'worker-1' },
					progress: { percent_complete: 42, current_item_number: 2, total_item_count: 4 },
					scheduler_status_copy: 'inside encode window'
				}
			],
			queued: [
				{
					job_id: 'encode-2',
					prefix: 'tv/show/season 2',
					status: 'retry_backoff',
					error: 'transient worker fault',
					scheduler_status_copy: 'retry backoff'
				}
			],
			recent: [
				{
					job_id: 'encode-3',
					prefix: 'tv/show/season 3',
					status: 'needs_attention',
					error: 'quality target missed'
				}
			],
			state: {
				is_paused: false,
				stop_requested: false,
				scheduler_summary: 'weekdays 09:00-22:00'
			},
			telemetry: { eta_copy: '24m' }
		},
		catalog_empty: false,
		folder_cache_key: 'fixture',
		metric_support: { vmaf: true, xpsnr: false, ssim: true },
		metric_status_copy: 'VMAF · SSIM'
	};
}

function hostsFixture(): HostsPayload {
	return {
		compact: true,
		hosts: [
			{
				key: 'worker-1',
				label: 'worker-1',
				available: true,
				message: 'ready',
				missing_paths: [],
				issues: [],
				detail: null,
				capabilities: ['encode_queue'],
				priority: 1,
				max_parallel_encodes: 2,
				active_encode_count: 1,
				schedule_profile_label: 'day shift',
				schedule_detail: 'open until 22:00',
				schedule_open: true,
				active_flag: 'ready',
				active_reason: 'ready',
				queue_active: true
			},
			{
				key: 'overnight',
				label: 'overnight',
				available: true,
				message: 'schedule closed',
				missing_paths: [],
				issues: [],
				detail: null,
				capabilities: ['encode_queue'],
				priority: 2,
				max_parallel_encodes: 1,
				active_encode_count: 0,
				schedule_profile_label: 'overnight',
				schedule_detail: 'opens at 23:00',
				schedule_open: false,
				active_flag: 'scheduled',
				active_reason: 'outside schedule',
				queue_active: true
			}
		]
	};
}

describe('Ops workstation mapping', () => {
	it('prioritizes current running, queued, retryable encode, and sample rows', () => {
		const rows = buildOpsQueueRows(dashboardFixture());

		expect(rows.map((row) => row.key)).toEqual([
			'encode:encode-1',
			'encode:encode-2',
			'encode:encode-3',
			'sample:sample-1'
		]);
		expect(rows[1]).toMatchObject({
			tone: 'wait',
			action: undefined,
			actionScope: undefined,
			detail: 'transient worker fault'
		});
		expect(rows[2]).toMatchObject({
			tone: 'wait',
			action: 'retry-encode-prefix',
			actionScope: 'row',
			detail: 'quality target missed'
		});
		expect(rowRecoveryLabel(rows[1])).toBe('No action');
		expect(rowRecoveryTitle(rows[1])).toBe('No action is available for this row.');
		expect(rowRecoveryLabel(rows[2])).toBe('Retry folder');
		expect(rowRecoveryTitle(rows[2])).toContain('folder only');
		expect(rowRecoveryLabel(rows[3])).toBe('No action');
	});

	it('keeps old sample failures in history instead of current work', () => {
		const rows = buildOpsHistoryRows(dashboardFixture());

		expect(rows.map((row) => row.key)).toEqual(['sample:sample-failed-1']);
		expect(rows[0]).toMatchObject({
			tone: 'idle',
			status: 'History',
			prefix: 'tv/show/season 4',
			detail: 'could not find a viable sample'
		});
		expect(rowRecoveryLabel(rows[0])).toBe('No action');
	});

	it('surfaces unavailable data, retryable encodes, and schedule waits as attention items', () => {
		const blockers = buildOpsBlockers(dashboardFixture(), hostsFixture(), 'Dashboard unavailable');

		expect(blockers.map((blocker) => blocker.key)).toEqual([
			'runtime-load',
			'needs-attention',
			'schedule-waiting'
		]);
		expect(blockers[0]).toMatchObject({ tone: 'fail', title: 'Mediaforce data unavailable' });
		expect(blockers[1]).toMatchObject({ tone: 'wait', action: 'retry-failed-encode' });
		expect(blockers[2]).toMatchObject({ tone: 'wait' });
	});

	it('keeps scheduler, attention, sample, and worker tones semantic', () => {
		const tiles = buildOpsStatusTiles(dashboardFixture(), hostsFixture(), null);

		expect(tiles).toMatchObject([
			{ label: 'Scheduler', tone: 'ready' },
			{ label: 'Encode jobs', tone: 'wait' },
			{ label: 'Sample checks', tone: 'active' },
			{ label: 'Workers', tone: 'ready' }
		]);
	});

	it('summarizes the first-glance Ops readiness answer', () => {
		const summary = buildOpsReadinessSummary(dashboardFixture(), hostsFixture(), null);

		expect(summary).toMatchObject({
			tone: 'wait',
			title: 'Retry is available',
			metricLabel: 'Retry',
			metricValue: '1'
		});
	});

	it('treats active work as the headline when nothing needs operator attention', () => {
		const dashboard = dashboardFixture();
		dashboard.encode_queue.needs_attention_count = 0;
		dashboard.encode_queue.recent = [];

		const summary = buildOpsReadinessSummary(dashboard, hostsFixture(), null);

		expect(summary).toMatchObject({
			tone: 'active',
			title: 'Mediaforce is working',
			metricLabel: 'Running',
			metricValue: '2'
		});
	});

	it('distinguishes available, scheduled-off, and unavailable host states', () => {
		const [ready, scheduledOff] = hostsFixture().hosts;
		const unavailable = { ...ready, available: false, active_encode_count: 0 };

		expect(hostTone(ready)).toBe('active');
		expect(hostStateCopy(ready)).toBe('Encoding');
		expect(hostTone(scheduledOff)).toBe('idle');
		expect(hostStateCopy(scheduledOff)).toBe('Scheduled off');
		expect(hostTone(unavailable)).toBe('fail');
		expect(hostTone(unavailable, true)).toBe('wait');
		expect(hostStateCopy(unavailable)).toBe('Unavailable');
	});

	it('keeps password-gated host prepare disabled until a password is present', () => {
		const [ready] = hostsFixture().hosts;
		const passwordHost = {
			...ready,
			setup_supported: true,
			setup_requires_password: true
		};
		const unsupportedHost = {
			...ready,
			setup_supported: false,
			setup_requires_password: false
		};

		expect(hostPrepareDisabled(passwordHost, '')).toBe(true);
		expect(hostPrepareDisabled(passwordHost, 'secret')).toBe(false);
		expect(hostPrepareTitle(passwordHost)).toBe('Enter the prepare password for this worker.');
		expect(hostPrepareDisabled(unsupportedHost, 'secret')).toBe(true);
		expect(hostPrepareTitle(unsupportedHost)).toBe('Prepare is unavailable for this worker.');
	});

	it('does not expose prefix retry for stale historical encode rows', () => {
		const dashboard = dashboardFixture();
		dashboard.encode_queue.queued.push({
			job_id: 'encode-newer',
			prefix: 'tv/show/season 3',
			status: 'queued',
			created_at: '2026-05-05T12:00:00Z',
			updated_at: '2026-05-05T12:00:00Z'
		});
		dashboard.encode_queue.recent = [
			{
				job_id: 'encode-stale',
				prefix: 'tv/show/season 3',
				status: 'needs_attention',
				error: 'older quality target miss',
				created_at: '2026-05-05T11:00:00Z',
				updated_at: '2026-05-05T11:00:00Z'
			}
		];

		const rows = buildOpsQueueRows(dashboard);
		const staleRow = rows.find((row) => row.key === 'encode:encode-stale');

		expect(staleRow).toMatchObject({ action: undefined, actionScope: undefined });
		expect(
			retryableEncodeJobIds([...dashboard.encode_queue.queued, ...dashboard.encode_queue.recent])
		).not.toContain('encode-stale');
	});
});
