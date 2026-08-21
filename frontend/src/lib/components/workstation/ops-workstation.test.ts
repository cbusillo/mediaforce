import { describe, expect, it } from 'vitest';
import type { DashboardSummaryPayload, HostsPayload, MediaScopePayload } from '$lib/api/types';
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

function mediaScope(
	prefix: string,
	domain: MediaScopePayload['domain'],
	kind: MediaScopePayload['kind'],
	match: MediaScopePayload['match'] = 'descendants'
): MediaScopePayload {
	const segments = prefix.split('/');
	return {
		schema_version: 1,
		prefix,
		root: segments[0] ?? '',
		domain,
		kind,
		match,
		title: segments.at(-1) ?? prefix,
		subtitle: segments.at(-2) ?? '',
		scope_label:
			kind === 'tv_season'
				? 'Season'
				: kind === 'tv_series'
					? 'Series'
					: match === 'exact_item'
						? 'File'
						: 'Title'
	};
}

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
			recent_failed_count: 1,
			review_ready: []
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
					media_scope: mediaScope('movies/feature', 'movie', 'movie_title'),
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
					media_scope: mediaScope('tv/show/season 2', 'tv', 'tv_season'),
					status: 'retry_backoff',
					error: 'transient worker fault',
					scheduler_status_copy: 'retry backoff'
				}
			],
			recent: [
				{
					job_id: 'encode-3',
					prefix: 'tv/show/season 3',
					media_scope: mediaScope('tv/show/season 3', 'tv', 'tv_season'),
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
	it('prioritizes current running, queued, and sample rows', () => {
		const rows = buildOpsQueueRows(dashboardFixture());

		expect(rows.map((row) => row.key)).toEqual([
			'encode:encode-1',
			'encode:encode-2',
			'sample:sample-1'
		]);
		expect(rows[1]).toMatchObject({
			scopeLabel: 'Season',
			tone: 'wait',
			action: undefined,
			actionScope: undefined,
			host: 'Selecting computer',
			detail: 'transient worker fault'
		});
		expect(rows[0]).toMatchObject({ scopeLabel: 'Movie' });
		expect(rowRecoveryLabel(rows[1])).toBe('Waiting');
		expect(rowRecoveryTitle(rows[1])).toBe('No action is available for this row.');
		expect(rowRecoveryLabel(rows[2])).toBe('Automatic');
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
				media_scope: mediaScope('tv/Long Show/Season 1', 'tv', 'tv_season'),
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

		dashboard.encode_queue.running = [
			{
				job_id: 'encode-running',
				prefix: 'movies/Arrival (2016)',
				status: 'running'
			}
		];
		dashboard.encode_queue.running_count = 1;

		expect(buildOpsReadinessSummary(dashboard, hostsFixture(), null)).toMatchObject({
			tone: 'fail',
			title: 'A season needs a longer work window'
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

		expect(blockers.map((blocker) => blocker.key)).toEqual([
			'runtime-load',
			'needs-attention:encode-3'
		]);
		expect(blockers[0]).toMatchObject({ tone: 'fail', title: 'Activity is unavailable' });
		expect(blockers[1]).toMatchObject({
			tone: 'wait',
			href: '/folders/tv/show/season%203',
			linkLabel: 'Review item'
		});
	});

	it('keeps terminal recent jobs visible when the explicit attention list is empty', () => {
		const dashboard = dashboardFixture();
		dashboard.encode_queue.needs_attention = [];
		dashboard.encode_queue.needs_attention_count = 0;
		dashboard.encode_queue.recent = [
			{
				job_id: 'stopped-encode',
				prefix: 'tv/Futurama/Season 8',
				media_scope: {
					...mediaScope('tv/Futurama/Season 8', 'tv', 'tv_season'),
					title: 'Futurama · Season 8'
				},
				status: 'stopped',
				error: 'Processing was stopped before completion.'
			}
		];

		expect(buildOpsBlockers(dashboard, hostsFixture(), null)[0]).toMatchObject({
			key: 'needs-attention:stopped-encode',
			title: 'Futurama · Season 8 needs review',
			href: '/folders/tv/Futurama/Season%208',
			linkLabel: 'Review item'
		});
		expect(buildOpsReadinessSummary(dashboard, hostsFixture(), null)).toMatchObject({
			tone: 'active',
			detail: expect.stringContaining('A season needs attention')
		});
	});

	it('surfaces review-ready samples before stopped processing work', () => {
		const dashboard = dashboardFixture();
		dashboard.calibration_queue.review_ready = [
			{
				job_id: 'sample-review-1',
				prefix: 'tv/Bluey (2018)/Season 3/Bluey.2018.S03E49.1080p.mkv',
				media_scope: {
					...mediaScope(
						'tv/Bluey (2018)/Season 3/Bluey.2018.S03E49.1080p.mkv',
						'tv',
						'media_file',
						'exact_item'
					),
					parent: {
						prefix: 'tv/Bluey (2018)/Season 3',
						title: 'Season 3'
					}
				}
			}
		];
		dashboard.calibration_queue.review_ready_count = 1;

		const blockers = buildOpsBlockers(dashboard, hostsFixture(), null);

		expect(blockers.map((blocker) => blocker.key)).toEqual([
			'review-ready:sample-review-1',
			'needs-attention:encode-3'
		]);
		expect(blockers[0]).toMatchObject({
			tone: 'ready',
			title: 'Bluey · Season 3 · Episode 49 is ready for your review',
			detail: 'Compare the sample and decide whether to approve it.',
			href: '/folders/tv/Bluey%20(2018)/Season%203/Bluey.2018.S03E49.1080p.mkv',
			linkLabel: 'Review sample'
		});
		expect(buildOpsReadinessSummary(dashboard, hostsFixture(), null)).toMatchObject({
			tone: 'ready',
			title: 'Bluey · Season 3 · Episode 49 is ready for review',
			detail: 'Compare the sample and decide whether to approve it.',
			metricLabel: 'Needs you',
			metricValue: '1'
		});
		expect(buildOpsStatusTiles(dashboard, hostsFixture(), null)[2]).toMatchObject({
			label: 'Sample checks',
			detail: '1 waiting for review',
			tone: 'ready'
		});
	});

	it('does not repeat the series name when an exact TV item has no season folder', () => {
		const dashboard = dashboardFixture();
		dashboard.calibration_queue.review_ready = [
			{
				job_id: 'sample-review-flat-tv',
				prefix: 'tv/Bluey (2018)/Bluey.2018.S03E49.1080p.mkv',
				media_scope: {
					...mediaScope(
						'tv/Bluey (2018)/Bluey.2018.S03E49.1080p.mkv',
						'tv',
						'media_file',
						'exact_item'
					),
					parent: {
						prefix: 'tv/Bluey (2018)',
						title: 'Bluey (2018)'
					}
				}
			}
		];

		expect(buildOpsBlockers(dashboard, hostsFixture(), null)[0].title).toBe(
			'Bluey · Episode 49 is ready for your review'
		);
	});

	it('hides target-band internals from the attention summary', () => {
		const dashboard = dashboardFixture();
		dashboard.encode_queue.recent = [
			{
				job_id: 'encode-size-miss',
				prefix: 'tv/show/season 3',
				status: 'needs_attention',
				error:
					'Final output size missed the approved target band: status=over_target, actual=60204063, target=48673111, lower=46239455, upper=51106767.'
			}
		];

		expect(buildOpsBlockers(dashboard, hostsFixture(), null)[0].detail).toBe(
			'The finished file was outside the approved size range. Your original media is safe; review the item before retrying.'
		);

		dashboard.encode_queue.recent[0].error =
			'The approved target size conflicts with the configured quality floor (target_band_violates_quality_floor); target=225000000 bytes, best_reachable=523458023 bytes.';
		expect(buildOpsBlockers(dashboard, hostsFixture(), null)[0].detail).toBe(
			'The saved size goal is too small to preserve the required quality. Choose a new size goal before retrying.'
		);
	});

	it('uses the active media scope in paused and mixed queue headlines', () => {
		const dashboard = dashboardFixture();
		dashboard.encode_queue.state.is_paused = true;
		dashboard.encode_queue.running = [];
		dashboard.encode_queue.running_count = 0;
		dashboard.encode_queue.recent = [];
		dashboard.encode_queue.needs_attention_count = 0;
		dashboard.encode_queue.queued = [
			{
				job_id: 'movie-queued',
				prefix: 'movies/Arrival (2016)',
				media_scope: mediaScope('movies/Arrival (2016)', 'movie', 'movie_title'),
				job_kind: 'folder',
				item_count: 1,
				status: 'queued'
			}
		];
		dashboard.encode_queue.queued_count = 1;

		expect(buildOpsBlockers(dashboard, hostsFixture(), null)[0].title).toBe('Movie work is paused');
		expect(buildOpsReadinessSummary(dashboard, hostsFixture(), null).title).toBe(
			'Movie work is paused'
		);

		dashboard.encode_queue.queued.push({
			job_id: 'season-queued',
			prefix: 'tv/Constellation/Season 1',
			media_scope: mediaScope('tv/Constellation/Season 1', 'tv', 'tv_season'),
			job_kind: 'folder',
			item_count: 8,
			status: 'queued'
		});
		dashboard.encode_queue.queued_count = 2;

		expect(buildOpsReadinessSummary(dashboard, hostsFixture(), null).title).toBe(
			'Media work is paused'
		);

		dashboard.encode_queue.queued = [dashboard.encode_queue.queued[1]];
		dashboard.encode_queue.queued_count = 9;
		expect(buildOpsReadinessSummary(dashboard, hostsFixture(), null).title).toBe(
			'Media work is paused'
		);
	});

	it('recognizes exact episode work in attention copy', () => {
		const dashboard = dashboardFixture();
		dashboard.encode_queue.running = [];
		dashboard.encode_queue.queued = [];
		dashboard.encode_queue.running_count = 0;
		dashboard.encode_queue.queued_count = 0;
		dashboard.encode_queue.recent = [
			{
				job_id: 'episode-attention',
				prefix: 'tv/Constellation/Season 1/S01E01.mkv',
				media_scope: mediaScope(
					'tv/Constellation/Season 1/S01E01.mkv',
					'tv',
					'media_file',
					'exact_item'
				),
				job_kind: 'folder',
				item_count: 1,
				status: 'needs_attention'
			}
		];
		dashboard.encode_queue.needs_attention_count = 1;

		expect(buildOpsReadinessSummary(dashboard, hostsFixture(), null).title).toBe(
			'An episode needs attention'
		);
		expect(buildOpsBlockers(dashboard, hostsFixture(), null)[0].title).toBe(
			'Constellation · Season 1 · Episode 1 needs review'
		);

		dashboard.encode_queue.recent[0].prefix = 'tv/Constellation/S01E01.mkv';
		dashboard.encode_queue.recent[0].media_scope = mediaScope(
			'tv/Constellation/S01E01.mkv',
			'tv',
			'media_file',
			'exact_item'
		);
		expect(buildOpsReadinessSummary(dashboard, hostsFixture(), null).title).toBe(
			'An episode needs attention'
		);
	});

	it('routes final-size misses back to Studio instead of offering a plain retry', () => {
		const dashboard = dashboardFixture();
		dashboard.encode_queue.running = [];
		dashboard.encode_queue.queued = [];
		dashboard.encode_queue.running_count = 0;
		dashboard.encode_queue.queued_count = 0;
		dashboard.encode_queue.recent = [
			{
				job_id: 'episode-size-miss',
				prefix: 'tv/Constellation/Season 1/S01E01.mkv',
				media_scope: mediaScope(
					'tv/Constellation/Season 1/S01E01.mkv',
					'tv',
					'media_file',
					'exact_item'
				),
				job_kind: 'folder',
				item_count: 1,
				status: 'needs_attention',
				progress: {
					failure_analysis: {
						kind: 'final_size_target_miss',
						retry_strategy: 'fresh_goal_required',
						summary: 'Choose a fresh size or compression goal before retrying.'
					}
				}
			}
		];
		dashboard.encode_queue.needs_attention_count = 1;

		const row = buildOpsQueueRows(dashboard, hostsFixture()).find(
			(candidate) => candidate.key === 'encode:episode-size-miss'
		);
		const blocker = buildOpsBlockers(dashboard, hostsFixture(), null)[0];

		expect(row).toBeUndefined();
		expect(blocker).toMatchObject({
			detail: 'Choose a fresh size or compression goal before retrying.'
		});
	});

	it('uses media-item copy for mixed attention work', () => {
		const dashboard = dashboardFixture();
		dashboard.encode_queue.running = [];
		dashboard.encode_queue.queued = [];
		dashboard.encode_queue.running_count = 0;
		dashboard.encode_queue.queued_count = 0;
		dashboard.encode_queue.recent = [
			{
				job_id: 'movie-attention',
				prefix: 'movies/Arrival (2016)',
				job_kind: 'folder',
				item_count: 1,
				status: 'needs_attention'
			},
			{
				job_id: 'season-attention',
				prefix: 'tv/Constellation/Season 1',
				job_kind: 'folder',
				item_count: 8,
				status: 'needs_attention'
			}
		];
		dashboard.encode_queue.needs_attention_count = 2;

		expect(buildOpsReadinessSummary(dashboard, hostsFixture(), null).title).toBe(
			'2 media items need attention'
		);
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

		expect(row).toBeUndefined();
		expect(blockers[0]).toMatchObject({
			title: 'Constellation · Season 1 needs review',
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
		const dashboard = dashboardFixture();
		dashboard.encode_queue.running = [];
		dashboard.encode_queue.running_count = 0;
		dashboard.encode_queue.queued = [];
		dashboard.encode_queue.queued_count = 0;
		dashboard.calibration_queue.sample.running = [];
		dashboard.calibration_queue.active_count = 0;
		const summary = buildOpsReadinessSummary(dashboard, hostsFixture(), null);

		expect(summary).toMatchObject({
			tone: 'wait',
			title: 'A season needs attention',
			metricLabel: 'Needs you',
			metricValue: '1'
		});
	});

	it('keeps active work as the headline while surfacing queued and attention context', () => {
		const dashboard = dashboardFixture();

		const summary = buildOpsReadinessSummary(dashboard, hostsFixture(), null);

		expect(summary).toMatchObject({
			tone: 'active',
			title: 'feature is working',
			metricLabel: 'Running',
			metricValue: '2'
		});
		expect(summary.detail).toContain('1 item processing across 1 computer');
		expect(summary.detail).toContain('1 test active');
		expect(summary.detail).toContain('show · season 2 is queued');
		expect(summary.detail).toContain('A season needs attention');
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
			phase: '2 active processing tasks'
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
			'Next in line; all current media work is already assigned.'
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

	it('omits stale historical encode rows from current work', () => {
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

		expect(staleRow).toBeUndefined();
		expect(
			retryableEncodeJobIds([...dashboard.encode_queue.queued, ...dashboard.encode_queue.recent])
		).not.toContain('encode-stale');
	});
});
