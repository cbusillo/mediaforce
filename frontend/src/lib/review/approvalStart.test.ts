import { describe, expect, it } from 'vitest';

import { approvalStartOutcome } from './approvalStart';

describe('approval that also starts the encode', () => {
	it('reports the started work when the encode was queued', () => {
		expect(approvalStartOutcome({ ok: true }, 'started')).toEqual({ message: 'started' });
	});

	it('keeps the approval but flags attention when the encode did not start', () => {
		const outcome = approvalStartOutcome(
			{ ok: false, message: 'No computer is ready.' },
			'started'
		);
		expect(outcome.attention).toBe(true);
		expect(outcome.message).toContain('No computer is ready.');
		expect(outcome.message).not.toBe('started');
	});

	it('treats a missing start result as not started rather than started', () => {
		expect(approvalStartOutcome(undefined, 'started').attention).toBe(true);
		expect(approvalStartOutcome(null, 'started').attention).toBe(true);
	});

	it('does not claim a second encode when one was already waiting', () => {
		const outcome = approvalStartOutcome({ ok: true, already_active: true }, 'started');
		expect(outcome.attention).toBeUndefined();
		expect(outcome.message).not.toBe('started');
	});
});
