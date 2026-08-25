import { describe, expect, it } from 'vitest';

import type { EncodeQueueJob, EncodeQueueSummary, HostRuntime } from '$lib/api/types';
import {
	hostSchedulePresentation,
	jobSchedulePresentation,
	workScheduleSummaryCopy
} from './schedule';

const NOW = new Date('2026-07-23T16:00:00Z');

function host(overrides: Partial<HostRuntime> = {}): HostRuntime {
	return {
		key: 'worker-1',
		label: 'Worker 1',
		available: true,
		message: 'ready',
		missing_paths: [],
		issues: [],
		detail: null,
		capabilities: ['encode_queue'],
		priority: 10,
		max_parallel_encodes: 1,
		active_encode_count: 0,
		schedule_profile_label: 'Evening',
		schedule_detail: 'runs weekdays between 18:00 and 15:00 (host local)',
		schedule_open: true,
		schedule_timezone: 'America/New_York',
		schedule_closes_at: '2026-07-23T19:00:00Z',
		schedule_next_opens_at: '2026-07-23T22:00:00Z',
		active_flag: 'ready',
		active_reason: '',
		queue_active: true,
		...overrides
	};
}

function job(overrides: Partial<EncodeQueueJob> = {}): EncodeQueueJob {
	return {
		job_id: 'job-1',
		prefix: 'tv/Example Show/Season 1',
		status: 'queued',
		...overrides
	};
}

function queue(overrides: Partial<EncodeQueueSummary> = {}): EncodeQueueSummary {
	return {
		running_count: 0,
		queued_count: 0,
		running: [],
		queued: [],
		state: { is_paused: false, stop_requested: false },
		...overrides
	};
}

describe('schedule presentation', () => {
	it('adapts every live scheduler summary without changing unknown text', () => {
		expect(workScheduleSummaryCopy('runs anytime')).toBe('Work runs anytime');
		expect(workScheduleSummaryCopy('never runs')).toBe('Work schedule is off');
		expect(workScheduleSummaryCopy('runs weekdays between 20:00 and 06:00 (host local)')).toBe(
			'Work runs weekdays between 20:00 and 06:00 (computer local time)'
		);
		expect(
			workScheduleSummaryCopy('runs weekdays between 20:00 and 06:00 (controller local)')
		).toBe('Work runs weekdays between 20:00 and 06:00 (Mediaforce local time)');
		expect(workScheduleSummaryCopy('runs weekdays between 20:00 and 06:00 (utc)')).toBe(
			'Work runs weekdays between 20:00 and 06:00 (UTC)'
		);
		expect(workScheduleSummaryCopy('custom scheduler detail')).toBe('custom scheduler detail');
		expect(workScheduleSummaryCopy('')).toBe('');
	});

	it('shows an exact hard stop for active work', () => {
		const presentation = jobSchedulePresentation(
			job({
				status: 'running',
				host: { key: 'worker-1', label: 'Worker 1' },
				schedule_state: 'active_hard_stop',
				schedule_close_deadline_at: '2026-07-23T19:00:00Z'
			}),
			[host({ active_encode_count: 1 })],
			NOW
		);

		expect(presentation).toMatchObject({
			label: 'Stops at close',
			tone: 'active',
			transitionAt: '2026-07-23T19:00:00Z'
		});
		expect(presentation.detail).toContain('today at 3:00 PM EDT');
		expect(presentation.detail).toContain('3h');
	});

	it('explains automatic whole-item restart after a schedule interruption', () => {
		const presentation = jobSchedulePresentation(
			job({
				schedule_state: 'schedule_interrupted',
				progress: { progress_state: 'schedule_waiting' }
			}),
			[host({ schedule_open: false })],
			NOW
		);

		expect(presentation.label).toBe('Paused by schedule');
		expect(presentation.detail).toContain('restart from the beginning automatically');
		expect(presentation.detail).toContain('today at 6:00 PM EDT');
		expect(presentation.detail).toContain('No failure attempt was used');
	});

	it('makes impossible schedule fit actionable', () => {
		const presentation = jobSchedulePresentation(
			job({
				schedule_state: 'draining_impossible',
				waiting_reason:
					'Estimated runtime about 4h is longer than every configured host schedule window. Widen a host window or use Bypass scheduler.'
			}),
			[host()],
			NOW
		);

		expect(presentation).toMatchObject({ label: 'Window too short', tone: 'fail' });
		expect(presentation.detail).toContain('Widen a host window or use Bypass scheduler');
	});

	it('distinguishes off-schedule and draining workers', () => {
		const offSchedule = hostSchedulePresentation(host({ schedule_open: false }), queue(), NOW);
		const drainingJob = job({ schedule_state: 'draining_no_fit' });
		const draining = hostSchedulePresentation(
			host(),
			queue({ queued_count: 1, queued: [drainingJob] }),
			NOW
		);

		expect(offSchedule).toMatchObject({ label: 'Off schedule', tone: 'wait' });
		expect(offSchedule?.detail).toContain('today at 6:00 PM EDT');
		expect(draining).toMatchObject({ label: 'Draining', tone: 'wait' });
		expect(draining?.detail).toContain('no queued item safely fits');
	});

	it('makes bypassed work unmistakable', () => {
		const bypassJob = job({
			status: 'running',
			bypass_schedule: true,
			schedule_state: 'bypassed',
			host: { key: 'worker-1' }
		});
		const presentation = jobSchedulePresentation(bypassJob, [host()], NOW);
		const workerPresentation = hostSchedulePresentation(
			host({ active_encode_count: 1, schedule_profile_label: 'Never', schedule_open: false }),
			queue({ running_count: 1, running: [bypassJob] }),
			NOW
		);

		expect(presentation).toMatchObject({ label: 'Bypassing schedule', tone: 'wait' });
		expect(workerPresentation).toMatchObject({ label: 'Bypassing schedule', tone: 'wait' });
	});
});
