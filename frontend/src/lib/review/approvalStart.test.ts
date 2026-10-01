import { describe, expect, it } from 'vitest';

import { approvalStartOutcome } from './approvalStart';

const queued = (count: number) => `queued ${count}`;

describe('approval that also starts the encode', () => {
	it('reports what the server actually queued, not the page estimate', () =>
		expect(approvalStartOutcome({ ok: true, queued_count: 2 }, queued, 3)).toEqual({
			message: queued(2)
		}));

	it('falls back to the page estimate when the server gives no count', () =>
		expect(approvalStartOutcome({ ok: true }, queued, 3)).toEqual({ message: queued(3) }));

	it('repeats the server explanation when files were left out', () => {
		const start = {
			ok: true,
			queued_count: 2,
			left_out_count: 1,
			message: 'Queued 2. Left 1 out.'
		};
		expect(approvalStartOutcome(start, queued, 3)).toEqual({ message: start.message });
	});

	it('keeps the approval but flags attention when the encode did not start', () => {
		const outcome = approvalStartOutcome(
			{ ok: false, message: 'No computer is ready.' },
			queued,
			1
		);
		expect(outcome.attention).toBe(true);
		expect(outcome.message).toContain('No computer is ready.');
	});

	it('treats a missing start result as not started rather than started', () => {
		expect(approvalStartOutcome(undefined, queued, 1).attention).toBe(true);
		expect(approvalStartOutcome(null, queued, 1).attention).toBe(true);
	});

	it('does not claim a second encode when one was already waiting', () => {
		const outcome = approvalStartOutcome({ ok: true, already_active: true }, queued, 1);
		expect(outcome.attention).toBeUndefined();
		expect(outcome.message).not.toBe(queued(1));
	});
});
