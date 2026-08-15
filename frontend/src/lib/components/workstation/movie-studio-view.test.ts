import { describe, expect, it } from 'vitest';

import {
	canRetrySampleJob,
	movieCurrentWorkView,
	movieGoalFactsView,
	movieReviewStatusLabel,
	parentSampleAppliesToExactItem
} from './movie-studio-view';

describe('canRetrySampleJob', () => {
	it('allows retry when the failed sample has no replacement proposal', () =>
		expect(canRetrySampleJob('sample-job-1', false)).toBe(true));

	it('suppresses retry after a replacement proposal is prepared', () =>
		expect(canRetrySampleJob('sample-job-1', true)).toBe(false));

	it('requires a retryable sample job identifier', () => {
		expect(canRetrySampleJob('', false)).toBe(false);
		expect(canRetrySampleJob(null, false)).toBe(false);
	});
});

describe('parentSampleAppliesToExactItem', () => {
	it('recognizes a parent sample prepared from the exact file', () =>
		expect(
			parentSampleAppliesToExactItem('movies/title/movie.mkv', null, {
				job_id: 'sample-job-1',
				prefix: 'movies/title',
				status: 'completed',
				sample_item: { rel_path: 'movies/title/movie.mkv' }
			})
		).toBe(true));

	it('does not replace an exact-file sample job', () =>
		expect(
			parentSampleAppliesToExactItem(
				'movies/title/movie.mkv',
				{ job_id: 'exact-job', status: 'completed' },
				{
					job_id: 'title-job',
					prefix: 'movies/title',
					status: 'completed',
					sample_item: { rel_path: 'movies/title/movie.mkv' }
				}
			)
		).toBe(false));

	it('rejects a parent sample prepared from another file', () =>
		expect(
			parentSampleAppliesToExactItem('movies/title/extra.mkv', null, {
				job_id: 'title-job',
				prefix: 'movies/title',
				status: 'completed',
				sample_item: { rel_path: 'movies/title/movie.mkv' }
			})
		).toBe(false));
});

describe('movieReviewStatusLabel', () => {
	it('uses operator-facing copy for approval-ready and missing samples', () => {
		expect(movieReviewStatusLabel('needs_approval')).toBe('Ready to review');
		expect(movieReviewStatusLabel('missing_sample')).toBe('Not prepared');
	});

	it('directs inherited samples back to the title workspace', () =>
		expect(movieReviewStatusLabel('missing_sample', true)).toBe('Review at title level'));
});

describe('movieCurrentWorkView', () => {
	it('explains a paused queue with no available worker', () => {
		const view = movieCurrentWorkView(
			{
				job_id: 'encode-1',
				prefix: 'movies/title',
				status: 'queued',
				queue_position: 1,
				queue_depth: 1
			},
			{ is_paused: true, stop_requested: false },
			0
		);

		expect(view).toMatchObject({
			state: 'Nothing is processing yet',
			headline: 'Queued, but not able to start',
			queuePosition: '1 of 1',
			worker: 'Not assigned',
			availableWorkers: 'None ready',
			blockers: ['The processing queue is paused.', 'No processing worker is ready.']
		});
		expect(view?.nextCondition).toBe(
			'This movie starts automatically after the global processing queue is resumed and a processing worker becomes available.'
		);
	});

	it('shows a queued movie with no known blockers', () => {
		const view = movieCurrentWorkView(
			{
				job_id: 'encode-1',
				prefix: 'movies/title',
				status: 'queued',
				queue_position: 2,
				queue_depth: 4,
				scheduler_status_copy: 'Ready when a worker is free.'
			},
			{ is_paused: false, stop_requested: false },
			1
		);

		expect(view).toMatchObject({
			headline: 'Queued 2 of 4',
			detail: 'Ready when a worker is free.',
			blockers: [],
			queuePosition: '2 of 4'
		});
	});

	it('shows running progress, worker, speed, elapsed time, and ETA', () => {
		const view = movieCurrentWorkView(
			{
				job_id: 'encode-1',
				prefix: 'movies/title',
				status: 'running',
				started_at: '2026-08-15T12:00:00Z',
				progress: {
					percent_complete: 42.4,
					phase_label: 'Encoding',
					current_item_rel_path: 'movies/title/movie.mkv',
					active_host_labels: ['Studio Mac'],
					speed: 1.25,
					eta_copy: '24m'
				}
			},
			{ is_paused: false, stop_requested: false },
			1,
			Date.parse('2026-08-15T12:05:30Z')
		);

		expect(view).toMatchObject({
			state: 'Processing',
			headline: 'Processing movie.mkv',
			detail: 'Encoding',
			percentComplete: 42.4,
			worker: 'Studio Mac',
			availableWorkers: '1 ready',
			elapsed: '5m 30s',
			speed: '1.25× realtime',
			eta: '24m'
		});
	});

	it('normalizes malformed running telemetry', () => {
		const view = movieCurrentWorkView(
			{
				job_id: 'encode-1',
				prefix: 'movies/title',
				status: 'running',
				progress: {
					percent_complete: Number.NaN,
					current_item_rel_path: '   '
				}
			},
			{ is_paused: false, stop_requested: false },
			1
		);

		expect(view).toMatchObject({
			percentComplete: 0,
			worker: 'Not assigned',
			currentItem: null
		});
	});

	it('ignores non-active encode jobs', () =>
		expect(
			movieCurrentWorkView(
				{ job_id: 'encode-1', prefix: 'movies/title', status: 'completed' },
				{ is_paused: false, stop_requested: false },
				1
			)
		).toBeNull());
});

describe('movieGoalFactsView', () => {
	it('formats duration, expected output, savings, and final target range', () =>
		expect(
			movieGoalFactsView(6454.857, 2_824_183_089, {
				schema_version: 1,
				mode: 'absolute',
				source: 'profile',
				status: 'resolved',
				requires_confirmation: false,
				target_size_bytes: 717_206_333,
				sample_projection_tolerance_percent: 8,
				final_output_tolerance_percent: 5,
				final_lower_bound_bytes: 681_346_016,
				final_upper_bound_bytes: 753_066_650,
				rationale: 'Runtime-adjusted movie target.'
			})
		).toEqual({
			duration: '1h 47m 35s',
			sourceSize: '2.82 GB',
			expectedOutput: '717 MB',
			expectedSavings: '2.11 GB · 75%',
			targetRange: '681 MB–753 MB',
			estimateQuality: 'Planning range, not a guarantee'
		}));

	it('does not claim savings when the target is not smaller', () =>
		expect(
			movieGoalFactsView(3600, 1_000_000_000, {
				schema_version: 1,
				mode: 'absolute',
				source: 'profile',
				status: 'resolved',
				requires_confirmation: false,
				target_size_bytes: 1_000_000_000,
				sample_projection_tolerance_percent: 8,
				final_output_tolerance_percent: 5,
				rationale: 'No reduction.'
			}).expectedSavings
		).toBe('No size reduction planned'));
});
