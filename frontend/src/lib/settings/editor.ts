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

export type HostActionState = {
	preparing: boolean;
	resettingTrust: boolean;
	password: string;
	showPassword: boolean;
};

export type HostPrimaryAction = 'prepare' | 'start' | null;

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
			.map((profile) => ({ ...profile }))
	};
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
