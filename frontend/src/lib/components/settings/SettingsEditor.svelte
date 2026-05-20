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
	import { workerCapabilityLabel } from '../workstation/ops-workstation';

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
			saveMessage = '';
			saveError = '';
			clearArchiveArmed = false;
			archiveMessage = '';
			archiveError = '';
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
		{ key: 'never', label: 'Never', summary: 'Never starts queued processing.' },
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
			detail: saveError || saveMessage || 'Local settings draft',
			tone: (saveError ? 'fail' : dirty ? 'wait' : savePending ? 'active' : 'ready') as BadgeTone
		},
		{
			label: 'Libraries',
			value: configuredLibraries.length.toLocaleString('en-US'),
			detail: draft.transcode_root.trim() || 'Working folder missing',
			tone: (draft.transcode_root.trim() && configuredLibraries.length
				? 'ready'
				: 'wait') as BadgeTone,
			mono: true
		},
		{
			label: 'Workers',
			value: `${readyHostCount}/${configuredHosts.length}`,
			detail: loadError || 'Ready workers from latest status check',
			tone: (loadError ? 'fail' : readyHostCount > 0 ? 'ready' : 'idle') as BadgeTone,
			mono: true
		},
		{
			label: 'Work windows',
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
		if (!runtime) return 'Not checked';
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
		saveMessage = 'Draft reset to saved settings.';
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
				saveError = response.message || 'Settings could not be saved.';
			} else {
				if (response.settings) {
					savedSettings = response.settings;
					lastSettingsKey = settingsKey(response.settings);
					draft = draftFromSettings(response.settings);
				}
				saveMessage = response.message || 'Settings saved.';
				await invalidateAll();
			}
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
			archiveMessage = `Confirm removal of waiting originals in ${savedArchiveRootCopy} with a second click.`;
			return;
		}
		clearArchivePending = true;
		try {
			const response = await postJson<ArchiveClearResponse>(
				`${resolve('/')}api/archive-cleanup/clear`,
				buildArchiveCleanupClearPayload(savedSettings)
			);
			if (!response.ok) {
				archiveError = response.message || 'Original removal failed.';
			} else {
				if (response.archive_cleanup) {
					savedSettings = { ...savedSettings, archive_cleanup: response.archive_cleanup };
				}
				archiveMessage = response.message || 'Waiting originals removed.';
				clearArchiveArmed = false;
				await invalidateAll();
			}
		} catch (error) {
			archiveError = error instanceof Error ? error.message : 'Original removal failed.';
		} finally {
			clearArchivePending = false;
		}
	}
</script>

<OperatorShell route="settings" subject="Settings" crumb="/settings" {statusTiles} {footerSignals}>
	<main class="settings-console">
		{#if !savedSettings}
			<section class="settings-console__main">
				<WorkstationPanel eyebrow="Settings" title="Settings unavailable">
					<p class="empty-note">
						{loadError || 'Mediaforce could not load settings.'}
					</p>
				</WorkstationPanel>
			</section>
		{:else}
			<section class="settings-console__main" aria-label="Settings workstation">
				<header class="settings-header">
					<div>
						<span class="mf-eyebrow">Settings</span>
						<h1>Configure Mediaforce</h1>
						<p>
							Set the media libraries, working storage, worker machines, and run windows used by
							this workstation.
						</p>
					</div>
					<div class="settings-header__actions" aria-label="Settings actions">
						<StateBadge
							tone={saveError ? 'fail' : dirty ? 'wait' : 'ready'}
							label={saveError ? 'Error' : dirty ? 'Unsaved' : 'Saved'}
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

				<div class="settings-overview" aria-label="Settings sections">
					<a href="#settings-libraries">
						<span>Libraries</span>
						<strong>{configuredLibraries.length.toLocaleString('en-US')}</strong>
					</a>
					<a href="#settings-storage">
						<span>Storage</span>
						<strong>{draft.transcode_root.trim() ? 'Set' : 'Missing'}</strong>
					</a>
					<a href="#settings-schedules">
						<span>Work windows</span>
						<strong>{(configuredProfiles.length + 2).toLocaleString('en-US')}</strong>
					</a>
					<a href="#settings-workers">
						<span>Workers</span>
						<strong>{configuredHosts.length.toLocaleString('en-US')}</strong>
					</a>
					<a href="#settings-danger">
						<span>Cleanup</span>
						<strong>{archiveCleanup?.has_cleanup ? 'Waiting' : 'Clear'}</strong>
					</a>
				</div>

				<section class="settings-section" aria-labelledby="settings-basic-title">
					<header class="settings-section__head">
						<div>
							<span class="mf-eyebrow">Basic setup</span>
							<h2 id="settings-basic-title">Libraries and storage</h2>
						</div>
						<p>These are the only settings most setup sessions need.</p>
					</header>

					<div id="settings-libraries" class="settings-anchor">
						<WorkstationPanel
							eyebrow="Libraries"
							title="Libraries"
							meta={`${configuredLibraries.length.toLocaleString('en-US')} configured`}
						>
							<div class="table-wrap">
								<table class="settings-table settings-table--libraries">
									<thead>
										<tr>
											<th>Library</th>
											<th>Folder</th>
											<th>Action</th>
										</tr>
									</thead>
									<tbody>
										{#each draft.libraries as library, index (`library-${library.index}-${index}`)}
											<tr>
												<td>
													<div class="library-cell">
														<label
															class="swatch-field"
															aria-label={`Color for ${library.key || 'library'}`}
														>
															<input
																type="color"
																value={library.color}
																oninput={(event) =>
																	updateLibrary(index, { color: inputValue(event) })}
															/>
															<span class="mf-mono">{library.color || 'unset'}</span>
														</label>
														<label class="library-key-field">
															<span>Library name</span>
															<input
																id={`library-key-${index}`}
																class="field"
																value={library.key}
																placeholder="tv"
																oninput={(event) =>
																	updateLibrary(index, { key: inputValue(event) })}
															/>
														</label>
													</div>
												</td>
												<td>
													<label class="sr-label" for={`library-path-${index}`}>Folder path</label>
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
														onclick={() =>
															(draft.libraries = removeAtIndex(draft.libraries, index))}
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
					</div>

					<div id="settings-storage" class="settings-anchor">
						<WorkstationPanel eyebrow="Storage" title="Working storage">
							<div class="storage-grid">
								<label class="stacked-field">
									<span>Working folder</span>
									<input
										class="field field--path"
										value={draft.transcode_root}
										placeholder="/Volumes/Mediaforce/Transcode"
										oninput={(event) => (draft.transcode_root = inputValue(event))}
									/>
								</label>
								<div class="storage-readout">
									<span>Originals waiting folder</span>
									<strong class="mf-path">{savedArchiveRootCopy}</strong>
									{#if cleanupTargetDirty}
										<small>Save settings before deleting from a changed cleanup folder.</small>
									{/if}
								</div>
								<div class="storage-readout">
									<span>Originals waiting</span>
									<strong>{archiveCleanup?.file_count.toLocaleString('en-US') ?? '0'} files</strong>
									<small>{formatGiB(archiveCleanup?.total_size_bytes ?? 0)}</small>
								</div>
								<div class="storage-readout">
									<span>Cleanup state</span>
									<StateBadge
										tone={archiveCleanup?.has_cleanup ? 'wait' : 'ready'}
										label={archiveCleanup?.has_cleanup ? 'Waiting' : 'Clear'}
									/>
									<small>Deletes are grouped in the danger section.</small>
								</div>
							</div>
						</WorkstationPanel>
					</div>
				</section>

				<section class="settings-section" aria-labelledby="settings-advanced-title">
					<header class="settings-section__head">
						<div>
							<span class="mf-eyebrow">Advanced setup</span>
							<h2 id="settings-advanced-title">Workers and run windows</h2>
						</div>
						<p>Use these when adding machines or changing when processing work may run.</p>
					</header>

					<div id="settings-schedules" class="settings-anchor">
						<WorkstationPanel
							eyebrow="Work windows"
							title="Work windows"
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
									<span>Never starts queued processing</span>
								</div>
								{#each draft.schedule_profiles as profile, index (`schedule-${profile.index}-${index}`)}
									<div class="schedule-row">
										<div class="schedule-row__fields">
											<label>
												<span>Window key</span>
												<input
													class="field"
													value={profile.key}
													placeholder="overnight"
													oninput={(event) => updateSchedule(index, { key: inputValue(event) })}
												/>
											</label>
											<label>
												<span>Name</span>
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
													oninput={(event) =>
														updateSchedule(index, { start_hour: inputValue(event) })}
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
													oninput={(event) =>
														updateSchedule(index, { end_hour: inputValue(event) })}
												/>
											</label>
											<button
												type="button"
												class="control control--compact control--danger"
												onclick={() =>
													(draft.schedule_profiles = removeAtIndex(draft.schedule_profiles, index))}
											>
												Remove window
											</button>
										</div>
										<div
											class="day-grid"
											aria-label={`${profile.label || profile.key || 'Schedule'} days`}
										>
											<span>Runs</span>
											{#each SCHEDULE_DAY_OPTIONS as day (day.key)}
												<button
													type="button"
													class:day-pill--active={scheduleDayActive(
														profile,
														day.key,
														'days_of_week'
													)}
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
									onclick={() =>
										(draft.schedule_profiles = addScheduleDraft(draft.schedule_profiles))}
								>
									Add work window
								</button>
							</div>
						</WorkstationPanel>
					</div>

					<div id="settings-workers" class="settings-anchor">
						<WorkstationPanel
							eyebrow="Workers"
							title="Remote workers"
							meta={`${configuredHosts.length.toLocaleString('en-US')} configured`}
						>
							<div class="host-runtime-board" aria-label="Worker status check">
								{#each configuredHosts as host, index (`probe-host-${host.index}-${index}`)}
									{@const runtime = hostRuntime(host)}
									<div class="host-runtime-card host-runtime-card--{runtimeTone(runtime)}">
										<div class="host-runtime-card__head">
											<StateBadge
												compact
												tone={runtimeTone(runtime)}
												label={runtimeCopy(runtime)}
											/>
											<strong>{host.label || host.host || `Remote worker ${index + 1}`}</strong>
										</div>
										<span
											>{runtime?.schedule_detail ||
												runtime?.schedule_profile_label ||
												'No worker status yet'}</span
										>
										<small
											>{runtime?.message || runtime?.active_reason || 'Status check pending'}</small
										>
									</div>
								{:else}
									<p class="empty-note">No remote workers are configured.</p>
								{/each}
							</div>
							<div class="host-editor-list">
								{#each draft.remote_hosts as host, index (`host-${host.index}-${index}`)}
									{@const runtime = hostRuntime(host)}
									<section class="host-editor" id={`remote-worker-${index}`}>
										<header class="host-editor__head">
											<div>
												<StateBadge
													compact
													tone={runtimeTone(runtime)}
													label={runtimeCopy(runtime)}
												/>
												<strong>{host.label || host.host || `Remote worker ${index + 1}`}</strong>
												<span
													>{runtime?.message ||
														runtime?.active_reason ||
														'Status check pending'}</span
												>
											</div>
											<button
												type="button"
												class="control control--compact control--danger"
												onclick={() =>
													(draft.remote_hosts = removeAtIndex(draft.remote_hosts, index))}
											>
												Remove worker
											</button>
										</header>
										<div class="host-grid">
											<label>
												<span>Worker name</span>
												<input
													class="field"
													value={host.label}
													placeholder="Studio Mac"
													oninput={(event) => updateHost(index, { label: inputValue(event) })}
												/>
											</label>
											<label>
												<span>Media access</span>
												<select
													class="field"
													bind:value={draft.remote_hosts[index].media_access}
													onchange={(event) =>
														updateHost(index, { media_access: selectValue(event) })}
												>
													<option value="mounted">Mounted</option>
													<option value="stream">Stream</option>
												</select>
											</label>
											<label>
												<span>Work window</span>
												<select
													class="field"
													bind:value={draft.remote_hosts[index].schedule_profile}
													onchange={(event) =>
														updateHost(index, { schedule_profile: selectValue(event) })}
												>
													{#each draftScheduleOptions as option (option.key)}
														<option value={option.key}>{option.label}</option>
													{/each}
												</select>
											</label>
										</div>
										<div class="host-options host-options--primary">
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
														<span class="muted-copy"
															>Add library keys before assigning workers.</span
														>
													{/each}
												</div>
											</div>
										</div>
										<details class="host-advanced">
											<summary>
												<strong>Connection and advanced settings</strong>
												<span
													>SSH address, repo path, staging folder, command hooks, and source
													overrides.</span
												>
											</summary>
											<div class="host-grid host-grid--advanced">
												<label>
													<span>SSH address</span>
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
													<span>Priority</span>
													<input
														class="field field--number"
														type="number"
														value={host.priority}
														oninput={(event) => updateHost(index, { priority: inputValue(event) })}
													/>
												</label>
												<label>
													<span>Parallel processing</span>
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
													<span>Worker staging folder</span>
													<input
														class="field field--path"
														value={host.staging_root}
														placeholder="Uses working folder"
														oninput={(event) =>
															updateHost(index, { staging_root: inputValue(event) })}
													/>
												</label>
												<label>
													<span>Start command</span>
													<input
														class="field field--path"
														value={host.start_command}
														placeholder="Optional wake/start command"
														oninput={(event) =>
															updateHost(index, { start_command: inputValue(event) })}
													/>
												</label>
												<label>
													<span>Stop command</span>
													<input
														class="field field--path"
														value={host.stop_command}
														placeholder="Optional stop command"
														oninput={(event) =>
															updateHost(index, { stop_command: inputValue(event) })}
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
													<span class="option-label">Worker abilities</span>
													<div class="toggle-grid">
														{#each savedSettings.host_capability_options as option (option.key)}
															<label class="toggle-chip">
																<input
																	type="checkbox"
																	checked={host.capabilities.includes(option.key)}
																	onchange={() => toggleCapability(index, option.key)}
																/>
																<span>{workerCapabilityLabel(option.key)}</span>
															</label>
														{/each}
													</div>
												</div>
											</div>
											<label class="stacked-field">
												<span>Source root overrides JSON</span>
												<textarea
													class="field field--textarea"
													bind:value={draft.remote_hosts[index].source_roots_json}
													placeholder={'{"tv": "/Volumes/TV"}'}
													oninput={(event) =>
														updateHost(index, { source_roots_json: inputValue(event) })}
												></textarea>
											</label>
										</details>
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
									Add worker
								</button>
							</div>
						</WorkstationPanel>
					</div>
				</section>

				<section
					class="settings-section settings-section--danger"
					aria-labelledby="settings-danger-title"
				>
					<header class="settings-section__head">
						<div>
							<span class="mf-eyebrow">Danger zone</span>
							<h2 id="settings-danger-title">Originals cleanup</h2>
						</div>
						<p>Deletes waiting originals from the cleanup folder after a second confirmation.</p>
					</header>

					<div id="settings-danger" class="settings-anchor">
						<WorkstationPanel eyebrow="Danger zone" title="Delete archived originals">
							<div class="danger-zone">
								<div>
									<span>Cleanup folder</span>
									<strong class="mf-path">{savedArchiveRootCopy}</strong>
									<small
										>{archiveCleanup?.file_count.toLocaleString('en-US') ?? '0'} files · {formatGiB(
											archiveCleanup?.total_size_bytes ?? 0
										)}</small
									>
								</div>
								<div class="archive-actions">
									<StateBadge
										tone={archiveCleanup?.has_cleanup ? 'wait' : 'ready'}
										label={archiveCleanup?.has_cleanup ? 'Originals waiting' : 'Nothing waiting'}
									/>
									<button
										type="button"
										class="control control--danger"
										class:control--armed={clearArchiveArmed}
										disabled={!archiveCleanup?.has_cleanup ||
											clearArchivePending ||
											cleanupTargetDirty}
										onclick={clearArchiveCleanup}
										title={cleanupTargetDirty
											? 'Save the changed cleanup folder before deleting archived originals.'
											: undefined}
									>
										{clearArchivePending
											? 'Removing originals'
											: clearArchiveArmed
												? 'Confirm delete originals'
												: 'Delete waiting originals'}
									</button>
								</div>
							</div>
							{#if archiveError}
								<p class="action-error">{archiveError}</p>
							{:else if archiveMessage}
								<p class="action-message">{archiveMessage}</p>
							{/if}
						</WorkstationPanel>
					</div>
				</section>
			</section>

			<aside class="settings-console__rail" aria-label="Settings navigation and files">
				<nav class="settings-rail-nav" aria-label="Settings sections">
					<span class="mf-eyebrow">Sections</span>
					<a href="#settings-libraries">Libraries</a>
					<a href="#settings-storage">Storage</a>
					<a href="#settings-schedules">Work windows</a>
					<a href="#settings-workers">Workers</a>
					<a href="#settings-danger">Cleanup</a>
				</nav>
				<WorkstationPanel eyebrow="Scope" title="Machine-local settings">
					<div class="rail-list">
						<div class="rail-row">
							<span>Settings file</span>
							<strong class="mf-path">{savedSettings.runtime_settings_path}</strong>
						</div>
						<div class="rail-row">
							<span>Default file</span>
							<strong class="mf-path">{savedSettings.repo_config_path}</strong>
						</div>
						<div class="rail-row">
							<span>Cleanup folder</span>
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

	.settings-overview {
		display: grid;
		gap: var(--mf-space-3);
		grid-template-columns: repeat(5, minmax(0, 1fr));
	}

	.settings-overview a {
		background: var(--mf-bg-strip);
		border: var(--mf-border-muted);
		border-top: 2px solid var(--mf-line-strong);
		display: grid;
		gap: var(--mf-space-2);
		min-width: 0;
		padding: var(--mf-space-4);
	}

	.settings-overview a:hover,
	.settings-rail-nav a:hover {
		background: var(--mf-bg-panel-2);
		color: var(--mf-fg-primary);
	}

	.settings-overview span,
	.settings-rail-nav a {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-semibold);
		letter-spacing: 0.08em;
		text-transform: uppercase;
	}

	.settings-overview strong {
		font-family: var(--mf-font-mono), monospace;
		font-size: var(--mf-text-sm);
		font-weight: var(--mf-weight-semibold);
		overflow-wrap: anywhere;
	}

	.settings-section {
		display: grid;
		gap: var(--mf-space-5);
		scroll-margin-top: 150px;
	}

	.settings-section__head {
		align-items: end;
		border-bottom: var(--mf-border-muted);
		display: grid;
		gap: var(--mf-space-4);
		grid-template-columns: minmax(0, 1fr) minmax(220px, 0.45fr);
		padding-bottom: var(--mf-space-3);
	}

	.settings-section__head h2 {
		font-size: var(--mf-text-lg);
		margin-top: var(--mf-space-2);
	}

	.settings-section__head p {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-xs);
		line-height: var(--mf-leading-snug);
	}

	.settings-section--danger .settings-section__head {
		border-bottom-color: var(--mf-fail-line);
	}

	.settings-anchor {
		display: grid;
		scroll-margin-top: 150px;
	}

	.settings-rail-nav {
		background: var(--mf-bg-strip);
		border: var(--mf-border);
		display: grid;
		gap: var(--mf-space-1);
		padding: var(--mf-space-4);
		position: sticky;
		top: 160px;
		z-index: 1;
	}

	.settings-rail-nav .mf-eyebrow {
		margin-bottom: var(--mf-space-2);
	}

	.settings-rail-nav a {
		border-left: 2px solid var(--mf-line-strong);
		display: block;
		min-height: var(--mf-control-sm);
		padding: var(--mf-space-2) var(--mf-space-3);
	}

	.table-wrap {
		overflow: auto;
	}

	table {
		border-collapse: collapse;
		min-width: 700px;
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

	.settings-table--libraries td:nth-child(1) {
		min-width: 240px;
	}

	.settings-table--libraries td:nth-child(2) {
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
		font-family: var(--mf-font-mono), monospace;
		font-size: var(--mf-text-xs);
	}

	.field--number {
		font-family: var(--mf-font-mono), monospace;
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

	.library-cell {
		align-items: end;
		display: grid;
		gap: var(--mf-space-4);
		grid-template-columns: 92px minmax(120px, 1fr);
	}

	.library-key-field {
		display: grid;
		gap: var(--mf-space-2);
		min-width: 0;
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
	.host-advanced label,
	.host-grid label,
	.schedule-row__fields label {
		display: grid;
		gap: var(--mf-space-2);
		min-width: 0;
	}

	.stacked-field span,
	.library-key-field span,
	.host-advanced label span,
	.host-grid label span,
	.schedule-row__fields label span,
	.storage-readout span,
	.danger-zone > div > span,
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
	.danger-zone > div,
	.rail-row {
		display: grid;
		gap: var(--mf-space-2);
		min-width: 0;
	}

	.storage-readout strong,
	.danger-zone strong,
	.rail-row strong,
	.host-editor__head strong,
	.schedule-row strong {
		font-size: var(--mf-text-sm);
		font-weight: var(--mf-weight-semibold);
		overflow-wrap: anywhere;
	}

	.storage-readout small,
	.danger-zone small,
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
		grid-template-columns: repeat(3, minmax(150px, 1fr));
	}

	.host-advanced {
		background: var(--mf-bg-strip);
		border: var(--mf-border-muted);
		display: grid;
		gap: var(--mf-space-4);
		padding: var(--mf-space-4);
	}

	.host-advanced summary {
		cursor: pointer;
		display: grid;
		gap: var(--mf-space-1);
		list-style-position: inside;
	}

	.host-advanced summary strong {
		font-size: var(--mf-text-sm);
		font-weight: var(--mf-weight-semibold);
	}

	.host-advanced summary span {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-xs);
	}

	.host-grid--advanced {
		grid-template-columns: repeat(4, minmax(130px, 1fr));
	}

	.host-grid--advanced label:nth-child(1),
	.host-grid--advanced label:nth-child(2),
	.host-grid--advanced label:nth-child(5),
	.host-grid--advanced label:nth-child(6),
	.host-grid--advanced label:nth-child(7) {
		grid-column: span 2;
	}

	.danger-zone {
		align-items: center;
		display: grid;
		gap: var(--mf-space-5);
		grid-template-columns: minmax(0, 1fr) auto;
		padding: var(--mf-space-5);
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
		.settings-overview,
		.storage-grid,
		.danger-zone,
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
		.settings-section__head,
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

		.table-wrap {
			overflow: visible;
		}

		.settings-table--libraries {
			min-width: 0;
		}

		.settings-table--libraries thead {
			display: none;
		}

		.settings-table--libraries tbody,
		.settings-table--libraries tr,
		.settings-table--libraries td {
			display: block;
			width: 100%;
		}

		.settings-table--libraries tr {
			border-bottom: var(--mf-border-muted);
			display: grid;
			gap: var(--mf-space-3);
			grid-template-columns: minmax(0, 1fr);
			padding: var(--mf-space-4);
		}

		.settings-table--libraries td {
			border-bottom: 0;
			height: auto;
			min-width: 0;
			padding: 0;
		}

		.settings-table--libraries td:nth-child(1),
		.settings-table--libraries td:nth-child(2) {
			display: grid;
			gap: var(--mf-space-2);
		}

		.settings-table--libraries td:nth-child(1)::before,
		.settings-table--libraries td:nth-child(2)::before {
			color: var(--mf-fg-tertiary);
			font-size: var(--mf-text-2xs);
			font-weight: var(--mf-weight-semibold);
			letter-spacing: 0.08em;
			text-transform: uppercase;
		}

		.settings-table--libraries td:nth-child(1)::before {
			content: 'Library';
		}

		.settings-table--libraries td:nth-child(2)::before {
			content: 'Folder path';
		}
	}
</style>
