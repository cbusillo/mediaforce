<script lang="ts">
	import { browser } from '$app/environment';
	import { onMount } from 'svelte';
	import type {
		HostRuntime,
		ScheduleProfile,
		SettingsHost,
		SettingsLibrary,
		SettingsPayload
	} from '$lib/api/types';
	import Button from '$lib/components/Button.svelte';
	import HostCard from '$lib/components/HostCard.svelte';
	import { formatGiB, hostSettingsAnchor } from '$lib/format';
	import {
		SCHEDULE_DAY_OPTIONS,
		scheduleWindowSummaryCopy,
		type ScheduleDayKey
	} from '$lib/settings/editor';

	let {
		settings,
		runtimeHostsByKey,
		libraries,
		remoteHosts,
		scheduleProfiles,
		transcodeRoot = $bindable(),
		isDirty,
		isSaving,
		isRefreshingHosts,
		archiveCleanupPending,
		archiveCleanupError,
		hostsLoadError,
		isClearingArchive,
		hostActionKey,
		getHostActionState,
		updateHostActionPassword,
		revealHostActionPassword,
		primaryHostAction,
		primaryHostActionLabel,
		primaryHostActionHelp,
		hasPrimaryHostAction,
		shouldShowHostActions,
		addLibrary,
		addHost,
		addSchedule,
		removeLibrary,
		removeHost,
		removeSchedule,
		toggleScheduleDay,
		toggleCapability,
		saveSettings,
		refreshHostStatuses,
		refreshArchiveCleanup,
		clearArchiveCleanup,
		prepareHost,
		startHost,
		resetHostTrust
	}: {
		settings: SettingsPayload;
		runtimeHostsByKey: Map<string, HostRuntime>;
		libraries: SettingsLibrary[];
		remoteHosts: SettingsHost[];
		scheduleProfiles: ScheduleProfile[];
		transcodeRoot: string;
		isDirty: boolean;
		isSaving: boolean;
		isRefreshingHosts: boolean;
		archiveCleanupPending: boolean;
		archiveCleanupError: string | null;
		hostsLoadError: string | null;
		isClearingArchive: boolean;
		hostActionKey: (host: SettingsHost, runtimeHost: HostRuntime | null, index: number) => string;
		getHostActionState: (hostKey: string) => {
			preparing: boolean;
			resettingTrust: boolean;
			password: string;
			showPassword: boolean;
		};
		updateHostActionPassword: (hostKey: string, value: string) => void;
		revealHostActionPassword: (hostKey: string) => void;
		primaryHostAction: (runtimeHost: HostRuntime, host: SettingsHost) => 'prepare' | 'start' | null;
		primaryHostActionLabel: (runtimeHost: HostRuntime, host: SettingsHost) => string;
		primaryHostActionHelp: (runtimeHost: HostRuntime, host: SettingsHost) => string;
		hasPrimaryHostAction: (runtimeHost: HostRuntime, host: SettingsHost) => boolean;
		shouldShowHostActions: (runtimeHost: HostRuntime, host: SettingsHost) => boolean;
		addLibrary: () => void;
		addHost: () => void;
		addSchedule: () => void;
		removeLibrary: (index: number) => void;
		removeHost: (index: number) => void;
		removeSchedule: (index: number) => void;
		toggleScheduleDay: (
			index: number,
			dayKey: ScheduleDayKey,
			target?: 'days_of_week' | 'all_day_days_of_week'
		) => void;
		toggleCapability: (index: number, capability: string) => void;
		saveSettings: () => Promise<void>;
		refreshHostStatuses: () => Promise<void>;
		refreshArchiveCleanup: (options?: {
			transcodeRoot?: string | null;
			silent?: boolean;
		}) => Promise<void>;
		clearArchiveCleanup: () => Promise<void>;
		prepareHost: (hostKey: string, runtimeHost: HostRuntime) => Promise<void>;
		startHost: (hostKey: string, runtimeHost: HostRuntime) => Promise<void>;
		resetHostTrust: (hostKey: string) => Promise<void>;
	} = $props();

	function handleHostActionPasswordInput(
		hostKey: string,
		event: Event & { currentTarget: EventTarget & HTMLInputElement }
	): void {
		updateHostActionPassword(hostKey, event.currentTarget.value);
	}

	function toggleAllowedLibrary(host: SettingsHost, libraryKey: string): void {
		host.allowed_libraries = host.allowed_libraries.includes(libraryKey)
			? host.allowed_libraries.filter((value) => value !== libraryKey)
			: [...host.allowed_libraries, libraryKey];
	}

	function jumpToSettingsAnchor(anchor: string): void {
		if (!browser) return;
		window.location.hash = anchor;
		document.getElementById(anchor)?.scrollIntoView({ block: 'start' });
	}

	function scheduleSummaryCopy(profile: ScheduleProfile): string {
		return scheduleWindowSummaryCopy(profile);
	}

	const settingsSections = [
		{ anchor: 'libraries', label: 'Libraries' },
		{ anchor: 'transcode', label: 'Transcode' },
		{ anchor: 'schedule-profiles', label: 'Schedules' },
		{ anchor: 'remote-hosts', label: 'Hosts' }
	] as const;
	const runtimeHosts = $derived.by(() => Array.from(runtimeHostsByKey.values()));
	const sectionSummary = {
		libraries: () => `${libraries.length} ${libraries.length === 1 ? 'library' : 'libraries'}`,
		schedules: () =>
			`${scheduleProfiles.length} ${scheduleProfiles.length === 1 ? 'schedule' : 'schedules'}`,
		hosts: () => `${remoteHosts.length} ${remoteHosts.length === 1 ? 'worker' : 'workers'}`
	};

	function fileTail(path: string, fallback: string): string {
		const tail = path.split('/').filter(Boolean).at(-1)?.trim();
		return tail || fallback;
	}

	const pathHighlights = $derived.by(() => [
		{
			label: 'Live runtime file',
			value: fileTail(settings.runtime_settings_path, 'runtime-settings.json')
		},
		{
			label: 'Checked-in defaults',
			value: fileTail(settings.repo_config_path, 'defaults.toml')
		}
	]);
	const draftArchiveRoot = $derived.by(() => {
		const cleanedRoot = transcodeRoot.trim().replace(/\/$/, '');
		return cleanedRoot ? `${cleanedRoot}/_replaced` : settings.archive_root;
	});
	const archiveCleanup = $derived(
		settings.archive_cleanup ?? {
			archive_root: '',
			file_count: 0,
			total_size_bytes: 0,
			has_cleanup: false
		}
	);
	const archiveCleanupUsesDraftPath = $derived.by(
		() => transcodeRoot.trim() !== settings.transcode_root.trim()
	);
	const archiveCleanupCopy = $derived.by(() => {
		if (archiveCleanupPending && !archiveCleanupUsesDraftPath) {
			return 'Checking archived originals';
		}
		if (archiveCleanupError && !archiveCleanupUsesDraftPath) {
			return 'Archive cleanup status unavailable';
		}
		if (archiveCleanupUsesDraftPath) {
			return 'Archive cleanup will target the current draft transcode path';
		}
		const count = Number(archiveCleanup.file_count ?? 0);
		if (count <= 0) return 'No archived originals waiting for cleanup';
		return `${count} archived original${count === 1 ? '' : 's'} waiting for cleanup`;
	});
	const archiveCleanupSizeCopy = $derived.by(() =>
		archiveCleanupPending && !archiveCleanupUsesDraftPath
			? 'Checking...'
			: archiveCleanupError && !archiveCleanupUsesDraftPath
				? 'Needs retry'
				: archiveCleanupUsesDraftPath
					? 'Draft path'
					: formatGiB(Number(archiveCleanup.total_size_bytes ?? 0), 2)
	);
	const readyRuntimeHostCount = $derived.by(
		() => runtimeHosts.filter((host) => host.available).length
	);
	const runtimeHostIssueCount = $derived.by(
		() => runtimeHosts.filter((host) => !host.available).length
	);
	const queueCapableHostCount = $derived.by(
		() => remoteHosts.filter((host) => host.capabilities.includes('encode_queue')).length
	);
	const archiveStateTone = $derived.by(() => {
		if (archiveCleanupError && !archiveCleanupUsesDraftPath) {
			return 'warning-state';
		}
		if (archiveCleanup.file_count > 0) {
			return 'warning-state';
		}
		if (archiveCleanupPending) {
			return 'selection-state';
		}
		return 'normal-state';
	});
	const hostStateTone = $derived.by(() => {
		if (hostsLoadError) {
			return 'warning-state';
		}
		if (runtimeHostIssueCount > 0) {
			return 'warning-state';
		}
		if (isRefreshingHosts) {
			return 'selection-state';
		}
		return 'normal-state';
	});
	const saveStateTone = $derived.by(() => (isDirty ? 'warning-state' : 'selection-state'));
	const saveStateLabel = $derived.by(() =>
		isDirty ? 'Unsaved changes are staged in the editor now.' : 'Live runtime file is in sync.'
	);
	let expandedHostAnchor = $state<string | null>(null);

	function syncExpandedHostAnchor() {
		if (!browser || !remoteHosts.length) return;
		const hash = window.location.hash.replace(/^#/, '').trim();
		expandedHostAnchor = hash || null;
	}

	onMount(() => {
		if (!browser) return;
		syncExpandedHostAnchor();
		const handleHashChange = () => syncExpandedHostAnchor();
		window.addEventListener('hashchange', handleHashChange);
		return () => window.removeEventListener('hashchange', handleHashChange);
	});

	$effect(() => {
		if (!browser || !remoteHosts.length) return;
		if (!window.location.hash) return;
		if (expandedHostAnchor) return;
		syncExpandedHostAnchor();
	});
</script>

<div class="workstation-screen settings-screen">
	<section class="system-strip" aria-label="Runtime settings state">
		<div class="system-cell accent-cell">
			<p class="system-label">Runtime contract</p>
			<p class="system-value">Configure the live machine state</p>
			<p class="system-detail">
				{sectionSummary.libraries()} · {sectionSummary.schedules()} · {sectionSummary.hosts()}.
				Settings writes go to the machine-local runtime file, not repo defaults.
			</p>
		</div>

		<div class={`system-cell ${archiveStateTone}`.trim()}>
			<p class="system-label">Archived originals</p>
			<p class="system-value">{archiveCleanupCopy}</p>
			<p class="system-detail">
				{#if archiveCleanupUsesDraftPath}
					Using the draft transcode path until you save.
				{:else}
					{formatGiB(Number(archiveCleanup.total_size_bytes ?? 0), 1)} waiting under the archive root.
				{/if}
			</p>
		</div>

		<div class={`system-cell ${hostStateTone}`.trim()}>
			<p class="system-label">Worker fleet</p>
			<p class="system-value">
				{readyRuntimeHostCount} ready / {remoteHosts.length} configured
			</p>
			<p class="system-detail">
				{#if hostsLoadError}
					Live host status is unavailable: {hostsLoadError}
				{:else if runtimeHostIssueCount > 0}
					{runtimeHostIssueCount} worker{runtimeHostIssueCount === 1 ? '' : 's'} need attention. {queueCapableHostCount}
					queue-capable host{queueCapableHostCount === 1 ? '' : 's'} configured.
				{:else if isRefreshingHosts}
					Refreshing live host state now.
				{:else}
					All visible workers report clean runtime status. {queueCapableHostCount} queue-capable host{queueCapableHostCount ===
					1
						? ''
						: 's'} configured.
				{/if}
			</p>
		</div>

		<div class={`system-cell ${saveStateTone}`.trim()}>
			<p class="system-label">Save state</p>
			<p class="system-value">{isDirty ? 'Changes waiting to write' : 'Runtime file synced'}</p>
			<p class="system-detail">{saveStateLabel}</p>
		</div>
	</section>

	<div class="settings-console-grid">
		<div class="settings-main-column">
			<section id="libraries" class="station-card settings-section" aria-label="Library settings">
				<div class="section-head compact">
					<div>
						<p class="section-label">Libraries</p>
						<h2 class="section-title small">Mounted media roots</h2>
						<p class="section-copy">
							These keys become the top-level prefixes used across queue, review, and folder
							workstation flows.
						</p>
					</div>
					<div class="section-actions-row">
						<span class="section-summary-badge">{sectionSummary.libraries()}</span>
						<Button variant="secondary" onclick={addLibrary}>Add Library</Button>
					</div>
				</div>
				<div class="row-stack">
					{#each libraries as library, index (`library-${index}`)}
						<details class="editor-card library-shell">
							<summary class="library-summary">
								<span class="summary-copy-block">
									<span class="eyebrow-copy">Library</span>
									<span class="library-summary-title">{library.key || 'Untitled library'}</span>
									<span class="muted-copy library-summary-path"
										>{library.path || 'Set a mounted path'}</span
									>
								</span>
								<span class="summary-action-block">
									<span class="summary-action-copy">Edit</span>
									<span
										class="library-color-chip"
										style={`--library-swatch: ${library.color || '#d1d5db'}`}
									></span>
								</span>
							</summary>
							<div class="library-editor">
								<label class="field-block">
									<span class="eyebrow-copy field-label-hidden">Library name</span>
									<input bind:value={library.key} placeholder="movies" />
								</label>
								<label class="field-block">
									<span class="eyebrow-copy field-label-hidden">Mounted path</span>
									<input bind:value={library.path} placeholder="/Volumes/media/movies" />
								</label>
								<label class="field-block color-field">
									<span class="eyebrow-copy field-label-hidden">Colors</span>
									<span class="color-control">
										<input
											type="color"
											bind:value={library.color}
											aria-label="Library accent color"
										/>
									</span>
								</label>
								<div class="editor-actions compact-actions">
									<button
										type="button"
										class="icon-button danger"
										aria-label="Remove library"
										title="Remove library"
										onclick={() => removeLibrary(index)}
									>
										<svg viewBox="0 0 24 24" aria-hidden="true">
											<path
												d="M9 3h6l1 2h4v2H4V5h4l1-2Zm1 7h2v7h-2v-7Zm4 0h2v7h-2v-7ZM7 10h2v7H7v-7Zm-1 10h12a2 2 0 0 0 2-2V8H4v10a2 2 0 0 0 2 2Z"
											/>
										</svg>
									</button>
								</div>
							</div>
						</details>
					{/each}
				</div>
			</section>

			<section id="transcode" class="station-card settings-section" aria-label="Transcode settings">
				<div class="section-head compact">
					<div>
						<p class="section-label">Transcode</p>
						<h2 class="section-title small">Scratch and replacement root</h2>
						<p class="section-copy">
							Archive cleanup is derived from this root so scratch space, rollback media, and the
							live transcode target stay in one operator flow.
						</p>
					</div>
					<div class="section-actions-row">
						<span class="section-summary-badge">{transcodeRoot || '/Volumes/media/transcode'}</span>
					</div>
				</div>
				<div class="transcode-shell">
					<label class="field-block">
						<span class="eyebrow-copy">Transcode root</span>
						<input bind:value={transcodeRoot} placeholder="/Volumes/media/transcode" />
					</label>
					<div class="embedded-path-card">
						<p class="eyebrow-copy">Derived archive path</p>
						<p class="mono-copy">{settings.archive_root}</p>
					</div>
					<div class="archive-cleanup-card">
						<div class="archive-cleanup-copy">
							<p class="eyebrow-copy">Archived originals</p>
							<p class="transcode-summary-title">{archiveCleanupCopy}</p>
							<p class="muted-copy">
								Rollback copies live under `{draftArchiveRoot}` until you clear them.
							</p>
							{#if archiveCleanupError && !archiveCleanupUsesDraftPath}
								<p class="warning-copy">{archiveCleanupError}</p>
							{/if}
							{#if archiveCleanupUsesDraftPath}
								<p class="muted-copy">
									Save settings first if you want the on-screen backup counts to refresh for this
									new path.
								</p>
							{/if}
						</div>
						<div class="archive-cleanup-side">
							<span class="transcode-summary-chip">{archiveCleanupSizeCopy}</span>
							{#if archiveCleanupError && !archiveCleanupUsesDraftPath}
								<Button
									variant="secondary"
									onclick={() => refreshArchiveCleanup({ silent: false })}
								>
									Retry status
								</Button>
							{/if}
							<Button
								variant="danger"
								loading={isClearingArchive}
								disabled={!transcodeRoot.trim() ||
									(archiveCleanupPending && !archiveCleanupUsesDraftPath) ||
									(Boolean(archiveCleanupError) && !archiveCleanupUsesDraftPath) ||
									(!archiveCleanupUsesDraftPath && archiveCleanup.file_count <= 0)}
								onclick={clearArchiveCleanup}
							>
								Clear archived originals
							</Button>
						</div>
					</div>
				</div>
			</section>

			<section
				id="schedule-profiles"
				class="station-card settings-section"
				aria-label="Schedule profiles"
			>
				<div class="section-head compact">
					<div>
						<p class="section-label">Schedules</p>
						<h2 class="section-title small">Reusable worker windows</h2>
						<p class="section-copy">
							Define queue windows plus all-day exceptions, then assign those profiles to each
							worker.
						</p>
					</div>
					<div class="section-actions-row">
						<span class="section-summary-badge">{sectionSummary.schedules()}</span>
						<Button variant="secondary" onclick={addSchedule}>Add Schedule</Button>
					</div>
				</div>
				<p class="muted-copy schedule-section-note">
					The machine key is how hosts remember a schedule. Rename it carefully after workers are
					assigned.
				</p>
				<div class="field-grid-header schedule-grid-header" aria-hidden="true">
					<span>Machine key</span>
					<span>Label</span>
					<span>Start</span>
					<span>End</span>
					<span>Actions</span>
				</div>
				<div class="row-stack">
					{#each scheduleProfiles as profile, index (`profile-${profile.index || index}`)}
						<details class="editor-card schedule-shell">
							<summary class="schedule-summary">
								<span class="summary-copy-block">
									<span class="eyebrow-copy">Schedule</span>
									<span class="schedule-summary-title"
										>{profile.label || profile.key || 'Untitled schedule'}</span
									>
								</span>
								<span class="summary-action-block">
									<span class="summary-action-copy">Edit</span>
									<span class="schedule-summary-chip">{scheduleSummaryCopy(profile)}</span>
								</span>
							</summary>
							<div class="schedule-editor">
								<label class="field-block">
									<span class="eyebrow-copy field-label-hidden">Machine key</span>
									<input bind:value={profile.key} placeholder="quiet_hours" />
								</label>
								<label class="field-block">
									<span class="eyebrow-copy field-label-hidden">Label</span>
									<input bind:value={profile.label} placeholder="Quiet Hours" />
								</label>
								<label class="field-block">
									<span class="eyebrow-copy field-label-hidden">Start</span>
									<input
										type="number"
										min="0"
										max="23"
										step="1"
										bind:value={profile.start_hour}
										placeholder="0-23"
									/>
								</label>
								<label class="field-block">
									<span class="eyebrow-copy field-label-hidden">End</span>
									<input
										type="number"
										min="0"
										max="23"
										step="1"
										bind:value={profile.end_hour}
										placeholder="0-23"
									/>
								</label>
								<div class="editor-actions compact-actions">
									<button
										type="button"
										class="icon-button danger"
										aria-label="Remove schedule"
										title="Remove schedule"
										onclick={() => removeSchedule(index)}
									>
										<svg viewBox="0 0 24 24" aria-hidden="true">
											<path
												d="M9 3h6l1 2h4v2H4V5h4l1-2Zm1 7h2v7h-2v-7Zm4 0h2v7h-2v-7ZM7 10h2v7H7v-7Zm-1 10h12a2 2 0 0 0 2-2V8H4v10a2 2 0 0 0 2 2Z"
											/>
										</svg>
									</button>
								</div>
								<fieldset class="field-block schedule-days-field">
									<legend class="eyebrow-copy">Window days</legend>
									<div class="weekday-pill-row">
										{#each SCHEDULE_DAY_OPTIONS as option (option.key)}
											<button
												type="button"
												class:active={profile.days_of_week.includes(option.key)}
												class="weekday-pill"
												aria-pressed={profile.days_of_week.includes(option.key)}
												title={option.label}
												onclick={() => toggleScheduleDay(index, option.key, 'days_of_week')}
											>
												{option.shortLabel}
											</button>
										{/each}
									</div>
								</fieldset>
								<fieldset class="field-block schedule-days-field">
									<legend class="eyebrow-copy">All day</legend>
									<div class="weekday-pill-row">
										{#each SCHEDULE_DAY_OPTIONS as option (option.key)}
											<button
												type="button"
												class:active={profile.all_day_days_of_week.includes(option.key)}
												class="weekday-pill"
												aria-pressed={profile.all_day_days_of_week.includes(option.key)}
												title={`${option.label} all day`}
												onclick={() => toggleScheduleDay(index, option.key, 'all_day_days_of_week')}
											>
												{option.shortLabel}
											</button>
										{/each}
									</div>
								</fieldset>
							</div>
						</details>
					{/each}
				</div>
			</section>

			<section id="remote-hosts" class="station-card settings-section" aria-label="Remote hosts">
				<div class="legacy-anchor" id="remote-workers" aria-hidden="true"></div>
				<div class="section-head compact">
					<div>
						<p class="section-label">Hosts</p>
						<h2 class="section-title small">Remote workers</h2>
						<p class="section-copy">
							Edit queue policy, scheduling, and live-host setup from the same worker row so machine
							state stays explicit.
						</p>
					</div>
					<div class="section-actions-row">
						<span class="section-summary-badge">{sectionSummary.hosts()}</span>
						<button
							type="button"
							class="refresh-status-button"
							disabled={isRefreshingHosts}
							onclick={refreshHostStatuses}
						>
							{isRefreshingHosts ? 'Refreshing...' : 'Refresh Status'}
						</button>
						<Button variant="secondary" onclick={addHost}>Add Host</Button>
					</div>
				</div>
				<div class="row-stack">
					{#if isRefreshingHosts && runtimeHostsByKey.size === 0}
						<p class="muted-copy">Loading live host status…</p>
					{:else if hostsLoadError}
						<p class="muted-copy">Live host status unavailable: {hostsLoadError}</p>
					{/if}
					{#each remoteHosts as host, index (`host-${host.index || index}`)}
						{@const runtimeHost = runtimeHostsByKey.get(host.host) ?? null}
						{@const runtimeHostKey = hostActionKey(host, runtimeHost, index)}
						{@const runtimeActionState = getHostActionState(runtimeHostKey)}
						{@const primaryActionKind = runtimeHost ? primaryHostAction(runtimeHost, host) : null}
						{@const showPrimaryHostAction = runtimeHost
							? hasPrimaryHostAction(runtimeHost, host)
							: false}
						{@const hostAnchor = hostSettingsAnchor(host.host || `host-${index}`)}
						{@const hostSummaryLabel = host.label || 'Untitled host'}
						{@const hostSummaryAddress =
							host.host || 'Set an SSH target to bring this host online.'}
						{@const hostSummaryStatus = runtimeHost?.available
							? `${runtimeHost.label} ready`
							: runtimeHost?.message || 'No live status yet'}
						<div id={hostAnchor} class="editor-card host-card-editor">
							<details class="host-editor-shell" open={expandedHostAnchor === hostAnchor}>
								<summary class="host-head">
									<span class="host-summary-main">
										<span class="eyebrow-copy">Remote worker</span>
										<span class="host-summary-title">{hostSummaryLabel}</span>
										<span class="muted-copy">{hostSummaryAddress}</span>
									</span>
									<span class="host-summary-side">
										<span class="summary-action-copy">Edit</span>
										<span class="host-summary-chip">{hostSummaryStatus}</span>
									</span>
								</summary>
								<div class="host-editor-layout">
									<div class="two-col">
										<p class="form-subhead">Connection</p>
										<label class="field-block"
											><span class="eyebrow-copy">Label</span><input
												bind:value={host.label}
												placeholder="M4 Studio"
											/></label
										>
										<label class="field-block"
											><span class="eyebrow-copy">SSH host</span><input
												bind:value={host.host}
												placeholder="cbusillo@localhost"
											/></label
										>
										<p class="form-subhead">Queue behavior</p>
										<label class="field-block host-schedule-field"
											><span class="eyebrow-copy">Schedule profile</span><span class="select-shell"
												><select bind:value={host.schedule_profile}
													>{#each settings.schedule_profile_options as option (option.key)}<option
															value={option.key}>{option.label}</option
														>{/each}</select
												></span
											></label
										>
										<label class="field-block"
											><span class="eyebrow-copy">Priority</span><input
												type="number"
												min="0"
												step="1"
												bind:value={host.priority}
											/></label
										>
										<label class="field-block"
											><span class="eyebrow-copy">Parallel encodes</span><input
												type="number"
												min="1"
												step="1"
												bind:value={host.max_parallel_encodes}
											/></label
										>
										<label class="field-block"
											><span class="eyebrow-copy">Host staging root</span><input
												bind:value={host.staging_root}
												placeholder="Defaults to global transcode root"
											/></label
										>
										<p class="form-subhead">Library access</p>
										<label class="field-block host-json-field"
											><span class="eyebrow-copy">Allowed libraries</span>
											<span class="library-allowlist" aria-label="Allowed libraries for this host">
												<button
													type="button"
													class={`library-allow-pill ${host.allowed_libraries.length === 0 ? 'active' : ''}`.trim()}
													onclick={() => (host.allowed_libraries = [])}
												>
													All libraries
												</button>
												{#each libraries.filter( (library) => library.key.trim() ) as library (`${host.index}-${library.key}`)}
													<button
														type="button"
														class={`library-allow-pill ${host.allowed_libraries.includes(library.key) ? 'active' : ''}`.trim()}
														onclick={() => toggleAllowedLibrary(host, library.key)}
													>
														{library.key}
													</button>
												{/each}
											</span></label
										>
										<details class="host-advanced-shell host-json-field">
											<summary>Advanced worker settings</summary>
											<div class="two-col advanced-host-grid">
												<label class="field-block"
													><span class="eyebrow-copy">Repo path</span><input
														bind:value={host.repo_path}
														placeholder="/Users/.../mediaforce"
													/></label
												>
												<label class="field-block"
													><span class="eyebrow-copy">Wake MAC</span><input
														bind:value={host.wake_mac}
														placeholder="Optional"
													/></label
												>
												<label class="field-block"
													><span class="eyebrow-copy">Start command</span><input
														bind:value={host.start_command}
														placeholder="ssh prox-main.shiny pct start 103"
													/></label
												>
												<label class="field-block"
													><span class="eyebrow-copy">Stop command</span><input
														bind:value={host.stop_command}
														placeholder="ssh prox-main.shiny pct shutdown 103"
													/></label
												>
												<label class="field-block"
													><span class="eyebrow-copy">Start timeout sec</span><input
														type="number"
														min="1"
														step="1"
														bind:value={host.start_timeout_seconds}
													/></label
												>
												<label class="field-block"
													><span class="eyebrow-copy">Media access</span><span class="select-shell"
														><select bind:value={host.media_access}
															><option value="mounted">Mounted paths</option><option value="stream"
																>Two-way stream</option
															></select
														></span
													></label
												>
												<label class="field-block host-json-field advanced-textarea-field"
													><span class="eyebrow-copy">Library path overrides</span><textarea
														bind:value={host.source_roots_json}
														rows="5"
														placeholder={`{
  "movies": "/mnt/media/movies",
  "tv": "/mnt/media/tv"
}`}
													></textarea></label
												>
											</div>
										</details>
									</div>
									{#if runtimeHost}
										<div class="host-runtime-column">
											<p class="eyebrow-copy">Live Status</p>
											<HostCard host={runtimeHost} />
											{#if shouldShowHostActions(runtimeHost, host)}
												<div class="host-actions-panel">
													<div class="host-actions-head">
														<p class="eyebrow-copy">Remote Actions</p>
														{#if isDirty}
															<p class="muted-copy">
																Save host edits before running remote actions.
															</p>
														{:else if showPrimaryHostAction}
															<p class="muted-copy">{primaryHostActionHelp(runtimeHost, host)}</p>
														{/if}
													</div>
													{#if showPrimaryHostAction}
														{#if runtimeHost.setup_requires_password || runtimeActionState.showPassword}
															<label class="field-block inline-password-field">
																<span class="eyebrow-copy">Remote account password</span>
																<input
																	type="password"
																	value={runtimeActionState.password}
																	oninput={(event) =>
																		handleHostActionPasswordInput(runtimeHostKey, event)}
																	placeholder="Only used for this setup run"
																/>
															</label>
														{/if}
														<div class="host-actions-row">
															<Button
																variant="secondary"
																loading={runtimeActionState.preparing}
																disabled={isDirty}
																onclick={() =>
																	primaryActionKind === 'start'
																		? startHost(runtimeHostKey, runtimeHost)
																		: prepareHost(runtimeHostKey, runtimeHost)}
																>{primaryHostActionLabel(runtimeHost, host)}</Button
															>
															{#if runtimeHost.setup_requires_password && !runtimeActionState.showPassword}
																<Button
																	variant="ghost"
																	disabled={isDirty}
																	onclick={() => revealHostActionPassword(runtimeHostKey)}
																	>Add Password</Button
																>
															{/if}
															{#if runtimeHost.trust_reset_supported}
																<Button
																	variant="ghost"
																	loading={runtimeActionState.resettingTrust}
																	disabled={isDirty || runtimeActionState.preparing}
																	onclick={() => resetHostTrust(runtimeHostKey)}
																	>Reset SSH Trust</Button
																>
															{/if}
														</div>
													{:else if runtimeHost.trust_reset_supported}
														<div class="host-actions-row">
															<Button
																variant="ghost"
																loading={runtimeActionState.resettingTrust}
																disabled={isDirty}
																onclick={() => resetHostTrust(runtimeHostKey)}
																>Reset SSH Trust</Button
															>
														</div>
													{/if}
												</div>
											{/if}
										</div>
									{/if}
								</div>
								<div class="capability-row">
									{#each settings.host_capability_options as capability (capability.key)}
										<label
											class={`capability-pill ${host.capabilities.includes(capability.key) ? 'checked' : ''}`.trim()}
										>
											<input
												type="checkbox"
												class="visually-hidden"
												checked={host.capabilities.includes(capability.key)}
												onchange={() => toggleCapability(index, capability.key)}
											/>
											<span class="capability-mark" aria-hidden="true">✓</span>
											<span class="capability-label">{capability.label}</span>
										</label>
									{/each}
								</div>
								<div class="host-remove-row">
									<button
										type="button"
										class="icon-button danger"
										aria-label="Remove host"
										title="Remove host"
										onclick={() => removeHost(index)}
									>
										<svg viewBox="0 0 24 24" aria-hidden="true">
											<path
												d="M9 3h6l1 2h4v2H4V5h4l1-2Zm1 7h2v7h-2v-7Zm4 0h2v7h-2v-7ZM7 10h2v7H7v-7Zm-1 10h12a2 2 0 0 0 2-2V8H4v10a2 2 0 0 0 2 2Z"
											/>
										</svg>
									</button>
								</div>
							</details>
						</div>
					{/each}
				</div>
			</section>
		</div>

		<aside class="settings-side-rail" aria-label="Settings side rail">
			<div class="side-column-sticky">
				<section class="station-card rail-card settings-rail-card" aria-label="Settings navigation">
					<div class="section-head compact rail-head">
						<div>
							<p class="section-label">Console map</p>
							<h2 class="section-title small">Jump between runtime sections</h2>
							<p class="section-copy">
								The same operator shell now frames Settings, so navigation, save state, and next
								actions stay visible while you edit.
							</p>
						</div>
					</div>
					<nav class="settings-jump-nav" aria-label="Settings sections">
						{#each settingsSections as section (section.anchor)}
							<button
								type="button"
								class="settings-jump-link"
								onclick={() => jumpToSettingsAnchor(section.anchor)}
							>
								{section.label}
							</button>
						{/each}
						{#if isDirty}
							<button
								type="button"
								class="settings-jump-link attention-link"
								onclick={() => jumpToSettingsAnchor('save-settings')}
							>
								Save checkpoint
							</button>
						{/if}
					</nav>
					<p class={`settings-jump-status ${isDirty ? 'dirty' : ''}`.trim()}>
						{isDirty ? 'Unsaved changes waiting' : 'All changes saved'}
					</p>
					{#if isDirty}
						<Button loading={isSaving} onclick={saveSettings}>Save Runtime Settings</Button>
					{/if}
				</section>

				<section class="station-card rail-card settings-rail-card" aria-label="Runtime files">
					<div class="section-head compact rail-head">
						<div>
							<p class="section-label">Runtime files</p>
							<h2 class="section-title small">Live machine contract</h2>
							<p class="section-copy">
								Edits write to the runtime file below. Repo defaults remain the baseline reference.
							</p>
						</div>
					</div>
					<div class="path-highlight-row">
						{#each pathHighlights as item (item.label)}
							<div class="path-highlight-chip">
								<span class="eyebrow-copy">{item.label}</span>
								<strong>{item.value}</strong>
							</div>
						{/each}
					</div>
					<details class="path-disclosure">
						<summary>Show full file paths</summary>
						<p class="mono-copy">Runtime file: {settings.runtime_settings_path}</p>
						<p class="mono-copy">Repo defaults: {settings.repo_config_path}</p>
					</details>
				</section>

				<section class="station-card rail-card settings-rail-card" aria-label="Operator guidance">
					<div class="section-head compact rail-head">
						<div>
							<p class="section-label">Operator flow</p>
							<h2 class="section-title small">Edit, verify, then save</h2>
						</div>
					</div>
					<ul class="rail-guidance-list">
						<li>
							Refresh host status after changing worker connectivity, schedules, or startup details.
						</li>
						<li>
							Save before using remote setup actions so runtime behavior matches the edited form.
						</li>
						<li>
							Clear archived originals only after promoted outputs are trusted and rollback media is
							no longer needed.
						</li>
					</ul>
					<div class="rail-actions">
						<button
							type="button"
							class="refresh-status-button"
							disabled={isRefreshingHosts}
							onclick={refreshHostStatuses}
						>
							{isRefreshingHosts ? 'Refreshing...' : 'Refresh Host Status'}
						</button>
						{#if archiveCleanupError && !archiveCleanupUsesDraftPath}
							<Button variant="ghost" onclick={() => refreshArchiveCleanup({ silent: false })}>
								Retry cleanup status
							</Button>
						{/if}
					</div>
				</section>
			</div>
		</aside>
	</div>
</div>

{#if isDirty}
	<div id="save-settings" class="settings-save-bar dirty">
		<div class="settings-save-shell">
			<div>
				<p class="eyebrow-copy">Unsaved Changes</p>
				<p class="muted-copy">Unsaved changes are ready to write to the runtime settings file.</p>
			</div>
			<Button loading={isSaving} onclick={saveSettings}>Save Runtime Settings</Button>
		</div>
	</div>
{/if}

<style>
	.row-stack {
		display: grid;
		gap: var(--space-3);
	}

	.workstation-screen {
		display: grid;
		gap: 1rem;
		padding: 0.25rem 0 1rem;
		position: relative;
		isolation: isolate;
		z-index: 0;
	}

	.workstation-screen::before {
		content: '';
		position: fixed;
		inset: 0;
		z-index: -2;
		pointer-events: none;
		background: #0b1014;
	}

	.workstation-screen::after {
		display: none;
	}

	.system-strip {
		display: grid;
		grid-template-columns: repeat(4, minmax(0, 1fr));
		gap: 1rem;
	}

	.system-cell,
	.station-card {
		position: relative;
		border: 1px solid rgba(148, 163, 184, 0.18);
		background: rgba(15, 20, 27, 0.94);
		box-shadow: 0 18px 38px rgba(2, 6, 23, 0.2);
		overflow: hidden;
	}

	.system-cell::before {
		content: '';
		position: absolute;
		inset: 0 0 auto;
		height: 2px;
		background: rgba(56, 189, 248, 0.82);
	}

	.system-cell {
		padding: 1rem 1.1rem;
		min-height: 8.4rem;
	}

	.accent-cell {
		background: rgba(13, 33, 42, 0.94);
	}

	.normal-state {
		background: rgba(15, 20, 27, 0.94);
	}

	.warning-state {
		border-color: rgba(249, 115, 22, 0.28);
		background: rgba(58, 26, 13, 0.94);
	}

	.warning-state::before {
		background: rgba(251, 146, 60, 0.92);
	}

	.selection-state {
		border-color: rgba(45, 212, 191, 0.24);
		background: rgba(10, 36, 38, 0.94);
	}

	.selection-state::before {
		background: rgba(45, 212, 191, 0.88);
	}

	.system-label,
	.section-label {
		margin: 0;
		font-size: 0.72rem;
		font-weight: 800;
		letter-spacing: 0.16em;
		text-transform: uppercase;
		color: rgba(148, 163, 184, 0.88);
	}

	.system-value,
	.section-title,
	.transcode-summary-title,
	.library-summary-title,
	.schedule-summary-title,
	.host-summary-title {
		margin: 0;
		color: #f8fafc;
	}

	.system-value {
		margin-top: 0.4rem;
		font-size: 1.2rem;
		font-weight: 700;
		line-height: 1.25;
	}

	.system-detail,
	.section-copy,
	.path-disclosure,
	.path-disclosure p,
	.rail-guidance-list,
	.rail-guidance-list li,
	.schedule-section-note,
	.warning-copy,
	.host-summary-main .muted-copy,
	.path-card p,
	.archive-cleanup-copy .muted-copy {
		margin: 0;
		color: rgba(226, 232, 240, 0.74);
		line-height: 1.5;
	}

	.section-title {
		margin-top: 0.28rem;
		font-size: 1rem;
		font-weight: 700;
		line-height: 1.28;
	}

	.section-title.small {
		font-size: 1rem;
	}

	.station-card {
		padding: 1.1rem;
	}

	.settings-console-grid {
		display: grid;
		grid-template-columns: minmax(0, 1.7fr) minmax(21rem, 0.92fr);
		gap: 1rem;
		align-items: start;
	}

	.settings-main-column,
	.settings-side-rail {
		display: grid;
		gap: 1rem;
		min-width: 0;
	}

	.side-column-sticky {
		display: grid;
		gap: 1rem;
		align-content: start;
		min-width: 0;
	}

	@media (min-width: 961px) {
		.side-column-sticky {
			position: sticky;
			top: 5.9rem;
			max-height: calc(100dvh - 5.9rem - 1rem);
			overflow-y: auto;
			overflow-x: clip;
			scrollbar-gutter: stable;
			padding-right: 0.15rem;
			z-index: 5;
		}
	}

	.settings-section,
	.settings-rail-card {
		display: grid;
		gap: 0.95rem;
	}

	.section-head {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		gap: 1rem;
		align-items: start;
		margin-bottom: 0.15rem;
	}

	.section-head.compact {
		margin-bottom: 0.1rem;
	}

	.rail-head {
		margin-bottom: 0;
	}

	.settings-jump-nav {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: 0.7rem;
	}

	.settings-jump-link,
	.settings-jump-status {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		padding: 0.62rem 0.8rem;
		border-radius: 0;
		font-size: 0.82rem;
		font-weight: 700;
		text-decoration: none;
	}

	.settings-jump-link {
		background: rgba(15, 23, 42, 0.82);
		border: 1px solid rgba(148, 163, 184, 0.16);
		color: rgba(226, 232, 240, 0.84);
		font: inherit;
		cursor: pointer;
	}

	.settings-jump-link:hover {
		border-color: rgba(56, 189, 248, 0.38);
		color: #f8fafc;
	}

	.settings-jump-link.attention-link {
		border-color: rgba(251, 146, 60, 0.28);
		background: rgba(67, 20, 7, 0.78);
	}

	.settings-jump-status {
		justify-content: flex-start;
		background: rgba(15, 23, 42, 0.7);
		border: 1px solid rgba(148, 163, 184, 0.16);
		color: rgba(226, 232, 240, 0.74);
	}

	.settings-jump-status.dirty {
		background: rgba(67, 20, 7, 0.78);
		border-color: rgba(251, 146, 60, 0.28);
		color: #fed7aa;
	}

	.transcode-shell {
		display: grid;
		gap: 0.85rem;
	}

	.summary-copy-block {
		display: grid;
		gap: 0.15rem;
	}

	.transcode-action-block {
		justify-content: flex-start;
		flex-wrap: wrap;
	}

	.transcode-summary-title {
		font-size: 1rem;
		font-weight: 700;
		line-height: 1.3;
	}

	.transcode-summary-chip {
		display: inline-flex;
		align-items: center;
		padding: 0.45rem 0.7rem;
		border-radius: 0;
		background: rgba(15, 23, 42, 0.82);
		border: 1px solid rgba(148, 163, 184, 0.16);
		font-size: 0.82rem;
		font-weight: 700;
		color: rgba(226, 232, 240, 0.84);
		max-width: 18rem;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.archive-cleanup-card {
		display: flex;
		justify-content: space-between;
		gap: 1rem;
		align-items: start;
		padding: 0.95rem 1rem;
		border-radius: 0;
		background: rgba(58, 26, 13, 0.72);
		border: 1px solid rgba(249, 115, 22, 0.22);
	}

	.archive-cleanup-copy,
	.archive-cleanup-side {
		display: grid;
		gap: 0.45rem;
	}

	.archive-cleanup-side {
		justify-items: start;
	}

	.warning-copy {
		color: #fdba74;
		font-weight: 600;
	}

	.path-highlight-row {
		display: grid;
		gap: 0.65rem;
	}

	.path-highlight-chip {
		display: grid;
		gap: 0.12rem;
		padding: 0.68rem 0.78rem;
		border-radius: 0;
		background: rgba(15, 23, 42, 0.82);
		border: 1px solid rgba(148, 163, 184, 0.16);
	}

	.path-highlight-chip strong {
		font-size: 0.98rem;
		line-height: 1.3;
		color: #f8fafc;
	}

	.path-disclosure {
		display: grid;
		gap: 0.45rem;
	}

	.path-disclosure summary {
		cursor: pointer;
		font-size: 0.86rem;
		font-weight: 700;
		color: #7dd3fc;
	}

	.section-actions-row {
		display: flex;
		justify-content: space-between;
		gap: 1rem;
		align-items: center;
		flex-wrap: wrap;
	}

	.section-summary-badge {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		padding: 0.42rem 0.72rem;
		border-radius: 0;
		background: rgba(15, 23, 42, 0.82);
		border: 1px solid rgba(148, 163, 184, 0.16);
		font-size: 0.8rem;
		font-weight: 700;
		color: rgba(226, 232, 240, 0.78);
	}

	.section-summary-badge.warn {
		background: rgba(67, 20, 7, 0.78);
		border-color: rgba(251, 146, 60, 0.28);
		color: #fed7aa;
	}

	.field-grid-header {
		display: grid;
		gap: 0.85rem;
		padding: 0 0.2rem;
		font-size: 0.72rem;
		font-weight: 800;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: rgba(148, 163, 184, 0.88);
	}

	.library-grid-header {
		grid-template-columns: repeat(3, minmax(0, 1fr)) auto;
	}

	.library-shell {
		display: grid;
		gap: 0.85rem;
	}

	.library-summary {
		display: flex;
		justify-content: space-between;
		gap: 1rem;
		align-items: start;
		cursor: pointer;
	}

	.summary-action-block {
		display: flex;
		align-items: center;
		gap: 0.65rem;
	}

	.summary-copy-block {
		display: grid;
		gap: 0.18rem;
	}

	.summary-action-copy {
		font-size: 0.8rem;
		font-weight: 700;
		color: rgba(148, 163, 184, 0.82);
	}

	.library-summary-title,
	.schedule-summary-title,
	.host-summary-title,
	.library-summary-path {
		margin: 0;
	}

	.library-summary-title,
	.schedule-summary-title,
	.host-summary-title {
		font-size: 1rem;
		font-weight: 700;
		line-height: 1.25;
	}

	.library-summary-path {
		margin-top: 0.18rem;
	}

	.library-color-chip {
		display: inline-flex;
		width: 2.5rem;
		height: 2.5rem;
		border-radius: 0;
		background: var(--library-swatch);
		border: 1px solid rgba(148, 163, 184, 0.22);
	}

	.schedule-grid-header {
		grid-template-columns:
			minmax(0, 1.2fr) minmax(0, 1.2fr) minmax(84px, 0.7fr) minmax(84px, 0.7fr)
			auto;
	}

	.schedule-section-note {
		margin: -0.2rem 0 0;
	}

	.field-grid-header span:last-child {
		text-align: right;
	}

	.editor-card,
	.embedded-path-card {
		padding: 1rem;
		border-radius: 0;
		background: rgba(9, 14, 22, 0.88);
		border: 1px solid rgba(148, 163, 184, 0.16);
	}

	.library-editor,
	.schedule-editor {
		display: grid;
		grid-template-columns:
			minmax(0, 1.2fr) minmax(0, 1.2fr) minmax(84px, 0.7fr) minmax(84px, 0.7fr)
			auto;
		gap: 0.85rem;
		align-items: end;
	}

	.schedule-days-field {
		border: 0;
		padding: 0;
		margin: 0;
		min-width: 0;
		grid-column: 1 / span 4;
	}

	.weekday-pill-row {
		display: flex;
		flex-wrap: wrap;
		gap: 0.4rem;
	}

	.weekday-pill {
		border: 1px solid rgba(148, 163, 184, 0.16);
		background: rgba(15, 23, 42, 0.82);
		color: rgba(226, 232, 240, 0.74);
		border-radius: 0;
		padding: 0.48rem 0.72rem;
		font: inherit;
		font-size: 0.84rem;
		font-weight: 700;
		cursor: pointer;
	}

	.weekday-pill.active {
		background: rgba(8, 47, 73, 0.82);
		border-color: rgba(56, 189, 248, 0.24);
		color: #dbeafe;
	}

	.field-block {
		display: grid;
		gap: 0.4rem;
	}

	.form-subhead {
		grid-column: span 2;
		font-size: 0.98rem;
		font-weight: 700;
		line-height: 1.3;
		color: #f8fafc;
		padding-top: 0.2rem;
	}

	.field-label-hidden {
		position: absolute;
		width: 1px;
		height: 1px;
		padding: 0;
		margin: -1px;
		overflow: hidden;
		clip: rect(0, 0, 0, 0);
		white-space: nowrap;
		border: 0;
	}

	.field-block input,
	.field-block textarea,
	.select-shell select {
		width: 100%;
		padding: 0.72rem 0.82rem;
		border-radius: 0;
		border: 1px solid rgba(148, 163, 184, 0.16);
		background: rgba(15, 23, 42, 0.92);
		font: inherit;
		color: #f8fafc;
	}

	.field-block textarea::placeholder,
	.field-block input::placeholder {
		color: rgba(148, 163, 184, 0.72);
	}

	.select-shell select {
		appearance: none;
	}

	.select-shell {
		position: relative;
		display: block;
	}

	.select-shell::after {
		content: '▾';
		position: absolute;
		right: 0.85rem;
		top: 50%;
		transform: translateY(-50%);
		pointer-events: none;
		font-size: 0.82rem;
		color: rgba(148, 163, 184, 0.82);
	}

	.select-shell select {
		padding-right: 2.2rem;
		cursor: pointer;
	}

	.color-control input {
		padding: 0;
		height: 2.75rem;
	}

	.editor-actions {
		display: flex;
		justify-content: flex-end;
	}

	.icon-button {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 2.75rem;
		height: 2.75rem;
		border-radius: 0;
		border: 1px solid rgba(148, 163, 184, 0.16);
		background: rgba(15, 23, 42, 0.82);
		cursor: pointer;
		color: rgba(226, 232, 240, 0.82);
	}

	.icon-button svg {
		width: 1rem;
		height: 1rem;
		fill: currentColor;
	}

	.icon-button.danger {
		color: #fdba74;
	}

	.host-card-editor {
		display: grid;
		gap: 0.7rem;
		padding: 0.8rem 0.95rem;
	}

	.host-editor-shell {
		display: grid;
		gap: 0.8rem;
	}

	.host-head {
		display: flex;
		justify-content: space-between;
		gap: 0.85rem;
		align-items: center;
		cursor: pointer;
		padding: 0;
	}

	.host-head::after {
		content: none;
	}

	.host-editor-shell[open] .host-summary-side::after {
		content: '▴';
	}

	.host-summary-main {
		display: grid;
		gap: 0.18rem;
	}

	.host-head::-webkit-details-marker,
	.host-editor-shell summary::-webkit-details-marker {
		display: none;
	}

	.host-summary-side {
		display: grid;
		gap: 0.35rem;
		justify-items: end;
		margin-left: auto;
	}

	.host-summary-side::after {
		content: '▾';
		display: block;
		font-size: 0.92rem;
		font-weight: 700;
		line-height: 1;
		color: rgba(148, 163, 184, 0.82);
		width: 0.85rem;
		text-align: center;
		justify-self: end;
	}

	.host-summary-chip {
		display: inline-flex;
		align-items: center;
		padding: 0.42rem 0.7rem;
		border-radius: 0;
		background: rgba(15, 23, 42, 0.82);
		border: 1px solid rgba(148, 163, 184, 0.16);
		font-size: 0.82rem;
		font-weight: 700;
		color: rgba(226, 232, 240, 0.78);
	}

	.path-card p {
		margin: 0;
	}

	.host-summary-main .muted-copy {
		margin: 0;
	}

	.legacy-anchor {
		position: relative;
		top: -5rem;
		height: 0;
		pointer-events: none;
	}

	.host-editor-layout {
		display: grid;
		grid-template-columns: minmax(0, 1.3fr) minmax(260px, 0.9fr);
		gap: 1rem;
		align-items: start;
	}

	.two-col {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: 0.85rem;
	}

	.host-json-field,
	.host-schedule-field {
		grid-column: span 2;
	}

	.host-runtime-column,
	.host-actions-panel {
		display: grid;
		gap: 0.75rem;
		align-content: start;
	}

	.host-advanced-shell {
		display: grid;
		gap: 0.85rem;
		padding: 0.85rem 0.95rem;
		border-radius: 0;
		background: rgba(15, 23, 42, 0.62);
		border: 1px solid rgba(148, 163, 184, 0.14);
	}

	.host-advanced-shell summary {
		cursor: pointer;
		font-size: 0.84rem;
		font-weight: 800;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: #7dd3fc;
	}

	.advanced-host-grid {
		grid-template-columns: repeat(2, minmax(0, 1fr));
	}

	.advanced-textarea-field {
		grid-column: span 2;
	}

	.host-actions-row,
	.capability-row {
		display: flex;
		gap: 0.65rem;
		flex-wrap: wrap;
	}

	.host-remove-row {
		display: flex;
		justify-content: flex-end;
	}

	.library-allowlist {
		display: flex;
		flex-wrap: wrap;
		gap: 0.5rem;
	}

	.library-allow-pill {
		border: 1px solid rgba(148, 163, 184, 0.16);
		background: rgba(15, 23, 42, 0.82);
		color: rgba(226, 232, 240, 0.74);
		border-radius: 0;
		padding: 0.42rem 0.7rem;
		font: inherit;
		font-size: 0.86rem;
		font-weight: 700;
		cursor: pointer;
	}

	.library-allow-pill.active {
		background: rgba(8, 47, 73, 0.82);
		border-color: rgba(56, 189, 248, 0.24);
		color: #dbeafe;
	}

	.capability-pill {
		display: inline-flex;
		align-items: center;
		gap: 0.45rem;
		padding: 0.48rem 0.72rem;
		border-radius: 0;
		border: 1px solid rgba(148, 163, 184, 0.16);
		background: rgba(15, 23, 42, 0.82);
		color: rgba(226, 232, 240, 0.82);
		font-weight: 700;
	}

	.capability-pill.checked {
		background: rgba(8, 47, 73, 0.82);
		border-color: rgba(56, 189, 248, 0.24);
		color: #dbeafe;
	}

	.capability-mark {
		font-size: 0.8rem;
	}

	.refresh-status-button {
		padding: 0.7rem 0.9rem;
		border-radius: 0;
		border: 1px solid rgba(148, 163, 184, 0.16);
		background: rgba(15, 23, 42, 0.82);
		font: inherit;
		font-weight: 700;
		color: rgba(226, 232, 240, 0.84);
		cursor: pointer;
	}

	.rail-guidance-list {
		padding-left: 1.15rem;
		display: grid;
		gap: 0.45rem;
	}

	.settings-save-bar {
		position: sticky;
		bottom: 0;
		padding: 1rem 0 0;
	}

	.settings-save-shell {
		display: flex;
		justify-content: space-between;
		gap: 1rem;
		align-items: center;
		padding: 1rem 1.15rem;
		border: 1px solid rgba(251, 146, 60, 0.24);
		background: rgba(58, 26, 13, 0.96);
		color: #f8fafc;
	}

	.settings-save-shell p {
		margin: 0;
	}

	@media (max-width: 960px) {
		.settings-console-grid,
		.host-editor-layout {
			grid-template-columns: 1fr;
		}

		.library-editor,
		.schedule-editor,
		.two-col {
			grid-template-columns: 1fr;
		}

		.form-subhead {
			grid-column: auto;
		}

		.field-grid-header {
			display: none;
		}

		.field-label-hidden {
			position: static;
			width: auto;
			height: auto;
			padding: 0;
			margin: 0;
			overflow: visible;
			clip: auto;
			white-space: normal;
		}

		.host-json-field,
		.host-schedule-field {
			grid-column: auto;
		}
	}

	@media (max-width: 720px) {
		.system-strip,
		.section-head,
		.settings-save-shell,
		.host-head {
			grid-template-columns: 1fr;
			flex-direction: column;
		}

		.section-actions-row,
		.library-summary,
		.schedule-summary {
			width: 100%;
		}

		.library-summary,
		.schedule-summary,
		.host-head {
			align-items: start;
		}

		.summary-action-block,
		.section-actions-row {
			justify-content: space-between;
		}

		.archive-cleanup-card {
			flex-direction: column;
		}

		.host-summary-side {
			justify-items: start;
		}

		.settings-jump-nav {
			grid-template-columns: 1fr;
		}
	}
</style>
