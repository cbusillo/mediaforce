import { describe, expect, it } from 'vitest';

import { operatorErrorView, operatorStateCopy, safeOperatorErrorCopy } from './operator-copy';

describe('operator copy boundary', () => {
	it('maps known backend states without exposing underscored values', () => {
		expect(operatorStateCopy('pending_review')).toBe('Ready to review');
		expect(operatorStateCopy('ready_to_validate')).toBe('Ready to check');
		expect(operatorStateCopy('unmapped_backend_state')).toBe('Unknown state');
	});

	it('supports scope-specific labels without weakening the unknown-state fallback', () => {
		expect(operatorStateCopy('running', { running: 'Creating sample' })).toBe('Creating sample');
		expect(operatorStateCopy('new_internal_state', {}, 'Not started')).toBe('Not started');
	});

	it('keeps raw backend errors out of primary copy while preserving technical detail', () => {
		const view = operatorErrorView(
			'RuntimeError: encoder returned status 17',
			'Mediaforce could not finish this work.'
		);

		expect(view.summary).toBe('Mediaforce could not finish this work.');
		expect(view.technicalDetail).toBe('RuntimeError: encoder returned status 17');
		expect(
			safeOperatorErrorCopy(
				'RuntimeError: encoder returned status 17',
				'Mediaforce could not finish this work.'
			)
		).toBe('Mediaforce could not finish this work.');
	});

	it('preserves already-written operator copy and telemetry', () => {
		expect(safeOperatorErrorCopy('Source offline', 'Fallback')).toBe('Source offline');
		expect(safeOperatorErrorCopy('0.36x · 8.7 fps · ETA 11h 4m', 'Fallback')).toBe(
			'0.36x · 8.7 fps · ETA 11h 4m'
		);
		expect(safeOperatorErrorCopy('transient worker fault', 'Fallback')).toBe('Fallback');
		expect(safeOperatorErrorCopy(new Error('HTTP 500 from /api/folders'), 'Fallback')).toBe(
			'Fallback'
		);
	});
});
