import { describe, expect, it } from 'vitest';

import { formatCounts, formatGiB, formatTimestamp, titleCase } from './format';

describe('format helpers', () => {
	it('formats GiB values with the requested precision', () => {
		expect(formatGiB(0)).toBe('0 GiB');
		expect(formatGiB(1024 ** 3)).toBe('1.0 GiB');
		expect(formatGiB(1536 * 1024 ** 2, 2)).toBe('1.50 GiB');
	});

	it('formats mapping counts into a readable summary', () => {
		expect(formatCounts(undefined)).toBe('None');
		expect(formatCounts({})).toBe('None');
		expect(formatCounts({ hevc: 2, av1: 1 })).toBe('2 hevc · 1 av1');
	});

	it('converts underscore-separated strings to title case', () => {
		expect(titleCase('sample_calibration')).toBe('Sample Calibration');
		expect(titleCase('already  spaced')).toBe('Already Spaced');
	});

	it('formats timestamps as minutes and seconds', () => {
		expect(formatTimestamp(0)).toBe('0m 0s');
		expect(formatTimestamp(125)).toBe('2m 5s');
		expect(formatTimestamp(359.6)).toBe('6m 0s');
	});
});
