<script lang="ts">
	import { invalidateAll } from '$app/navigation';
	import { postJson } from '$lib/api/client';
	import type {
		HostRuntime,
		HostsPayload,
		ScheduleProfile,
		SettingsHost,
		SettingsLibrary,
		SettingsPayload
	} from '$lib/api/types';
	import SettingsEditor from '$lib/components/settings/SettingsEditor.svelte';
	import {
		addHostDraft,
		addLibraryDraft,
		addScheduleDraft,
		defaultHostActionState,
		draftFromSettings,
		hasPrimaryHostAction,
		hostActionKey,
		primaryHostActionHelp,
		primaryHostActionLabel,
		removeAtIndex,
		shouldShowHostActions,
		toggleHostCapability,
		type HostActionState
	} from '$lib/settings/editor';
	import { toasts } from '$lib/stores/toasts';

	let {
		data
	}: {
		data: {
			settings: SettingsPayload;
			hosts: HostsPayload;
		};
	} = $props();

	const settings = $derived(data.settings);
	const hosts = $derived(data.hosts);
	const runtimeHostsByKey = $derived.by(() => new Map(hosts.hosts.map((host) => [host.key, host])));

	let libraries = $state<SettingsLibrary[]>([]);
	let remoteHosts = $state<SettingsHost[]>([]);
	let scheduleProfiles = $state<ScheduleProfile[]>([]);
	let transcodeRoot = $state('');
	let isSaving = $state(false);
	let isRefreshingHosts = $state(false);
	let hostActionState = $state<Record<string, HostActionState>>({});

	const initialSettingsDraft = $derived.by(() => draftFromSettings(settings));
	const currentSettingsDraft = $derived.by(() => ({
		libraries: libraries.map((library) => ({ ...library })),
		remote_hosts: remoteHosts.map((host) => ({ ...host, capabilities: [...host.capabilities] })),
		transcode_root: transcodeRoot,
		schedule_profiles: scheduleProfiles.map((profile) => ({ ...profile }))
	}));
	const isDirty = $derived(
		JSON.stringify(currentSettingsDraft) !== JSON.stringify(initialSettingsDraft)
	);

	function applySettingsDraft(payload: SettingsPayload) {
		const draft = draftFromSettings(payload);
		libraries = draft.libraries;
		remoteHosts = draft.remote_hosts;
		scheduleProfiles = draft.schedule_profiles;
		transcodeRoot = draft.transcode_root;
	}

	$effect(() => {
		if (!libraries.length) {
			libraries = draftFromSettings(settings).libraries;
		}
		if (!remoteHosts.length) {
			remoteHosts = draftFromSettings(settings).remote_hosts;
		}
		if (!scheduleProfiles.length) {
			scheduleProfiles = draftFromSettings(settings).schedule_profiles;
		}
		if (!transcodeRoot) {
			transcodeRoot = settings.transcode_root;
		}
	});

	function addLibrary() {
		libraries = addLibraryDraft(libraries);
	}

	function addHost() {
		remoteHosts = addHostDraft(remoteHosts, settings.schedule_profile_options);
	}

	function addSchedule() {
		scheduleProfiles = addScheduleDraft(scheduleProfiles);
	}

	function removeLibrary(index: number) {
		libraries = removeAtIndex(libraries, index);
	}

	function removeHost(index: number) {
		remoteHosts = removeAtIndex(remoteHosts, index);
	}

	function removeSchedule(index: number) {
		scheduleProfiles = removeAtIndex(scheduleProfiles, index);
	}

	function toggleCapability(index: number, capability: string) {
		remoteHosts = toggleHostCapability(remoteHosts, index, capability);
	}

	async function saveSettings() {
		isSaving = true;
		try {
			const response = await postJson<{ message: string; settings: SettingsPayload }>(
				'/api/settings',
				{
					libraries,
					remote_hosts: remoteHosts,
					transcode_root: transcodeRoot,
					encode_queue_scheduler: settings.encode_queue_scheduler,
					schedule_profiles: scheduleProfiles
				}
			);
			applySettingsDraft(response.settings);
			toasts.success('Settings saved', response.message);
			await invalidateAll();
		} catch (error) {
			toasts.error(
				'Settings save failed',
				error instanceof Error ? error.message : 'Unexpected settings error'
			);
		} finally {
			isSaving = false;
		}
	}

	async function refreshHostStatuses() {
		isRefreshingHosts = true;
		try {
			await invalidateAll();
		} catch (error) {
			toasts.error(
				'Host refresh failed',
				error instanceof Error ? error.message : 'Unexpected host refresh error'
			);
		} finally {
			isRefreshingHosts = false;
		}
	}

	function getHostActionState(hostKey: string) {
		return hostActionState[hostKey] ?? defaultHostActionState();
	}

	function patchHostActionState(hostKey: string, patch: Partial<HostActionState>) {
		hostActionState = {
			...hostActionState,
			[hostKey]: {
				...getHostActionState(hostKey),
				...patch
			}
		};
	}

	function updateHostActionPassword(hostKey: string, value: string) {
		patchHostActionState(hostKey, { password: value });
	}

	function revealHostActionPassword(hostKey: string) {
		patchHostActionState(hostKey, { showPassword: true });
	}

	async function prepareHost(hostKey: string, runtimeHost: HostRuntime) {
		const state = getHostActionState(hostKey);
		if (runtimeHost.setup_requires_password && !state.password.trim()) {
			patchHostActionState(hostKey, { showPassword: true });
			toasts.info(
				'Remote password required',
				'Enter the remote account password to continue setup.'
			);
			return;
		}

		patchHostActionState(hostKey, { preparing: true });
		try {
			const response = await postJson<{ ok: boolean; message: string; kind?: string }>(
				'/api/hosts/prepare',
				{
					host_key: hostKey,
					remote_password: state.password.trim() || undefined
				}
			);
			if (response.ok) {
				toasts.success('Remote setup finished', response.message);
				patchHostActionState(hostKey, { password: '', showPassword: false });
			} else {
				toasts.error('Remote setup failed', response.message);
			}
			await invalidateAll();
		} catch (error) {
			toasts.error(
				'Remote setup failed',
				error instanceof Error ? error.message : 'Unexpected host setup error'
			);
		} finally {
			patchHostActionState(hostKey, { preparing: false });
		}
	}

	async function resetHostTrust(hostKey: string) {
		patchHostActionState(hostKey, { resettingTrust: true });
		try {
			const response = await postJson<{ ok: boolean; message: string; kind?: string }>(
				'/api/hosts/reset-trust',
				{ host_key: hostKey }
			);
			if (response.ok) {
				toasts.success('SSH trust reset', response.message);
			} else {
				toasts.error('SSH trust reset failed', response.message);
			}
			await invalidateAll();
		} catch (error) {
			toasts.error(
				'SSH trust reset failed',
				error instanceof Error ? error.message : 'Unexpected SSH trust error'
			);
		} finally {
			patchHostActionState(hostKey, { resettingTrust: false });
		}
	}
</script>

<svelte:head>
	<title>Settings · Mediaforce</title>
</svelte:head>

<SettingsEditor
	{settings}
	{runtimeHostsByKey}
	{libraries}
	{remoteHosts}
	{scheduleProfiles}
	bind:transcodeRoot
	{isDirty}
	{isSaving}
	{isRefreshingHosts}
	{hostActionKey}
	{getHostActionState}
	{updateHostActionPassword}
	{revealHostActionPassword}
	{primaryHostActionLabel}
	{primaryHostActionHelp}
	{hasPrimaryHostAction}
	{shouldShowHostActions}
	{addLibrary}
	{addHost}
	{addSchedule}
	{removeLibrary}
	{removeHost}
	{removeSchedule}
	{toggleCapability}
	{saveSettings}
	{refreshHostStatuses}
	{prepareHost}
	{resetHostTrust}
/>
