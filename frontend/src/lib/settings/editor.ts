import type {
	LibraryPolicy,
	LibraryReadiness,
	LibraryType,
	LibraryTypeChangePreview,
	ScheduleProfile,
	SettingsHost,
	SettingsLibrary,
	SettingsPayload
} from '$lib/api/types';
export const SCHEDULE_DAY_OPTIONS = [
	{ key: 'mon', shortLabel: 'Mon', label: 'Monday' },
	{ key: 'tue', shortLabel: 'Tue', label: 'Tuesday' },
	{ key: 'wed', shortLabel: 'Wed', label: 'Wednesday' },
	{ key: 'thu', shortLabel: 'Thu', label: 'Thursday' },
	{ key: 'fri', shortLabel: 'Fri', label: 'Friday' },
	{ key: 'sat', shortLabel: 'Sat', label: 'Saturday' },
	{ key: 'sun', shortLabel: 'Sun', label: 'Sunday' }
] as const;
export type ScheduleDayKey = (typeof SCHEDULE_DAY_OPTIONS)[number]['key'];
export const DEFAULT_SCHEDULE_DAYS = SCHEDULE_DAY_OPTIONS.map((option) => option.key);

const scheduleDayIndex = new Map<ScheduleDayKey, number>(
	SCHEDULE_DAY_OPTIONS.map((option, index) => [option.key, index])
);

export type SettingsDraft = ReturnType<typeof draftFromSettings>;
export type SettingsSavePayload = {
	libraries: SettingsLibrary[];
	remote_hosts: SettingsHost[];
	transcode_root: string;
	video_defaults: SettingsPayload['video_defaults'];
	encode_queue_scheduler: SettingsPayload['encode_queue_scheduler'];
	schedule_profiles: ScheduleProfile[];
	metadata: SettingsPayload['metadata'];
};
export type ArchiveCleanupClearPayload = {
	transcode_root: string;
};

export function cloneScheduleProfile(profile: ScheduleProfile): ScheduleProfile {
	return normalizeScheduleProfile(profile);
}

export function scheduleDaysSummaryCopy(daysOfWeek: string[]): string {
	const normalizedDays = normalizeScheduleDays(daysOfWeek, { fallbackToDefault: false });
	if (!normalizedDays.length) return 'Choose days';
	if (normalizedDays.length === DEFAULT_SCHEDULE_DAYS.length) return 'Every day';
	if (normalizedDays.join(',') === DEFAULT_SCHEDULE_DAYS.slice(0, 5).join(',')) return 'Weekdays';
	if (normalizedDays.join(',') === DEFAULT_SCHEDULE_DAYS.slice(5).join(',')) return 'Weekends';
	return normalizedDays
		.map((day) => SCHEDULE_DAY_OPTIONS[scheduleDayIndex.get(day) ?? 0]?.shortLabel ?? day)
		.join(', ');
}

export function scheduleWindowSummaryCopy(profile: ScheduleProfile): string {
	const normalizedProfile = normalizeScheduleProfile(profile);
	const numericStartHour = Number(normalizedProfile.start_hour);
	const numericEndHour = Number(normalizedProfile.end_hour);
	const start = formatScheduleHour(numericStartHour);
	const end = formatScheduleHour(numericEndHour);
	const parts: string[] = [];
	const windowDayCopy = scheduleDaysSummaryCopy(normalizedProfile.days_of_week);
	const allDayCopy = scheduleDaysSummaryCopy(normalizedProfile.all_day_days_of_week);
	if (normalizedProfile.all_day_days_of_week.length > 0) {
		parts.push(allDayCopy === 'Every day' ? 'All day' : `${allDayCopy} all day`);
	}
	if (normalizedProfile.days_of_week.length > 0) {
		const timeCopy = start === end ? `${start} all day` : `${start} - ${end}`;
		parts.push(windowDayCopy === 'Every day' ? timeCopy : `${windowDayCopy} · ${timeCopy}`);
	}
	if (!parts.length) return 'Choose days';
	return parts.join(' + ');
}

export function toggleScheduleProfileDay(
	profile: ScheduleProfile,
	dayKey: ScheduleDayKey,
	target: 'days_of_week' | 'all_day_days_of_week' = 'days_of_week'
): ScheduleProfile {
	const normalizedProfile = normalizeScheduleProfile(profile);
	const activeDays = normalizedProfile[target];
	const nextDays = activeDays.includes(dayKey)
		? activeDays.filter((day) => day !== dayKey)
		: [...activeDays, dayKey];
	return normalizeScheduleProfile({
		...normalizedProfile,
		[target]: normalizeScheduleDays(nextDays, { fallbackToDefault: false })
	});
}

function normalizeScheduleDays(
	daysOfWeek: string[] | undefined,
	{ fallbackToDefault }: { fallbackToDefault: boolean }
): ScheduleDayKey[] {
	const seen = new Set<string>();
	return [...(daysOfWeek ?? (fallbackToDefault ? DEFAULT_SCHEDULE_DAYS : []))]
		.filter((day): day is ScheduleDayKey => scheduleDayIndex.has(day as ScheduleDayKey))
		.filter((day) => {
			if (seen.has(day)) return false;
			seen.add(day);
			return true;
		})
		.sort((left, right) => (scheduleDayIndex.get(left) ?? 0) - (scheduleDayIndex.get(right) ?? 0));
}

function normalizeScheduleProfile(profile: ScheduleProfile): ScheduleProfile {
	const allDayDays = normalizeScheduleDays(profile.all_day_days_of_week, {
		fallbackToDefault: false
	});
	const defaultWindowDaysToAll = profile.days_of_week === undefined && allDayDays.length === 0;
	const windowDays = normalizeScheduleDays(profile.days_of_week, {
		fallbackToDefault: defaultWindowDaysToAll
	}).filter((day) => !allDayDays.includes(day));
	return {
		...profile,
		days_of_week: [...windowDays],
		all_day_days_of_week: [...allDayDays]
	};
}

function formatScheduleHour(hour: number): string {
	const normalized = Math.max(0, Math.min(23, Number.isFinite(hour) ? hour : 0));
	return `${normalized.toString().padStart(2, '0')}:00`;
}

export function draftFromSettings(payload: SettingsPayload) {
	return {
		libraries: payload.libraries
			.filter((library) => library.key || library.path)
			.map((library) => ({ ...library, policy: { ...library.policy } })),
		remote_hosts: payload.remote_hosts
			.filter((host) => host.label || host.host)
			.map((host) => ({
				...host,
				capabilities: [...host.capabilities],
				allowed_libraries: [...host.allowed_libraries]
			})),
		transcode_root: payload.transcode_root,
		video_defaults: { ...payload.video_defaults },
		metadata: {
			plex: {
				...payload.metadata.plex,
				library_roots: { ...payload.metadata.plex.library_roots }
			},
			tmdb: { ...payload.metadata.tmdb }
		},
		schedule_profiles: payload.schedule_profiles
			.filter((profile) => profile.key || profile.label)
			.map((profile) => cloneScheduleProfile(profile))
	};
}

export function buildSettingsSavePayload(
	draft: SettingsDraft,
	settings: SettingsPayload
): SettingsSavePayload {
	return {
		libraries: draft.libraries.map((library) => ({ ...library, policy: { ...library.policy } })),
		remote_hosts: draft.remote_hosts.map((host) => ({
			...host,
			capabilities: [...host.capabilities],
			allowed_libraries: [...host.allowed_libraries]
		})),
		transcode_root: draft.transcode_root,
		video_defaults: { ...draft.video_defaults },
		encode_queue_scheduler: { ...settings.encode_queue_scheduler },
		schedule_profiles: draft.schedule_profiles.map((profile) => cloneScheduleProfile(profile)),
		metadata: {
			plex: {
				...draft.metadata.plex,
				library_roots: Object.fromEntries(
					draft.libraries
						.filter((library) => library.key.trim() && library.plex_path.trim())
						.map((library) => [library.key.trim(), library.plex_path.trim()])
				)
			},
			tmdb: { ...draft.metadata.tmdb }
		}
	};
}

export function settingsDraftIsDirty(draft: SettingsDraft, settings: SettingsPayload): boolean {
	return (
		JSON.stringify(buildSettingsSavePayload(draft, settings)) !==
		JSON.stringify(buildSettingsSavePayload(draftFromSettings(settings), settings))
	);
}

export function archiveCleanupTargetDirty(
	draft: SettingsDraft,
	settings: SettingsPayload
): boolean {
	return draft.transcode_root.trim() !== settings.transcode_root.trim();
}

export function buildArchiveCleanupClearPayload(
	settings: SettingsPayload
): ArchiveCleanupClearPayload {
	return { transcode_root: settings.transcode_root };
}

export function hostDraftRuntimeKey(host: SettingsHost): string {
	return host.host.trim() || host.label.trim();
}

export function addLibraryDraft(libraries: SettingsLibrary[]): SettingsLibrary[] {
	const key = nextLibraryKey(libraries);
	return [
		...libraries,
		{
			index: String(libraries.length),
			key,
			label: '',
			path: '',
			color: '#0f766e',
			library_type: 'other',
			availability: 'browse_only',
			default_profile: 'other_conservative',
			plex_path: '',
			policy: libraryPolicyDefaults('other'),
			readiness: libraryReadiness({
				key,
				label: '',
				path: '',
				library_type: 'other',
				availability: 'browse_only',
				policy: libraryPolicyDefaults('other')
			}),
			type_change_confirmation: ''
		}
	];
}

export function moveLibraryDraft(
	libraries: SettingsLibrary[],
	index: number,
	direction: -1 | 1
): SettingsLibrary[] {
	const target = index + direction;
	if (target < 0 || target >= libraries.length) return libraries;
	const reordered = [...libraries];
	[reordered[index], reordered[target]] = [reordered[target], reordered[index]];
	return reordered.map((library, candidate) => ({ ...library, index: String(candidate) }));
}

export function applyLibraryTypeChange(
	libraries: SettingsLibrary[],
	index: number,
	libraryType: LibraryType,
	profileOptions: SettingsPayload['library_profile_options'],
	preview?: LibraryTypeChangePreview
): SettingsLibrary[] {
	return libraries.map((library, candidate) => {
		if (candidate !== index) return library;
		const policy = libraryPolicyDefaults(libraryType);
		const updated = {
			...library,
			library_type: libraryType,
			availability: preview
				? ('browse_only' as const)
				: libraryType === 'tv'
					? ('production' as const)
					: ('browse_only' as const),
			default_profile: profileOptions[libraryType][0]?.key ?? defaultLibraryProfile(libraryType),
			policy,
			type_change_confirmation: preview?.acknowledgement ?? ''
		};
		return { ...updated, readiness: libraryReadiness(updated) };
	});
}

export function libraryPolicyDefaults(libraryType: LibraryType): LibraryPolicy {
	if (libraryType === 'tv') {
		return {
			series_lifecycle_mode: 'auto',
			current_season_inactive_days: 365,
			season_acquisition_hold_days: 30,
			series_metadata_stale_days: 7
		};
	}
	if (libraryType === 'movie') {
		return {
			grouping: 'title',
			editions: 'separate',
			extras: 'exclude',
			ranking: 'oldest_added_first'
		};
	}
	if (libraryType === 'spatial') {
		return {
			playback_target: '',
			stereo_layout: 'unknown',
			projection: 'unknown',
			geometry_policy: 'preserve',
			container_profile: 'unqualified'
		};
	}
	return { grouping: 'folder' };
}

export function libraryReadiness(
	library: Pick<
		SettingsLibrary,
		'key' | 'label' | 'path' | 'library_type' | 'availability' | 'policy'
	> &
		Partial<Pick<SettingsLibrary, 'default_profile'>>
): LibraryReadiness {
	if (!library.key.trim() || !library.label.trim() || !library.path.trim()) {
		return {
			state: 'incomplete',
			label: 'Incomplete',
			detail: 'Add a label, root ID, and local path.'
		};
	}
	if (library.availability === 'disabled') {
		return {
			state: 'disabled',
			label: 'Disabled',
			detail: 'Mediaforce will not scan or process this library.'
		};
	}
	if (library.library_type === 'spatial') {
		const missing = [
			!library.policy.playback_target?.trim() ? 'playback target' : '',
			library.policy.stereo_layout === 'unknown' ? 'stereo layout' : '',
			library.policy.projection === 'unknown' ? 'projection' : ''
		].filter(Boolean);
		if (missing.length) {
			return {
				state: 'incomplete',
				label: 'Incomplete',
				detail: `Add ${missing.join(', ')} before playback qualification.`
			};
		}
		return {
			state: 'blocked',
			label: 'Safety blocked',
			detail: 'Browse-only until 3D / VR playback qualification is implemented.'
		};
	}
	if (library.availability === 'browse_only') {
		return {
			state: 'browse_only',
			label: 'Browse only',
			detail: 'Mediaforce scans and shows this library but never processes it.'
		};
	}
	if (!['tv', 'movie', 'other'].includes(library.library_type)) {
		return {
			state: 'blocked',
			label: 'Workflow blocked',
			detail: 'Production is not available for this library type yet.'
		};
	}
	if (library.library_type === 'other') {
		if (!library.default_profile?.trim()) {
			return {
				state: 'incomplete',
				label: 'Profile needed',
				detail: 'Choose an explicit Other processing profile before enabling Production.'
			};
		}
		return {
			state: 'ready',
			label: 'Ready',
			detail: 'Generic file and folder scopes use the selected profile and require probe readiness.'
		};
	}
	return {
		state: 'ready',
		label: 'Ready',
		detail: 'Scanning and production processing are enabled.'
	};
}

function nextLibraryKey(libraries: SettingsLibrary[]): string {
	const used = new Set(libraries.map((library) => library.key));
	let suffix = libraries.length + 1;
	while (used.has(`library_${suffix}`)) suffix += 1;
	return `library_${suffix}`;
}

function defaultLibraryProfile(libraryType: LibraryType): string {
	if (libraryType === 'tv') return 'inherit_defaults';
	if (libraryType === 'movie') return 'movie_balanced';
	if (libraryType === 'spatial') return 'spatial_preserve';
	return 'other_conservative';
}

export function addHostDraft(
	remoteHosts: SettingsHost[],
	scheduleProfiles: Array<{ key: string }>
): SettingsHost[] {
	return [
		...remoteHosts,
		{
			index: String(remoteHosts.length),
			label: '',
			host: '',
			repo_path: '',
			wake_mac: '',
			start_command: '',
			stop_command: '',
			start_timeout_seconds: '180',
			media_access: 'mounted',
			priority: '0',
			max_parallel_encodes: '1',
			schedule_profile: scheduleProfiles[0]?.key ?? 'always',
			capabilities: ['encode_queue', 'sample_calibration'],
			allowed_libraries: [],
			source_roots_json: '',
			staging_root: ''
		}
	];
}

export function addScheduleDraft(scheduleProfiles: ScheduleProfile[]): ScheduleProfile[] {
	return [
		...scheduleProfiles,
		{
			index: String(scheduleProfiles.length),
			key: '',
			label: '',
			days_of_week: [...DEFAULT_SCHEDULE_DAYS],
			all_day_days_of_week: [],
			start_hour: '22',
			end_hour: '8'
		}
	];
}

export function removeAtIndex<T>(values: T[], index: number): T[] {
	return values.filter((_, candidate) => candidate !== index);
}

export function toggleHostCapability(
	remoteHosts: SettingsHost[],
	index: number,
	capability: string
): SettingsHost[] {
	return remoteHosts.map((host, candidate) => {
		if (candidate !== index) return host;
		const capabilities = host.capabilities.includes(capability)
			? host.capabilities.filter((value) => value !== capability)
			: [...host.capabilities, capability];
		return { ...host, capabilities };
	});
}

export function toggleHostAllowedLibrary(
	remoteHosts: SettingsHost[],
	index: number,
	libraryKey: string,
	libraryKeys: string[] = []
): SettingsHost[] {
	const normalizedKey = libraryKey.trim();
	if (!normalizedKey) return remoteHosts;
	const normalizedLibraryKeys = [...new Set(libraryKeys.map((key) => key.trim()).filter(Boolean))];
	return remoteHosts.map((host, candidate) => {
		if (candidate !== index) return host;
		const explicitAllowedLibraries =
			host.allowed_libraries.length > 0 ? host.allowed_libraries : normalizedLibraryKeys;
		const allowed_libraries = explicitAllowedLibraries.includes(normalizedKey)
			? explicitAllowedLibraries.filter((value) => value !== normalizedKey)
			: [...explicitAllowedLibraries, normalizedKey];
		const canonicalAllowedLibraries =
			normalizedLibraryKeys.length > 0 &&
			allowed_libraries.length === normalizedLibraryKeys.length &&
			normalizedLibraryKeys.every((key) => allowed_libraries.includes(key))
				? []
				: allowed_libraries;
		return { ...host, allowed_libraries: canonicalAllowedLibraries };
	});
}

export function hostLibraryAccessChecked(host: SettingsHost, libraryKey: string): boolean {
	const normalizedKey = libraryKey.trim();
	if (!normalizedKey) return false;
	return host.allowed_libraries.length === 0 || host.allowed_libraries.includes(normalizedKey);
}

export function hostLibraryAccessCopy(host: SettingsHost): string {
	return host.allowed_libraries.length === 0
		? 'All libraries allowed'
		: `${host.allowed_libraries.length.toLocaleString('en-US')} libraries allowed`;
}
