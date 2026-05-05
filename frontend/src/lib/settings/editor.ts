import type {
	HostRuntime,
	ScheduleProfile,
	SettingsHost,
	SettingsLibrary,
	SettingsPayload
} from '$lib/api/types';

export const AB_AV1_MISSING_ISSUE = 'ab-av1 is not installed on the remote PATH.';
export const FFMPEG_MISSING_ISSUE = 'ffmpeg is not installed on the remote PATH.';
export const FFMPEG_VIDEOTOOLBOX_MISSING_ISSUE =
	'ffmpeg is missing VideoToolbox hardware decode required for H.264/H.265 sources.';
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

export type HostActionState = {
	preparing: boolean;
	resettingTrust: boolean;
	password: string;
	showPassword: boolean;
};

export type HostPrimaryAction = 'prepare' | 'start' | null;
export type SettingsDraft = ReturnType<typeof draftFromSettings>;
export type SettingsSavePayload = {
	libraries: SettingsLibrary[];
	remote_hosts: SettingsHost[];
	transcode_root: string;
	encode_queue_scheduler: SettingsPayload['encode_queue_scheduler'];
	schedule_profiles: ScheduleProfile[];
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
			.map((library) => ({ ...library })),
		remote_hosts: payload.remote_hosts
			.filter((host) => host.label || host.host)
			.map((host) => ({
				...host,
				capabilities: [...host.capabilities],
				allowed_libraries: [...host.allowed_libraries]
			})),
		transcode_root: payload.transcode_root,
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
		libraries: draft.libraries.map((library) => ({ ...library })),
		remote_hosts: draft.remote_hosts.map((host) => ({
			...host,
			capabilities: [...host.capabilities],
			allowed_libraries: [...host.allowed_libraries]
		})),
		transcode_root: draft.transcode_root,
		encode_queue_scheduler: { ...settings.encode_queue_scheduler },
		schedule_profiles: draft.schedule_profiles.map((profile) => cloneScheduleProfile(profile))
	};
}

export function settingsDraftIsDirty(draft: SettingsDraft, settings: SettingsPayload): boolean {
	return (
		JSON.stringify(buildSettingsSavePayload(draft, settings)) !==
		JSON.stringify(buildSettingsSavePayload(draftFromSettings(settings), settings))
	);
}

export function hostDraftRuntimeKey(host: SettingsHost): string {
	return host.host.trim() || host.label.trim();
}

export function addLibraryDraft(libraries: SettingsLibrary[]): SettingsLibrary[] {
	return [...libraries, { index: String(libraries.length), key: '', path: '', color: '#0f766e' }];
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
	libraryKey: string
): SettingsHost[] {
	const normalizedKey = libraryKey.trim();
	if (!normalizedKey) return remoteHosts;
	return remoteHosts.map((host, candidate) => {
		if (candidate !== index) return host;
		const allowed_libraries = host.allowed_libraries.includes(normalizedKey)
			? host.allowed_libraries.filter((value) => value !== normalizedKey)
			: [...host.allowed_libraries, normalizedKey];
		return { ...host, allowed_libraries };
	});
}

export function hostActionKey(
	host: SettingsHost,
	runtimeHost: HostRuntime | null,
	index: number
): string {
	return runtimeHost?.key || host.host || `${host.label || 'host'}-${index}`;
}

export function defaultHostActionState(): HostActionState {
	return {
		preparing: false,
		resettingTrust: false,
		password: '',
		showPassword: false
	};
}

export function primaryHostAction(runtimeHost: HostRuntime, host: SettingsHost): HostPrimaryAction {
	if (
		!runtimeHost.available &&
		Boolean(host.start_command.trim()) &&
		runtimeHost.message === 'SSH unavailable'
	) {
		return 'start';
	}
	if (!runtimeHost.setup_supported) return null;
	if (runtimeHost.missing_paths.length > 0 && runtimeHost.issues.length === 0) return null;
	return 'prepare';
}

export function primaryHostActionLabel(runtimeHost: HostRuntime, host: SettingsHost): string {
	if (primaryHostAction(runtimeHost, host) === 'start') {
		return 'Start host';
	}
	if (runtimeHost.issues.includes(AB_AV1_MISSING_ISSUE)) {
		return 'Install ab-av1';
	}
	if (runtimeHost.issues.includes(FFMPEG_MISSING_ISSUE)) {
		return 'Install ffmpeg';
	}
	if (runtimeHost.issues.includes(FFMPEG_VIDEOTOOLBOX_MISSING_ISSUE)) {
		return 'Reinstall ffmpeg';
	}
	if (runtimeHost.message === 'SSH access setup required') {
		return 'Install SSH key';
	}
	return 'Prepare host';
}

export function primaryHostActionHelp(runtimeHost: HostRuntime, host: SettingsHost): string {
	if (primaryHostAction(runtimeHost, host) === 'start') {
		return 'Runs the configured host start command, then waits for SSH status to come back before refreshing this card.';
	}
	if (runtimeHost.issues.includes(AB_AV1_MISSING_ISSUE)) {
		return 'Runs remote setup so sampled calibration can find ab-av1 on the host PATH.';
	}
	if (runtimeHost.issues.includes(FFMPEG_MISSING_ISSUE)) {
		return 'Installs the missing ffmpeg toolchain on the remote Mac through the normal setup path.';
	}
	if (runtimeHost.issues.includes(FFMPEG_VIDEOTOOLBOX_MISSING_ISSUE)) {
		return 'Refreshes the ffmpeg toolchain on the remote Mac so VideoToolbox decode is available for H.264/H.265 sources.';
	}
	if (runtimeHost.message === 'SSH access setup required') {
		return "Installs this Mac's SSH key so Mediaforce can reconnect without prompting in the future.";
	}
	return 'Runs the built-in remote setup flow for this worker.';
}

export function hasPrimaryHostAction(runtimeHost: HostRuntime, host: SettingsHost): boolean {
	return primaryHostAction(runtimeHost, host) !== null;
}

export function shouldShowHostActions(runtimeHost: HostRuntime, host: SettingsHost): boolean {
	return (
		Boolean(runtimeHost.trust_reset_supported) ||
		(!runtimeHost.available && hasPrimaryHostAction(runtimeHost, host))
	);
}
