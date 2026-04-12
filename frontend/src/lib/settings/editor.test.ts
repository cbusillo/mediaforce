import { describe, expect, it } from 'vitest';

import {
	DEFAULT_SCHEDULE_DAYS,
	addScheduleDraft,
	cloneScheduleProfile,
	scheduleDaysSummaryCopy,
	scheduleWindowSummaryCopy,
	toggleScheduleProfileDay
} from './editor';

describe('schedule profile helpers', () => {
	it('defaults new schedules to every day', () => {
		expect(addScheduleDraft([])[0]?.days_of_week).toEqual(DEFAULT_SCHEDULE_DAYS);
	});

	it('clones weekday arrays so draft state stays isolated', () => {
		const original = {
			index: '0',
			key: 'sunday_only',
			label: 'Sunday only',
			days_of_week: ['sun'],
			all_day_days_of_week: [],
			start_hour: '0',
			end_hour: '0'
		};

		const cloned = cloneScheduleProfile(original);
		cloned.days_of_week.push('mon');

		expect(original.days_of_week).toEqual(['sun']);
		expect(cloned.days_of_week).toEqual(['sun', 'mon']);
	});

	it('preserves all-day-only schedules when cloning drafts', () => {
		const cloned = cloneScheduleProfile({
			index: '0',
			key: 'sunday_all_day',
			label: 'Sunday all day',
			days_of_week: [],
			all_day_days_of_week: ['sun'],
			start_hour: '20',
			end_hour: '6'
		});

		expect(cloned.days_of_week).toEqual([]);
		expect(cloned.all_day_days_of_week).toEqual(['sun']);
	});

	it('summarizes common weekday groupings for the schedule card', () => {
		expect(scheduleDaysSummaryCopy(DEFAULT_SCHEDULE_DAYS)).toBe('Every day');
		expect(scheduleDaysSummaryCopy(['mon', 'tue', 'wed', 'thu', 'fri'])).toBe('Weekdays');
		expect(scheduleDaysSummaryCopy(['sun'])).toBe('Sun');
		expect(
			scheduleWindowSummaryCopy({
				index: '0',
				key: 'sunday_only',
				label: 'Sunday only',
				days_of_week: ['sun'],
				all_day_days_of_week: [],
				start_hour: '0',
				end_hour: '0'
			})
		).toBe('Sun · 00:00 all day');
		expect(
			scheduleWindowSummaryCopy({
				index: '1',
				key: 'after_hours_plus_sunday',
				label: 'After hours plus Sunday',
				days_of_week: ['mon', 'tue', 'wed', 'thu', 'fri'],
				all_day_days_of_week: ['sun'],
				start_hour: '20',
				end_hour: '6'
			})
		).toBe('Sun all day + Weekdays · 20:00 - 06:00');
	});

	it('keeps weekday order stable when toggling day pills', () => {
		const schedule = {
			index: '0',
			key: 'weekend_window',
			label: 'Weekend window',
			days_of_week: ['sun'],
			all_day_days_of_week: [],
			start_hour: '20',
			end_hour: '6'
		};

		expect(toggleScheduleProfileDay(schedule, 'sat').days_of_week).toEqual(['sat', 'sun']);
		expect(toggleScheduleProfileDay(schedule, 'sun').days_of_week).toEqual([]);
		expect(toggleScheduleProfileDay(schedule, 'sun', 'all_day_days_of_week')).toEqual({
			...schedule,
			days_of_week: [],
			all_day_days_of_week: ['sun']
		});
	});
});
