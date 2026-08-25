<script lang="ts">
	import type {
		LibraryAvailability,
		LibraryPolicy,
		LibraryType,
		SettingsLibrary,
		SettingsPayload
	} from '$lib/api/types';
	import { libraryReadiness } from '$lib/settings/editor';
	import StateBadge from '../workstation/StateBadge.svelte';

	type BadgeTone = 'active' | 'ready' | 'wait' | 'fail' | 'idle';

	let {
		libraries,
		typeOptions,
		profileOptions,
		plexReady,
		plexTokenEnv,
		onLibrariesChange,
		onAdd,
		onRemove,
		onMove,
		onTypeChange
	}: {
		libraries: SettingsLibrary[];
		typeOptions: SettingsPayload['library_type_options'];
		profileOptions: SettingsPayload['library_profile_options'];
		plexReady: boolean;
		plexTokenEnv: string;
		onLibrariesChange: (libraries: SettingsLibrary[]) => void;
		onAdd: () => void;
		onRemove: (index: number) => void;
		onMove: (index: number, direction: -1 | 1) => void;
		onTypeChange: (index: number, libraryType: LibraryType) => void;
	} = $props();

	let selectedIndex = $state(0);
	const selected = $derived(libraries[selectedIndex] ?? null);

	$effect(() => {
		if (libraries.length === 0) selectedIndex = 0;
		else if (selectedIndex >= libraries.length) selectedIndex = libraries.length - 1;
	});

	function inputValue(event: Event): string {
		return (event.currentTarget as HTMLInputElement).value;
	}

	function selectValue(event: Event): string {
		return (event.currentTarget as HTMLSelectElement).value;
	}

	function patchSelected(patch: Partial<SettingsLibrary>) {
		if (!selected) return;
		onLibrariesChange(
			libraries.map((library, index) => {
				if (index !== selectedIndex) return library;
				const updated = { ...library, ...patch };
				return { ...updated, readiness: libraryReadiness(updated) };
			})
		);
	}

	function patchPolicy(patch: Partial<LibraryPolicy>) {
		if (!selected) return;
		patchSelected({ policy: { ...selected.policy, ...patch } });
	}

	function readinessTone(state: SettingsLibrary['readiness']['state']): BadgeTone {
		if (state === 'ready') return 'ready';
		if (state === 'incomplete' || state === 'browse_only') return 'wait';
		if (state === 'blocked') return 'fail';
		return 'idle';
	}

	function availabilityLabel(availability: LibraryAvailability): string {
		if (availability === 'production') return 'Production';
		if (availability === 'browse_only') return 'Browse only';
		return 'Disabled';
	}
</script>

<div class="library-workstation">
	<section class="library-master" aria-label="Configured library roots">
		<header class="library-master__head">
			<div>
				<strong>Library roots</strong>
				<span>{libraries.length.toLocaleString('en-US')} configured</span>
			</div>
			<button type="button" class="control" onclick={onAdd}>Add library</button>
		</header>

		<div class="library-root-list">
			{#each libraries as library, index (`root-${library.key}-${index}`)}
				<button
					type="button"
					class="library-root-row"
					class:is-selected={selectedIndex === index}
					onclick={() => (selectedIndex = index)}
				>
					<span class="library-root-row__order mf-mono">{String(index + 1).padStart(2, '0')}</span>
					<span class="library-root-row__identity">
						<strong>{library.label || 'Untitled library'}</strong>
						<small>{library.path || 'Local path needed'}</small>
					</span>
					<span class="library-root-row__signals">
						<small>{typeOptions.find((option) => option.key === library.library_type)?.label}</small
						>
						<StateBadge
							tone={readinessTone(libraryReadiness(library).state)}
							label={libraryReadiness(library).label}
						/>
					</span>
				</button>
			{/each}
		</div>
	</section>

	<section class="library-inspector" aria-label="Selected library settings">
		{#if selected}
			<header class="library-inspector__head">
				<div>
					<span class="mf-eyebrow">Selected root</span>
					<h3>{selected.label || 'New library'}</h3>
					<p>{libraryReadiness(selected).detail}</p>
				</div>
				<StateBadge
					tone={readinessTone(libraryReadiness(selected).state)}
					label={libraryReadiness(selected).label}
				/>
			</header>

			<div class="library-inspector__actions" aria-label="Library order and removal">
				<button
					type="button"
					class="control control--quiet"
					disabled={selectedIndex === 0}
					onclick={() => onMove(selectedIndex, -1)}>Move up</button
				>
				<button
					type="button"
					class="control control--quiet"
					disabled={selectedIndex === libraries.length - 1}
					onclick={() => onMove(selectedIndex, 1)}>Move down</button
				>
				<button
					type="button"
					class="control control--danger"
					onclick={() => onRemove(selectedIndex)}
				>
					Remove library
				</button>
			</div>

			<div class="library-form-grid">
				<label class="stacked-field">
					<span>Label</span>
					<input
						class="field"
						value={selected.label}
						placeholder="Movies"
						oninput={(event) => patchSelected({ label: inputValue(event) })}
					/>
				</label>
				<label class="stacked-field">
					<span>Root ID</span>
					<input class="field mf-mono" value={selected.key} readonly />
					<small>Stable identity for scans, computers, and saved work.</small>
				</label>
				<label class="stacked-field library-form-grid__wide">
					<span>Local path</span>
					<input
						class="field field--path"
						value={selected.path}
						placeholder="/Volumes/media/movies"
						oninput={(event) => patchSelected({ path: inputValue(event) })}
					/>
				</label>
				<label class="stacked-field">
					<span>Library type</span>
					<select
						class="field"
						value={selected.library_type}
						onchange={(event) => {
							const target = event.currentTarget as HTMLSelectElement;
							onTypeChange(selectedIndex, target.value as LibraryType);
							target.value = selected.library_type;
						}}
					>
						{#each typeOptions as option (option.key)}
							<option value={option.key}>{option.label}</option>
						{/each}
					</select>
				</label>
				<label class="stacked-field">
					<span>Availability</span>
					<select
						class="field"
						value={selected.availability}
						onchange={(event) =>
							patchSelected({ availability: selectValue(event) as LibraryAvailability })}
					>
						<option value="production" disabled={selected.library_type === 'spatial'}
							>Production</option
						>
						<option value="browse_only">Browse only</option>
						<option value="disabled">Disabled</option>
					</select>
					<small>{availabilityLabel(selected.availability)}</small>
				</label>
				<label class="stacked-field">
					<span>Default processing profile</span>
					<select
						class="field"
						value={selected.default_profile}
						disabled={selected.availability !== 'production'}
						onchange={(event) => patchSelected({ default_profile: selectValue(event) })}
					>
						{#each profileOptions[selected.library_type] as option (option.key)}
							<option value={option.key}>{option.label}</option>
						{/each}
					</select>
					{#if selected.availability !== 'production'}
						<small>No processing profile is required outside Production.</small>
					{/if}
				</label>
				<label class="stacked-field">
					<span>Library color</span>
					<div class="color-field">
						<input
							type="color"
							value={selected.color}
							oninput={(event) => patchSelected({ color: inputValue(event) })}
						/>
						<code>{selected.color}</code>
					</div>
				</label>
			</div>

			<section class="library-policy" aria-label="Library-specific policy">
				<header>
					<div>
						<span class="mf-eyebrow">Type policy</span>
						<h4>{typeOptions.find((option) => option.key === selected.library_type)?.label}</h4>
					</div>
					<small>Only settings relevant to this library type appear here.</small>
				</header>

				{#if selected.library_type === 'tv'}
					<div class="policy-grid">
						<label class="stacked-field">
							<span>Current-season protection</span>
							<select
								class="field"
								value={selected.policy.series_lifecycle_mode ?? 'auto'}
								onchange={(event) =>
									patchPolicy({
										series_lifecycle_mode: selectValue(event) as 'auto' | 'on' | 'off'
									})}
							>
								<option value="auto">Automatic from TMDB</option>
								<option value="on">Always protect latest season</option>
								<option value="off">Off</option>
							</select>
						</label>
						<label class="stacked-field">
							<span>Release after inactive days</span>
							<input
								class="field"
								type="number"
								min="1"
								max="3650"
								value={selected.policy.current_season_inactive_days ?? 365}
								oninput={(event) =>
									patchPolicy({ current_season_inactive_days: Number(inputValue(event)) })}
							/>
						</label>
						<label class="stacked-field">
							<span>Hold newly acquired seasons</span>
							<input
								class="field"
								type="number"
								min="0"
								max="365"
								value={selected.policy.season_acquisition_hold_days ?? 30}
								oninput={(event) =>
									patchPolicy({ season_acquisition_hold_days: Number(inputValue(event)) })}
							/>
						</label>
						<label class="stacked-field">
							<span>Refresh series status after days</span>
							<input
								class="field"
								type="number"
								min="1"
								max="365"
								value={selected.policy.series_metadata_stale_days ?? 7}
								oninput={(event) =>
									patchPolicy({ series_metadata_stale_days: Number(inputValue(event)) })}
							/>
						</label>
					</div>
				{:else if selected.library_type === 'movie'}
					<div class="policy-grid">
						<div class="policy-readout"><span>Grouping</span><strong>Movie title</strong></div>
						<div class="policy-readout"><span>Editions</span><strong>Keep separate</strong></div>
						<label class="stacked-field">
							<span>Extras by default</span>
							<select
								class="field"
								value={selected.policy.extras ?? 'exclude'}
								onchange={(event) =>
									patchPolicy({ extras: selectValue(event) as 'exclude' | 'include' })}
							>
								<option value="exclude">Exclude</option>
								<option value="include">Include deliberately</option>
							</select>
						</label>
						<label class="stacked-field">
							<span>Ranking</span>
							<select
								class="field"
								value={selected.policy.ranking ?? 'oldest_added_first'}
								onchange={(event) =>
									patchPolicy({
										ranking: selectValue(event) as 'oldest_added_first' | 'largest_first'
									})}
							>
								<option value="oldest_added_first">Oldest added first</option>
								<option value="largest_first">Largest first</option>
							</select>
						</label>
					</div>
				{:else if selected.library_type === 'spatial'}
					<div class="policy-grid">
						<label class="stacked-field library-form-grid__wide">
							<span>Playback target</span>
							<input
								class="field"
								value={selected.policy.playback_target ?? ''}
								placeholder="Headset and player application"
								oninput={(event) => patchPolicy({ playback_target: inputValue(event) })}
							/>
						</label>
						<label class="stacked-field">
							<span>Stereo layout</span>
							<select
								class="field"
								value={selected.policy.stereo_layout ?? 'unknown'}
								onchange={(event) =>
									patchPolicy({
										stereo_layout: selectValue(event) as LibraryPolicy['stereo_layout']
									})}
							>
								<option value="unknown">Needs classification</option>
								<option value="side_by_side">Side by side</option>
								<option value="top_bottom">Top / bottom</option>
								<option value="frame_sequential">Frame sequential</option>
							</select>
						</label>
						<label class="stacked-field">
							<span>Projection</span>
							<select
								class="field"
								value={selected.policy.projection ?? 'unknown'}
								onchange={(event) =>
									patchPolicy({ projection: selectValue(event) as LibraryPolicy['projection'] })}
							>
								<option value="unknown">Needs classification</option>
								<option value="flat">Flat stereoscopic</option>
								<option value="equirectangular_180">Equirectangular 180</option>
								<option value="equirectangular_360">Equirectangular 360</option>
							</select>
						</label>
						<div class="policy-readout"><span>Geometry</span><strong>Preserve source</strong></div>
						<div class="policy-readout">
							<span>Container / codec</span><strong>Unqualified</strong>
						</div>
					</div>
					<p class="policy-warning">
						Production remains blocked until #188 validates geometry, streams, metadata, audio, and
						target-device playback.
					</p>
				{:else}
					<div class="policy-grid">
						<label class="stacked-field">
							<span>Default work unit</span>
							<select
								class="field"
								value={selected.policy.grouping ?? 'folder'}
								onchange={(event) =>
									patchPolicy({ grouping: selectValue(event) as 'folder' | 'file' })}
							>
								<option value="folder">Bounded folder</option>
								<option value="file">Exact file</option>
							</select>
						</label>
						<p class="policy-copy">
							Other libraries never inherit season, edition, or spatial-video behavior.
						</p>
					</div>
				{/if}
			</section>

			<section class="library-plex" aria-label="Plex mapping">
				<div>
					<span class="mf-eyebrow">Plex mapping</span>
					<h4>{selected.plex_path.trim() ? 'Custom server path' : 'Uses local path'}</h4>
					<p>Leave blank when Plex reports the same path Mediaforce uses.</p>
				</div>
				<label class="stacked-field">
					<span>Path reported by Plex</span>
					<input
						class="field field--path"
						value={selected.plex_path}
						placeholder="/mnt/media/movies"
						oninput={(event) => patchSelected({ plex_path: inputValue(event) })}
					/>
				</label>
				<div class="plex-readiness">
					<StateBadge
						tone={plexReady ? 'ready' : 'wait'}
						label={plexReady ? 'Token ready' : 'Token needed'}
					/>
					<code>{plexTokenEnv}</code>
					<small>Token values are never stored or shown here.</small>
				</div>
			</section>
		{:else}
			<div class="library-empty">
				<strong>No library selected</strong>
				<p>Add a root to configure where Mediaforce scans.</p>
			</div>
		{/if}
	</section>
</div>

<style>
	.library-workstation {
		display: grid;
		grid-template-columns: minmax(16rem, 0.72fr) minmax(0, 1.7fr);
		min-height: 34rem;
		border: 1px solid var(--mf-line);
		background: color-mix(in srgb, var(--mf-bg-panel) 94%, transparent);
	}

	.library-master {
		border-right: 1px solid var(--mf-line);
		background: color-mix(in srgb, var(--mf-bg-base) 86%, var(--mf-bg-panel));
	}

	.library-master__head,
	.library-inspector__head,
	.library-policy > header,
	.library-plex {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: 1rem;
	}

	.library-master__head {
		padding: 0.85rem;
		border-bottom: 1px solid var(--mf-line);
	}

	.library-master__head > div,
	.library-root-row__identity,
	.library-root-row__signals,
	.library-inspector__head > div,
	.library-policy > header > div,
	.library-plex > div,
	.plex-readiness {
		display: grid;
		gap: 0.24rem;
	}

	.library-master__head span,
	.library-root-row small,
	.library-inspector__head p,
	.library-policy header small,
	.stacked-field small,
	.library-plex p,
	.plex-readiness small {
		color: var(--mf-fg-secondary);
	}

	.library-root-list {
		display: grid;
	}

	.library-root-row {
		display: grid;
		grid-template-columns: auto minmax(0, 1fr) auto;
		gap: 0.65rem;
		align-items: center;
		width: 100%;
		padding: 0.72rem 0.8rem;
		border: 0;
		border-bottom: 1px solid var(--mf-line);
		background: transparent;
		color: inherit;
		text-align: left;
		cursor: pointer;
	}

	.library-root-row:hover,
	.library-root-row.is-selected {
		background: color-mix(in srgb, var(--mf-active-fg) 10%, transparent);
	}

	.library-root-row.is-selected {
		box-shadow: inset 3px 0 var(--mf-active-fg);
	}

	.library-root-row__order {
		color: var(--mf-fg-secondary);
		font-size: 0.72rem;
	}

	.library-root-row__identity {
		min-width: 0;
	}

	.library-root-row__identity strong,
	.library-root-row__identity small {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.library-root-row__signals {
		justify-items: end;
	}

	.library-inspector {
		min-width: 0;
		padding: 1rem;
	}

	.library-inspector__head {
		padding-bottom: 0.9rem;
		border-bottom: 1px solid var(--mf-line);
	}

	.library-inspector__head h3,
	.library-policy h4,
	.library-plex h4 {
		margin: 0;
	}

	.library-inspector__head p,
	.library-plex p {
		margin: 0;
	}

	.library-inspector__actions {
		display: flex;
		gap: 0.45rem;
		padding: 0.75rem 0;
	}

	.library-inspector__actions .control--danger {
		margin-left: auto;
	}

	.library-form-grid,
	.policy-grid {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: 0.75rem;
	}

	.library-form-grid__wide {
		grid-column: 1 / -1;
	}

	.stacked-field {
		display: grid;
		gap: 0.34rem;
	}

	.stacked-field > span,
	.policy-readout span {
		font-size: 0.72rem;
		font-weight: 700;
		letter-spacing: 0.05em;
		text-transform: uppercase;
		color: var(--mf-fg-secondary);
	}

	.color-field {
		display: flex;
		align-items: center;
		gap: 0.6rem;
		min-height: 2.5rem;
		padding: 0.3rem 0.55rem;
		border: 1px solid var(--mf-line);
		background: var(--mf-bg-input);
	}

	.color-field input {
		width: 2.5rem;
		height: 1.7rem;
		padding: 0;
		border: 0;
		background: transparent;
	}

	.library-policy,
	.library-plex {
		margin-top: 1rem;
		padding: 0.9rem;
		border: 1px solid var(--mf-line);
		background: color-mix(in srgb, var(--mf-bg-base) 72%, var(--mf-bg-panel));
	}

	.library-policy > header {
		margin-bottom: 0.8rem;
	}

	.policy-readout {
		display: grid;
		gap: 0.3rem;
		align-content: center;
		min-height: 4.2rem;
		padding: 0.7rem;
		border: 1px solid var(--mf-line);
		background: color-mix(in srgb, var(--mf-bg-panel) 88%, transparent);
	}

	.policy-warning,
	.policy-copy {
		margin: 0.8rem 0 0;
		padding: 0.65rem 0.75rem;
		border-left: 3px solid var(--mf-wait-fg);
		background: color-mix(in srgb, var(--mf-wait-fg) 10%, transparent);
		color: var(--mf-fg-secondary);
	}

	.library-plex {
		align-items: end;
		flex-wrap: wrap;
	}

	.library-plex > div,
	.library-plex > label {
		flex: 1 1 14rem;
	}

	.plex-readiness {
		flex: 0 1 13rem;
	}

	.plex-readiness code {
		font-size: 0.72rem;
	}

	.library-empty {
		display: grid;
		place-content: center;
		min-height: 30rem;
		text-align: center;
		color: var(--mf-fg-secondary);
	}

	.library-empty p {
		margin: 0.35rem 0 0;
	}

	@media (max-width: 900px) {
		.library-workstation {
			grid-template-columns: 1fr;
		}

		.library-master {
			border-right: 0;
			border-bottom: 1px solid var(--mf-line);
		}

		.library-root-list {
			grid-template-columns: repeat(auto-fit, minmax(14rem, 1fr));
		}

		.library-root-row {
			border-right: 1px solid var(--mf-line);
		}
	}

	@media (max-width: 620px) {
		.library-master__head,
		.library-inspector__head,
		.library-policy > header,
		.library-plex {
			align-items: stretch;
			flex-direction: column;
		}

		.library-inspector {
			padding: 0.75rem;
		}

		.library-inspector__actions {
			display: grid;
			grid-template-columns: 1fr 1fr;
		}

		.library-inspector__actions .control--danger {
			grid-column: 1 / -1;
			margin-left: 0;
		}

		.library-form-grid,
		.policy-grid {
			grid-template-columns: 1fr;
		}

		.library-form-grid__wide {
			grid-column: auto;
		}
	}
</style>
