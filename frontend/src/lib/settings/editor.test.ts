import { describe, expect, it } from 'vitest';

import {
	DEFAULT_SCHEDULE_DAYS,
	addScheduleDraft,
	archiveCleanupTargetDirty,
	buildArchiveCleanupClearPayload,
	buildSettingsSavePayload,
	cloneScheduleProfile,
	draftFromSettings,
	scheduleDaysSummaryCopy,
	scheduleWindowSummaryCopy,
	settingsDraftIsDirty,
	toggleHostAllowedLibrary,
	toggleScheduleProfileDay
} from './editor';
import type { SettingsPayload } from '$lib/api/types';

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

describe('settings draft helpers', () => {
	const payload: SettingsPayload = {
		error: null,
		saved: false,
		libraries: [{ index: '0', key: 'tv', path: '/Volumes/TV', color: '#4e6fa6' }],
		remote_hosts: [
			{
				index: '0',
				label: 'Studio Mac',
				host: 'studio.local',
				repo_path: '/opt/mediaforce',
				wake_mac: '',
				start_command: '',
				stop_command: '',
				start_timeout_seconds: '180',
				media_access: 'mounted',
				priority: '0',
				max_parallel_encodes: '1',
				schedule_profile: 'always',
				capabilities: ['encode_queue'],
				allowed_libraries: ['tv'],
				source_roots_json: '',
				staging_root: ''
			}
		],
		transcode_root: '/Volumes/Transcode',
		encode_queue_scheduler: {
			mode: 'night',
			start_hour: 22,
			end_hour: 8,
			timezone: 'host_local',
			summary: 'runs between 22:00 and 08:00'
		},
		schedule_profiles: [],
		schedule_profile_options: [{ key: 'always', label: 'Always' }],
		host_capability_options: [
			{ key: 'encode_queue', label: 'Queue encodes', help: 'Allow queue work.' }
		],
		archive_root: '/Volumes/Transcode/_replaced',
		archive_cleanup: null,
		runtime_settings_path: '/Users/operator/Library/Application Support/mediaforce/settings.json',
		repo_config_path: '/Users/operator/mediaforce/config.yaml',
		host_notice: null,
		host_notice_kind: null
	};

	it('keeps save payload arrays isolated from the live draft', () => {
		const draft = draftFromSettings(payload);
		const savePayload = buildSettingsSavePayload(draft, payload);

		draft.remote_hosts[0]?.capabilities.push('sample_calibration');
		draft.schedule_profiles.push({
			index: '0',
			key: 'overnight',
			label: 'Overnight',
			days_of_week: ['mon'],
			all_day_days_of_week: [],
			start_hour: '20',
			end_hour: '6'
		});

		expect(savePayload.remote_hosts[0]?.capabilities).toEqual(['encode_queue']);
		expect(savePayload.schedule_profiles).toEqual([]);
	});

	it('detects meaningful draft changes and ignores unchanged clones', () => {
		const draft = draftFromSettings(payload);
		expect(settingsDraftIsDirty(draft, payload)).toBe(false);

		draft.libraries[0] = { ...draft.libraries[0], path: '/Volumes/TV2' };
		expect(settingsDraftIsDirty(draft, payload)).toBe(true);
	});

	it('uses the saved transcode root for destructive archive cleanup', () => {
		const draft = draftFromSettings(payload);
		draft.transcode_root = '/Volumes/UnsavedTranscode';

		expect(buildArchiveCleanupClearPayload(payload)).toEqual({
			transcode_root: '/Volumes/Transcode'
		});
	});

	it('flags archive cleanup as dirty when the cleanup target is edited but unsaved', () => {
		const draft = draftFromSettings(payload);

		expect(archiveCleanupTargetDirty(draft, payload)).toBe(false);

		draft.transcode_root = ' /Volumes/Transcode ';
		expect(archiveCleanupTargetDirty(draft, payload)).toBe(false);

		draft.transcode_root = '/Volumes/UnsavedTranscode';
		expect(archiveCleanupTargetDirty(draft, payload)).toBe(true);
	});

	it('toggles host library assignment without duplicating selected keys', () => {
		const hosts = toggleHostAllowedLibrary(payload.remote_hosts, 0, 'movies');
		expect(hosts[0]?.allowed_libraries).toEqual(['tv', 'movies']);
		expect(toggleHostAllowedLibrary(hosts, 0, 'movies')[0]?.allowed_libraries).toEqual(['tv']);
		expect(toggleHostAllowedLibrary(hosts, 0, '')).toBe(hosts);
	});
});
