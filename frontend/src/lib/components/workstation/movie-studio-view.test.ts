import { describe, expect, it } from 'vitest';

import { canRetrySampleJob } from './movie-studio-view';

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
