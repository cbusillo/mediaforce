<script lang="ts">
	import { invalidateAll } from '$app/navigation';
	import { postJson } from '$lib/api/client';
	import type {
		HostsPayload,
		SettingsHost,
		SettingsLibrary,
		SettingsPayload,
		ScheduleProfile
	} from '$lib/api/types';
	import Button from '$lib/components/Button.svelte';
	import HostCard from '$lib/components/HostCard.svelte';
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

	let libraries = $state<SettingsLibrary[]>([]);
	let remoteHosts = $state<SettingsHost[]>([]);
	let scheduleProfiles = $state<ScheduleProfile[]>([]);
	let transcodeRoot = $state('');
	let isSaving = $state(false);

	$effect(() => {
		if (!libraries.length) {
			libraries = settings.libraries
				.filter((library) => library.key || library.path)
				.map((library) => ({ ...library }));
		}
		if (!remoteHosts.length) {
			remoteHosts = settings.remote_hosts
				.filter((host) => host.label || host.host)
				.map((host) => ({ ...host }));
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
		libraries = [...libraries, { index: String(libraries.length), key: '', path: '' }];
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
				priority: '0',
				max_parallel_encodes: '1',
				schedule_profile: settings.schedule_profile_options[0]?.key ?? 'always',
				capabilities: ['encode_queue', 'sample_calibration']
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
			const response = await postJson<{ message: string }>('/api/settings', {
				libraries,
				remote_hosts: remoteHosts,
				transcode_root: transcodeRoot,
				encode_queue_scheduler: settings.encode_queue_scheduler,
				schedule_profiles: scheduleProfiles
			});
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
</script>

<svelte:head>
	<title>Settings · Media Harness</title>
</svelte:head>

<div class="page-stack">
	<Panel>
		<div class="settings-hero">
			<SectionHead
				eyebrow="Runtime Settings"
				heading="Keep machine paths and remote hosts out of checked-in config"
				lede="Use one coherent control surface for libraries, transcode roots, remote hosts, and queue windows."
				size="section"
			/>
			<div class="path-card compact-paths">
				<p class="eyebrow-copy">Paths</p>
				<p class="mono-copy">Runtime file: {settings.runtime_settings_path}</p>
				<p class="mono-copy">Repo defaults: {settings.repo_config_path}</p>
			</div>
		</div>
	</Panel>

	<div class="save-row">
		<Button loading={isSaving} onclick={saveSettings}>Save Runtime Settings</Button>
	</div>

	<div class="settings-grid">
		<Panel>
			<div class="panel-stack">
				<div class="section-row">
					<SectionHead
						eyebrow="Libraries"
						heading="Mounted media roots"
						lede="These become the top-level prefixes in the harness."
					/>
					<Button variant="secondary" onclick={addLibrary}>Add Library</Button>
				</div>
				<div class="row-stack">
					{#each libraries as library, index (`library-${index}`)}
						<div class="editor-card two-col">
							<label class="field-block">
								<span class="eyebrow-copy">Library name</span>
								<input bind:value={library.key} placeholder="movies" />
							</label>
							<label class="field-block">
								<span class="eyebrow-copy">Mounted path</span>
								<input bind:value={library.path} placeholder="/Volumes/media/movies" />
							</label>
							<Button variant="ghost" onclick={() => removeLibrary(index)}>Remove</Button>
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
		<div class="panel-stack">
			<div class="section-row">
				<SectionHead
					eyebrow="Hosts"
					heading="Remote encode machines"
					lede="Host policy should be explicit, scannable, and easy to edit."
				/>
				<Button variant="secondary" onclick={addHost}>Add Host</Button>
			</div>
			<div class="row-stack">
				{#each remoteHosts as host, index (`host-${index}-${host.host || host.label}`)}
					<div class="editor-card host-card-editor">
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
								<input
									bind:value={host.repo_path}
									placeholder="/Users/.../mediaforce"
								/>
							</label>
							<label class="field-block">
								<span class="eyebrow-copy">Wake MAC</span>
								<input bind:value={host.wake_mac} placeholder="Optional" />
							</label>
							<label class="field-block">
								<span class="eyebrow-copy">Priority</span>
								<input bind:value={host.priority} />
							</label>
							<label class="field-block">
								<span class="eyebrow-copy">Parallel encodes</span>
								<input bind:value={host.max_parallel_encodes} />
							</label>
							<label class="field-block">
								<span class="eyebrow-copy">Schedule profile</span>
								<select bind:value={host.schedule_profile}>
									{#each settings.schedule_profile_options as option (option.key)}
										<option value={option.key}>{option.label}</option>
									{/each}
								</select>
							</label>
						</div>
						<div class="capability-row">
							{#each settings.host_capability_options as capability (capability.key)}
								<label class="capability-pill">
									<input
										type="checkbox"
										checked={host.capabilities.includes(capability.key)}
										onchange={() => toggleCapability(index, capability.key)}
									/>
									<span>{capability.label}</span>
								</label>
							{/each}
						</div>
						<div class="editor-actions">
							<Button variant="ghost" onclick={() => removeHost(index)}>Remove Host</Button>
						</div>
					</div>
				{/each}
			</div>
		</div>
	</Panel>

	<div class="settings-grid">
		<Panel>
			<div class="panel-stack">
				<div class="section-row">
					<SectionHead
						eyebrow="Schedule Profiles"
						heading="Custom queue windows"
						lede={settings.encode_queue_scheduler.summary}
					/>
					<Button variant="secondary" onclick={addSchedule}>Add Window</Button>
				</div>
				<div class="row-stack">
					{#each scheduleProfiles as profile, index (`profile-${index}-${profile.key}`)}
						<div class="editor-card two-col compact-card">
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
								<input bind:value={profile.start_hour} />
							</label>
							<label class="field-block">
								<span class="eyebrow-copy">End</span>
								<input bind:value={profile.end_hour} />
							</label>
							<Button variant="ghost" onclick={() => removeSchedule(index)}>Remove</Button>
						</div>
					{/each}
				</div>
			</div>
		</Panel>

		<Panel>
			<div class="panel-stack">
				<SectionHead
					eyebrow="Runtime Status"
					heading="Current host state"
					lede="These runtime snapshots stay visible while you edit host policy."
				/>
				<div class="host-grid">
					{#each hosts.hosts as host (host.key)}
						<HostCard {host} />
					{/each}
				</div>
			</div>
		</Panel>
	</div>
</div>

<style>
	.page-stack,
	.panel-stack,
	.row-stack {
		display: grid;
		gap: var(--space-4);
	}

	.save-row {
		display: flex;
		justify-content: flex-end;
	}

	.settings-hero {
		display: grid;
		grid-template-columns: minmax(0, 1.2fr) minmax(280px, 0.8fr);
		gap: var(--space-5);
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

	.path-card,
	.editor-card {
		display: grid;
		gap: var(--space-3);
		padding: 1rem 1.1rem;
		border-radius: var(--radius-md);
		background: var(--surface-2);
		border: 1px solid rgba(23, 35, 31, 0.08);
	}

	.two-col {
		grid-template-columns: repeat(2, minmax(0, 1fr));
		align-items: start;
	}

	.compact-card {
		grid-template-columns: repeat(4, minmax(0, 1fr)) auto;
	}

	.field-block {
		display: grid;
		gap: var(--space-2);
	}

	input,
	select {
		width: 100%;
		padding: 0.85rem 1rem;
		border-radius: var(--radius-md);
		border: 1px solid rgba(23, 35, 31, 0.12);
		background: rgba(255, 255, 255, 0.78);
		color: var(--ink);
	}

	.host-card-editor {
		gap: var(--space-4);
	}

	.capability-row {
		display: flex;
		gap: var(--space-2);
		flex-wrap: wrap;
	}

	.capability-pill {
		display: inline-flex;
		align-items: center;
		gap: 0.45rem;
		padding: 0.6rem 0.85rem;
		border-radius: var(--radius-pill);
		border: 1px solid rgba(15, 118, 110, 0.16);
		background: rgba(15, 118, 110, 0.08);
		color: var(--accent-deep);
	}

	.editor-actions {
		display: flex;
		justify-content: flex-end;
	}

	.host-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
		gap: var(--space-4);
	}

	@media (max-width: 980px) {
		.settings-grid,
		.settings-hero,
		.two-col,
		.compact-card {
			grid-template-columns: 1fr;
		}

		.save-row {
			justify-content: start;
		}
	}
</style>
