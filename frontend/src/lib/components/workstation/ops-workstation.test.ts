import { describe, expect, it } from 'vitest';
import type { DashboardSummaryPayload, HostsPayload, MediaScopePayload } from '$lib/api/types';
import { scheduleLabels } from '$lib/hosts/schedule';
import {
	activityScheduleDetailCopy,
	activitySchedulePresentationCopy,
	buildOpsBlockers,
	buildOpsHistoryRows,
	buildOpsQueueRows,
	buildOpsReadinessSummary,
	buildOpsFooterSignals,
	buildOpsStatusTiles,
	hostConnectionRetryAvailable,
	hostPrepareDisabled,
	hostPrepareTitle,
	hostStateCopy,
	hostTone,
	hostWorkReason,
	opsRowWorkLabel,
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
				scheduler_summary: 'runs weekdays between 09:00 and 22:00 (host local)'
			},
			telemetry: { eta_copy: '24m' }
		},
		catalog_empty: false,
		folder_cache_key: 'fixture',
		metric_support: { vmaf: true, xpsnr: false, ssim: true },
		metric_status_copy: 'VMAF · SSIM'
	};
}

function quietDashboardFixture(): DashboardSummaryPayload {
	const dashboard = dashboardFixture();
	dashboard.encode_queue.running = [];
	dashboard.encode_queue.running_count = 0;
	dashboard.encode_queue.queued = [];
	dashboard.encode_queue.queued_count = 0;
	dashboard.calibration_queue.sample.running = [];
	dashboard.calibration_queue.active_count = 0;
	return dashboard;
}

describe('Activity work-window totals', () => {
	it.each([true, false])(
		'shows the file wait count with ordinary queued rows present: %s',
		(hasQueuedRows) => {
			const dashboard = quietDashboardFixture();
			const queue = dashboard.encode_queue;
			queue.recent = [];
			queue.needs_attention_count = 0;
			queue.queued = hasQueuedRows
				? [{ job_id: 'waiting-show', prefix: 'tv/show', status: 'queued' }]
				: [];
			queue.queued_count = queue.queued.length;
			queue.queued_schedule_waiting_count = 10;
			queue.state.scheduler_summary = 'runs anytime';
			const hosts = hostsFixture();
			hosts.hosts = hosts.hosts.map((host) => ({ ...host, schedule_open: false }));

			expect(buildOpsReadinessSummary(dashboard, hosts, null)).toMatchObject({
				tone: 'wait',
				title: 'Waiting for a work window',
				metricLabel: 'Files waiting',
				metricValue: String(queue.queued_schedule_waiting_count)
			});
		}
	);

	it.each([true, false])(
		'keeps unavailable or busy computers distinct: available %s',
		(available) => {
			const dashboard = quietDashboardFixture();
			const queue = dashboard.encode_queue;
			queue.recent = [];
			queue.needs_attention_count = 0;
			queue.queued = [{ job_id: 'waiting-show', prefix: 'tv/show', status: 'queued' }];
			queue.queued_count = 1;
			queue.queued_schedule_waiting_count = 0;
			const hosts = hostsFixture();
			hosts.hosts = hosts.hosts.map((host) => ({
				...host,
				available,
				schedule_open: true,
				active_encode_count: host.max_parallel_encodes
			}));

			expect(buildOpsReadinessSummary(dashboard, hosts, null)).toMatchObject({
				title: available ? 'Computers are busy or waiting' : 'No computer can work right now',
				metricLabel: 'Available',
				metricValue: '0'
			});
		}
	);

	it('does not describe retry delays as window waits when no encode computer is configured', () => {
		const dashboard = quietDashboardFixture();
		const queue = dashboard.encode_queue;
		queue.recent = [];
		queue.needs_attention = [];
		queue.needs_attention_count = 0;
		queue.queued = [1, 2].map((number) => ({
			job_id: `retry-${number}`,
			prefix: `tv/show/season ${number}`,
			status: 'retry_backoff',
			waiting_reason: 'Waiting before retrying',
			schedule_state: 'none' as const
		}));
		queue.queued_count = queue.queued.length;
		queue.queued_waiting_count = queue.queued.length;
		queue.queued_schedule_waiting_count = 0;
		queue.state.scheduler_summary = 'runs anytime';
		const hosts = hostsFixture();
		hosts.hosts = [];
		expect(
			buildOpsBlockers(dashboard, hosts, null).some((row) => row.key === 'schedule-waiting')
		).toBe(false);
		expect(buildOpsReadinessSummary(dashboard, hosts, null).title).not.toMatch(/scheduled time/);
	});

	it('counts the waiting files of a show needing attention in the Activity blocker', () => {
		const dashboard = quietDashboardFixture();
		dashboard.encode_queue.queued_waiting_count = 0;
		dashboard.encode_queue.queued_schedule_waiting_count = 10;
		dashboard.encode_queue.state.scheduler_summary = 'runs anytime';
		const hosts = hostsFixture();
		hosts.hosts = [];
		const blocker = buildOpsBlockers(dashboard, hosts, null).find(
			(row) => row.key === 'schedule-waiting'
		);
		expect(blocker?.title).toMatch(/10 files.*work window/);
		expect(blocker?.detail).not.toMatch(/runs anytime/);
	});
});

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
	it('adapts shared schedule labels only for Activity', () => {
		const cases = [
			['Running', 'Working'],
			['Open', 'Ready'],
			['Not accepting', 'Off schedule'],
			[scheduleLabels.waitingForLongerWindow, scheduleLabels.waitingForLongerWindow],
			['Window too short', scheduleLabels.waitingForLongerWindow],
			['No schedule state', 'Schedule unavailable'],
			[scheduleLabels.bypassed, scheduleLabels.bypassed]
		] as const;

		for (const [label, expected] of cases) {
			const shared = {
				state: 'host_open' as const,
				label,
				tone: 'ready' as const,
				detail: '2 computers are ready'
			};
			expect(activitySchedulePresentationCopy(shared)).toMatchObject({
				label: expected,
				detail: shared.detail
			});
			expect(shared.label).toBe(label);
		}

		expect(activityScheduleDetailCopy('runs anytime')).toBe('Work runs anytime');
		expect(activityScheduleDetailCopy('runs weekdays between 20:00 and 06:00 (host local)')).toBe(
			'Work runs weekdays between 20:00 and 06:00 (computer local time)'
		);
		expect(
			activityScheduleDetailCopy('waiting for runs weekdays between 20:00 and 06:00 (host local)')
		).toBe('Waiting for work weekdays between 20:00 and 06:00 (computer local time)');
		expect(activityScheduleDetailCopy('waiting for never runs')).toBe('Work schedule is off');
		expect(
			activityScheduleDetailCopy('Mediaforce cannot access /Volumes/worker-staging on host-nuc.')
		).toBe('Mediaforce cannot access /Volumes/worker-staging on host-nuc.');
		expect(
			activitySchedulePresentationCopy({
				state: 'host_off_schedule',
				label: 'Off schedule',
				tone: 'wait',
				detail: 'Starts automatically when Smoke Drain Worker opens.'
			})?.detail
		).toBe('Starts automatically when Smoke Drain Worker opens.');
	});

	it('prioritizes current running, queued, and sample rows', () => {
		const rows = buildOpsQueueRows(dashboardFixture());

		expect(rows.map((row) => row.key)).toEqual([
			'encode:encode-1',
			'encode:encode-2',
			'sample:sample-1'
		]);
		expect(rows.map((row) => row.status)).toEqual(['Working', 'Retry waiting', 'Working']);
		expect(rows.map((row) => row.status).join(' ')).not.toMatch(/[a-z]+_[a-z_]+/);
		expect(rows[1]).toMatchObject({
			scopeLabel: 'Season',
			tone: 'wait',
			action: undefined,
			actionScope: undefined,
			host: 'Selecting computer',
			detail: 'Mediaforce could not finish this work.'
		});
		expect(rows[0]).toMatchObject({ scopeLabel: 'Movie' });
		expect(rowRecoveryLabel(rows[1])).toBe('Waiting');
		expect(rowRecoveryTitle(rows[1])).toBe('No action is available for this row.');
		expect(rowRecoveryLabel(rows[2])).toBe('Runs automatically');
	});

	it('maps internal sample stages to operator states', () => {
		const dashboard = dashboardFixture();
		for (const [stage, expected] of [
			['preparing_source', 'Starting'],
			['inspecting_source', 'Working'],
			['measuring_quality', 'Working'],
			['selecting_review_moments', 'Working']
		] as const) {
			dashboard.calibration_queue.sample.running[0].progress = { schema_version: 1, stage };
			const sample = buildOpsQueueRows(dashboard).find((row) => row.key === 'sample:sample-1');
			expect(sample).toMatchObject({ status: 'Working', progress: expected });
			expect(sample?.progress).not.toContain('_');
		}
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
			scheduler: scheduleLabels.stopsAtClose,
			schedulerTone: 'active',
			scheduleState: 'active_hard_stop'
		});
		expect(rows[0].schedulerDetail).toContain('today at 3:00 PM EDT');
		expect(rows[1]).toMatchObject({
			scheduler: scheduleLabels.bypassed,
			schedulerTone: 'wait',
			scheduleState: 'bypassed'
		});
	});

	it('surfaces impossible work windows as actionable instead of passive waiting', () => {
		const impossibleWindowReason =
			"Estimated runtime about 4h is longer than every work window. Widen a computer's work window or intentionally bypass the work schedule.";
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
				waiting_reason: impossibleWindowReason
			}
		];
		dashboard.encode_queue.queued_count = 1;

		const blockers = buildOpsBlockers(dashboard, hostsFixture(), null);
		const readiness = buildOpsReadinessSummary(dashboard, hostsFixture(), null);

		expect(blockers[0]).toMatchObject({
			key: 'schedule-window-impossible',
			tone: 'fail',
			title: 'Long Show · Season 1 needs a longer work window',
			// The scheduler's own sentence, shown as it is written.
			detail: impossibleWindowReason,
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
			progress: "Didn't finish",
			detail: 'The sample stopped before it finished.'
		});
		expect(rowRecoveryLabel(rows[0])).toBe('Nothing to do');
	});

	it('says when a finished sample ended and how, in plain words', () => {
		const dashboard = dashboardFixture();
		dashboard.calibration_queue.sample.recent_failed = [
			{
				job_id: 'size-goal',
				prefix: 'tv/House',
				status: 'failed',
				finished_at: '2026-09-09T03:42:33+00:00',
				updated_at: '2026-09-10T08:00:00+00:00',
				progress: { stage: 'searching_target' },
				error:
					'The approved target size was not reached before the configured search bound (largest_quality_safe_candidate_under_target_band); target=293777299 bytes.'
			},
			{
				job_id: 'cleanup',
				prefix: 'movies/Play Dirty (2025)',
				status: 'failed',
				finished_at: '2026-08-29T15:13:48+00:00',
				progress: { stage: 'searching_target' },
				error: 'Managed process containment cleanup is unproven.'
			},
			{
				job_id: 'unknown',
				prefix: 'tv/Suits/Season 2',
				status: 'stopped',
				updated_at: '2026-06-05T11:41:43+00:00',
				error: 'worker_exit status=137 in run_calibration_job'
			},
			{
				job_id: 'ledger',
				prefix: 'tv/Raising Hope/Season 2',
				status: 'failed',
				finished_at: '2026-05-01T10:00:00+00:00',
				error: 'The stream budget ledger does not contain a resolved target video budget.'
			},
			{
				job_id: 'jargon',
				prefix: 'tv/Raising Hope/Season 3',
				status: 'failed',
				finished_at: '2026-05-02T10:00:00+00:00',
				error: 'Boundary compatibility uses an unsupported schema version'
			},
			{
				job_id: 'other-ledger',
				prefix: 'tv/Raising Hope/Season 5',
				status: 'failed',
				finished_at: '2026-05-02T12:00:00+00:00',
				error: 'The stream budget ledger source fingerprint is stale.'
			},
			{
				job_id: 'unmapped',
				prefix: 'tv/Raising Hope/Season 4',
				status: 'failed',
				finished_at: '2026-05-03T10:00:00+00:00',
				error: 'Target-size search requires sample-encode measurement support'
			},
			{
				job_id: 'storage',
				prefix: 'tv/The Expanse/Season 4',
				status: 'failed',
				finished_at: '2026-05-04T10:00:00+00:00',
				error: 'Shared storage disconnected'
			},
			{
				job_id: 'no-reason',
				prefix: 'tv/Lucifer/Season 2',
				status: 'failed',
				created_at: '2026-04-25T18:28:35+00:00'
			}
		];

		const rows = buildOpsHistoryRows(dashboard);

		const shown = (value: string) =>
			new Date(value).toLocaleString('en-US', {
				month: 'short',
				day: 'numeric',
				hour: 'numeric',
				minute: '2-digit'
			});
		expect(rows.map((row) => [row.progress, row.detail])).toEqual([
			["Didn't finish", 'The size goal could not be reached at a quality that passes.'],
			["Didn't finish", 'Mediaforce could not confirm the sample stopped cleanly.'],
			['Stopped', 'The sample stopped before it finished.'],
			["Didn't finish", 'Mediaforce could not work out the size to aim for.'],
			["Didn't finish", 'The sample stopped before it finished.'],
			["Didn't finish", 'The sample stopped before it finished.'],
			["Didn't finish", 'The sample stopped before it finished.'],
			["Didn't finish", 'The shared storage disconnected.'],
			["Didn't finish", 'The sample stopped before it finished.']
		]);
		expect(rows.map((row) => row.scheduler)).toEqual([
			shown('2026-09-09T03:42:33+00:00'),
			shown('2026-08-29T15:13:48+00:00'),
			shown('2026-06-05T11:41:43+00:00'),
			shown('2026-05-01T10:00:00+00:00'),
			shown('2026-05-02T10:00:00+00:00'),
			shown('2026-05-02T12:00:00+00:00'),
			shown('2026-05-03T10:00:00+00:00'),
			shown('2026-05-04T10:00:00+00:00'),
			'Earlier'
		]);
	});

	it("leaves a current sample's note as it is", () => {
		const dashboard = dashboardFixture();
		dashboard.calibration_queue.sample.queued = [
			{
				job_id: 'waiting-sample',
				prefix: 'tv/House',
				status: 'queued',
				notes: 'Managed process containment cleanup is unproven.'
			}
		];

		const row = buildOpsQueueRows(dashboard).find(
			(candidate) => candidate.key === 'sample:waiting-sample'
		);

		expect(row?.detail).toBe('Managed process containment cleanup is unproven.');
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

	it('lists every unfinished reason instead of only the first failure', () => {
		const dashboard = dashboardFixture();
		dashboard.encode_queue.recent = [];
		dashboard.encode_queue.needs_attention = [
			{
				job_id: 'big-brother',
				prefix: 'tv/Big Brother (US)',
				status: 'needs_attention',
				error:
					'Final output size missed the approved target band: status=over_target, actual=2, target=1, lower=1, upper=1.',
				progress: {
					unfinished_breakdown: [
						{
							reason: 'quality_floor_size_conflict',
							label: 'waiting for your OK to use more space',
							count: 4,
							items: []
						},
						{ reason: 'stopped', label: 'stopped', count: 4, items: [] },
						{ reason: 'retrying', label: 'trying again soon', count: 2, items: [] }
					]
				}
			}
		];
		dashboard.encode_queue.needs_attention_count = 1;

		const blocker = buildOpsBlockers(dashboard, hostsFixture(), null).find(
			(item) => item.key === 'needs-attention:big-brother'
		);

		expect(blocker?.detail).toBe(
			'10 unfinished: 4 waiting for your OK to use more space · 4 stopped · 2 trying again soon'
		);
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
			title: 'Futurama · Season 8 is waiting for you to take a look',
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
		dashboard.calibration_queue.sample.pending_review = [
			{
				job_id: 'sample-review-1',
				prefix: 'tv/Bluey (2018)/Season 3/Bluey.2018.S03E49.1080p.mkv',
				status: 'pending_review'
			}
		];
		dashboard.calibration_queue.sample.pending_review_count = 1;

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
			tone: 'active',
			title: 'feature is working',
			detail: expect.stringContaining('1 sample ready for review'),
			metricLabel: 'Running'
		});
		expect(buildOpsStatusTiles(dashboard, hostsFixture(), null)[2]).toMatchObject({
			label: 'Sample queue',
			detail: '1 waiting for review',
			tone: 'ready'
		});
		expect(buildOpsQueueRows(dashboard, hostsFixture()).map((row) => row.key)).not.toContain(
			'sample:sample-review-1'
		);
	});

	it('shows one current row when an encode job transitions into sample review', () => {
		const dashboard = quietDashboardFixture();
		const prefix = 'tv/Bluey (2018)/Season 3/Bluey.2018.S03E49.1080p.mkv';
		dashboard.encode_queue.running = [
			{
				job_id: 'encode-sample-shadow',
				prefix,
				status: 'running',
				host: { label: 'M4 Studio' },
				progress: { percent_complete: 1, current_item_rel_path: prefix }
			},
			{
				job_id: 'encode-unrelated',
				prefix: 'movies/Unrelated Feature',
				status: 'running'
			}
		];
		dashboard.encode_queue.running_count = 2;
		dashboard.calibration_queue.sample.pending_review = [
			{
				job_id: 'sample-review-transition',
				prefix,
				status: 'pending_review',
				host: { label: 'M4 Studio' }
			}
		];
		dashboard.calibration_queue.sample.pending_review_count = 1;

		const rows = buildOpsQueueRows(dashboard, hostsFixture());
		expect(rows.map((row) => row.key)).toEqual([
			'encode:encode-unrelated',
			'sample:sample-review-transition'
		]);
		expect(rows[1]).toMatchObject({
			status: 'Waiting',
			progress: 'Complete',
			scheduler: 'Review unavailable',
			detail:
				'Comparison clips are unavailable. Mediaforce kept the completed sample visible for diagnosis.'
		});
	});

	it('hides the transitional encode row when the sample is ready for review', () => {
		const dashboard = quietDashboardFixture();
		const reviewPrefix = 'tv/Bluey (2018)/Season 3/Bluey.2018.S03E49.1080p.mkv';
		const reviewReadySample = {
			job_id: 'sample-review-ready',
			prefix: reviewPrefix,
			status: 'pending_review'
		};
		dashboard.encode_queue.running = [
			{
				job_id: 'encode-review-ready-shadow',
				prefix: reviewPrefix,
				media_scope: mediaScope(reviewPrefix, 'tv', 'media_file', 'exact_item'),
				status: 'running'
			}
		];
		dashboard.encode_queue.running_count = 1;
		dashboard.encode_queue.recent = [];
		dashboard.encode_queue.needs_attention = [];
		dashboard.encode_queue.needs_attention_count = 0;
		dashboard.calibration_queue.sample.pending_review = [reviewReadySample];
		dashboard.calibration_queue.sample.pending_review_count = 1;
		dashboard.calibration_queue.review_ready = [reviewReadySample];
		dashboard.calibration_queue.review_ready_count = 1;

		expect(buildOpsQueueRows(dashboard, hostsFixture())).toEqual([]);
		expect(buildOpsStatusTiles(dashboard, hostsFixture(), null)[1]).toMatchObject({
			value: '0 working · 0 waiting',
			tone: 'idle'
		});
		expect(buildOpsFooterSignals(dashboard, hostsFixture())[0]).toMatchObject({
			value: '0/0',
			tone: 'idle'
		});
	});

	it('only hides the encode row processing the reviewed item within a shared folder scope', () => {
		const dashboard = quietDashboardFixture();
		const folderPrefix = 'tv/Bluey (2018)/Season 3';
		const reviewPrefix = `${folderPrefix}/Bluey.2018.S03E49.1080p.mkv`;
		dashboard.encode_queue.running = [
			{
				job_id: 'encode-sample-shadow',
				prefix: folderPrefix,
				media_scope: mediaScope(folderPrefix, 'tv', 'tv_season'),
				status: 'running',
				progress: { current_item_rel_path: reviewPrefix }
			},
			{
				job_id: 'encode-sibling-episode',
				prefix: folderPrefix,
				media_scope: mediaScope(folderPrefix, 'tv', 'tv_season'),
				status: 'running',
				progress: { current_item_rel_path: `${folderPrefix}/Bluey.2018.S03E50.1080p.mkv` }
			}
		];
		dashboard.encode_queue.running_count = 2;
		dashboard.calibration_queue.sample.pending_review = [
			{
				job_id: 'sample-review-transition',
				prefix: reviewPrefix,
				status: 'pending_review'
			}
		];
		dashboard.calibration_queue.sample.pending_review_count = 1;

		expect(buildOpsQueueRows(dashboard, hostsFixture()).map((row) => row.key)).toEqual([
			'encode:encode-sibling-episode',
			'sample:sample-review-transition'
		]);
		expect(buildOpsStatusTiles(dashboard, hostsFixture(), null)[1]).toMatchObject({
			value: '1 working · 0 waiting'
		});
		expect(buildOpsFooterSignals(dashboard, hostsFixture())[0]).toMatchObject({
			value: '1/0',
			tone: 'active'
		});
	});

	it('uses review-ready as the headline only when queue work is quiet', () => {
		const dashboard = quietDashboardFixture();
		dashboard.calibration_queue.review_ready = [
			{
				job_id: 'sample-review-quiet',
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
		dashboard.encode_queue.recent = [];
		dashboard.encode_queue.needs_attention = [];
		dashboard.encode_queue.needs_attention_count = 0;

		expect(buildOpsReadinessSummary(dashboard, hostsFixture(), null)).toMatchObject({
			tone: 'ready',
			title: 'Bluey · Season 3 · Episode 49 is ready for review',
			detail: 'Compare the sample and decide whether to approve it.',
			metricLabel: 'Needs you',
			metricValue: '1'
		});
	});

	it('does not claim an unavailable sample review needs operator action', () => {
		const dashboard = quietDashboardFixture();
		dashboard.encode_queue.recent = [];
		dashboard.encode_queue.needs_attention = [];
		dashboard.encode_queue.needs_attention_count = 0;
		dashboard.calibration_queue.sample.pending_review = [
			{
				job_id: 'sample-review-unavailable',
				prefix: 'tv/show/ambiguous',
				status: 'pending_review'
			}
		];
		dashboard.calibration_queue.sample.pending_review_count = 1;

		expect(buildOpsReadinessSummary(dashboard, hostsFixture(), null)).toMatchObject({
			tone: 'ready',
			title: 'Ready for work',
			metricLabel: 'Available'
		});
		expect(buildOpsStatusTiles(dashboard, hostsFixture(), null)[2]).toMatchObject({
			detail: '0 ready · 1 unavailable',
			tone: 'wait'
		});
		expect(buildOpsQueueRows(dashboard, hostsFixture())[0]).toMatchObject({
			status: 'Waiting',
			scheduler: 'Review unavailable',
			detail:
				'Comparison clips are unavailable. Mediaforce kept the completed sample visible for diagnosis.'
		});
	});

	it('keeps processing attention ahead of review-ready samples', () => {
		const dashboard = quietDashboardFixture();
		dashboard.calibration_queue.review_ready = [
			{
				job_id: 'sample-review-with-attention',
				prefix: 'tv/show/season 1',
				media_scope: mediaScope('tv/show/season 1', 'tv', 'tv_season')
			}
		];
		expect(buildOpsReadinessSummary(dashboard, hostsFixture(), null)).toMatchObject({
			tone: 'wait',
			title: 'A season needs attention',
			detail: expect.stringContaining('1 sample ready for review')
		});
	});

	it('keeps encode attention dominant in the footer while still counting review-ready samples', () => {
		const dashboard = quietDashboardFixture();
		const reviewReadySample = {
			job_id: 'sample-review-mixed',
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
		};
		dashboard.encode_queue.recent = [];
		dashboard.encode_queue.needs_attention = [
			{
				job_id: 'encode-needs-review',
				prefix: 'tv/show/season 1',
				status: 'needs_attention'
			}
		];
		dashboard.encode_queue.needs_attention_count = 1;
		dashboard.calibration_queue.review_ready = [reviewReadySample];
		dashboard.calibration_queue.review_ready_count = 1;
		dashboard.calibration_queue.sample.pending_review = [reviewReadySample];
		dashboard.calibration_queue.sample.pending_review_count = 1;

		let signals = buildOpsFooterSignals(dashboard, hostsFixture());
		expect(signals).toHaveLength(4);
		expect(signals[1]).toMatchObject({ label: 'Attention', value: '2', tone: 'wait' });

		dashboard.encode_queue.needs_attention = [];
		dashboard.encode_queue.needs_attention_count = 0;
		signals = buildOpsFooterSignals(dashboard, hostsFixture());
		expect(signals[1]).toMatchObject({ label: 'Attention', value: '1', tone: 'ready' });

		dashboard.calibration_queue.sample.pending_review.push({
			job_id: 'sample-review-unavailable',
			prefix: 'tv/show/ambiguous',
			status: 'pending_review'
		});
		dashboard.calibration_queue.sample.pending_review_count = 2;
		signals = buildOpsFooterSignals(dashboard, hostsFixture());
		expect(signals[1]).toMatchObject({ label: 'Attention', value: '2', tone: 'wait' });
		expect(buildOpsStatusTiles(dashboard, hostsFixture(), null)[2]).toMatchObject({
			detail: '1 ready · 1 unavailable',
			tone: 'wait'
		});
		expect(buildOpsQueueRows(dashboard, hostsFixture()).map((row) => row.key)).toEqual(
			expect.arrayContaining(['sample:sample-review-unavailable'])
		);
		expect(buildOpsQueueRows(dashboard, hostsFixture()).map((row) => row.key)).not.toContain(
			'sample:sample-review-mixed'
		);

		dashboard.calibration_queue.review_ready = [];
		dashboard.calibration_queue.review_ready_count = 0;
		dashboard.calibration_queue.sample.pending_review = [];
		dashboard.calibration_queue.sample.pending_review_count = 0;
		signals = buildOpsFooterSignals(dashboard, hostsFixture());
		expect(signals[1]).toMatchObject({ label: 'Attention', value: '0', tone: 'idle' });
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

	it("shows the server's sentence for a failure and never the raw error", () => {
		const dashboard = dashboardFixture();
		const raw =
			'Final output size missed the approved target band: status=over_target, actual=60204063, target=48673111, lower=46239455, upper=51106767.';
		dashboard.encode_queue.recent = [
			{
				job_id: 'encode-size-miss',
				prefix: 'tv/show/season 3',
				status: 'needs_attention',
				error: raw,
				error_summary: 'The finished file came out bigger than the size you approved.'
			}
		];

		expect(buildOpsBlockers(dashboard, hostsFixture(), null)[0].detail).toBe(
			'The finished file came out bigger than the size you approved.'
		);

		delete dashboard.encode_queue.recent[0].error_summary;
		expect(buildOpsBlockers(dashboard, hostsFixture(), null)[0].detail).toBe(
			'Mediaforce could not finish this work.'
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
			'Constellation · Season 1 · Episode 1 is waiting for you to take a look'
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

	it('keeps speed and time remaining ahead of a size-miss note while the show is still running', () => {
		const dashboard = dashboardFixture();
		dashboard.encode_queue.queued = [];
		dashboard.encode_queue.queued_count = 0;
		dashboard.encode_queue.running = [
			{
				job_id: 'show-with-misses',
				prefix: 'tv/Raising Hope',
				job_kind: 'folder',
				item_count: 66,
				status: 'running',
				telemetry_summary: '54% · 2.63x · 63.1 fps · Est. ETA 4h 8m',
				progress: {
					percent_complete: 54,
					failure_analysis: {
						kind: 'final_size_target_miss',
						retry_strategy: 'fresh_goal_required',
						summary: '2 selected items missed the approved final size contract.'
					}
				}
			}
		];
		dashboard.encode_queue.running_count = 1;

		const row = buildOpsQueueRows(dashboard, hostsFixture()).find(
			(candidate) => candidate.key === 'encode:show-with-misses'
		);

		expect(row?.detail).toMatch(/^.*ETA 4h 8m.* · 2 selected items missed/);
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

	it('turns storage prefixes into human work labels', () => {
		expect(opsWorkLabel('tv/Constellation/Season 1')).toBe('Constellation · Season 1');
		expect(opsWorkLabel('movies/Arrival (2016)')).toBe('Arrival (2016)');
	});

	it('describes rows without a folder, computer, or live progress in plain words', () => {
		const dashboard = dashboardFixture();
		const sample = dashboard.calibration_queue.sample.running[0];
		delete sample.host;
		sample.prefix = '';
		const encode = dashboard.encode_queue.running[0];
		encode.prefix = '';
		encode.progress = null;
		encode.shard_count = 4;
		encode.completed_shard_count = 1;

		const rows = buildOpsQueueRows(dashboard);
		const sampleRow = rows.find((row) => row.key === 'sample:sample-1');
		const encodeRow = rows.find((row) => row.key === 'encode:encode-1');

		expect(sampleRow?.prefix).toBe('');
		expect(encodeRow?.prefix).toBe('');
		expect(sampleRow?.host).toBeTruthy();
		expect(encodeRow?.progress).toMatch(/\b1\b.*\b4\b/);
		const shown = [
			opsRowWorkLabel(sampleRow?.prefix ?? ''),
			sampleRow?.host,
			encodeRow?.progress
		].join(' ');
		expect(opsRowWorkLabel('')).toBeTruthy();
		expect(shown).not.toMatch(/\b(scope|unassigned|shards?)\b/i);
		expect(opsRowWorkLabel('tv/Constellation/Season 1')).toBe(
			opsWorkLabel('tv/Constellation/Season 1')
		);
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

	it('keeps work schedule, attention, sample, and computer tones semantic', () => {
		const tiles = buildOpsStatusTiles(dashboardFixture(), hostsFixture(), null);

		expect(tiles).toMatchObject([
			{ label: 'Work schedule', tone: 'ready' },
			{ label: 'Compression queue', tone: 'wait' },
			{ label: 'Sample queue', tone: 'active' },
			{ label: 'Computers', tone: 'ready', value: '1 ready / 2' }
		]);
	});

	it('counts working and off-schedule computers separately from encode-ready capacity', () => {
		const hosts = hostsFixture();
		hosts.hosts[0].active_encode_count = 2;

		const tiles = buildOpsStatusTiles(dashboardFixture(), hosts, null);

		expect(tiles.at(-1)).toMatchObject({
			label: 'Computers',
			tone: 'wait',
			value: '1 working / 2',
			detail: '2 reachable · 1 working · 0 unavailable'
		});
	});

	it('summarizes the first-glance Ops readiness answer', () => {
		const dashboard = quietDashboardFixture();
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
		expect(summary.detail).toContain('1 item compressing across 1 computer');
		expect(summary.detail).toContain('1 sample active');
		expect(summary.detail).toContain('show · season 2 is queued');
		expect(summary.detail).toContain('A season needs attention');
	});

	it('distinguishes available, scheduled-off, and unavailable host states', () => {
		const [ready, scheduledOff] = hostsFixture().hosts;
		const unavailable = { ...ready, available: false, active_encode_count: 0 };

		expect(hostTone(ready)).toBe('active');
		expect(hostStateCopy(ready)).toBe('Working');
		expect(hostTone(scheduledOff)).toBe('wait');
		expect(hostStateCopy(scheduledOff)).toBe('Off schedule');
		expect(hostTone(unavailable)).toBe('fail');
		expect(hostTone(unavailable, true)).toBe('wait');
		expect(hostStateCopy(unavailable)).toBe('Unavailable');
		expect(
			hostStateCopy({ ...ready, capabilities: [], active_encode_count: 0, queue_active: false })
		).toBe('Idle');
	});

	it('says when a computer is held back after repeated failures', () => {
		const [ready] = hostsFixture().hosts;
		const cooling = {
			...ready,
			active_encode_count: 0,
			cooldown_until: '2999-01-01T12:00:00+00:00'
		};
		const hosts: HostsPayload = { ...hostsFixture(), hosts: [cooling] };

		expect(hostStateCopy(cooling)).toBe('Cooling down');
		expect(hostTone(cooling)).toBe('wait');
		expect(hostWorkReason(cooling, hosts, dashboardFixture())).toMatch(
			/^Held back after repeated failures\. Mediaforce tries it again .+, or prepare it to try now\.$/
		);
		// Work already running on it, or a cooldown that has run out, reads as before.
		expect(hostStateCopy({ ...cooling, active_encode_count: 1 })).toBe('Working');
		expect(hostStateCopy({ ...cooling, cooldown_until: '2000-01-01T00:00:00+00:00' })).not.toBe(
			'Cooling down'
		);
	});

	it('says how many items the queue estimate leaves out', () => {
		const dashboard = dashboardFixture();
		dashboard.encode_queue.telemetry = {
			eta_copy: '5h 2m',
			left_out_needs_attention_item_count: 2,
			left_out_failed_item_count: 1
		};

		const tiles = buildOpsStatusTiles(dashboard, hostsFixture(), null);
		const summary = buildOpsReadinessSummary(dashboard, hostsFixture(), null);

		expect(tiles.find((tile) => tile.label === 'Compression queue')?.detail).toBe(
			"5h 2m · Leaves out 2 items waiting for you and 1 that didn't finish"
		);
		expect(summary.detail).toContain(
			"Estimated finish in 5h 2m (leaves out 2 items waiting for you and 1 that didn't finish)"
		);
	});

	it('treats storage-recoverable computers as reconnecting capacity', () => {
		const dashboard = dashboardFixture();
		dashboard.encode_queue.needs_attention_count = 0;
		dashboard.encode_queue.recent = [];
		const recoverable = {
			...hostsFixture().hosts[0],
			available: false,
			queue_active: false,
			active_encode_count: 0,
			storage_recovery_available: true,
			message: 'Storage is reconnecting in the background'
		};
		const hosts = { compact: true, hosts: [recoverable] };

		expect(buildOpsBlockers(dashboard, hosts, null).map((blocker) => blocker.key)).not.toContain(
			'no-hosts-ready'
		);
		expect(buildOpsStatusTiles(dashboard, hosts, null).at(-1)).toMatchObject({
			label: 'Computers',
			value: '1 ready / 1'
		});
		expect(hostTone(recoverable)).toBe('wait');
		expect(hostStateCopy(recoverable)).toBe('Reconnecting storage');
	});

	it('shows background storage recovery state on the computer card', () => {
		const now = new Date('2026-10-19T14:30:00Z');
		const retrying = {
			...hostsFixture().hosts[0],
			available: false,
			queue_active: false,
			active_encode_count: 0,
			storage_recovery_available: true,
			message: 'Storage is reconnecting in the background',
			recovery: {
				status: 'retrying' as const,
				reason: 'Missing required paths',
				first_failure_at: '2026-10-19T13:58:00Z',
				last_check_at: '2026-10-19T14:28:00Z',
				next_retry_at: '2026-10-19T14:42:00Z',
				failure_count: 4
			}
		};
		const hosts = { compact: true, hosts: [retrying] };
		const at = (iso: string) =>
			new Intl.DateTimeFormat('en-US', {
				hour: 'numeric',
				minute: '2-digit',
				timeZoneName: 'short'
			}).format(new Date(iso));

		expect(hostStateCopy(retrying)).toBe('Reconnecting storage');
		expect(hostWorkReason(retrying, hosts, null, now)).toBe(
			`Retrying, next check ${at('2026-10-19T14:42:00Z')}. First failed at ${at('2026-10-19T13:58:00Z')}; 4 tries so far.`
		);
		expect(
			hostWorkReason(
				{ ...retrying, recovery: { ...retrying.recovery, next_retry_at: '2026-10-19T14:29:00Z' } },
				hosts,
				null,
				now
			)
		).toMatch(/^Retrying now\./);

		const needsSomeone = {
			...retrying,
			message: 'Worker needs its share password saved.',
			recovery: {
				...retrying.recovery,
				status: 'action_required' as const,
				reason: 'Worker needs its share password saved.'
			}
		};
		expect(hostTone(needsSomeone)).toBe('wait');
		expect(hostStateCopy(needsSomeone)).toBe('Needs attention');
		expect(hostWorkReason(needsSomeone, hosts, null, now)).toBe(
			`Worker needs its share password saved. Last checked ${at('2026-10-19T14:28:00Z')}.`
		);
	});

	it("shows a computer's own plain status on its card without rewriting it", () => {
		const now = new Date('2026-10-19T14:30:00Z');
		const asleep = {
			...hostsFixture().hosts[0],
			label: 'M2 MBP',
			available: false,
			queue_active: false,
			active_encode_count: 0,
			storage_recovery_available: false,
			message: "Can't reach M2 MBP. Is it on and awake?",
			recovery: {
				status: 'retrying' as const,
				reason: "Can't reach M2 MBP. Is it on and awake?",
				first_failure_at: '2026-10-19T13:58:00Z',
				next_retry_at: '2026-10-19T14:42:00Z',
				failure_count: 2
			}
		};
		const hosts = { compact: true, hosts: [asleep] };

		expect(hostStateCopy(asleep)).toBe('Unavailable');
		const reason = hostWorkReason(asleep, hosts, null, now);
		expect(reason.startsWith(`${asleep.message} Retrying, next check`)).toBe(true);
		expect(reason).not.toMatch(/\b(ssh|host|worker)\b/i);
	});

	it('offers Retry connection only for a computer that background reconnects are working on', () => {
		const reachable = hostsFixture().hosts[0];
		const recovery = { status: 'retrying' as const, failure_count: 2 };
		expect(hostConnectionRetryAvailable({ ...reachable, available: false, recovery })).toBe(true);
		expect(
			hostConnectionRetryAvailable({
				...reachable,
				available: false,
				recovery: { ...recovery, status: 'action_required' }
			})
		).toBe(true);
		expect(hostConnectionRetryAvailable({ ...reachable, available: true, recovery })).toBe(false);
		expect(
			hostConnectionRetryAvailable({
				...reachable,
				available: false,
				recovery: { ...recovery, status: 'ready' }
			})
		).toBe(false);
		// This computer, or one not checked yet, has no background reconnect to run now.
		expect(hostConnectionRetryAvailable({ ...reachable, available: false, recovery: null })).toBe(
			false
		);
	});

	it('shows running encode telemetry instead of stale restart errors', () => {
		const dashboard = dashboardFixture();
		dashboard.encode_queue.running[0] = {
			...dashboard.encode_queue.running[0],
			error: 'Encode queue job was interrupted by a web process restart.',
			telemetry_summary: '1% · 0.36x · 8.7 fps · Est. ETA 11h 4m'
		};

		expect(buildOpsQueueRows(dashboard)[0]).toMatchObject({
			detail: 'ETA 11h 4m',
			technicalDetail: '1% · 0.36x · 8.7 fps · Est. ETA 11h 4m'
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
			phase: '2 active compression tasks'
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
			active_reason: 'already compressing as much as it can'
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

	it('maps computer capabilities to user-facing labels', () => {
		expect(workerCapabilitiesSummary(['encode_queue', 'sample_calibration', 'proof_encode'])).toBe(
			'Can compress media · Can create samples · Can build comparison clips'
		);
		expect(workerCapabilitiesSummary([])).toBe('No work assigned to this computer');
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
		expect(hostPrepareTitle(passwordHost)).toBe('Enter the setup password for this computer.');
		expect(hostPrepareDisabled(unsupportedHost, 'secret')).toBe(true);
		expect(hostPrepareTitle(unsupportedHost)).toBe('Setup is unavailable for this computer.');
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
