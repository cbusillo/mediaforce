import { describe, expect, it } from 'vitest';
import type { DashboardSummaryPayload, HostsPayload } from '$lib/api/types';
import {
	buildOpsBlockers,
	buildOpsQueueRows,
	buildOpsStatusTiles,
	hostStateCopy,
	hostTone
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
				pending_review_count: 0
			},
			full: {
				running: [],
				queued: [],
				pending_review: [],
				running_count: 0,
				queued_count: 0,
				pending_review_count: 0
			},
			active_count: 1
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
		archive_cleanup: {
			archive_root: '/runtime/archive',
			file_count: 0,
			total_size_bytes: 0,
			has_cleanup: false
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
	it('prioritizes running, queued, retryable encode, and sample rows', () => {
		const rows = buildOpsQueueRows(dashboardFixture());

		expect(rows.map((row) => row.key)).toEqual([
			'encode:encode-1',
			'encode:encode-2',
			'encode:encode-3',
			'sample:sample-1'
		]);
		expect(rows[1]).toMatchObject({
			tone: 'wait',
			action: 'retry-failed-encode',
			detail: 'transient worker fault'
		});
		expect(rows[2]).toMatchObject({
			tone: 'fail',
			action: 'retry-failed-encode',
			detail: 'quality target missed'
		});
	});

	it('surfaces runtime partials, failed encodes, and closed schedules as blockers', () => {
		const blockers = buildOpsBlockers(dashboardFixture(), hostsFixture(), 'Dashboard unavailable');

		expect(blockers.map((blocker) => blocker.key)).toEqual([
			'runtime-load',
			'needs-attention',
			'host:overnight'
		]);
		expect(blockers[0]).toMatchObject({ tone: 'fail', title: 'Runtime data partial' });
		expect(blockers[1]).toMatchObject({ action: 'retry-failed-encode' });
		expect(blockers[2]).toMatchObject({ tone: 'wait', detail: 'opens at 23:00' });
	});

	it('keeps scheduler, attention, sample, and host tones semantic', () => {
		const tiles = buildOpsStatusTiles(dashboardFixture(), hostsFixture(), null);

		expect(tiles).toMatchObject([
			{ label: 'Scheduler', tone: 'ready' },
			{ label: 'Encode jobs', tone: 'fail' },
			{ label: 'Samples', tone: 'active' },
			{ label: 'Hosts', tone: 'ready' }
		]);
	});

	it('distinguishes available, scheduled-off, and unavailable host states', () => {
		const [ready, scheduledOff] = hostsFixture().hosts;
		const unavailable = { ...ready, available: false, active_encode_count: 0 };

		expect(hostTone(ready)).toBe('active');
		expect(hostStateCopy(ready)).toBe('Encoding');
		expect(hostTone(scheduledOff)).toBe('wait');
		expect(hostStateCopy(scheduledOff)).toBe('Off window');
		expect(hostTone(unavailable)).toBe('fail');
		expect(hostStateCopy(unavailable)).toBe('Unavailable');
	});
});
