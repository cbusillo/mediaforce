<script lang="ts">
	import { browser } from '$app/environment';
	import '$lib/design/workstation-shell.css';
	import { onMount } from 'svelte';
	import { fetchJson, postJson } from '$lib/api/client';
	import type {
		ArchiveCleanupPayload,
		HostRuntime,
		HostsPayload,
		ScheduleProfile,
		SettingsHost,
		SettingsLibrary,
		SettingsPayload
	} from '$lib/api/types';
	import Button from '$lib/components/Button.svelte';
	import SettingsEditor from '$lib/components/settings/SettingsEditor.svelte';
	import { hostsStatusPending } from '$lib/hosts/runtime';
	import {
		addHostDraft,
		addLibraryDraft,
		addScheduleDraft,
		cloneScheduleProfile,
		defaultHostActionState,
		draftFromSettings,
		hasPrimaryHostAction,
		hostActionKey,
		primaryHostAction,
		primaryHostActionHelp,
		primaryHostActionLabel,
		removeAtIndex,
		shouldShowHostActions,
		type ScheduleDayKey,
		toggleScheduleProfileDay,
		toggleHostCapability,
		type HostActionState
	} from '$lib/settings/editor';
	import { toasts } from '$lib/stores/toasts';

	const EMPTY_ARCHIVE_CLEANUP: ArchiveCleanupPayload = {
		archive_root: '',
		file_count: 0,
		total_size_bytes: 0,
		has_cleanup: false
	};
	const EMPTY_SETTINGS: SettingsPayload = {
		error: null,
		saved: false,
		libraries: [],
		remote_hosts: [],
		transcode_root: '',
		encode_queue_scheduler: {
			mode: 'night',
			start_hour: 22,
			end_hour: 8,
			timezone: 'host_local',
			summary: ''
		},
		schedule_profiles: [],
		schedule_profile_options: [],
		host_capability_options: [],
		archive_root: '',
		archive_cleanup: EMPTY_ARCHIVE_CLEANUP,
		runtime_settings_path: '',
		repo_config_path: '',
		host_notice: null,
		host_notice_kind: null
	};

	let settingsPayload = $state<SettingsPayload | null>(null);
	let archiveCleanup = $state<ArchiveCleanupPayload | null>(null);
	let hostsPayload = $state<HostsPayload | null>(null);
	let archiveCleanupPending = $state(true);
	let archiveCleanupError = $state<string | null>(null);
	let isLoadingSettings = $state(true);
	let settingsLoadError = $state<string | null>(null);
	let hostsLoadError = $state<string | null>(null);
	const settings = $derived.by(() => ({
		...(settingsPayload ?? EMPTY_SETTINGS),
		archive_cleanup:
			archiveCleanup ?? settingsPayload?.archive_cleanup ?? EMPTY_SETTINGS.archive_cleanup
	}));
	const runtimeHostsByKey = $derived.by(
		() => new Map((hostsPayload?.hosts ?? []).map((host) => [host.key, host]))
	);

	let libraries = $state<SettingsLibrary[]>([]);
	let remoteHosts = $state<SettingsHost[]>([]);
	let scheduleProfiles = $state<ScheduleProfile[]>([]);
	let transcodeRoot = $state('');
	let isSaving = $state(false);
	let isRefreshingHosts = $state(false);
	let isClearingArchive = $state(false);
	let hostActionState = $state<Record<string, HostActionState>>({});
	let archiveCleanupController: AbortController | null = null;
	let activeArchiveCleanupRequest = 0;
	let hostRefreshController: AbortController | null = null;
	let activeHostRefreshRequest = 0;
	let hostRefreshRetryTimer: number | null = null;

	const initialSettingsDraft = $derived.by(() => draftFromSettings(settings));
	const currentSettingsDraft = $derived.by(() => ({
		libraries: libraries.map((library) => ({ ...library })),
		remote_hosts: remoteHosts.map((host) => ({
			...host,
			capabilities: [...host.capabilities],
			allowed_libraries: [...host.allowed_libraries]
		})),
		transcode_root: transcodeRoot,
		schedule_profiles: scheduleProfiles.map((profile) => cloneScheduleProfile(profile))
	}));
	const isDirty = $derived(
		JSON.stringify(currentSettingsDraft) !== JSON.stringify(initialSettingsDraft)
	);

	function applySettingsDraft(payload: SettingsPayload) {
		settingsPayload = payload;
		if (payload.archive_cleanup) {
			archiveCleanup = payload.archive_cleanup;
			archiveCleanupPending = false;
			archiveCleanupError = null;
		}
		const draft = draftFromSettings(payload);
		libraries = draft.libraries;
		remoteHosts = draft.remote_hosts;
		scheduleProfiles = draft.schedule_profiles;
		transcodeRoot = draft.transcode_root;
	}

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

	function toggleScheduleDay(
		index: number,
		dayKey: ScheduleDayKey,
		target: 'days_of_week' | 'all_day_days_of_week' = 'days_of_week'
	) {
		scheduleProfiles = scheduleProfiles.map((profile, candidate) =>
			candidate === index ? toggleScheduleProfileDay(profile, dayKey, target) : profile
		);
	}

	function toggleCapability(index: number, capability: string) {
		remoteHosts = toggleHostCapability(remoteHosts, index, capability);
	}

	async function loadSettings({ silent = false }: { silent?: boolean } = {}) {
		isLoadingSettings = true;
		try {
			const response = await fetchJson<SettingsPayload>('/api/settings?include_archive_cleanup=0');
			applySettingsDraft(response);
			settingsLoadError = null;
			if (!response.archive_cleanup) {
				void refreshArchiveCleanup({ silent: true });
			}
		} catch (error) {
			settingsLoadError =
				error instanceof Error ? error.message : 'Unexpected settings loading error';
			if (!silent) {
				toasts.error('Settings load failed', settingsLoadError);
			}
		} finally {
			isLoadingSettings = false;
		}
	}

	async function refreshArchiveCleanup({
		transcodeRoot: requestedTranscodeRoot,
		silent = false
	}: {
		transcodeRoot?: string | null;
		silent?: boolean;
	} = {}) {
		archiveCleanupController?.abort();
		archiveCleanupController = new AbortController();
		const requestId = ++activeArchiveCleanupRequest;
		archiveCleanupPending = true;
		try {
			const query = requestedTranscodeRoot?.trim()
				? `?transcode_root=${encodeURIComponent(requestedTranscodeRoot.trim())}`
				: '';
			const nextArchiveCleanup = await fetchJson<ArchiveCleanupPayload>(
				`/api/archive-cleanup${query}`,
				fetch,
				{
					signal: archiveCleanupController.signal
				}
			);
			if (requestId !== activeArchiveCleanupRequest) {
				return;
			}
			archiveCleanup = nextArchiveCleanup;
			archiveCleanupError = null;
		} catch (error) {
			if (requestId !== activeArchiveCleanupRequest) {
				return;
			}
			if (error instanceof DOMException && error.name === 'AbortError') {
				return;
			}
			archiveCleanupError =
				error instanceof Error ? error.message : 'Unexpected archive cleanup error';
			if (!silent) {
				toasts.error('Archive cleanup refresh failed', archiveCleanupError);
			}
		} finally {
			if (requestId === activeArchiveCleanupRequest) {
				archiveCleanupPending = false;
				archiveCleanupController = null;
			}
		}
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
			void refreshArchiveCleanup({ transcodeRoot: response.settings.transcode_root, silent: true });
			void refreshHostStatuses({ silent: true });
		} catch (error) {
			toasts.error(
				'Settings save failed',
				error instanceof Error ? error.message : 'Unexpected settings error'
			);
		} finally {
			isSaving = false;
		}
	}

	async function refreshHostStatuses({ silent = false }: { silent?: boolean } = {}) {
		if (hostRefreshRetryTimer !== null) {
			clearTimeout(hostRefreshRetryTimer);
			hostRefreshRetryTimer = null;
		}
		hostRefreshController?.abort();
		hostRefreshController = new AbortController();
		const requestId = ++activeHostRefreshRequest;
		isRefreshingHosts = true;
		try {
			const nextHostsPayload = await fetchJson<HostsPayload>('/api/hosts', fetch, {
				signal: hostRefreshController.signal
			});
			if (requestId !== activeHostRefreshRequest) {
				return;
			}
			hostsPayload = nextHostsPayload;
			hostsLoadError = null;
			if (browser && hostsStatusPending(nextHostsPayload)) {
				hostRefreshRetryTimer ??= window.setTimeout(() => {
					hostRefreshRetryTimer = null;
					void refreshHostStatuses({ silent: true });
				}, 1000);
			} else if (hostRefreshRetryTimer !== null) {
				clearTimeout(hostRefreshRetryTimer);
				hostRefreshRetryTimer = null;
			}
		} catch (error) {
			if (requestId !== activeHostRefreshRequest) {
				return;
			}
			if (error instanceof DOMException && error.name === 'AbortError') {
				return;
			}
			hostsLoadError = error instanceof Error ? error.message : 'Unexpected host refresh error';
			if (!silent) {
				toasts.error('Host refresh failed', hostsLoadError);
			}
		} finally {
			if (requestId === activeHostRefreshRequest) {
				isRefreshingHosts = false;
				hostRefreshController = null;
			}
		}
	}

	async function clearArchiveCleanup() {
		const targetArchiveRoot = `${transcodeRoot.trim().replace(/\/$/, '')}/_replaced`;
		const usingDraftArchiveRoot = transcodeRoot.trim() !== settings.transcode_root.trim();
		const cleanupCount = usingDraftArchiveRoot ? null : (archiveCleanup?.file_count ?? 0);
		if (
			!transcodeRoot.trim() ||
			!window.confirm(
				cleanupCount != null
					? `Remove ${cleanupCount} archived original file${cleanupCount === 1 ? '' : 's'} from ${targetArchiveRoot}? This deletes rollback copies and cannot be undone.`
					: `Remove archived original files from ${targetArchiveRoot}? This uses the current draft transcode root and cannot be undone.`
			)
		) {
			return;
		}

		isClearingArchive = true;
		try {
			const response = await postJson<{ message: string; archive_cleanup: ArchiveCleanupPayload }>(
				'/api/archive-cleanup/clear',
				{
					transcode_root: transcodeRoot
				}
			);
			if (!usingDraftArchiveRoot) {
				archiveCleanup = response.archive_cleanup;
				archiveCleanupPending = false;
				archiveCleanupError = null;
			}
			toasts.success('Archive cleanup finished', response.message);
		} catch (error) {
			toasts.error(
				'Archive cleanup failed',
				error instanceof Error ? error.message : 'Unexpected archive cleanup error'
			);
		} finally {
			isClearingArchive = false;
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
			await refreshHostStatuses({ silent: true });
		} catch (error) {
			toasts.error(
				'Remote setup failed',
				error instanceof Error ? error.message : 'Unexpected host setup error'
			);
		} finally {
			patchHostActionState(hostKey, { preparing: false });
		}
	}

	async function startHost(hostKey: string) {
		patchHostActionState(hostKey, { preparing: true });
		try {
			const response = await postJson<{ ok: boolean; message: string; kind?: string }>(
				'/api/hosts/start',
				{ host_key: hostKey }
			);
			if (response.ok) {
				toasts.success('Host start finished', response.message);
			} else {
				toasts.error('Host start failed', response.message);
			}
			await refreshHostStatuses({ silent: true });
		} catch (error) {
			toasts.error(
				'Host start failed',
				error instanceof Error ? error.message : 'Unexpected host start error'
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
			await refreshHostStatuses({ silent: true });
		} catch (error) {
			toasts.error(
				'SSH trust reset failed',
				error instanceof Error ? error.message : 'Unexpected SSH trust error'
			);
		} finally {
			patchHostActionState(hostKey, { resettingTrust: false });
		}
	}

	onMount(() => {
		void loadSettings({ silent: true });
		void refreshHostStatuses({ silent: true });
		return () => {
			archiveCleanupController?.abort();
			hostRefreshController?.abort();
			if (hostRefreshRetryTimer !== null) {
				clearTimeout(hostRefreshRetryTimer);
				hostRefreshRetryTimer = null;
			}
		};
	});
</script>

<svelte:head>
	<title>Settings · Mediaforce</title>
</svelte:head>

{#if settingsPayload}
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
		{archiveCleanupPending}
		{archiveCleanupError}
		{hostsLoadError}
		{hostActionKey}
		{getHostActionState}
		{updateHostActionPassword}
		{revealHostActionPassword}
		{primaryHostAction}
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
		{toggleScheduleDay}
		{toggleCapability}
		{saveSettings}
		{refreshHostStatuses}
		{prepareHost}
		{startHost}
		{resetHostTrust}
		{isClearingArchive}
		{clearArchiveCleanup}
		{refreshArchiveCleanup}
	/>
{:else}
	<div class="workstation-screen settings-screen settings-loading-screen">
		<section class="loading-shell" aria-label="Settings loading state">
			<div class="loading-copy-stack">
				<p class="system-label">Runtime settings</p>
				<h1 class="loading-title">
					{isLoadingSettings ? 'Loading workstation settings' : 'Runtime settings unavailable'}
				</h1>
				<p class="loading-copy">
					{settingsLoadError ??
						'The workstation shell is up. Waiting for the live runtime file and host snapshot to finish loading.'}
				</p>
			</div>
			<div class="loading-actions">
				<span class="loading-pill">
					{isLoadingSettings ? 'Waiting for runtime file' : 'Retry required'}
				</span>
				{#if settingsLoadError}
					<Button variant="secondary" onclick={() => loadSettings()}>Retry settings load</Button>
				{/if}
			</div>
		</section>

		<section class="system-strip" aria-label="Settings loading snapshot">
			<div class="system-cell accent-cell">
				<p class="system-label">Runtime contract</p>
				<p class="system-value">Editor warming up</p>
				<p class="system-detail">Live settings will appear here once the runtime file arrives.</p>
			</div>

			<div class="system-cell selection-state">
				<p class="system-label">Worker fleet</p>
				<p class="system-value">Polling host status</p>
				<p class="system-detail">Host readiness is still refreshing in the background.</p>
			</div>

			<div class="system-cell selection-state">
				<p class="system-label">Save state</p>
				<p class="system-value">Locked until load</p>
				<p class="system-detail">The editor appears as soon as the runtime data lands.</p>
			</div>
		</section>
	</div>
{/if}

<style>
	.settings-loading-screen {
		display: grid;
		gap: 0.95rem;
		padding: 0.25rem 0 1rem;
	}

	.loading-shell {
		display: flex;
		justify-content: space-between;
		gap: 1rem;
		align-items: start;
		padding: 1rem 1.1rem;
		border-radius: var(--radius-lg);
		border: 1px solid rgba(23, 35, 31, 0.08);
		background:
			linear-gradient(180deg, rgba(255, 255, 255, 0.96), rgba(248, 250, 252, 0.96)),
			var(--surface-1);
		box-shadow: 0 14px 32px rgba(15, 23, 42, 0.06);
	}

	.loading-copy-stack {
		display: grid;
		gap: 0.3rem;
		min-width: 0;
	}

	.loading-title {
		margin: 0;
		font-size: 1.4rem;
		font-weight: 750;
		line-height: 1.1;
		color: var(--ink);
	}

	.loading-copy {
		margin: 0;
		max-width: 58ch;
		color: var(--text-muted);
		line-height: 1.5;
	}

	.loading-actions {
		display: flex;
		align-items: center;
		gap: 0.7rem;
		flex-wrap: wrap;
		justify-content: flex-end;
	}

	.loading-pill {
		display: inline-flex;
		align-items: center;
		padding: 0.42rem 0.7rem;
		border-radius: 999px;
		border: 1px solid rgba(23, 35, 31, 0.12);
		background: rgba(23, 35, 31, 0.04);
		color: var(--ink-soft);
		font-size: 0.78rem;
		font-weight: 700;
		letter-spacing: 0.03em;
		text-transform: uppercase;
	}

	.settings-loading-screen .system-strip {
		grid-template-columns: repeat(3, minmax(0, 1fr));
	}

	@media (max-width: 960px) {
		.loading-shell {
			flex-direction: column;
		}

		.loading-actions {
			justify-content: flex-start;
		}

		.settings-loading-screen .system-strip {
			grid-template-columns: 1fr;
		}
	}
</style>
