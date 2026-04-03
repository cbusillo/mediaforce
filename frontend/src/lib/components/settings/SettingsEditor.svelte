<script lang="ts">
	import type {
		HostRuntime,
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
		hostActionKey,
		getHostActionState,
		updateHostActionPassword,
		revealHostActionPassword,
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
		toggleCapability,
		saveSettings,
		refreshHostStatuses,
		prepareHost,
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
		hostActionKey: (host: SettingsHost, runtimeHost: HostRuntime | null, index: number) => string;
		getHostActionState: (hostKey: string) => {
			preparing: boolean;
			resettingTrust: boolean;
			password: string;
			showPassword: boolean;
		};
		updateHostActionPassword: (hostKey: string, value: string) => void;
		revealHostActionPassword: (hostKey: string) => void;
		primaryHostActionLabel: (runtimeHost: HostRuntime) => string;
		primaryHostActionHelp: (runtimeHost: HostRuntime) => string;
		hasPrimaryHostAction: (runtimeHost: HostRuntime) => boolean;
		shouldShowHostActions: (runtimeHost: HostRuntime) => boolean;
		addLibrary: () => void;
		addHost: () => void;
		addSchedule: () => void;
		removeLibrary: (index: number) => void;
		removeHost: (index: number) => void;
		removeSchedule: (index: number) => void;
		toggleCapability: (index: number, capability: string) => void;
		saveSettings: () => Promise<void>;
		refreshHostStatuses: () => Promise<void>;
		prepareHost: (hostKey: string, runtimeHost: HostRuntime) => Promise<void>;
		resetHostTrust: (hostKey: string) => Promise<void>;
	} = $props();

	function handleHostActionPasswordInput(
		hostKey: string,
		event: Event & { currentTarget: EventTarget & HTMLInputElement }
	): void {
		updateHostActionPassword(hostKey, event.currentTarget.value);
	}
</script>

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
									><span class="eyebrow-copy">Host staging root</span><input
										bind:value={host.staging_root}
										placeholder="Defaults to global transcode root"
									/></label
								>
								<label class="field-block host-json-field"
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
														onclick={() => prepareHost(runtimeHostKey, runtimeHost)}
														>{primaryHostActionLabel(runtimeHost)}</Button
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
															onclick={() => resetHostTrust(runtimeHostKey)}>Reset SSH Trust</Button
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

	.settings-grid {
		display: grid;
		grid-template-columns: 1.4fr 1fr;
		gap: var(--space-4);
	}

	.settings-hero {
		display: grid;
		grid-template-columns: minmax(0, 1.15fr) minmax(22rem, 32rem);
		gap: var(--space-3);
		align-items: start;
	}

	.compact-paths {
		min-width: 0;
		max-inline-size: 32rem;
		justify-self: end;
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
		grid-template-columns: repeat(4, minmax(0, 1fr)) auto;
		gap: 0.85rem;
		align-items: end;
	}

	.field-block {
		display: grid;
		gap: 0.4rem;
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
		gap: 1rem;
	}

	.host-head {
		display: flex;
		justify-content: space-between;
		gap: 1rem;
		align-items: start;
	}

	.host-head h3,
	.path-card p {
		margin: 0;
	}

	.host-editor-layout {
		display: grid;
		grid-template-columns: minmax(0, 1.3fr) minmax(260px, 0.9fr);
		gap: 1rem;
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
	}

	.host-actions-row,
	.capability-row {
		display: flex;
		gap: 0.65rem;
		flex-wrap: wrap;
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

		.library-editor,
		.schedule-editor,
		.two-col {
			grid-template-columns: 1fr;
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

		.compact-paths {
			max-inline-size: none;
			justify-self: stretch;
		}
	}
</style>
