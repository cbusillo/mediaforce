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
	import Panel from '$lib/components/Panel.svelte';
	import SectionHead from '$lib/components/SectionHead.svelte';

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
			refreshArchiveCleanup: (options?: { transcodeRoot?: string | null; silent?: boolean }) => Promise<void>;
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
	const sectionSummary = {
		libraries: () => `${libraries.length} ${libraries.length === 1 ? 'library' : 'libraries'}`,
		schedules: () =>
			`${scheduleProfiles.length} ${scheduleProfiles.length === 1 ? 'schedule' : 'schedules'}`,
		hosts: () => `${remoteHosts.length} ${remoteHosts.length === 1 ? 'worker' : 'workers'}`
	};
	const pathHighlights = [
		{ label: 'Live runtime file', value: 'runtime-settings.json' },
		{ label: 'Checked-in defaults', value: 'config/defaults.toml' }
	];
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

<div class="page-stack">
	<Panel padding="1.05rem 1.2rem">
		<div class="settings-hero">
			<div class="settings-hero-copy">
				<SectionHead
					eyebrow="Runtime Settings"
					heading="Configure libraries, hosts, and queue windows"
					lede="The runtime file below is the live machine-specific contract."
					size="compact"
				/>
				<div class="settings-hero-pills">
					<span class="section-summary-badge">{sectionSummary.libraries()}</span>
					<span class="section-summary-badge">{sectionSummary.schedules()}</span>
					<span class="section-summary-badge">{sectionSummary.hosts()}</span>
						{#if archiveCleanupError && !archiveCleanupUsesDraftPath}
							<span class="section-summary-badge warn">Backup status unavailable</span>
						{:else if archiveCleanup.file_count > 0}
							<span class="section-summary-badge warn">
								{archiveCleanup.file_count} archived backups
							</span>
						{:else if archiveCleanupPending}
							<span class="section-summary-badge">Checking backups</span>
						{/if}
				</div>
			</div>
			<div class="path-card compact-paths">
				<p class="eyebrow-copy">Paths</p>
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
			</div>
		</div>
	</Panel>

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
				class="settings-jump-link"
				onclick={() => jumpToSettingsAnchor('save-settings')}
			>
				Save
			</button>
		{/if}
		{#if isDirty}
			<span class="settings-jump-status dirty">Unsaved changes</span>
		{:else}
			<span class="settings-jump-status">All changes saved</span>
		{/if}
	</nav>

	<div class="settings-grid">
		<Panel>
			<div id="libraries" class="panel-stack">
				<div class="section-row">
					<SectionHead
						eyebrow="Libraries"
						heading="Mounted media roots"
						lede="These become the top-level prefixes throughout Mediaforce."
						size="compact"
					/>
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
			</div>
		</Panel>

		<Panel>
			<div id="transcode" class="panel-stack">
				<details class="transcode-shell">
					<summary class="transcode-summary">
						<span class="summary-copy-block">
							<span class="eyebrow-copy">Transcode</span>
							<span class="transcode-summary-title">Scratch and replacement root</span>
							<span class="muted-copy"
								>Archive path is derived automatically from the transcode root.</span
							>
						</span>
						<span class="summary-action-block transcode-action-block">
							<span class="summary-action-copy">Edit</span>
							<span class="transcode-summary-chip"
								>{transcodeRoot || '/Volumes/media/transcode'}</span
							>
						</span>
					</summary>
					<label class="field-block">
						<span class="eyebrow-copy">Transcode root</span>
						<input bind:value={transcodeRoot} placeholder="/Volumes/media/transcode" />
					</label>
					<div class="path-card">
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
									<Button variant="secondary" onclick={() => refreshArchiveCleanup({ silent: false })}>
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
				</details>
			</div>
		</Panel>
	</div>

	<Panel>
		<div id="schedule-profiles" class="panel-stack">
			<div class="section-row">
				<SectionHead
					eyebrow="Schedules"
					heading="Reusable worker windows"
					lede="Define hour windows plus any full-day exceptions like Sunday, then assign one of these schedules to each host."
					size="compact"
				/>
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
		</div>
	</Panel>

	<Panel>
		<div class="legacy-anchor" id="remote-workers" aria-hidden="true"></div>
		<div id="remote-hosts" class="panel-stack">
			<div class="section-row">
				<SectionHead
					eyebrow="Hosts"
					heading="Remote workers"
					lede="Click a worker row to edit scheduling, queue policy, and live-host setup details in one place."
					size="compact"
				/>
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
					{@const hostSummaryAddress = host.host || 'Set an SSH target to bring this host online.'}
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
														<p class="muted-copy">Save host edits before running remote actions.</p>
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
															onclick={() => resetHostTrust(runtimeHostKey)}>Reset SSH Trust</Button
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
		</div>
	</Panel>
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
	.page-stack,
	.panel-stack,
	.row-stack {
		display: grid;
		gap: var(--space-3);
	}

	.settings-jump-nav {
		position: sticky;
		top: 0.8rem;
		z-index: 2;
		display: flex;
		gap: 0.7rem;
		align-items: center;
		flex-wrap: wrap;
		padding: 0.75rem 0.9rem;
		border-radius: var(--radius-lg);
		background: rgba(255, 253, 248, 0.9);
		backdrop-filter: blur(14px);
		border: 1px solid rgba(23, 35, 31, 0.08);
		box-shadow: 0 16px 30px rgba(23, 35, 31, 0.05);
	}

	.settings-jump-link,
	.settings-jump-status {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		padding: 0.45rem 0.8rem;
		border-radius: var(--radius-pill);
		font-size: 0.82rem;
		font-weight: 700;
		text-decoration: none;
	}

	.settings-jump-link {
		background: rgba(255, 255, 255, 0.72);
		border: 1px solid rgba(23, 35, 31, 0.08);
		color: var(--ink-soft);
	}

	.settings-jump-status {
		margin-left: auto;
		background: rgba(23, 35, 31, 0.06);
		color: var(--ink-soft);
	}

	.settings-jump-status.dirty {
		background: rgba(180, 83, 9, 0.12);
		color: #8f450a;
	}

	.settings-grid {
		display: grid;
		grid-template-columns: 1.4fr 1fr;
		gap: var(--space-4);
	}

	.settings-hero {
		display: grid;
		grid-template-columns: minmax(0, 1.35fr) minmax(20rem, 28rem);
		gap: var(--space-3);
		align-items: start;
	}

	.settings-hero-copy {
		display: grid;
		gap: 1rem;
	}

	.settings-hero-pills {
		display: flex;
		gap: 0.65rem;
		flex-wrap: wrap;
	}

	.compact-paths {
		min-width: 0;
		max-inline-size: 28rem;
		justify-self: end;
	}

	.transcode-shell {
		display: grid;
		gap: 0.85rem;
	}

	.transcode-summary {
		display: grid;
		gap: 0.75rem;
		cursor: pointer;
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
		margin: 0;
		font-size: 1rem;
		font-weight: 700;
		line-height: 1.3;
		color: var(--ink);
	}

	.transcode-summary-chip {
		display: inline-flex;
		align-items: center;
		padding: 0.45rem 0.7rem;
		border-radius: var(--radius-pill);
		background: rgba(255, 255, 255, 0.72);
		border: 1px solid rgba(23, 35, 31, 0.08);
		font-size: 0.82rem;
		font-weight: 700;
		color: var(--ink-soft);
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
		border-radius: var(--radius-lg);
		background: rgba(255, 248, 240, 0.9);
		border: 1px solid rgba(180, 83, 9, 0.16);
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
		margin: 0;
		color: #9a3412;
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
		border-radius: var(--radius-md);
		background: rgba(255, 255, 255, 0.7);
		border: 1px solid rgba(23, 35, 31, 0.08);
	}

	.path-highlight-chip strong {
		font-size: 0.98rem;
		line-height: 1.3;
		color: var(--ink);
	}

	.path-disclosure {
		display: grid;
		gap: 0.45rem;
	}

	.path-disclosure summary {
		cursor: pointer;
		font-size: 0.86rem;
		font-weight: 700;
		color: var(--accent-deep);
	}

	.compact-paths .mono-copy {
		line-height: 1.45;
	}

	.section-row,
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
		border-radius: var(--radius-pill);
		background: rgba(23, 35, 31, 0.06);
		border: 1px solid rgba(23, 35, 31, 0.08);
		font-size: 0.8rem;
		font-weight: 700;
		color: var(--ink-soft);
	}

	.section-summary-badge.warn {
		background: rgba(180, 83, 9, 0.12);
		border-color: rgba(180, 83, 9, 0.18);
		color: #8f450a;
	}

	.field-grid-header {
		display: grid;
		gap: 0.85rem;
		padding: 0 0.2rem;
		font-size: 0.72rem;
		font-weight: 800;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--ink-soft);
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
		color: var(--ink-soft);
	}

	.library-summary-title,
	.schedule-summary-title,
	.host-summary-title,
	.library-summary-path {
		margin: 0;
	}

	.library-summary-title {
		font-size: 1rem;
		font-weight: 700;
		line-height: 1.25;
		color: var(--ink);
	}

	.schedule-summary-title,
	.host-summary-title {
		font-size: 1rem;
		font-weight: 700;
		line-height: 1.25;
		color: var(--ink);
	}

	.library-summary-path {
		margin-top: 0.18rem;
	}

	.library-color-chip {
		display: inline-flex;
		width: 2.5rem;
		height: 2.5rem;
		border-radius: var(--radius-md);
		background: var(--library-swatch);
		border: 1px solid rgba(23, 35, 31, 0.08);
		box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.42);
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
	.path-card {
		padding: 1rem;
		border-radius: var(--radius-lg);
		background: rgba(255, 255, 255, 0.56);
		border: 1px solid rgba(23, 35, 31, 0.08);
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
		border: 1px solid rgba(23, 35, 31, 0.12);
		background: rgba(255, 255, 255, 0.76);
		color: var(--ink-soft);
		border-radius: var(--radius-pill);
		padding: 0.48rem 0.72rem;
		font: inherit;
		font-size: 0.84rem;
		font-weight: 700;
		cursor: pointer;
	}

	.weekday-pill.active {
		background: rgba(15, 118, 110, 0.12);
		border-color: rgba(15, 118, 110, 0.2);
		color: var(--accent-deep);
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
		color: var(--ink);
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
		border-radius: var(--radius-md);
		border: 1px solid rgba(23, 35, 31, 0.12);
		background: #fff;
		font: inherit;
		color: var(--ink);
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
		color: var(--ink-soft);
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
		border-radius: var(--radius-md);
		border: 1px solid rgba(23, 35, 31, 0.12);
		background: rgba(255, 255, 255, 0.76);
		cursor: pointer;
	}

	.icon-button svg {
		width: 1rem;
		height: 1rem;
		fill: currentColor;
	}

	.icon-button.danger {
		color: #9a4b0b;
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
		color: var(--ink-soft);
		width: 0.85rem;
		text-align: center;
		justify-self: end;
	}

	.host-summary-chip {
		display: inline-flex;
		align-items: center;
		padding: 0.42rem 0.7rem;
		border-radius: var(--radius-pill);
		background: rgba(255, 255, 255, 0.74);
		border: 1px solid rgba(23, 35, 31, 0.08);
		font-size: 0.82rem;
		font-weight: 700;
		color: var(--ink-soft);
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
		border-radius: var(--radius-md);
		background: rgba(255, 255, 255, 0.48);
		border: 1px solid rgba(23, 35, 31, 0.08);
	}

	.host-advanced-shell summary {
		cursor: pointer;
		font-size: 0.84rem;
		font-weight: 800;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--accent-deep);
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
		border: 1px solid rgba(23, 35, 31, 0.12);
		background: rgba(255, 255, 255, 0.76);
		color: var(--ink-soft);
		border-radius: var(--radius-pill);
		padding: 0.42rem 0.7rem;
		font: inherit;
		font-size: 0.86rem;
		font-weight: 700;
		cursor: pointer;
	}

	.library-allow-pill.active {
		background: rgba(15, 118, 110, 0.11);
		border-color: rgba(15, 118, 110, 0.18);
		color: #0f5f59;
	}

	.capability-pill {
		display: inline-flex;
		align-items: center;
		gap: 0.45rem;
		padding: 0.48rem 0.72rem;
		border-radius: var(--radius-pill);
		border: 1px solid rgba(23, 35, 31, 0.12);
		background: rgba(255, 255, 255, 0.76);
		font-weight: 700;
	}

	.capability-pill.checked {
		background: rgba(15, 118, 110, 0.12);
		border-color: rgba(15, 118, 110, 0.18);
		color: var(--accent-deep);
	}

	.capability-mark {
		font-size: 0.8rem;
	}

	.refresh-status-button {
		padding: 0.7rem 0.9rem;
		border-radius: var(--radius-pill);
		border: 1px solid rgba(23, 35, 31, 0.12);
		background: rgba(255, 255, 255, 0.76);
		font: inherit;
		font-weight: 700;
		cursor: pointer;
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
		border-radius: var(--radius-lg);
		background: rgba(23, 35, 31, 0.94);
		color: #f8fcfb;
	}

	.settings-save-shell p {
		margin: 0;
	}

	@media (max-width: 960px) {
		.settings-grid,
		.host-editor-layout {
			grid-template-columns: 1fr;
		}

		.settings-jump-nav {
			position: static;
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
		.settings-hero,
		.settings-save-shell,
		.host-head {
			grid-template-columns: 1fr;
			flex-direction: column;
		}

		.transcode-summary {
			flex-direction: column;
		}

		.transcode-summary .summary-action-block {
			width: 100%;
			justify-content: space-between;
		}

		.archive-cleanup-card {
			flex-direction: column;
		}

		.host-summary-side {
			justify-items: start;
		}

		.compact-paths {
			max-inline-size: none;
			justify-self: stretch;
		}

		.settings-jump-status {
			margin-left: 0;
		}
	}
</style>
