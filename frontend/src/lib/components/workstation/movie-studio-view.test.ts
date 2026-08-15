import { describe, expect, it } from 'vitest';

import {
	canRetrySampleJob,
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
	it('recognizes a parent sample prepared from the exact file', () => {
		expect(
			parentSampleAppliesToExactItem('movies/title/movie.mkv', null, {
				job_id: 'sample-job-1',
				prefix: 'movies/title',
				status: 'completed',
				sample_item: { rel_path: 'movies/title/movie.mkv' }
			})
		).toBe(true);
	});

	it('does not replace an exact-file sample job', () => {
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
		).toBe(false);
	});

	it('rejects a parent sample prepared from another file', () => {
		expect(
			parentSampleAppliesToExactItem('movies/title/extra.mkv', null, {
				job_id: 'title-job',
				prefix: 'movies/title',
				status: 'completed',
				sample_item: { rel_path: 'movies/title/movie.mkv' }
			})
		).toBe(false);
	});
});

describe('movieReviewStatusLabel', () => {
	it('uses operator-facing copy for approval-ready and missing samples', () => {
		expect(movieReviewStatusLabel('needs_approval')).toBe('Ready to review');
		expect(movieReviewStatusLabel('missing_sample')).toBe('Not prepared');
	});

	it('directs inherited samples back to the title workspace', () =>
		expect(movieReviewStatusLabel('missing_sample', true)).toBe('Review at title level'));
});
