import { describe, expect, it } from 'vitest';

import {
	DEFAULT_SCHEDULE_DAYS,
	applyLibraryTypeChange,
	addLibraryDraft,
	addScheduleDraft,
	archiveCleanupTargetDirty,
	buildArchiveCleanupClearPayload,
	buildSettingsSavePayload,
	cloneScheduleProfile,
	draftFromSettings,
	hostLibraryAccessChecked,
	hostLibraryAccessCopy,
	libraryReadiness,
	moveLibraryDraft,
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
		libraries: [
			{
				index: '0',
				key: 'tv',
				label: 'TV Shows',
				path: '/Volumes/TV',
				color: '#4e6fa6',
				library_type: 'tv',
				availability: 'production',
				default_profile: 'inherit_defaults',
				plex_path: '/data/tv',
				policy: {
					series_lifecycle_mode: 'auto',
					current_season_inactive_days: 365,
					season_acquisition_hold_days: 30,
					series_metadata_stale_days: 7
				},
				readiness: { state: 'ready', label: 'Ready', detail: 'Ready.' },
				type_change_confirmation: ''
			}
		],
		library_type_options: [
			{ key: 'tv', label: 'TV' },
			{ key: 'movie', label: 'Movies' },
			{ key: 'spatial', label: '3D / VR' },
			{ key: 'other', label: 'Other' }
		],
		library_profile_options: {
			tv: [{ key: 'inherit_defaults', label: 'Use assistant defaults' }],
			movie: [{ key: 'movie_balanced', label: 'Balanced movie' }],
			spatial: [{ key: 'spatial_preserve', label: 'Preserve source geometry' }],
			other: [{ key: 'other_conservative', label: 'Conservative' }]
		},
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
		video_defaults: {
			quality_metric: 'vmaf',
			target_vmaf: '85',
			min_target_vmaf: '80',
			target_xpsnr: '41',
			min_target_xpsnr: '35',
			target_size_mb: '300',
			target_runtime_minutes: '45',
			size_goal_mode: 'normalized',
			sample_projection_tolerance_percent: '10',
			final_output_tolerance_percent: '5',
			decision_model: 'size_first_review',
			quality_engine: 'ab_av1_fast_sample',
			max_height: '1080',
			default_grain: '8',
			max_encoded_percent: '80'
		},
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
		metadata: {
			plex: {
				enabled: true,
				base_url: 'http://plex.local:32400',
				library_roots: { tv: '/data/tv' },
				token_env: 'MEDIAFORCE_PLEX_TOKEN',
				token_configured: true,
				refresh_interval_hours: 2
			},
			tmdb: {
				enabled: true,
				base_url: 'https://api.themoviedb.org/3',
				token_env: 'MEDIAFORCE_TMDB_TOKEN',
				token_configured: true,
				refresh_interval_hours: 48
			}
		},
		runtime_settings_path: '/Users/operator/Library/Application Support/mediaforce/settings.json',
		repo_config_path: '/Users/operator/mediaforce/config.yaml',
		host_notice: null,
		host_notice_kind: null
	};

	it('keeps save payload arrays isolated from the live draft', () => {
		const draft = draftFromSettings(payload);
		const savePayload = buildSettingsSavePayload(draft, payload);

		draft.remote_hosts[0]?.capabilities.push('sample_calibration');
		draft.metadata.plex.library_roots.tv = '/changed/tv';
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
		expect(savePayload.metadata.plex.library_roots).toEqual({ tv: '/data/tv' });
	});

	it('detects meaningful draft changes and ignores unchanged clones', () => {
		const draft = draftFromSettings(payload);
		expect(settingsDraftIsDirty(draft, payload)).toBe(false);

		draft.libraries[0] = { ...draft.libraries[0], path: '/Volumes/TV2' };
		expect(settingsDraftIsDirty(draft, payload)).toBe(true);
	});

	it('creates stable library IDs and preserves order through moves', () => {
		const libraries = addLibraryDraft(payload.libraries);

		expect(libraries[1]?.key).toBe('library_2');
		expect(libraries[1]?.availability).toBe('browse_only');
		expect(moveLibraryDraft(libraries, 1, -1).map((library) => library.key)).toEqual([
			'library_2',
			'tv'
		]);
	});

	it('resets confirmed type changes to a safe browse-only policy', () => {
		const preview = {
			key: 'tv',
			from_type: 'tv' as const,
			to_type: 'movie' as const,
			item_count: 42,
			requires_rescan: true,
			clears_saved_profiles: true,
			acknowledgement: 'tv:tv->movie'
		};

		const changed = applyLibraryTypeChange(
			payload.libraries,
			0,
			'movie',
			payload.library_profile_options,
			preview
		)[0];

		expect(changed?.availability).toBe('browse_only');
		expect(changed?.policy.grouping).toBe('title');
		expect(changed?.type_change_confirmation).toBe('tv:tv->movie');
	});

	it('keeps spatial roots incomplete until playback facts are supplied', () => {
		const spatial = applyLibraryTypeChange(
			payload.libraries,
			0,
			'spatial',
			payload.library_profile_options
		)[0];

		expect(spatial && libraryReadiness(spatial).state).toBe('incomplete');
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

	it('collapses a fully selected allowlist back to the canonical empty state', () => {
		const allLibrariesHost = { ...payload.remote_hosts[0], allowed_libraries: [] };
		const libraryKeys = ['movies', 'tv'];

		const hostsAfterOff = toggleHostAllowedLibrary(
			[{ ...allLibrariesHost }],
			0,
			'movies',
			libraryKeys
		);
		expect(hostsAfterOff[0]?.allowed_libraries).toEqual(['tv']);

		const hostsAfterOn = toggleHostAllowedLibrary(hostsAfterOff, 0, 'movies', libraryKeys);
		expect(hostsAfterOn[0]?.allowed_libraries).toEqual([]);
	});

	it('normalizes trimmed and duplicate library keys when toggling', () => {
		const host = { ...payload.remote_hosts[0], allowed_libraries: [] };
		const hosts = toggleHostAllowedLibrary([{ ...host }], 0, ' movies ', [' movies ', 'tv', 'tv']);

		expect(hosts[0]?.allowed_libraries).toEqual(['tv']);
	});

	it('treats an empty host library list as all libraries allowed in the editor', () => {
		const allLibrariesHost = { ...payload.remote_hosts[0], allowed_libraries: [] };

		expect(hostLibraryAccessChecked(allLibrariesHost, 'tv')).toBe(true);
		expect(hostLibraryAccessCopy(allLibrariesHost)).toBe('All libraries allowed');

		const hosts = toggleHostAllowedLibrary([{ ...allLibrariesHost }], 0, 'movies', [
			'movies',
			'tv'
		]);

		expect(hosts[0]?.allowed_libraries).toEqual(['tv']);
	});
});
