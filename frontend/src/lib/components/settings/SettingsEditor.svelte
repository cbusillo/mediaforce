<script lang="ts">
	import { invalidateAll } from '$app/navigation';
	import { resolve } from '$app/paths';
	import { postJson } from '$lib/api/client';
	import type {
		ArchiveCleanupPayload,
		HostRuntime,
		HostsPayload,
		ScheduleProfile,
		SettingsHost,
		SettingsLibrary,
		SettingsPayload
	} from '$lib/api/types';
	import { formatGiB } from '$lib/format';
	import {
		SCHEDULE_DAY_OPTIONS,
		addHostDraft,
		addLibraryDraft,
		addScheduleDraft,
		archiveCleanupTargetDirty,
		buildArchiveCleanupClearPayload,
		buildSettingsSavePayload,
		draftFromSettings,
		hostDraftRuntimeKey,
		removeAtIndex,
		scheduleWindowSummaryCopy,
		settingsDraftIsDirty,
		toggleHostAllowedLibrary,
		toggleHostCapability,
		toggleScheduleProfileDay,
		type ScheduleDayKey,
		type SettingsDraft
	} from '$lib/settings/editor';
	import OperatorShell from '../workstation/OperatorShell.svelte';
	import StateBadge from '../workstation/StateBadge.svelte';
	import WorkstationPanel from '../workstation/WorkstationPanel.svelte';

	type SaveResponse = {
		ok: boolean;
		message?: string;
		settings?: SettingsPayload;
	};
	type ArchiveClearResponse = {
		ok: boolean;
		message?: string;
		archive_cleanup?: ArchiveCleanupPayload;
	};
	type BadgeTone = 'active' | 'ready' | 'wait' | 'fail' | 'idle';

	let {
		settings,
		hosts,
		loadError
	}: {
		settings: SettingsPayload | null;
		hosts: HostsPayload | null;
		loadError: string | null;
	} = $props();

	const emptyDraft: SettingsDraft = {
		libraries: [],
		remote_hosts: [],
		transcode_root: '',
		schedule_profiles: []
	};

	let savedSettings = $state<SettingsPayload | null>(null);
	let draft = $state<SettingsDraft>(emptyDraft);
	let savePending = $state(false);
	let saveMessage = $state('');
	let saveError = $state('');
	let clearArchivePending = $state(false);
	let clearArchiveArmed = $state(false);
	let archiveMessage = $state('');
	let archiveError = $state('');
	let lastSettingsKey = '';

	$effect(() => {
		if (!settings) {
			if (!loadError) return;
			lastSettingsKey = '';
			savedSettings = null;
			draft = emptyDraft;
			clearArchiveArmed = false;
			archiveMessage = '';
			return;
		}
		const nextSettingsKey = settingsKey(settings);
		if (nextSettingsKey === lastSettingsKey || savePending) return;
		lastSettingsKey = nextSettingsKey;
		savedSettings = settings;
		draft = draftFromSettings(settings);
	});

	const configuredLibraries = $derived(
		draft.libraries.filter((library) => library.key.trim() || library.path.trim())
	);
	const activeLibraryKeys = $derived(
		draft.libraries.map((library) => library.key.trim()).filter(Boolean)
	);
	const configuredHosts = $derived(
		draft.remote_hosts.filter((host) => host.label.trim() || host.host.trim())
	);
	const configuredProfiles = $derived(
		draft.schedule_profiles.filter((profile) => profile.key.trim() || profile.label.trim())
	);
	const runtimeHosts = $derived(hosts?.hosts ?? []);
	const readyHostCount = $derived(runtimeHosts.filter((host) => host.available).length);
	const archiveCleanup = $derived(savedSettings?.archive_cleanup ?? null);
	const dirty = $derived(savedSettings ? settingsDraftIsDirty(draft, savedSettings) : false);
	const savedArchiveRootCopy = $derived(savedSettings?.archive_root || 'unset');
	const cleanupTargetDirty = $derived(
		savedSettings ? archiveCleanupTargetDirty(draft, savedSettings) : false
	);
	const draftScheduleOptions = $derived([
		{ key: 'always', label: 'Always', summary: 'Runs anytime.' },
		{ key: 'never', label: 'Never', summary: 'Never accepts queued encodes.' },
		...configuredProfiles.map((profile) => ({
			key: profile.key.trim(),
			label: profile.label.trim() || profile.key.trim(),
			summary: scheduleWindowSummaryCopy(profile)
		}))
	]);
	const statusTiles = $derived([
		{
			label: 'Save state',
			value: savePending ? 'Saving' : dirty ? 'Unsaved' : saveError ? 'Save error' : 'Saved',
			detail: saveError || saveMessage || 'Runtime settings draft',
			tone: (saveError ? 'fail' : dirty ? 'wait' : savePending ? 'active' : 'ready') as BadgeTone
		},
		{
			label: 'Libraries',
			value: configuredLibraries.length.toLocaleString('en-US'),
			detail: draft.transcode_root.trim() || 'Transcode root missing',
			tone: (draft.transcode_root.trim() && configuredLibraries.length
				? 'ready'
				: 'wait') as BadgeTone,
			mono: true
		},
		{
			label: 'Hosts',
			value: `${readyHostCount}/${configuredHosts.length}`,
			detail: loadError || 'Ready hosts from runtime probe',
			tone: (loadError ? 'fail' : readyHostCount > 0 ? 'ready' : 'idle') as BadgeTone,
			mono: true
		},
		{
			label: 'Schedules',
			value: (configuredProfiles.length + 2).toLocaleString('en-US'),
			detail: 'Includes Always and Never',
			tone: 'idle' as BadgeTone,
			mono: true
		}
	]);
	const footerSignals = $derived([
		{
			label: 'Runtime',
			value: savedSettings?.runtime_settings_path ?? 'unavailable',
			tone: (loadError ? 'fail' : 'idle') as BadgeTone
		},
		{
			label: 'Defaults',
			value: savedSettings?.repo_config_path ?? 'unavailable',
			tone: 'idle' as BadgeTone
		}
	]);

	function inputValue(event: Event): string {
		return (event.currentTarget as HTMLInputElement | HTMLTextAreaElement).value;
	}

	function selectValue(event: Event): string {
		return (event.currentTarget as HTMLSelectElement).value;
	}

	function settingsKey(payload: SettingsPayload | null): string {
		if (!payload) return '';
		return JSON.stringify(payload);
	}

	function hostRuntime(host: SettingsHost): HostRuntime | null {
		const key = hostDraftRuntimeKey(host);
		if (!key) return null;
		return (
			runtimeHosts.find(
				(runtime) =>
					runtime.key === key ||
					runtime.host === key ||
					runtime.label === host.label.trim() ||
					runtime.label === host.host.trim()
			) ?? null
		);
	}

	function runtimeTone(runtime: HostRuntime | null): BadgeTone {
		if (!runtime) return 'idle';
		if (runtime.available && runtime.issues.length === 0) return 'ready';
		if (runtime.available) return 'wait';
		return 'fail';
	}

	function runtimeCopy(runtime: HostRuntime | null): string {
		if (!runtime) return 'Not probed';
		if (runtime.available && runtime.issues.length === 0) return 'Ready';
		if (runtime.available) return 'Needs setup';
		return 'Offline';
	}

	function updateLibrary(index: number, patch: Partial<SettingsLibrary>) {
		draft.libraries = draft.libraries.map((library, candidate) =>
			candidate === index ? { ...library, ...patch } : library
		);
	}

	function updateHost(index: number, patch: Partial<SettingsHost>) {
		draft.remote_hosts = draft.remote_hosts.map((host, candidate) =>
			candidate === index ? { ...host, ...patch } : host
		);
	}

	function updateSchedule(index: number, patch: Partial<ScheduleProfile>) {
		draft.schedule_profiles = draft.schedule_profiles.map((profile, candidate) =>
			candidate === index ? { ...profile, ...patch } : profile
		);
	}

	function toggleScheduleDay(
		index: number,
		dayKey: ScheduleDayKey,
		target: 'days_of_week' | 'all_day_days_of_week'
	) {
		draft.schedule_profiles = draft.schedule_profiles.map((profile, candidate) =>
			candidate === index ? toggleScheduleProfileDay(profile, dayKey, target) : profile
		);
	}

	function scheduleDayActive(
		profile: ScheduleProfile,
		dayKey: ScheduleDayKey,
		target: 'days_of_week' | 'all_day_days_of_week'
	): boolean {
		return profile[target].includes(dayKey);
	}

	function toggleCapability(index: number, capability: string) {
		draft.remote_hosts = toggleHostCapability(draft.remote_hosts, index, capability);
	}

	function toggleLibraryAccess(index: number, libraryKey: string) {
		draft.remote_hosts = toggleHostAllowedLibrary(draft.remote_hosts, index, libraryKey);
	}

	function resetDraft() {
		if (!savedSettings) return;
		draft = draftFromSettings(savedSettings);
		saveError = '';
		saveMessage = 'Draft reset to saved runtime settings.';
		clearArchiveArmed = false;
	}

	async function saveSettings() {
		if (!savedSettings || savePending) return;
		savePending = true;
		saveError = '';
		saveMessage = '';
		clearArchiveArmed = false;
		try {
			const response = await postJson<SaveResponse>(
				`${resolve('/')}api/settings`,
				buildSettingsSavePayload(draft, savedSettings)
			);
			if (!response.ok) {
				throw new Error(response.message || 'Settings could not be saved.');
			}
			if (response.settings) {
				savedSettings = response.settings;
				lastSettingsKey = settingsKey(response.settings);
				draft = draftFromSettings(response.settings);
			}
			saveMessage = response.message || 'Settings saved.';
			await invalidateAll();
		} catch (error) {
			saveError = error instanceof Error ? error.message : 'Settings could not be saved.';
		} finally {
			savePending = false;
		}
	}

	async function clearArchiveCleanup() {
		if (!savedSettings || clearArchivePending) return;
		archiveMessage = '';
		archiveError = '';
		if (!clearArchiveArmed) {
			clearArchiveArmed = true;
			archiveMessage = `Confirm archive cleanup for ${savedArchiveRootCopy} with a second click.`;
			return;
		}
		clearArchivePending = true;
		try {
			const response = await postJson<ArchiveClearResponse>(
				`${resolve('/')}api/archive-cleanup/clear`,
				buildArchiveCleanupClearPayload(savedSettings)
			);
			if (!response.ok) {
				throw new Error(response.message || 'Archive cleanup failed.');
			}
			if (response.archive_cleanup) {
				savedSettings = { ...savedSettings, archive_cleanup: response.archive_cleanup };
			}
			archiveMessage = response.message || 'Archive cleanup complete.';
			clearArchiveArmed = false;
			await invalidateAll();
		} catch (error) {
			archiveError = error instanceof Error ? error.message : 'Archive cleanup failed.';
		} finally {
			clearArchivePending = false;
		}
	}
</script>

<OperatorShell route="settings" subject="Settings" crumb="/settings" {statusTiles} {footerSignals}>
	<main class="settings-console">
		{#if !savedSettings}
			<section class="settings-console__main">
				<WorkstationPanel eyebrow="Settings" title="Runtime settings unavailable">
					<p class="empty-note">
						{loadError || 'The settings API did not return a payload.'}
					</p>
				</WorkstationPanel>
			</section>
		{:else}
			<section class="settings-console__main" aria-label="Settings workstation">
				<header class="settings-header">
					<div>
						<span class="mf-eyebrow">Runtime config</span>
						<h1>Settings console</h1>
						<p>
							Edit machine-local library roots, storage paths, host workers, and queue schedule
							profiles from the runtime payload.
						</p>
					</div>
					<div class="settings-header__actions" aria-label="Settings actions">
						<StateBadge
							tone={saveError ? 'fail' : dirty ? 'wait' : 'ready'}
							label={saveError ? 'Error' : dirty ? 'Dirty' : 'Saved'}
						/>
						<button
							type="button"
							class="control"
							disabled={!dirty || savePending}
							onclick={resetDraft}
						>
							Reset
						</button>
						<button
							type="button"
							class="control control--ready"
							disabled={!dirty || savePending}
							onclick={saveSettings}
						>
							{savePending ? 'Saving' : 'Save'}
						</button>
					</div>
				</header>

				{#if loadError}
					<div class="notice notice--fail">{loadError}</div>
				{/if}
				{#if saveError}
					<div class="notice notice--fail">{saveError}</div>
				{:else if saveMessage}
					<div class="notice notice--ready">{saveMessage}</div>
				{/if}
				{#if savedSettings.host_notice}
					<div class:notice--fail={savedSettings.host_notice_kind === 'error'} class="notice">
						{savedSettings.host_notice}
					</div>
				{/if}

				<WorkstationPanel
					eyebrow="Libraries"
					title="Mounted roots"
					meta={`${configuredLibraries.length.toLocaleString('en-US')} configured`}
				>
					<div class="table-wrap">
						<table class="settings-table settings-table--libraries">
							<thead>
								<tr>
									<th>Color</th>
									<th>Key</th>
									<th>Mounted path</th>
									<th>Action</th>
								</tr>
							</thead>
							<tbody>
								{#each draft.libraries as library, index (`library-${library.index}-${index}`)}
									<tr>
										<td>
											<label
												class="swatch-field"
												aria-label={`Color for ${library.key || 'library'}`}
											>
												<input
													type="color"
													value={library.color}
													oninput={(event) => updateLibrary(index, { color: inputValue(event) })}
												/>
												<span class="mf-mono">{library.color || 'unset'}</span>
											</label>
										</td>
										<td>
											<label class="sr-label" for={`library-key-${index}`}>Library key</label>
											<input
												id={`library-key-${index}`}
												class="field"
												value={library.key}
												placeholder="tv"
												oninput={(event) => updateLibrary(index, { key: inputValue(event) })}
											/>
										</td>
										<td>
											<label class="sr-label" for={`library-path-${index}`}>Mounted path</label>
											<input
												id={`library-path-${index}`}
												class="field field--path"
												value={library.path}
												placeholder="/Volumes/Media/TV"
												oninput={(event) => updateLibrary(index, { path: inputValue(event) })}
											/>
										</td>
										<td>
											<button
												type="button"
												class="control control--compact control--danger"
												onclick={() => (draft.libraries = removeAtIndex(draft.libraries, index))}
											>
												Remove
											</button>
										</td>
									</tr>
								{/each}
							</tbody>
						</table>
					</div>
					<div class="panel-actions">
						<button
							type="button"
							class="control"
							onclick={() => (draft.libraries = addLibraryDraft(draft.libraries))}
						>
							Add library
						</button>
					</div>
				</WorkstationPanel>

				<WorkstationPanel eyebrow="Storage" title="Transcode, review, and archive roots">
					<div class="storage-grid">
						<label class="stacked-field">
							<span>Transcode root</span>
							<input
								class="field field--path"
								value={draft.transcode_root}
								placeholder="/Volumes/Mediaforce/Transcode"
								oninput={(event) => (draft.transcode_root = inputValue(event))}
							/>
						</label>
						<div class="storage-readout">
							<span>Archive cleanup root</span>
							<strong class="mf-path">{savedArchiveRootCopy}</strong>
							{#if cleanupTargetDirty}
								<small>Save settings before clearing a changed archive target.</small>
							{/if}
						</div>
						<div class="storage-readout">
							<span>Archived originals</span>
							<strong>{archiveCleanup?.file_count.toLocaleString('en-US') ?? '0'} files</strong>
							<small>{formatGiB(archiveCleanup?.total_size_bytes ?? 0)}</small>
						</div>
						<div class="archive-actions">
							<StateBadge
								tone={archiveCleanup?.has_cleanup ? 'wait' : 'ready'}
								label={archiveCleanup?.has_cleanup ? 'Cleanup' : 'Clear'}
							/>
							<button
								type="button"
								class="control control--danger"
								class:control--armed={clearArchiveArmed}
								disabled={!archiveCleanup?.has_cleanup || clearArchivePending || cleanupTargetDirty}
								onclick={clearArchiveCleanup}
								title={cleanupTargetDirty
									? 'Save the changed transcode root before clearing archived originals.'
									: undefined}
							>
								{clearArchivePending ? 'Clearing' : clearArchiveArmed ? 'Confirm' : 'Clear archive'}
							</button>
						</div>
					</div>
					{#if archiveError}
						<p class="action-error">{archiveError}</p>
					{:else if archiveMessage}
						<p class="action-message">{archiveMessage}</p>
					{/if}
				</WorkstationPanel>

				<WorkstationPanel
					eyebrow="Schedules"
					title="Queue profiles"
					meta={`${configuredProfiles.length.toLocaleString('en-US')} custom`}
				>
					<div class="schedule-list">
						<div class="schedule-row schedule-row--builtin">
							<StateBadge compact tone="ready" label="Built in" />
							<strong>Always</strong>
							<span>Runs anytime</span>
						</div>
						<div class="schedule-row schedule-row--builtin">
							<StateBadge compact tone="idle" label="Built in" />
							<strong>Never</strong>
							<span>Never accepts queued encodes</span>
						</div>
						{#each draft.schedule_profiles as profile, index (`schedule-${profile.index}-${index}`)}
							<div class="schedule-row">
								<div class="schedule-row__fields">
									<label>
										<span>Key</span>
										<input
											class="field"
											value={profile.key}
											placeholder="overnight"
											oninput={(event) => updateSchedule(index, { key: inputValue(event) })}
										/>
									</label>
									<label>
										<span>Label</span>
										<input
											class="field"
											value={profile.label}
											placeholder="Overnight"
											oninput={(event) => updateSchedule(index, { label: inputValue(event) })}
										/>
									</label>
									<label>
										<span>Start</span>
										<input
											class="field field--number"
											type="number"
											min="0"
											max="23"
											value={profile.start_hour}
											oninput={(event) => updateSchedule(index, { start_hour: inputValue(event) })}
										/>
									</label>
									<label>
										<span>End</span>
										<input
											class="field field--number"
											type="number"
											min="0"
											max="23"
											value={profile.end_hour}
											oninput={(event) => updateSchedule(index, { end_hour: inputValue(event) })}
										/>
									</label>
									<button
										type="button"
										class="control control--compact control--danger"
										onclick={() =>
											(draft.schedule_profiles = removeAtIndex(draft.schedule_profiles, index))}
									>
										Remove
									</button>
								</div>
								<div
									class="day-grid"
									aria-label={`${profile.label || profile.key || 'Schedule'} days`}
								>
									<span>Window</span>
									{#each SCHEDULE_DAY_OPTIONS as day (day.key)}
										<button
											type="button"
											class:day-pill--active={scheduleDayActive(profile, day.key, 'days_of_week')}
											class="day-pill"
											aria-pressed={scheduleDayActive(profile, day.key, 'days_of_week')}
											onclick={() => toggleScheduleDay(index, day.key, 'days_of_week')}
										>
											{day.shortLabel}
										</button>
									{/each}
									<span>All day</span>
									{#each SCHEDULE_DAY_OPTIONS as day (day.key)}
										<button
											type="button"
											class:day-pill--active={scheduleDayActive(
												profile,
												day.key,
												'all_day_days_of_week'
											)}
											class="day-pill"
											aria-pressed={scheduleDayActive(profile, day.key, 'all_day_days_of_week')}
											onclick={() => toggleScheduleDay(index, day.key, 'all_day_days_of_week')}
										>
											{day.shortLabel}
										</button>
									{/each}
								</div>
								<p class="schedule-summary">{scheduleWindowSummaryCopy(profile)}</p>
							</div>
						{/each}
					</div>
					<div class="panel-actions">
						<button
							type="button"
							class="control"
							onclick={() => (draft.schedule_profiles = addScheduleDraft(draft.schedule_profiles))}
						>
							Add schedule
						</button>
					</div>
				</WorkstationPanel>

				<WorkstationPanel
					eyebrow="Hosts"
					title="Remote workers"
					meta={`${configuredHosts.length.toLocaleString('en-US')} configured`}
				>
					<div class="host-runtime-board" aria-label="Host runtime probe">
						{#each configuredHosts as host, index (`probe-host-${host.index}-${index}`)}
							{@const runtime = hostRuntime(host)}
							<div class="host-runtime-card host-runtime-card--{runtimeTone(runtime)}">
								<div class="host-runtime-card__head">
									<StateBadge compact tone={runtimeTone(runtime)} label={runtimeCopy(runtime)} />
									<strong>{host.label || host.host || `Remote worker ${index + 1}`}</strong>
								</div>
								<span
									>{runtime?.schedule_detail ||
										runtime?.schedule_profile_label ||
										'No runtime data'}</span
								>
								<small>{runtime?.message || runtime?.active_reason || 'Probe pending'}</small>
							</div>
						{:else}
							<p class="empty-note">No remote hosts are configured.</p>
						{/each}
					</div>
					<div class="host-editor-list">
						{#each draft.remote_hosts as host, index (`host-${host.index}-${index}`)}
							{@const runtime = hostRuntime(host)}
							<section class="host-editor" id={`remote-worker-${index}`}>
								<header class="host-editor__head">
									<div>
										<StateBadge compact tone={runtimeTone(runtime)} label={runtimeCopy(runtime)} />
										<strong>{host.label || host.host || `Remote worker ${index + 1}`}</strong>
										<span
											>{runtime?.message || runtime?.active_reason || 'Runtime probe pending'}</span
										>
									</div>
									<button
										type="button"
										class="control control--compact control--danger"
										onclick={() => (draft.remote_hosts = removeAtIndex(draft.remote_hosts, index))}
									>
										Remove
									</button>
								</header>
								<div class="host-grid">
									<label>
										<span>Label</span>
										<input
											class="field"
											value={host.label}
											placeholder="Studio Mac"
											oninput={(event) => updateHost(index, { label: inputValue(event) })}
										/>
									</label>
									<label>
										<span>SSH host</span>
										<input
											class="field"
											value={host.host}
											placeholder="studio.local"
											oninput={(event) => updateHost(index, { host: inputValue(event) })}
										/>
									</label>
									<label>
										<span>Repo path</span>
										<input
											class="field field--path"
											value={host.repo_path}
											placeholder="/Users/operator/mediaforce"
											oninput={(event) => updateHost(index, { repo_path: inputValue(event) })}
										/>
									</label>
									<label>
										<span>Media access</span>
										<select
											class="field"
											value={host.media_access}
											onchange={(event) => updateHost(index, { media_access: selectValue(event) })}
										>
											<option value="mounted">Mounted</option>
											<option value="stream">Stream</option>
										</select>
									</label>
									<label>
										<span>Priority</span>
										<input
											class="field field--number"
											type="number"
											value={host.priority}
											oninput={(event) => updateHost(index, { priority: inputValue(event) })}
										/>
									</label>
									<label>
										<span>Parallel encodes</span>
										<input
											class="field field--number"
											type="number"
											min="1"
											value={host.max_parallel_encodes}
											oninput={(event) =>
												updateHost(index, { max_parallel_encodes: inputValue(event) })}
										/>
									</label>
									<label>
										<span>Schedule</span>
										<select
											class="field"
											value={host.schedule_profile}
											onchange={(event) =>
												updateHost(index, { schedule_profile: selectValue(event) })}
										>
											{#each draftScheduleOptions as option (option.key)}
												<option value={option.key}>{option.label}</option>
											{/each}
										</select>
									</label>
									<label>
										<span>Staging root</span>
										<input
											class="field field--path"
											value={host.staging_root}
											placeholder="Uses transcode root"
											oninput={(event) => updateHost(index, { staging_root: inputValue(event) })}
										/>
									</label>
									<label>
										<span>Start command</span>
										<input
											class="field field--path"
											value={host.start_command}
											placeholder="Optional wake/start command"
											oninput={(event) => updateHost(index, { start_command: inputValue(event) })}
										/>
									</label>
									<label>
										<span>Stop command</span>
										<input
											class="field field--path"
											value={host.stop_command}
											placeholder="Optional stop command"
											oninput={(event) => updateHost(index, { stop_command: inputValue(event) })}
										/>
									</label>
									<label>
										<span>Wake MAC</span>
										<input
											class="field"
											value={host.wake_mac}
											placeholder="Optional"
											oninput={(event) => updateHost(index, { wake_mac: inputValue(event) })}
										/>
									</label>
									<label>
										<span>Start timeout</span>
										<input
											class="field field--number"
											type="number"
											min="1"
											value={host.start_timeout_seconds}
											oninput={(event) =>
												updateHost(index, { start_timeout_seconds: inputValue(event) })}
										/>
									</label>
								</div>
								<div class="host-options">
									<div>
										<span class="option-label">Capabilities</span>
										<div class="toggle-grid">
											{#each savedSettings.host_capability_options as option (option.key)}
												<label class="toggle-chip">
													<input
														type="checkbox"
														checked={host.capabilities.includes(option.key)}
														onchange={() => toggleCapability(index, option.key)}
													/>
													<span>{option.label}</span>
												</label>
											{/each}
										</div>
									</div>
									<div>
										<span class="option-label">Allowed libraries</span>
										<div class="toggle-grid">
											{#each activeLibraryKeys as libraryKey (libraryKey)}
												<label class="toggle-chip">
													<input
														type="checkbox"
														checked={host.allowed_libraries.includes(libraryKey)}
														onchange={() => toggleLibraryAccess(index, libraryKey)}
													/>
													<span>{libraryKey}</span>
												</label>
											{:else}
												<span class="muted-copy">Add library keys before assigning hosts.</span>
											{/each}
										</div>
									</div>
								</div>
								<label class="stacked-field">
									<span>Source root overrides JSON</span>
									<textarea
										class="field field--textarea"
										value={host.source_roots_json}
										placeholder={'{"tv": "/Volumes/TV"}'}
										oninput={(event) => updateHost(index, { source_roots_json: inputValue(event) })}
									></textarea>
								</label>
							</section>
						{/each}
					</div>
					<div class="panel-actions">
						<button
							type="button"
							class="control"
							onclick={() =>
								(draft.remote_hosts = addHostDraft(draft.remote_hosts, draftScheduleOptions))}
						>
							Add host
						</button>
					</div>
				</WorkstationPanel>
			</section>

			<aside class="settings-console__rail" aria-label="Settings runtime summary">
				<WorkstationPanel eyebrow="Scope" title="Machine-local config">
					<div class="rail-list">
						<div class="rail-row">
							<span>Runtime settings</span>
							<strong class="mf-path">{savedSettings.runtime_settings_path}</strong>
						</div>
						<div class="rail-row">
							<span>Repo defaults</span>
							<strong class="mf-path">{savedSettings.repo_config_path}</strong>
						</div>
						<div class="rail-row">
							<span>Review/archive output</span>
							<strong class="mf-path">{savedArchiveRootCopy}</strong>
						</div>
					</div>
				</WorkstationPanel>
			</aside>
		{/if}
	</main>
</OperatorShell>

<style>
	.settings-console {
		display: grid;
		grid-template-columns: minmax(0, 1fr) var(--mf-workstation-rail-width);
		min-height: calc(100vh - 178px);
	}

	.settings-console__main {
		align-content: start;
		display: grid;
		gap: var(--mf-space-5);
		min-width: 0;
		padding: var(--mf-space-6);
	}

	.settings-console__rail {
		align-content: start;
		background: var(--mf-bg-shell);
		border-left: var(--mf-border);
		display: flex;
		flex-direction: column;
		gap: var(--mf-space-5);
		min-width: 0;
		padding: var(--mf-space-5);
	}

	.settings-header {
		align-items: end;
		border-bottom: var(--mf-border);
		display: grid;
		gap: var(--mf-space-6);
		grid-template-columns: minmax(0, 1fr) auto;
		padding-bottom: var(--mf-space-5);
	}

	.settings-header h1 {
		margin-top: var(--mf-space-3);
	}

	.settings-header p {
		margin-top: var(--mf-space-3);
		max-width: 78ch;
	}

	.settings-header__actions,
	.panel-actions,
	.archive-actions {
		align-items: center;
		display: flex;
		flex-wrap: wrap;
		gap: var(--mf-space-3);
	}

	.panel-actions {
		border-top: var(--mf-border-muted);
		padding: var(--mf-space-4) var(--mf-space-5);
	}

	.notice,
	.action-message,
	.action-error {
		border-left: 2px solid var(--mf-line-strong);
		color: var(--mf-fg-secondary);
		font-size: var(--mf-text-xs);
		padding: var(--mf-space-3) var(--mf-space-4);
	}

	.notice--ready,
	.action-message {
		border-left-color: var(--mf-ready-fg);
	}

	.notice--fail,
	.action-error {
		border-left-color: var(--mf-fail-fg);
		color: var(--mf-fail-fg);
	}

	.table-wrap {
		overflow: auto;
	}

	table {
		border-collapse: collapse;
		min-width: 780px;
		width: 100%;
	}

	th,
	td {
		border-bottom: var(--mf-border-muted);
		font-size: var(--mf-text-xs);
		height: var(--mf-row-default);
		padding: var(--mf-space-2) var(--mf-space-5);
		text-align: left;
		vertical-align: middle;
	}

	th {
		background: var(--mf-bg-strip);
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-semibold);
		letter-spacing: 0.08em;
		position: sticky;
		text-transform: uppercase;
		top: 0;
	}

	.settings-table--libraries td:nth-child(2),
	.settings-table--libraries td:nth-child(3) {
		min-width: 220px;
	}

	.field {
		background: var(--mf-bg-input);
		border: var(--mf-border);
		border-radius: var(--mf-radius-1);
		color: var(--mf-fg-primary);
		min-height: var(--mf-control-md);
		min-width: 0;
		padding: 0 var(--mf-space-3);
		width: 100%;
	}

	.field--path,
	.field--textarea {
		font-family: var(--mf-font-mono);
		font-size: var(--mf-text-xs);
	}

	.field--number {
		font-family: var(--mf-font-mono);
		max-width: 92px;
	}

	.field--textarea {
		min-height: 82px;
		padding-bottom: var(--mf-space-3);
		padding-top: var(--mf-space-3);
		resize: vertical;
	}

	.swatch-field {
		align-items: center;
		display: flex;
		gap: var(--mf-space-3);
	}

	.swatch-field input {
		background: transparent;
		border: var(--mf-border);
		border-radius: var(--mf-radius-1);
		height: var(--mf-control-md);
		padding: 0;
		width: 36px;
	}

	.sr-label {
		border: 0;
		clip-path: inset(50%);
		height: 1px;
		margin: -1px;
		overflow: hidden;
		padding: 0;
		position: fixed;
		transform: translateX(-100vw);
		white-space: nowrap;
		width: 1px;
	}

	.stacked-field,
	.host-grid label,
	.schedule-row__fields label {
		display: grid;
		gap: var(--mf-space-2);
		min-width: 0;
	}

	.stacked-field span,
	.host-grid label span,
	.schedule-row__fields label span,
	.storage-readout span,
	.option-label,
	.rail-row span {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-semibold);
		letter-spacing: 0.08em;
		text-transform: uppercase;
	}

	.storage-grid {
		display: grid;
		gap: var(--mf-space-5);
		grid-template-columns: minmax(260px, 1.4fr) minmax(220px, 1fr) minmax(150px, auto) auto;
		padding: var(--mf-space-5);
	}

	.storage-readout,
	.rail-row {
		display: grid;
		gap: var(--mf-space-2);
		min-width: 0;
	}

	.storage-readout strong,
	.rail-row strong,
	.host-editor__head strong,
	.schedule-row strong {
		font-size: var(--mf-text-sm);
		font-weight: var(--mf-weight-semibold);
		overflow-wrap: anywhere;
	}

	.storage-readout small,
	.rail-row small,
	.muted-copy,
	.schedule-summary,
	.host-editor__head span {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-xs);
	}

	.schedule-list,
	.host-editor-list,
	.host-runtime-board,
	.rail-list {
		display: grid;
		gap: var(--mf-space-4);
		padding: var(--mf-space-5);
	}

	.host-runtime-board {
		grid-template-columns: repeat(auto-fit, minmax(145px, 1fr));
	}

	.host-runtime-card {
		background: var(--mf-bg-strip);
		border: var(--mf-border-muted);
		border-top: 2px solid var(--mf-line-strong);
		display: grid;
		gap: var(--mf-space-2);
		min-height: 104px;
		min-width: 0;
		padding: var(--mf-space-4);
	}

	.host-runtime-card--ready {
		border-top-color: var(--mf-ready-fg);
	}

	.host-runtime-card--wait {
		border-top-color: var(--mf-wait-fg);
	}

	.host-runtime-card--fail {
		border-top-color: var(--mf-fail-fg);
	}

	.host-runtime-card__head {
		align-content: start;
		display: grid;
		gap: var(--mf-space-2);
		min-width: 0;
	}

	.host-runtime-card strong,
	.host-runtime-card span,
	.host-runtime-card small {
		overflow-wrap: anywhere;
	}

	.host-runtime-card strong {
		font-size: var(--mf-text-sm);
		font-weight: var(--mf-weight-semibold);
	}

	.host-runtime-card span {
		color: var(--mf-fg-secondary);
		font-size: var(--mf-text-xs);
	}

	.host-runtime-card small {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
		line-height: var(--mf-leading-snug);
	}

	.schedule-row,
	.host-editor {
		background: var(--mf-bg-panel-2);
		border: var(--mf-border-muted);
		display: grid;
		gap: var(--mf-space-4);
		padding: var(--mf-space-4);
	}

	.schedule-row--builtin {
		align-items: center;
		grid-template-columns: auto minmax(90px, 0.18fr) minmax(0, 1fr);
		min-height: var(--mf-row-comfy);
	}

	.schedule-row__fields {
		align-items: end;
		display: grid;
		gap: var(--mf-space-4);
		grid-template-columns: minmax(120px, 0.5fr) minmax(140px, 0.8fr) 92px 92px auto;
	}

	.day-grid {
		align-items: center;
		display: grid;
		gap: var(--mf-space-2);
		grid-template-columns: 62px repeat(7, minmax(40px, 1fr));
	}

	.day-grid > span {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-semibold);
		text-transform: uppercase;
	}

	.day-pill {
		background: var(--mf-bg-input);
		border: var(--mf-border);
		border-radius: var(--mf-radius-1);
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-xs);
		min-height: var(--mf-control-sm);
		padding: 0 var(--mf-space-2);
	}

	.day-pill--active {
		background: var(--mf-active-bg);
		border-color: var(--mf-active-line);
		color: var(--mf-active-fg-bright);
	}

	.host-editor__head {
		align-items: center;
		border-bottom: var(--mf-border-muted);
		display: grid;
		gap: var(--mf-space-4);
		grid-template-columns: minmax(0, 1fr) auto;
		padding-bottom: var(--mf-space-4);
	}

	.host-editor__head > div {
		align-items: center;
		display: flex;
		flex-wrap: wrap;
		gap: var(--mf-space-3);
		min-width: 0;
	}

	.host-grid {
		display: grid;
		gap: var(--mf-space-4);
		grid-template-columns: repeat(4, minmax(130px, 1fr));
	}

	.host-grid label:nth-child(3),
	.host-grid label:nth-child(9),
	.host-grid label:nth-child(10) {
		grid-column: span 2;
	}

	.host-options {
		display: grid;
		gap: var(--mf-space-4);
		grid-template-columns: repeat(2, minmax(0, 1fr));
	}

	.host-options > div {
		display: grid;
		gap: var(--mf-space-3);
	}

	.toggle-grid {
		display: flex;
		flex-wrap: wrap;
		gap: var(--mf-space-3);
	}

	.toggle-chip {
		align-items: center;
		background: var(--mf-bg-input);
		border: var(--mf-border);
		border-radius: var(--mf-radius-1);
		color: var(--mf-fg-secondary);
		display: inline-flex;
		font-size: var(--mf-text-xs);
		gap: var(--mf-space-3);
		min-height: var(--mf-control-sm);
		padding: 0 var(--mf-space-3);
	}

	.toggle-chip input {
		accent-color: var(--mf-active-solid);
	}

	.rail-row {
		border-left: 2px solid var(--mf-line-strong);
		padding: var(--mf-space-3) var(--mf-space-4);
	}

	.control {
		align-items: center;
		background: var(--mf-bg-panel-2);
		border: var(--mf-border-strong);
		border-radius: var(--mf-radius-1);
		color: var(--mf-fg-primary);
		display: inline-flex;
		font-size: var(--mf-text-xs);
		font-weight: var(--mf-weight-semibold);
		justify-content: center;
		min-height: var(--mf-control-md);
		padding: 0 var(--mf-space-4);
		white-space: nowrap;
	}

	.control:hover:not(:disabled) {
		background: var(--mf-bg-raised);
	}

	.control:disabled {
		border-color: var(--mf-line-muted);
	}

	.control--compact {
		min-height: var(--mf-control-sm);
	}

	.control--ready {
		border-color: var(--mf-ready-line);
		color: var(--mf-ready-fg);
	}

	.control--danger {
		border-color: var(--mf-fail-line);
		color: var(--mf-fail-fg);
	}

	.control--armed {
		background: var(--mf-fail-bg-strong);
		border-color: var(--mf-fail-fg);
		color: var(--mf-fail-fg-bright);
	}

	.empty-note {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-sm);
		padding: var(--mf-space-5);
	}

	@media (max-width: 1120px) {
		.settings-console,
		.storage-grid,
		.host-options {
			grid-template-columns: 1fr;
		}

		.settings-console__rail {
			border-left: 0;
			border-top: var(--mf-border);
		}

		.host-grid {
			grid-template-columns: repeat(2, minmax(130px, 1fr));
		}
	}

	@media (max-width: 680px) {
		.settings-console__main,
		.settings-console__rail {
			padding: var(--mf-space-4);
		}

		.settings-header,
		.schedule-row--builtin,
		.schedule-row__fields,
		.host-editor__head,
		.host-grid {
			grid-template-columns: 1fr;
		}

		.day-grid {
			grid-template-columns: repeat(4, minmax(0, 1fr));
		}

		.day-grid > span {
			grid-column: 1 / -1;
		}

		.settings-header__actions,
		.panel-actions,
		.archive-actions {
			align-items: stretch;
		}

		.settings-header__actions .control,
		.panel-actions .control,
		.archive-actions .control {
			flex: 1 1 auto;
		}
	}
</style>
