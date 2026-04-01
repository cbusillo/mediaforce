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
	import Button from '$lib/components/Button.svelte';
	import HostCard from '$lib/components/HostCard.svelte';
	import { hostSettingsAnchor } from '$lib/format';
	import Panel from '$lib/components/Panel.svelte';
	import SectionHead from '$lib/components/SectionHead.svelte';
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
	let hostActionState = $state<
		Record<
			string,
			{
				preparing: boolean;
				resettingTrust: boolean;
				password: string;
				showPassword: boolean;
			}
		>
	>({});

	const AB_AV1_MISSING_ISSUE = 'ab-av1 is not installed on the remote PATH.';
	const FFMPEG_MISSING_ISSUE = 'ffmpeg is not installed on the remote PATH.';
	const FFMPEG_VIDEOTOOLBOX_MISSING_ISSUE =
		'ffmpeg is missing VideoToolbox hardware decode required for H.264/H.265 sources.';

	const initialSettingsDraft = $derived.by(() => ({
		libraries: settings.libraries
			.filter((library) => library.key || library.path)
			.map((library) => ({ ...library })),
		remote_hosts: settings.remote_hosts
			.filter((host) => host.label || host.host)
			.map((host) => ({ ...host, capabilities: [...host.capabilities] })),
		transcode_root: settings.transcode_root,
		schedule_profiles: settings.schedule_profiles
			.filter((profile) => profile.key || profile.label)
			.map((profile) => ({ ...profile }))
	}));

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
		libraries = payload.libraries
			.filter((library) => library.key || library.path)
			.map((library) => ({ ...library }));
		remoteHosts = payload.remote_hosts
			.filter((host) => host.label || host.host)
			.map((host) => ({ ...host, capabilities: [...host.capabilities] }));
		scheduleProfiles = payload.schedule_profiles
			.filter((profile) => profile.key || profile.label)
			.map((profile) => ({ ...profile }));
		transcodeRoot = payload.transcode_root;
	}

	$effect(() => {
		if (!libraries.length) {
			libraries = settings.libraries
				.filter((library) => library.key || library.path)
				.map((library) => ({ ...library }));
		}
		if (!remoteHosts.length) {
			remoteHosts = settings.remote_hosts
				.filter((host) => host.label || host.host)
				.map((host) => ({ ...host, capabilities: [...host.capabilities] }));
		}
		if (!scheduleProfiles.length) {
			scheduleProfiles = settings.schedule_profiles
				.filter((profile) => profile.key || profile.label)
				.map((profile) => ({ ...profile }));
		}
		if (!transcodeRoot) {
			transcodeRoot = settings.transcode_root;
		}
	});

	function addLibrary() {
		libraries = [
			...libraries,
			{ index: String(libraries.length), key: '', path: '', color: '#0f766e' }
		];
	}

	function addHost() {
		remoteHosts = [
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
				schedule_profile: settings.schedule_profile_options[0]?.key ?? 'always',
				capabilities: ['encode_queue', 'sample_calibration'],
				source_roots_json: '',
				staging_root: ''
			}
		];
	}

	function addSchedule() {
		scheduleProfiles = [
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

	function removeLibrary(index: number) {
		libraries = libraries.filter((_, candidate) => candidate !== index);
	}

	function removeHost(index: number) {
		remoteHosts = remoteHosts.filter((_, candidate) => candidate !== index);
	}

	function removeSchedule(index: number) {
		scheduleProfiles = scheduleProfiles.filter((_, candidate) => candidate !== index);
	}

	function toggleCapability(index: number, capability: string) {
		remoteHosts = remoteHosts.map((host, candidate) => {
			if (candidate !== index) return host;
			const capabilities = host.capabilities.includes(capability)
				? host.capabilities.filter((value) => value !== capability)
				: [...host.capabilities, capability];
			return { ...host, capabilities };
		});
	}

	async function saveSettings() {
		isSaving = true;
		try {
			const response = await postJson<{ message: string; settings: SettingsPayload }>('/api/settings', {
				libraries,
				remote_hosts: remoteHosts,
				transcode_root: transcodeRoot,
				encode_queue_scheduler: settings.encode_queue_scheduler,
				schedule_profiles: scheduleProfiles
			});
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

	function hostActionKey(
		host: SettingsHost,
		runtimeHost: HostRuntime | null,
		index: number
	): string {
		return runtimeHost?.key || host.host || `${host.label || 'host'}-${index}`;
	}

	function getHostActionState(hostKey: string) {
		return (
			hostActionState[hostKey] ?? {
				preparing: false,
				resettingTrust: false,
				password: '',
				showPassword: false
			}
		);
	}

	function patchHostActionState(
		hostKey: string,
		patch: Partial<{
			preparing: boolean;
			resettingTrust: boolean;
			password: string;
			showPassword: boolean;
		}>
	) {
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

	function primaryHostActionLabel(runtimeHost: HostRuntime): string {
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

	function primaryHostActionHelp(runtimeHost: HostRuntime): string {
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

	function hasPrimaryHostAction(runtimeHost: HostRuntime): boolean {
		if (!runtimeHost.setup_supported) return false;
		return !(runtimeHost.missing_paths.length > 0 && runtimeHost.issues.length === 0);
	}

	function shouldShowHostActions(runtimeHost: HostRuntime): boolean {
		return (
			Boolean(runtimeHost.trust_reset_supported) ||
			(!runtimeHost.available && Boolean(runtimeHost.setup_supported) && hasPrimaryHostAction(runtimeHost))
		);
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
				{
					host_key: hostKey
				}
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

<div class="page-stack">
	<Panel padding="1.05rem 1.2rem">
		<div class="settings-hero">
			<SectionHead
				eyebrow="Runtime Settings"
				heading="Configure libraries, hosts, and queue windows"
				lede="The runtime file below is the live machine-specific contract."
				size="compact"
			/>
			<div class="path-card compact-paths">
				<p class="eyebrow-copy">Paths</p>
				<p class="mono-copy">Runtime file: {settings.runtime_settings_path}</p>
				<p class="mono-copy">Repo defaults: {settings.repo_config_path}</p>
			</div>
		</div>
	</Panel>

	<div class="settings-grid">
		<Panel>
			<div class="panel-stack">
				<div class="section-row">
					<SectionHead
						eyebrow="Libraries"
						heading="Mounted media roots"
						lede="These become the top-level prefixes throughout Mediaforce."
						size="compact"
					/>
					<Button variant="secondary" onclick={addLibrary}>Add Library</Button>
				</div>
				<div class="row-stack">
					{#each libraries as library, index (`library-${index}`)}
						<div class="editor-card library-editor">
							<label class="field-block">
								<span class="eyebrow-copy">Library name</span>
								<input bind:value={library.key} placeholder="movies" />
							</label>
							<label class="field-block">
								<span class="eyebrow-copy">Mounted path</span>
								<input bind:value={library.path} placeholder="/Volumes/media/movies" />
							</label>
								<label class="field-block color-field">
									<span class="eyebrow-copy">Colors</span>
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
					{/each}
				</div>
			</div>
		</Panel>

		<Panel>
			<div class="panel-stack">
				<SectionHead
					eyebrow="Transcode"
					heading="Scratch and replacement root"
					lede="Archive path is derived automatically from the transcode root."
					size="compact"
				/>
				<label class="field-block">
					<span class="eyebrow-copy">Transcode root</span>
					<input bind:value={transcodeRoot} placeholder="/Volumes/media/transcode" />
				</label>
				<div class="path-card">
					<p class="eyebrow-copy">Derived archive path</p>
					<p class="mono-copy">{settings.archive_root}</p>
				</div>
			</div>
		</Panel>
	</div>

	<Panel>
		<div id="schedule-profiles" class="panel-stack">
			<div class="section-row">
				<SectionHead
					eyebrow="Schedule Profiles"
					heading="Custom queue windows"
					lede="Define reusable host windows for machines that should only accept queue work at certain times."
					size="compact"
				/>
				<Button variant="secondary" onclick={addSchedule}>Add Window</Button>
			</div>
			<div class="row-stack">
				{#each scheduleProfiles as profile, index (`profile-${profile.index || index}`)}
					<div class="editor-card schedule-editor">
						<label class="field-block">
							<span class="eyebrow-copy">Key</span>
							<input bind:value={profile.key} placeholder="quiet_hours" />
						</label>
						<label class="field-block">
							<span class="eyebrow-copy">Label</span>
							<input bind:value={profile.label} placeholder="Quiet Hours" />
						</label>
						<label class="field-block">
							<span class="eyebrow-copy">Start</span>
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
							<span class="eyebrow-copy">End</span>
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
								aria-label="Remove schedule window"
								title="Remove schedule window"
								onclick={() => removeSchedule(index)}
							>
								<svg viewBox="0 0 24 24" aria-hidden="true">
									<path
										d="M9 3h6l1 2h4v2H4V5h4l1-2Zm1 7h2v7h-2v-7Zm4 0h2v7h-2v-7ZM7 10h2v7H7v-7Zm-1 10h12a2 2 0 0 0 2-2V8H4v10a2 2 0 0 0 2 2Z"
									/>
								</svg>
							</button>
						</div>
					</div>
				{/each}
			</div>
		</div>
	</Panel>

	<Panel>
		<div id="remote-workers" class="panel-stack">
			<div class="section-row">
				<SectionHead
					eyebrow="Hosts"
					heading="Remote workers"
					lede="Edit policy and see the live runtime state in the same place."
					size="compact"
				/>
				<div class="section-actions-row">
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
				{#each remoteHosts as host, index (`host-${host.index || index}`)}
					{@const runtimeHost = runtimeHostsByKey.get(host.host) ?? null}
					{@const runtimeHostKey = hostActionKey(host, runtimeHost, index)}
					{@const runtimeActionState = getHostActionState(runtimeHostKey)}
					{@const showPrimaryHostAction = runtimeHost ? hasPrimaryHostAction(runtimeHost) : false}
					<div
						id={hostSettingsAnchor(host.host || `host-${index}`)}
						class="editor-card host-card-editor"
					>
						<div class="host-head">
							<div>
								<p class="eyebrow-copy">Remote worker</p>
								<h3>{host.label || 'Untitled host'}</h3>
								<p class="muted-copy">
									{host.host || 'Set an SSH target to bring this host online.'}
								</p>
							</div>
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
						<div class="host-editor-layout">
							<div class="two-col">
								<label class="field-block">
									<span class="eyebrow-copy">Label</span>
									<input bind:value={host.label} placeholder="M4 Studio" />
								</label>
								<label class="field-block">
									<span class="eyebrow-copy">SSH host</span>
									<input bind:value={host.host} placeholder="cbusillo@localhost" />
								</label>
								<label class="field-block">
									<span class="eyebrow-copy">Repo path</span>
									<input bind:value={host.repo_path} placeholder="/Users/.../mediaforce" />
								</label>
									<label class="field-block">
										<span class="eyebrow-copy">Wake MAC</span>
										<input bind:value={host.wake_mac} placeholder="Optional" />
									</label>
									<label class="field-block">
										<span class="eyebrow-copy">Start command</span>
										<input bind:value={host.start_command} placeholder="ssh prox-main.shiny pct start 103" />
									</label>
									<label class="field-block">
										<span class="eyebrow-copy">Stop command</span>
										<input bind:value={host.stop_command} placeholder="ssh prox-main.shiny pct shutdown 103" />
									</label>
									<label class="field-block">
										<span class="eyebrow-copy">Start timeout sec</span>
										<input type="number" min="1" step="1" bind:value={host.start_timeout_seconds} />
									</label>
									<label class="field-block">
										<span class="eyebrow-copy">Media access</span>
										<span class="select-shell">
											<select bind:value={host.media_access}>
												<option value="mounted">Mounted paths</option>
												<option value="stream">Two-way stream</option>
											</select>
										</span>
									</label>
									<label class="field-block">
										<span class="eyebrow-copy">Priority</span>
										<input type="number" min="0" step="1" bind:value={host.priority} />
								</label>
								<label class="field-block">
									<span class="eyebrow-copy">Parallel encodes</span>
									<input type="number" min="1" step="1" bind:value={host.max_parallel_encodes} />
								</label>
									<label class="field-block host-schedule-field">
											<span class="eyebrow-copy">Schedule profile</span>
											<span class="select-shell">
												<select bind:value={host.schedule_profile}>
													{#each settings.schedule_profile_options as option (option.key)}
														<option value={option.key}>{option.label}</option>
													{/each}
												</select>
											</span>
										</label>
									<label class="field-block">
										<span class="eyebrow-copy">Host staging root</span>
										<input bind:value={host.staging_root} placeholder="Defaults to global transcode root" />
									</label>
									<label class="field-block host-json-field">
										<span class="eyebrow-copy">Library path overrides</span>
										<textarea
											bind:value={host.source_roots_json}
											rows="5"
											placeholder={`{
  "movies": "/mnt/media/movies",
  "tv": "/mnt/media/tv"
}`}
										></textarea>
									</label>
								</div>
							{#if runtimeHost}
								<div class="host-runtime-column">
									<p class="eyebrow-copy">Live Status</p>
									<HostCard host={runtimeHost} />
									{#if shouldShowHostActions(runtimeHost)}
										<div class="host-actions-panel">
											<div class="host-actions-head">
												<p class="eyebrow-copy">Remote Actions</p>
												{#if isDirty}
													<p class="muted-copy">Save host edits before running remote actions.</p>
												{:else if showPrimaryHostAction}
													<p class="muted-copy">{primaryHostActionHelp(runtimeHost)}</p>
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
																	updateHostActionPassword(
																		runtimeHostKey,
																		(event.currentTarget as HTMLInputElement).value
																	)}
															placeholder="Only used for this setup run"
														/>
													</label>
												{/if}
												<div class="host-actions-row">
													<Button
														variant="secondary"
														loading={runtimeActionState.preparing}
														disabled={isDirty}
														onclick={() => prepareHost(runtimeHostKey, runtimeHost)}
													>
														{primaryHostActionLabel(runtimeHost)}
													</Button>
													{#if runtimeHost.setup_requires_password && !runtimeActionState.showPassword}
														<Button
															variant="ghost"
															disabled={isDirty}
															onclick={() => revealHostActionPassword(runtimeHostKey)}
														>
															Add Password
														</Button>
													{/if}
													{#if runtimeHost.trust_reset_supported}
														<Button
															variant="ghost"
															loading={runtimeActionState.resettingTrust}
															disabled={isDirty || runtimeActionState.preparing}
															onclick={() => resetHostTrust(runtimeHostKey)}
														>
															Reset SSH Trust
														</Button>
													{/if}
												</div>
											{:else if runtimeHost.trust_reset_supported}
												<div class="host-actions-row">
													<Button
														variant="ghost"
														loading={runtimeActionState.resettingTrust}
														disabled={isDirty}
														onclick={() => resetHostTrust(runtimeHostKey)}
													>
														Reset SSH Trust
													</Button>
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
					</div>
				{/each}
			</div>
		</div>
	</Panel>
</div>

{#if isDirty}
	<div class="settings-save-bar dirty">
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

	.page-stack {
		padding-bottom: 6.5rem;
	}

	.settings-hero {
		display: grid;
		grid-template-columns: minmax(0, 1fr) minmax(320px, 0.9fr);
		gap: var(--space-3);
		align-items: start;
	}

	.settings-grid {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: var(--space-4);
		align-items: start;
	}

	.section-row {
		display: flex;
		justify-content: space-between;
		gap: var(--space-3);
		align-items: start;
		flex-wrap: wrap;
	}

	.section-actions-row {
		display: flex;
		flex-wrap: wrap;
		gap: 0.7rem;
		align-items: center;
		justify-content: flex-end;
	}

	.refresh-status-button {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: 0.35rem;
		padding: 0.82rem 1.05rem;
		border-radius: var(--radius-md);
		border: 1px solid var(--border);
		background: rgba(255, 255, 255, 0.64);
		color: var(--ink);
		font-weight: 700;
		letter-spacing: -0.01em;
		transition:
			transform 150ms ease,
			opacity 150ms ease,
			background-color 150ms ease,
			border-color 150ms ease,
			box-shadow 150ms ease;
	}

	.refresh-status-button:hover:not(:disabled) {
		transform: translateY(-1px);
	}

	.refresh-status-button:disabled {
		opacity: 0.7;
		cursor: default;
	}

	.path-card,
	.editor-card {
		display: grid;
		gap: var(--space-3);
		padding: 0.95rem 1rem;
		border-radius: var(--radius-md);
		background: var(--surface-2);
		border: 1px solid rgba(23, 35, 31, 0.08);
	}

	.compact-paths {
		background: rgba(255, 255, 255, 0.62);
		gap: 0.65rem;
	}

	.library-editor {
		grid-template-columns: minmax(0, 0.78fr) minmax(0, 1.45fr) 5.1rem auto;
		align-items: end;
	}

	.color-field {
		min-width: 0;
	}

	.color-control {
		display: flex;
		gap: 0.65rem;
		align-items: center;
	}

	.two-col {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: var(--space-3);
		align-items: start;
	}

	.schedule-editor {
		grid-template-columns: repeat(4, minmax(0, 1fr)) auto;
		align-items: end;
	}

	.field-block {
		display: grid;
		gap: var(--space-2);
	}

	input,
	select,
	textarea {
		width: 100%;
		padding: 0.85rem 1rem;
		border-radius: var(--radius-md);
		border: 1px solid rgba(23, 35, 31, 0.14);
		background: rgba(255, 255, 255, 0.9);
		color: var(--ink);
		box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.7);
	}

	textarea {
		min-height: 7.5rem;
		resize: vertical;
		font-family: 'SFMono-Regular', Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace;
		line-height: 1.45;
	}

	input::placeholder {
		color: rgba(82, 101, 94, 0.6);
	}

	input[type='number'] {
		padding-right: 0.7rem;
	}

	input[type='color'] {
		padding: 0.2rem;
		height: 3rem;
		min-width: 3rem;
		cursor: pointer;
	}

	.select-shell {
		position: relative;
	}

	.select-shell::after {
		content: '';
		position: absolute;
		right: 1rem;
		top: 50%;
		width: 0.55rem;
		height: 0.55rem;
		border-right: 2px solid rgba(82, 101, 94, 0.82);
		border-bottom: 2px solid rgba(82, 101, 94, 0.82);
		transform: translateY(-65%) rotate(45deg);
		pointer-events: none;
	}

	.select-shell select {
		appearance: none;
		padding-right: 2.5rem;
	}

	.host-card-editor {
		gap: var(--space-4);
		background: rgba(255, 255, 255, 0.66);
	}

	.host-editor-layout {
		display: grid;
		grid-template-columns: minmax(0, 1.15fr) minmax(250px, 0.85fr);
		gap: var(--space-3);
		align-items: start;
	}

	.host-runtime-column {
		display: grid;
		gap: 0.6rem;
	}

	.host-actions-panel {
		display: grid;
		gap: 0.8rem;
		padding: 0.85rem 0.9rem;
		border-radius: var(--radius-md);
		border: 1px solid rgba(23, 35, 31, 0.08);
		background: rgba(255, 255, 255, 0.72);
	}

	.host-actions-head {
		display: grid;
		gap: 0.35rem;
	}

	.host-actions-head .muted-copy {
		margin: 0;
		font-size: 0.9rem;
	}

	.host-actions-row {
		display: flex;
		flex-wrap: wrap;
		gap: 0.65rem;
		align-items: center;
	}

	.inline-password-field {
		gap: 0.45rem;
	}

	.host-runtime-column :global(.host-card) {
		height: 100%;
	}

	.host-schedule-field {
		grid-column: 1 / -1;
	}

	.host-head {
		display: flex;
		justify-content: space-between;
		gap: var(--space-3);
		align-items: start;
		flex-wrap: nowrap;
	}

	h3 {
		font-size: 1.15rem;
		line-height: 1.1;
	}

	.icon-button {
		flex: 0 0 auto;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 2.45rem;
		height: 2.45rem;
		border-radius: 0.85rem;
		border: 1px solid rgba(138, 68, 19, 0.14);
		background: rgba(255, 255, 255, 0.78);
		color: #8a4413;
		transition:
			transform 150ms ease,
			background-color 150ms ease,
			border-color 150ms ease,
			box-shadow 150ms ease;
	}

	.icon-button:hover {
		transform: translateY(-1px);
		background: #efe2d8;
		border-color: rgba(138, 68, 19, 0.2);
	}

	.icon-button svg {
		width: 1.05rem;
		height: 1.05rem;
		fill: currentColor;
	}

	.capability-row {
		display: flex;
		gap: var(--space-2);
		flex-wrap: wrap;
	}

	.capability-pill {
		display: inline-flex;
		align-items: center;
		gap: 0.55rem;
		padding: 0.65rem 0.9rem;
		border-radius: var(--radius-pill);
		border: 1px solid rgba(23, 35, 31, 0.1);
		background: rgba(255, 255, 255, 0.72);
		color: var(--ink-soft);
		cursor: pointer;
		transition:
			transform 150ms ease,
			background-color 150ms ease,
			border-color 150ms ease,
			color 150ms ease,
			box-shadow 150ms ease;
	}

	.capability-pill:hover {
		transform: translateY(-1px);
		border-color: rgba(15, 118, 110, 0.16);
	}

	.capability-pill.checked {
		background: rgba(15, 118, 110, 0.1);
		border-color: rgba(15, 118, 110, 0.24);
		color: var(--accent-deep);
	}

	.capability-mark {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 1.15rem;
		height: 1.15rem;
		border-radius: 999px;
		background: rgba(23, 35, 31, 0.08);
		color: transparent;
		font-size: 0.72rem;
		font-weight: 700;
		line-height: 1;
	}

	.capability-pill.checked .capability-mark {
		background: var(--accent);
		color: white;
	}

	.capability-label {
		font-weight: 600;
	}

	.editor-actions {
		display: flex;
		justify-content: flex-end;
	}

	.compact-actions {
		align-items: end;
	}

	.host-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
		gap: var(--space-4);
	}

	.settings-save-bar {
		position: fixed;
		left: 0;
		right: 0;
		bottom: 1rem;
		z-index: 20;
		pointer-events: none;
		padding: 0 1rem;
	}

	.settings-save-shell {
		width: min(780px, calc(100vw - 2rem));
		margin: 0 auto;
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: var(--space-3);
		padding: 0.7rem 0.85rem;
		border-radius: 1.1rem;
		border: 1px solid rgba(23, 35, 31, 0.1);
		background: rgba(255, 252, 246, 0.94);
		box-shadow: 0 16px 36px rgba(23, 35, 31, 0.14);
		backdrop-filter: blur(18px);
		pointer-events: auto;
	}

	.settings-save-bar .muted-copy {
		font-size: 0.94rem;
	}

	@media (max-width: 980px) {
		.settings-grid,
		.settings-hero,
		.host-editor-layout,
		.library-editor,
		.schedule-editor {
			grid-template-columns: 1fr;
		}
	}

	@media (max-width: 720px) {
		.two-col {
			grid-template-columns: 1fr;
		}

		.settings-save-shell {
			align-items: stretch;
			flex-direction: column;
		}

		.host-head {
			flex-wrap: wrap;
		}

		.host-grid {
			grid-template-columns: 1fr;
		}
	}
</style>
