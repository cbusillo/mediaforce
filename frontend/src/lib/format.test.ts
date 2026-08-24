import { describe, expect, it } from 'vitest';

import {
	formatCounts,
	formatFileSize,
	normalizeFileSizeCopy,
	formatScheduleCountdown,
	formatScheduleMoment,
	formatTimestamp,
	titleCase
} from './format';

describe('format helpers', () => {
	it('formats operator-facing file sizes with decimal units and consistent rounding', () => {
		expect(formatFileSize(null)).toBe('—');
		expect(formatFileSize(0, 'No estimate')).toBe('No estimate');
		expect(formatFileSize(337_200_000)).toBe('337 MB');
		expect(formatFileSize(4_690_000_000)).toBe('4.69 GB');
		expect(formatFileSize(22_880_013_000)).toBe('22.9 GB');
	});

	it('converts binary-unit backend copy instead of relabelling it', () => {
		expect(normalizeFileSizeCopy('Saved 4.1 GiB and 766 MiB')).toBe('Saved 4.4 GB and 803 MB');
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

	it('formats exact schedule transitions in worker-local time', () => {
		const now = new Date('2026-07-23T16:00:00Z');

		expect(formatScheduleMoment('2026-07-23T19:00:00Z', 'America/New_York', now)).toBe(
			'today at 3:00 PM EDT'
		);
		expect(formatScheduleMoment('2026-07-24T22:00:00Z', 'America/New_York', now)).toBe(
			'tomorrow at 6:00 PM EDT'
		);
	});

	it('formats bounded schedule countdowns and drops elapsed deadlines', () => {
		const now = new Date('2026-07-23T16:00:00Z');

		expect(formatScheduleCountdown('2026-07-23T17:30:00Z', now)).toBe('1h 30m');
		expect(formatScheduleCountdown('2026-07-25T18:00:00Z', now)).toBe('2d 2h');
		expect(formatScheduleCountdown('2026-07-23T15:59:00Z', now)).toBeNull();
	});
});
