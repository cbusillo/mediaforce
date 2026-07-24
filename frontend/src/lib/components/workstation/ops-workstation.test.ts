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
	hostWorkReason,
	opsWorkLabel,
	RefreshCoordinator,
	rowRecoveryLabel,
	rowRecoveryTitle,
	retryableEncodeJobIds,
	workerCapabilitiesSummary
} from './ops-workstation';

describe('RefreshCoordinator', () => {
	it('lets a manual refresh supersede an in-flight quiet poll', () => {
		const coordinator = new RefreshCoordinator();

		const quietId = coordinator.start('quiet');
		expect(quietId).toBe(1);
		expect(coordinator.start('quiet')).toBeNull();

		const manualId = coordinator.start('manual');
		expect(manualId).toBe(2);
		expect(coordinator.start('quiet')).toBeNull();

		expect(coordinator.finish('quiet', quietId ?? 0)).toBe(false);
		expect(coordinator.finish('manual', manualId ?? 0)).toBe(true);
		expect(coordinator.start('quiet')).toBe(3);
	});

	it('blocks overlapping manual refreshes', () => {
		const coordinator = new RefreshCoordinator();

		const firstManualId = coordinator.start('manual');
		expect(firstManualId).toBe(1);
		expect(coordinator.start('manual')).toBeNull();
		expect(coordinator.finish('manual', firstManualId ?? 0)).toBe(true);
		expect(coordinator.start('manual')).toBe(2);
	});
});

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
			host: 'Selecting computer',
			detail: 'transient worker fault'
		});
		expect(rows[2]).toMatchObject({
			tone: 'wait',
			action: 'retry-encode-prefix',
			actionScope: 'row',
			host: 'Unassigned',
			detail: 'quality target missed'
		});
		expect(rowRecoveryLabel(rows[1])).toBe('Waiting');
		expect(rowRecoveryTitle(rows[1])).toBe('No action is available for this row.');
		expect(rowRecoveryLabel(rows[2])).toBe('Retry folder');
		expect(rowRecoveryTitle(rows[2])).toContain('folder only');
		expect(rowRecoveryLabel(rows[3])).toBe('Automatic');
	});

	it('renders exact hard-stop and bypass states in the work-window column', () => {
		const dashboard = dashboardFixture();
		dashboard.encode_queue.running = [
			{
				job_id: 'encode-hard-stop',
				prefix: 'tv/show/season 1',
				status: 'running',
				host: { key: 'worker-1', label: 'worker-1' },
				schedule_state: 'active_hard_stop',
				schedule_close_deadline_at: '2026-07-24T19:00:00Z'
			},
			{
				job_id: 'encode-bypass',
				prefix: 'tv/show/season 2',
				status: 'running',
				host: { key: 'worker-1', label: 'worker-1' },
				bypass_schedule: true,
				schedule_state: 'bypassed'
			}
		];
		dashboard.encode_queue.running_count = 2;
		const hosts = hostsFixture();
		hosts.hosts[0].schedule_timezone = 'America/New_York';
		hosts.hosts[0].schedule_closes_at = '2026-07-24T19:00:00Z';

		const rows = buildOpsQueueRows(dashboard, hosts, new Date('2026-07-24T16:00:00Z'));

		expect(rows[0]).toMatchObject({
			scheduler: 'Stops at close',
			schedulerTone: 'active',
			scheduleState: 'active_hard_stop'
		});
		expect(rows[0].schedulerDetail).toContain('today at 3:00 PM EDT');
		expect(rows[1]).toMatchObject({
			scheduler: 'Bypassing schedule',
			schedulerTone: 'wait',
			scheduleState: 'bypassed'
		});
	});

	it('surfaces impossible work windows as actionable instead of passive waiting', () => {
		const dashboard = dashboardFixture();
		dashboard.encode_queue.running = [];
		dashboard.encode_queue.running_count = 0;
		dashboard.encode_queue.recent = [];
		dashboard.encode_queue.needs_attention_count = 0;
		dashboard.encode_queue.queued = [
			{
				job_id: 'encode-window-impossible',
				prefix: 'tv/Long Show/Season 1',
				status: 'queued',
				schedule_state: 'draining_impossible',
				waiting_reason:
					'Estimated runtime about 4h is longer than every configured host schedule window. Widen a host window or use Bypass scheduler.'
			}
		];
		dashboard.encode_queue.queued_count = 1;

		const blockers = buildOpsBlockers(dashboard, hostsFixture(), null);
		const readiness = buildOpsReadinessSummary(dashboard, hostsFixture(), null);

		expect(blockers[0]).toMatchObject({
			key: 'schedule-window-impossible',
			tone: 'fail',
			title: 'Long Show · Season 1 needs a longer work window',
			href: '/settings',
			linkLabel: 'Edit work windows'
		});
		expect(readiness).toMatchObject({
			tone: 'fail',
			title: 'A season needs a longer work window',
			metricLabel: 'Blocked',
			metricValue: '1'
		});
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

	it('surfaces unavailable data and retryable encodes as attention items', () => {
		const blockers = buildOpsBlockers(dashboardFixture(), hostsFixture(), 'Dashboard unavailable');

		expect(blockers.map((blocker) => blocker.key)).toEqual(['runtime-load', 'needs-attention']);
		expect(blockers[0]).toMatchObject({ tone: 'fail', title: 'Activity is unavailable' });
		expect(blockers[1]).toMatchObject({ tone: 'wait', action: 'retry-failed-encode' });
	});

	it('explains controller storage failures instead of exposing raw errno copy', () => {
		const dashboard = dashboardFixture();
		dashboard.encode_queue.recent = [
			{
				job_id: 'encode-storage-failure',
				prefix: 'tv/Constellation/Season 1',
				status: 'needs_attention',
				error: "[Errno 13] Permission denied: '/Volumes/media'"
			}
		];

		const row = buildOpsQueueRows(dashboard).find(
			(candidate) => candidate.key === 'encode:encode-storage-failure'
		);
		const blockers = buildOpsBlockers(dashboard, hostsFixture(), null);

		expect(row).toMatchObject({
			host: 'Unassigned',
			detail: 'Storage unavailable'
		});
		expect(blockers[0]).toMatchObject({
			title: 'Constellation · Season 1 needs attention',
			detail:
				'Mediaforce cannot access /Volumes/media on this computer. Mount the storage, then retry.'
		});
	});

	it('turns storage prefixes into human work labels', () => {
		expect(opsWorkLabel('tv/Constellation/Season 1')).toBe('Constellation · Season 1');
		expect(opsWorkLabel('movies/Arrival (2016)')).toBe('Arrival (2016)');
	});

	it('surfaces controller storage waits before claiming a computer can start', () => {
		const dashboard = dashboardFixture();
		dashboard.encode_queue.running = [];
		dashboard.encode_queue.running_count = 0;
		dashboard.encode_queue.needs_attention_count = 0;
		dashboard.encode_queue.recent = [];
		dashboard.encode_queue.queued = [
			{
				job_id: 'encode-storage-wait',
				prefix: 'tv/Constellation/Season 1',
				status: 'queued',
				scheduler_status_copy:
					'Mediaforce cannot access /Volumes/media/transcode on this computer. Mount the storage to continue.'
			}
		];
		dashboard.encode_queue.queued_count = 1;

		const blockers = buildOpsBlockers(dashboard, hostsFixture(), null);
		const summary = buildOpsReadinessSummary(dashboard, hostsFixture(), null);

		expect(blockers).toContainEqual({
			key: 'controller-storage',
			tone: 'wait',
			title: 'Media storage is not available',
			detail:
				'Mediaforce cannot access /Volumes/media/transcode on this computer. Mount the storage to continue.'
		});
		expect(summary).toMatchObject({
			tone: 'wait',
			title: 'Waiting for media storage',
			metricLabel: 'Waiting',
			metricValue: '1'
		});
	});

	it('surfaces no-ready-worker blockers when queued work cannot start', () => {
		const dashboard = dashboardFixture();
		dashboard.encode_queue.needs_attention_count = 0;
		dashboard.encode_queue.recent = [];
		dashboard.encode_queue.running_count = 0;
		dashboard.encode_queue.running = [];
		const hosts = hostsFixture();
		hosts.hosts[0].active_encode_count = 2;

		const blockers = buildOpsBlockers(dashboard, hosts, null);

		expect(blockers.map((blocker) => blocker.key)).toEqual(['no-hosts-ready']);
		expect(blockers[0]).toMatchObject({ tone: 'wait' });
	});

	it('keeps work schedule, attention, sample, and worker tones semantic', () => {
		const tiles = buildOpsStatusTiles(dashboardFixture(), hostsFixture(), null);

		expect(tiles).toMatchObject([
			{ label: 'Work schedule', tone: 'ready' },
			{ label: 'Processing', tone: 'wait' },
			{ label: 'Sample checks', tone: 'active' },
			{ label: 'Workers', tone: 'ready', value: '1 can encode / 2' }
		]);
	});

	it('counts busy and off-schedule workers separately from encode-ready capacity', () => {
		const hosts = hostsFixture();
		hosts.hosts[0].active_encode_count = 2;

		const tiles = buildOpsStatusTiles(dashboardFixture(), hosts, null);

		expect(tiles.at(-1)).toMatchObject({
			label: 'Workers',
			tone: 'wait',
			value: '1 busy / 2',
			detail: '2 reachable · 1 busy · 0 unavailable'
		});
	});

	it('summarizes the first-glance Ops readiness answer', () => {
		const summary = buildOpsReadinessSummary(dashboardFixture(), hostsFixture(), null);

		expect(summary).toMatchObject({
			tone: 'wait',
			title: 'A season needs attention',
			metricLabel: 'Needs you',
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
		expect(summary.detail).toContain('1 episode part across 1 computer');
		expect(summary.detail).toContain('1 test active');
	});

	it('distinguishes available, scheduled-off, and unavailable host states', () => {
		const [ready, scheduledOff] = hostsFixture().hosts;
		const unavailable = { ...ready, available: false, active_encode_count: 0 };

		expect(hostTone(ready)).toBe('active');
		expect(hostStateCopy(ready)).toBe('Busy');
		expect(hostTone(scheduledOff)).toBe('wait');
		expect(hostStateCopy(scheduledOff)).toBe('Off schedule');
		expect(hostTone(unavailable)).toBe('fail');
		expect(hostTone(unavailable, true)).toBe('wait');
		expect(hostStateCopy(unavailable)).toBe('Unavailable');
	});

	it('treats storage-recoverable workers as reconnecting capacity', () => {
		const dashboard = dashboardFixture();
		dashboard.encode_queue.needs_attention_count = 0;
		dashboard.encode_queue.recent = [];
		const recoverable = {
			...hostsFixture().hosts[0],
			available: false,
			queue_active: false,
			active_encode_count: 0,
			storage_recovery_available: true,
			message: 'Storage will reconnect when work starts'
		};
		const hosts = { compact: true, hosts: [recoverable] };

		expect(buildOpsBlockers(dashboard, hosts, null).map((blocker) => blocker.key)).not.toContain(
			'no-hosts-ready'
		);
		expect(buildOpsStatusTiles(dashboard, hosts, null).at(-1)).toMatchObject({
			label: 'Workers',
			value: '1 can encode / 1'
		});
		expect(hostTone(recoverable)).toBe('wait');
		expect(hostStateCopy(recoverable)).toBe('Reconnects storage');
	});

	it('shows running encode telemetry instead of stale restart errors', () => {
		const dashboard = dashboardFixture();
		dashboard.encode_queue.running[0] = {
			...dashboard.encode_queue.running[0],
			error: 'Encode queue job was interrupted by a web process restart.',
			telemetry_summary: '1% · 0.36x · 8.7 fps · Est. ETA 11h 4m'
		};

		expect(buildOpsQueueRows(dashboard)[0]).toMatchObject({
			detail: '0.36x · 8.7 fps · ETA 11h 4m'
		});
	});

	it('uses aggregate progress host labels for folder jobs', () => {
		const dashboard = dashboardFixture();
		dashboard.encode_queue.running[0] = {
			...dashboard.encode_queue.running[0],
			host: {},
			active_hosts: undefined,
			progress: {
				...dashboard.encode_queue.running[0].progress,
				active_host_labels: ['M2 MBP', 'M1 MBP']
			}
		};

		expect(buildOpsQueueRows(dashboard)[0]).toMatchObject({
			host: 'M2 MBP, M1 MBP',
			phase: '2 active episode parts'
		});
	});

	it('explains why an eligible computer is idle', () => {
		const dashboard = dashboardFixture();
		dashboard.encode_queue.queued = [];
		dashboard.encode_queue.queued_count = 0;
		const hosts = hostsFixture();
		hosts.hosts[0] = {
			...hosts.hosts[0],
			priority: 40,
			active_encode_count: 1,
			max_parallel_encodes: 1,
			queue_active: false,
			active_reason: 'parallel encode slots are full'
		};
		hosts.hosts[1] = {
			...hosts.hosts[1],
			key: 'mini',
			label: 'M1 mini',
			priority: 20,
			schedule_open: true,
			queue_active: true,
			active_encode_count: 0
		};

		expect(hostWorkReason(hosts.hosts[0], hosts, dashboard)).toBe('Working at capacity.');
		expect(hostWorkReason(hosts.hosts[1], hosts, dashboard)).toBe(
			'Next in line; all current episode parts are already assigned.'
		);
	});

	it('maps worker capabilities to user-facing labels', () => {
		expect(workerCapabilitiesSummary(['encode_queue', 'sample_calibration', 'proof_encode'])).toBe(
			'Process folders · Run samples · Run review evidence'
		);
		expect(workerCapabilitiesSummary([])).toBe('No work assigned');
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
