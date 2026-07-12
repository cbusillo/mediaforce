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
	import {
		SCHEDULE_DAY_OPTIONS,
		addHostDraft,
		addLibraryDraft,
		addScheduleDraft,
		archiveCleanupTargetDirty,
		buildArchiveCleanupClearPayload,
		buildSettingsSavePayload,
		draftFromSettings,
		hostLibraryAccessChecked,
		hostLibraryAccessCopy,
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
		metadata: {
			plex: {
				enabled: true,
				base_url: '',
				library_roots: {},
				token_env: 'MEDIAFORCE_PLEX_TOKEN',
				token_configured: false,
				refresh_interval_hours: 1
			},
			tmdb: {
				enabled: true,
				base_url: 'https://api.themoviedb.org/3',
				token_env: 'MEDIAFORCE_TMDB_TOKEN',
				token_configured: false,
				refresh_interval_hours: 24
			}
		},
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
	const defaultMetricCopy = $derived(metricDefaultsCopy(draft.video_defaults));
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
		{ label: 'Assistant defaults', value: defaultMetricCopy, tone: 'ready' as BadgeTone },
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

	function formatStorage(bytes: number): string {
		if (!Number.isFinite(bytes) || bytes <= 0) return '0 GB';
		return `${(bytes / 1024 ** 3).toLocaleString('en-US', { maximumFractionDigits: 1 })} GB`;
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

	function updateVideoDefault(key: keyof SettingsPayload['video_defaults'], value: string) {
		draft.video_defaults = { ...draft.video_defaults, [key]: value };
	}

	function updatePlexMetadata(patch: Partial<SettingsPayload['metadata']['plex']>) {
		draft.metadata = {
			...draft.metadata,
			plex: { ...draft.metadata.plex, ...patch }
		};
	}

	function updateTmdbMetadata(patch: Partial<SettingsPayload['metadata']['tmdb']>) {
		draft.metadata = {
			...draft.metadata,
			tmdb: { ...draft.metadata.tmdb, ...patch }
		};
	}

	function updatePlexLibraryRoot(libraryKey: string, value: string) {
		const library_roots = { ...draft.metadata.plex.library_roots };
		if (value.trim()) library_roots[libraryKey] = value;
		else delete library_roots[libraryKey];
		updatePlexMetadata({ library_roots });
	}

	function metricDefaultsCopy(defaults: SettingsPayload['video_defaults']) {
		const target = `${defaults.target_size_mb} MB / ${defaults.target_runtime_minutes} min normalized`;
		const metric = defaults.quality_metric.trim().toLowerCase();
		if (metric === 'xpsnr') return `${target} · XPSNR floor ${defaults.min_target_xpsnr}`;
		if (metric === 'auto') return `${target} · Auto metric guardrails`;
		return `${target} · VMAF floor ${defaults.min_target_vmaf}`;
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
		draft.remote_hosts = toggleHostAllowedLibrary(
			draft.remote_hosts,
			index,
			libraryKey,
			activeLibraryKeys
		);
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
						<h1>Settings</h1>
						<p>Choose where your shows live, where Mediaforce works, and when it may run.</p>
					</div>
					<div
						class="settings-header__actions"
						class:has-changes={dirty || Boolean(saveError)}
						aria-label="Settings actions"
					>
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
							Discard
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
						<span>Library folders</span>
						<strong>{configuredLibraries.length.toLocaleString('en-US')}</strong>
					</a>
					<a href="#settings-storage">
						<span>Working space</span>
						<strong>{draft.transcode_root.trim() ? 'Set' : 'Missing'}</strong>
					</a>
					<a href="#settings-assistant-defaults">
						<span>Default size</span>
						<strong>{defaultMetricCopy}</strong>
					</a>
					<a href="#settings-schedules">
						<span>Schedule</span>
						<strong>{(configuredProfiles.length + 2).toLocaleString('en-US')}</strong>
					</a>
					<a href="#settings-workers">
						<span>Computers</span>
						<strong>{configuredHosts.length.toLocaleString('en-US')}</strong>
					</a>
					<a href="#settings-danger">
						<span>Old originals</span>
						<strong>{archiveCleanup?.has_cleanup ? 'Waiting' : 'Clear'}</strong>
					</a>
				</div>

				<section class="settings-section" aria-labelledby="settings-basic-title">
					<header class="settings-section__head">
						<div>
							<span class="mf-eyebrow">Basic setup</span>
							<h2 id="settings-basic-title">Library and working space</h2>
						</div>
						<p>These are the only settings most setup sessions need.</p>
					</header>

					<div id="settings-libraries" class="settings-anchor">
						<WorkstationPanel
							eyebrow="Libraries"
							title="Library folders"
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
						<WorkstationPanel eyebrow="Working space" title="Working space">
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
									<span>Original backup area</span>
									<strong class="mf-path">{savedArchiveRootCopy}</strong>
									{#if cleanupTargetDirty}
										<small>Save settings before deleting from a changed cleanup folder.</small>
									{/if}
								</div>
								<div class="storage-readout">
									<span>Old originals waiting</span>
									<strong>{archiveCleanup?.file_count.toLocaleString('en-US') ?? '0'} files</strong>
									<small>{formatStorage(archiveCleanup?.total_size_bytes ?? 0)}</small>
								</div>
								<div class="storage-readout">
									<span>Backup area</span>
									<StateBadge
										tone={archiveCleanup?.has_cleanup ? 'wait' : 'ready'}
										label={archiveCleanup?.has_cleanup ? 'Waiting' : 'Clear'}
									/>
									<small>Old originals stay here until you choose to remove them below.</small>
								</div>
							</div>
						</WorkstationPanel>
					</div>

					<div id="settings-assistant-defaults" class="settings-anchor">
						<WorkstationPanel eyebrow="Default size" title="Default episode size">
							<div class="encode-defaults-grid">
								<label class="stacked-field">
									<span>Reference size MB</span>
									<input
										class="field field--number"
										type="number"
										min="1"
										max="100000"
										step="1"
										value={draft.video_defaults.target_size_mb}
										oninput={(event) => updateVideoDefault('target_size_mb', inputValue(event))}
									/>
								</label>
								<label class="stacked-field">
									<span>Reference runtime minutes</span>
									<input
										class="field field--number"
										type="number"
										min="1"
										max="1440"
										step="1"
										value={draft.video_defaults.target_runtime_minutes}
										oninput={(event) =>
											updateVideoDefault('target_runtime_minutes', inputValue(event))}
									/>
								</label>
								<label class="stacked-field">
									<span>Test target band %</span>
									<input
										class="field field--number"
										type="number"
										min="0.1"
										max="100"
										step="0.1"
										value={draft.video_defaults.sample_projection_tolerance_percent}
										oninput={(event) =>
											updateVideoDefault('sample_projection_tolerance_percent', inputValue(event))}
									/>
								</label>
								<label class="stacked-field">
									<span>Final output band %</span>
									<input
										class="field field--number"
										type="number"
										min="0.1"
										max="100"
										step="0.1"
										value={draft.video_defaults.final_output_tolerance_percent}
										oninput={(event) =>
											updateVideoDefault('final_output_tolerance_percent', inputValue(event))}
									/>
								</label>
								<label class="stacked-field">
									<span>Decision model</span>
									<select
										class="field"
										bind:value={draft.video_defaults.decision_model}
										onchange={(event) => updateVideoDefault('decision_model', selectValue(event))}
									>
										<option value="size_first_review">Size-first review</option>
										<option value="quality_first_sample">Quality-first sample</option>
									</select>
								</label>
								<label class="stacked-field">
									<span>Sample engine</span>
									<select
										class="field"
										bind:value={draft.video_defaults.quality_engine}
										onchange={(event) => updateVideoDefault('quality_engine', selectValue(event))}
									>
										<option value="ab_av1_fast_sample">ab-av1 fast sample</option>
									</select>
								</label>
								<label class="stacked-field">
									<span>Quality metric</span>
									<select
										class="field"
										bind:value={draft.video_defaults.quality_metric}
										onchange={(event) => updateVideoDefault('quality_metric', selectValue(event))}
									>
										<option value="vmaf">VMAF</option>
										<option value="auto">Auto</option>
										<option value="xpsnr">XPSNR</option>
									</select>
								</label>
								<label class="stacked-field">
									<span>VMAF target</span>
									<input
										class="field field--number"
										type="number"
										min="1"
										max="100"
										step="0.1"
										value={draft.video_defaults.target_vmaf}
										oninput={(event) => updateVideoDefault('target_vmaf', inputValue(event))}
									/>
								</label>
								<label class="stacked-field">
									<span>VMAF floor</span>
									<input
										class="field field--number"
										type="number"
										min="1"
										max="100"
										step="0.1"
										value={draft.video_defaults.min_target_vmaf}
										oninput={(event) => updateVideoDefault('min_target_vmaf', inputValue(event))}
									/>
								</label>
								<label class="stacked-field">
									<span>XPSNR target</span>
									<input
										class="field field--number"
										type="number"
										min="1"
										max="100"
										step="0.1"
										value={draft.video_defaults.target_xpsnr}
										oninput={(event) => updateVideoDefault('target_xpsnr', inputValue(event))}
									/>
								</label>
								<label class="stacked-field">
									<span>XPSNR floor</span>
									<input
										class="field field--number"
										type="number"
										min="1"
										max="100"
										step="0.1"
										value={draft.video_defaults.min_target_xpsnr}
										oninput={(event) => updateVideoDefault('min_target_xpsnr', inputValue(event))}
									/>
								</label>
								<label class="stacked-field">
									<span>Max height</span>
									<input
										class="field field--number"
										type="number"
										min="0"
										max="4320"
										step="1"
										value={draft.video_defaults.max_height}
										oninput={(event) => updateVideoDefault('max_height', inputValue(event))}
									/>
								</label>
								<label class="stacked-field">
									<span>Grain</span>
									<input
										class="field field--number"
										type="number"
										min="0"
										max="50"
										step="1"
										value={draft.video_defaults.default_grain}
										oninput={(event) => updateVideoDefault('default_grain', inputValue(event))}
									/>
								</label>
								<label class="stacked-field">
									<span>Safety ceiling %</span>
									<input
										class="field field--number"
										type="number"
										min="1"
										max="100"
										step="0.1"
										value={draft.video_defaults.max_encoded_percent}
										oninput={(event) =>
											updateVideoDefault('max_encoded_percent', inputValue(event))}
									/>
								</label>
								<div class="encode-defaults-readout">
									<span>Default goal</span>
									<strong
										>{draft.video_defaults.target_size_mb} MB / {draft.video_defaults
											.target_runtime_minutes} min</strong
									>
									<small>
										Scaled to each episode runtime · ±{draft.video_defaults
											.sample_projection_tolerance_percent}% test · ±{draft.video_defaults
											.final_output_tolerance_percent}% final
									</small>
								</div>
								<div class="encode-defaults-readout">
									<span>Resolution guardrail</span>
									<strong
										>{Number(draft.video_defaults.max_height) > 0
											? `max ${draft.video_defaults.max_height}p`
											: 'source resolution'}</strong
									>
									<small>Used unless the operator explicitly asks for a resolution change.</small>
								</div>
							</div>
						</WorkstationPanel>
					</div>
				</section>

				<section class="settings-section" aria-labelledby="settings-advanced-title">
					<header class="settings-section__head">
						<div>
							<span class="mf-eyebrow">Advanced setup</span>
							<h2 id="settings-advanced-title">Metadata, computers, and schedule</h2>
						</div>
						<p>Connect catalog metadata, add machines, and control when processing work may run.</p>
					</header>

					<div id="settings-metadata" class="settings-anchor">
						<WorkstationPanel
							eyebrow="Metadata"
							title="Plex and series status"
							meta={draft.metadata.plex.token_configured && draft.metadata.tmdb.token_configured
								? 'credentials ready'
								: 'credentials needed'}
						>
							<div class="metadata-grid">
								<label class="toggle-chip metadata-toggle">
									<input
										type="checkbox"
										checked={draft.metadata.plex.enabled}
										onchange={(event) =>
											updatePlexMetadata({
												enabled: (event.currentTarget as HTMLInputElement).checked
											})}
									/>
									<span>Use Plex catalog metadata</span>
								</label>
								<label class="stacked-field metadata-url">
									<span>Plex server URL</span>
									<input
										class="field field--path"
										value={draft.metadata.plex.base_url}
										placeholder="http://plex.local:32400"
										oninput={(event) => updatePlexMetadata({ base_url: inputValue(event) })}
									/>
								</label>
								<div class="metadata-status">
									<StateBadge
										compact
										tone={draft.metadata.plex.token_configured ? 'ready' : 'wait'}
										label={draft.metadata.plex.token_configured ? 'Token ready' : 'Token missing'}
									/>
									<code>{draft.metadata.plex.token_env}</code>
								</div>
							</div>

							<div class="table-wrap metadata-mappings">
								<table class="settings-table settings-table--metadata">
									<thead><tr><th>Library</th><th>Path reported by Plex</th></tr></thead>
									<tbody>
										{#each configuredLibraries as library (library.index)}
											<tr>
												<td><strong class="mf-mono">{library.key}</strong></td>
												<td>
													<input
														class="field field--path"
														value={draft.metadata.plex.library_roots[library.key] ?? ''}
														placeholder={library.path || '/data/media/tv'}
														oninput={(event) =>
															updatePlexLibraryRoot(library.key, inputValue(event))}
													/>
												</td>
											</tr>
										{/each}
									</tbody>
								</table>
							</div>

							<div class="metadata-grid metadata-grid--tmdb">
								<label class="toggle-chip metadata-toggle">
									<input
										type="checkbox"
										checked={draft.metadata.tmdb.enabled}
										onchange={(event) =>
											updateTmdbMetadata({
												enabled: (event.currentTarget as HTMLInputElement).checked
											})}
									/>
									<span>Resolve current-series status with TMDB</span>
								</label>
								<div class="metadata-status">
									<StateBadge
										compact
										tone={draft.metadata.tmdb.token_configured ? 'ready' : 'wait'}
										label={draft.metadata.tmdb.token_configured ? 'Token ready' : 'Token missing'}
									/>
									<code>{draft.metadata.tmdb.token_env}</code>
								</div>
							</div>
							<p class="metadata-help">
								Tokens stay outside Mediaforce settings. A full library scan refreshes Plex age and
								TMDB series status; cached metadata remains available during provider outages.
							</p>
						</WorkstationPanel>
					</div>

					<div id="settings-schedules" class="settings-anchor">
						<WorkstationPanel
							eyebrow="Schedule"
							title="Schedule"
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
							eyebrow="Computers"
							title="Computers"
							meta={`${configuredHosts.length.toLocaleString('en-US')} configured`}
						>
							<div class="host-runtime-board" aria-label="Computer status check">
								{#each configuredHosts as host, index (`probe-host-${host.index}-${index}`)}
									{@const runtime = hostRuntime(host)}
									<div class="host-runtime-card host-runtime-card--{runtimeTone(runtime)}">
										<div class="host-runtime-card__head">
											<StateBadge
												compact
												tone={runtimeTone(runtime)}
												label={runtimeCopy(runtime)}
											/>
											<strong>{host.label || host.host || `Computer ${index + 1}`}</strong>
										</div>
										<span
											>{runtime?.schedule_detail ||
												runtime?.schedule_profile_label ||
												'No computer status yet'}</span
										>
										<small
											>{runtime?.message || runtime?.active_reason || 'Status check pending'}</small
										>
									</div>
								{:else}
									<p class="empty-note">No additional computers are configured.</p>
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
												<strong>{host.label || host.host || `Computer ${index + 1}`}</strong>
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
												Remove computer
											</button>
										</header>
										<div class="host-grid">
											<label>
												<span>Computer name</span>
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
												<small class="option-hint">{hostLibraryAccessCopy(host)}</small>
												<div class="toggle-grid">
													{#each activeLibraryKeys as libraryKey (libraryKey)}
														<label class="toggle-chip">
															<input
																type="checkbox"
																checked={hostLibraryAccessChecked(host, libraryKey)}
																onchange={() => toggleLibraryAccess(index, libraryKey)}
															/>
															<span>{libraryKey}</span>
														</label>
													{:else}
														<span class="muted-copy"
															>Add library folders before assigning computers.</span
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
													<span>Temporary folder</span>
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
													<span class="option-label">What this computer can do</span>
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
														updateHost(index, { source_roots_json: inputValue(event) })}></textarea>
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
									Add computer
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
							<span class="mf-eyebrow">Old originals</span>
							<h2 id="settings-danger-title">Remove old originals</h2>
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
										>{archiveCleanup?.file_count.toLocaleString('en-US') ?? '0'} files · {formatStorage(
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
				<WorkstationPanel eyebrow="Save state" title="Settings draft">
					<div class="rail-save">
						<StateBadge
							tone={saveError ? 'fail' : dirty ? 'wait' : 'ready'}
							label={saveError ? 'Error' : dirty ? 'Unsaved' : 'Saved'}
						/>
						<p>{saveError || saveMessage || 'Changes stay local until you save.'}</p>
						<div class="rail-save__actions" aria-label="Settings save actions">
							<button
								type="button"
								class="control control--compact"
								disabled={!dirty || savePending}
								onclick={resetDraft}
							>
								Reset
							</button>
							<button
								type="button"
								class="control control--compact control--ready"
								disabled={!dirty || savePending}
								onclick={saveSettings}
							>
								{savePending ? 'Saving' : 'Save'}
							</button>
						</div>
					</div>
				</WorkstationPanel>
				<nav class="settings-rail-nav" aria-label="Settings sections">
					<span class="mf-eyebrow">Sections</span>
					<a href="#settings-libraries">Library folders</a>
					<a href="#settings-storage">Working space</a>
					<a href="#settings-assistant-defaults">Default size</a>
					<a href="#settings-schedules">Schedule</a>
					<a href="#settings-workers">Computers</a>
					<a href="#settings-danger">Old originals</a>
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

			{#if dirty || savePending || saveError}
				<div class="settings-save-dock" role="status" aria-live="polite">
					<StateBadge
						tone={saveError ? 'fail' : dirty ? 'wait' : 'ready'}
						label={saveError ? 'Error' : savePending ? 'Saving' : 'Unsaved'}
					/>
					<span>{saveError || 'Settings changes are not saved yet.'}</span>
					<button
						type="button"
						class="control control--compact"
						disabled={!dirty || savePending}
						onclick={resetDraft}
					>
						Reset
					</button>
					<button
						type="button"
						class="control control--compact control--ready"
						disabled={!dirty || savePending}
						onclick={saveSettings}
					>
						{savePending ? 'Saving' : 'Save'}
					</button>
				</div>
			{/if}
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
	.archive-actions,
	.settings-save-dock {
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
		grid-template-columns: repeat(6, minmax(0, 1fr));
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

	.settings-save-dock {
		background: var(--mf-bg-strip);
		border: var(--mf-border-strong);
		border-left: 2px solid var(--mf-wait-fg);
		bottom: var(--mf-space-5);
		box-shadow: var(--mf-shadow-2);
		grid-column: 1 / -1;
		justify-content: flex-end;
		left: var(--mf-space-5);
		padding: var(--mf-space-3);
		position: fixed;
		right: var(--mf-space-5);
		z-index: 30;
	}

	.settings-save-dock span:not(:global(.state-badge span)) {
		color: var(--mf-fg-secondary);
		font-size: var(--mf-text-xs);
		margin-right: auto;
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
	.encode-defaults-readout span,
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

	.metadata-grid {
		align-items: end;
		display: grid;
		gap: var(--mf-space-4);
		grid-template-columns: minmax(220px, 0.9fr) minmax(300px, 1.5fr) minmax(190px, auto);
		padding: var(--mf-space-5);
	}

	.metadata-grid--tmdb {
		border-top: var(--mf-border);
		grid-template-columns: minmax(280px, 1fr) minmax(190px, auto);
	}

	.metadata-toggle {
		align-self: center;
		justify-self: start;
	}

	.metadata-status {
		align-items: center;
		display: flex;
		gap: var(--mf-space-3);
		justify-content: flex-end;
	}

	.metadata-status code {
		color: var(--mf-fg-secondary);
		font-size: var(--mf-text-xs);
	}

	.metadata-mappings {
		border-top: var(--mf-border);
	}

	.metadata-help {
		border-top: var(--mf-border);
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-sm);
		line-height: 1.5;
		margin: 0;
		padding: var(--mf-space-4) var(--mf-space-5);
	}

	.encode-defaults-grid {
		align-items: end;
		display: grid;
		gap: var(--mf-space-4);
		grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
		padding: var(--mf-space-5);
	}

	.encode-defaults-grid .field--number {
		max-width: none;
	}

	.storage-readout,
	.encode-defaults-readout,
	.danger-zone > div,
	.rail-row {
		display: grid;
		gap: var(--mf-space-2);
		min-width: 0;
	}

	.storage-readout strong,
	.encode-defaults-readout strong,
	.danger-zone strong,
	.rail-row strong,
	.host-editor__head strong,
	.schedule-row strong {
		font-size: var(--mf-text-sm);
		font-weight: var(--mf-weight-semibold);
		overflow-wrap: anywhere;
	}

	.storage-readout small,
	.encode-defaults-readout small,
	.danger-zone small,
	.rail-row small,
	.option-hint,
	.muted-copy,
	.schedule-summary,
	.host-editor__head span {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-xs);
	}

	.schedule-list,
	.host-editor-list,
	.host-runtime-board,
	.rail-save,
	.rail-list {
		display: grid;
		gap: var(--mf-space-4);
		padding: var(--mf-space-5);
	}

	.rail-save {
		gap: var(--mf-space-3);
	}

	.rail-save p {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-xs);
		line-height: var(--mf-leading-snug);
		margin: 0;
	}

	.rail-save__actions {
		display: grid;
		gap: var(--mf-space-3);
		grid-template-columns: repeat(2, minmax(0, 1fr));
	}

	.rail-save__actions .control {
		width: 100%;
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
		.metadata-grid,
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
		.archive-actions,
		.settings-save-dock {
			align-items: stretch;
		}

		.settings-save-dock {
			left: var(--mf-space-3);
			right: var(--mf-space-3);
		}

		.settings-save-dock span:not(:global(.state-badge span)) {
			flex-basis: 100%;
		}

		.settings-header__actions .control,
		.panel-actions .control,
		.archive-actions .control,
		.settings-save-dock .control {
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

	/* Human settings surface */
	.settings-console {
		display: block;
		margin: 0 auto;
		max-width: 1088px;
		min-height: 0;
		padding: 34px 24px 64px;
	}

	.settings-console__main {
		gap: 18px;
		padding: 0;
	}

	.settings-console__rail,
	.settings-overview {
		display: none;
	}

	.settings-header {
		align-items: flex-end;
		border: 0;
		display: flex;
		gap: 20px;
		justify-content: space-between;
		padding: 0 0 8px;
	}

	.settings-header > div:first-child {
		display: grid;
		gap: 5px;
	}

	.settings-header h1 {
		font-size: clamp(22px, 2.5vw, 28px);
		font-weight: 600;
		letter-spacing: -0.02em;
	}

	.settings-header p {
		color: var(--mf-fg-secondary);
		font-size: 14px;
	}

	:global(.settings-console .mf-eyebrow) {
		background: var(--mf-active-bg);
		border-radius: 999px;
		color: var(--mf-active-fg);
		font-family: var(--mf-font-sans);
		font-size: 11px;
		font-weight: 700;
		letter-spacing: 0.02em;
		padding: 6px 9px;
		text-transform: none;
		width: fit-content;
	}

	.settings-header__actions {
		display: none;
	}

	.settings-header__actions.has-changes {
		align-items: center;
		background: var(--mf-bg-panel);
		border: 1px solid var(--mf-wait-line);
		border-radius: var(--mf-radius-3);
		display: flex;
		gap: 8px;
		padding: 8px;
	}

	.notice {
		background: var(--mf-bg-panel);
		border: 1px solid var(--mf-line);
		border-radius: var(--mf-radius-3);
		color: var(--mf-fg-secondary);
		padding: 12px 14px;
	}

	.notice--fail {
		background: var(--mf-fail-bg);
		border-color: var(--mf-fail-line);
		color: var(--mf-fail-fg);
	}

	.notice--ready {
		background: var(--mf-ready-bg);
		border-color: var(--mf-ready-line);
		color: var(--mf-ready-fg);
	}

	.settings-section {
		border: 0;
		display: grid;
		gap: 12px;
		padding: 0;
	}

	.settings-section__head {
		align-items: flex-end;
		border: 0;
		display: flex;
		gap: 16px;
		justify-content: space-between;
		padding: 14px 2px 0;
	}

	.settings-section__head > div {
		display: grid;
		gap: 5px;
	}

	.settings-section__head h2 {
		font-size: 18px;
		font-weight: 600;
		letter-spacing: -0.01em;
	}

	.settings-section__head p {
		color: var(--mf-fg-tertiary);
		font-size: 12px;
		max-width: 360px;
		text-align: right;
	}

	.settings-anchor {
		display: block;
		scroll-margin-top: 18px;
	}

	.table-wrap {
		background: var(--mf-bg-panel);
		border: 0;
		overflow-x: auto;
	}

	.settings-table {
		background: var(--mf-bg-panel);
		color: var(--mf-fg-primary);
	}

	.settings-table th {
		background: var(--mf-bg-panel-2);
		border-bottom: 1px solid var(--mf-line);
		color: var(--mf-fg-tertiary);
		font-family: var(--mf-font-sans);
		font-size: 11px;
		letter-spacing: 0.03em;
		text-transform: none;
	}

	.settings-table td {
		border-bottom: 1px solid var(--mf-line-muted);
		color: var(--mf-fg-secondary);
		font-family: var(--mf-font-sans);
		padding: 12px;
	}

	.library-cell {
		align-items: end;
	}

	.swatch-field {
		display: none;
	}

	.field,
	select.field,
	textarea.field {
		background: var(--mf-bg-input);
		border: 1px solid var(--mf-line-strong);
		border-radius: var(--mf-radius-2);
		color: var(--mf-fg-primary);
		font-family: var(--mf-font-sans);
		min-height: 36px;
	}

	.field:focus,
	select.field:focus,
	textarea.field:focus {
		border-color: var(--mf-active-fg);
		box-shadow: var(--mf-ring-focus);
		outline: none;
	}

	.stacked-field > span,
	.library-key-field > span,
	.option-label,
	.storage-readout span,
	.encode-defaults-readout span,
	.host-runtime-card span,
	.host-editor span,
	.schedule-row span {
		color: var(--mf-fg-tertiary);
		font-family: var(--mf-font-sans);
		font-size: 11px;
		letter-spacing: 0.02em;
		text-transform: none;
	}

	.storage-grid,
	.encode-defaults-grid,
	.host-grid,
	.host-options {
		gap: 10px;
		padding: 12px;
	}

	.storage-readout,
	.encode-defaults-readout,
	.schedule-row,
	.host-runtime-card,
	.host-editor,
	.danger-zone,
	.toggle-chip,
	.day-pill {
		background: var(--mf-bg-panel-2);
		border: 1px solid var(--mf-line-muted);
		border-radius: var(--mf-radius-2);
		box-shadow: none;
		color: var(--mf-fg-primary);
	}

	.storage-readout,
	.encode-defaults-readout,
	.schedule-row,
	.host-runtime-card,
	.host-editor,
	.danger-zone {
		padding: 12px;
	}

	.schedule-list,
	.host-runtime-board,
	.host-editor-list {
		gap: 8px;
		padding: 12px;
	}

	.host-editor__head,
	.schedule-row__fields {
		border: 0;
		padding: 0 0 10px;
	}

	.host-advanced {
		background: var(--mf-bg-panel);
		border: 1px solid var(--mf-line);
		border-radius: var(--mf-radius-2);
		color: var(--mf-fg-secondary);
	}

	.host-advanced summary {
		color: var(--mf-fg-primary);
		font-family: var(--mf-font-sans);
		font-weight: 600;
		padding: 11px 12px;
	}

	.panel-actions,
	.archive-actions {
		background: transparent;
		border: 0;
		gap: 8px;
		padding: 12px;
	}

	.control {
		background: var(--mf-bg-panel);
		border: 1px solid var(--mf-line-strong);
		border-radius: var(--mf-radius-2);
		color: var(--mf-fg-primary);
		font-family: var(--mf-font-sans);
		font-weight: 600;
		min-height: 34px;
		padding: 0 11px;
	}

	.control:hover {
		background: var(--mf-bg-panel-2);
		border-color: var(--mf-active-line);
		color: var(--mf-active-fg);
	}

	.control--ready {
		background: var(--mf-active-solid);
		border-color: var(--mf-active-solid);
		color: var(--mf-fg-on-accent);
	}

	.control--danger {
		background: var(--mf-bg-panel);
		border-color: var(--mf-fail-line);
		color: var(--mf-fail-fg);
	}

	.control--armed {
		background: var(--mf-fail-solid);
		border-color: var(--mf-fail-solid);
		color: var(--mf-fg-on-accent);
	}

	.settings-section--danger {
		border: 0;
		padding: 0;
	}

	.settings-section--danger .settings-section__head {
		border: 0;
	}

	.settings-save-dock {
		background: var(--mf-bg-panel);
		border: 1px solid var(--mf-wait-line);
		border-radius: var(--mf-radius-3);
		bottom: 18px;
		box-shadow: var(--mf-shadow-modal);
		color: var(--mf-fg-primary);
		left: 50%;
		max-width: 520px;
		padding: 9px;
		position: fixed;
		right: auto;
		transform: translateX(-50%);
		width: calc(100% - 32px);
		z-index: 45;
	}

	.empty-note {
		color: var(--mf-fg-secondary);
	}

	button:focus-visible,
	a:focus-visible,
	input:focus-visible,
	select:focus-visible,
	textarea:focus-visible,
	summary:focus-visible {
		box-shadow: var(--mf-ring-focus);
		outline: none;
	}

	@media (max-width: 680px) {
		.settings-console {
			padding: 26px 12px 48px;
		}

		.settings-header,
		.settings-section__head {
			align-items: stretch;
			flex-direction: column;
		}

		.settings-section__head p {
			max-width: none;
			text-align: left;
		}

		.settings-header__actions.has-changes {
			align-items: stretch;
			flex-wrap: wrap;
		}

		.settings-header__actions.has-changes :global(.state-badge) {
			flex-basis: 100%;
		}

		.table-wrap {
			overflow: visible;
		}

		.settings-table--libraries,
		.settings-table--metadata {
			display: block;
			min-width: 0;
			width: 100%;
		}

		.settings-table--metadata thead {
			display: none;
		}

		.settings-table--metadata tbody,
		.settings-table--metadata tr,
		.settings-table--metadata td {
			display: block;
			width: 100%;
		}

		.settings-table--metadata tr {
			border-bottom: var(--mf-border-muted);
			display: grid;
			gap: var(--mf-space-3);
			padding: var(--mf-space-4);
		}

		.settings-table--metadata td {
			border-bottom: 0;
			height: auto;
			min-width: 0;
			padding: 0;
		}

		.settings-table--metadata td::before {
			color: var(--mf-fg-tertiary);
			display: block;
			font-size: var(--mf-text-2xs);
			font-weight: var(--mf-weight-semibold);
			letter-spacing: 0.06em;
			margin-bottom: var(--mf-space-2);
			text-transform: uppercase;
		}

		.settings-table--metadata td:nth-child(1)::before {
			content: 'Library';
		}

		.settings-table--metadata td:nth-child(2)::before {
			content: 'Path reported by Plex';
		}
	}
</style>
