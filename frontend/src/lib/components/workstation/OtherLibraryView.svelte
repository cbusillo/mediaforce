<script lang="ts">
	import { resolve } from '$app/paths';

	import type { OtherLibraryPayload, OtherWorkUnit } from '$lib/api/types';
	import { folderRoutePath } from '$lib/folder-display';
	import { formatFileSize } from '$lib/format';
	import { otherWorkflowLabel } from '$lib/other/library';
	import LibraryModeNav from './LibraryModeNav.svelte';

	let {
		payload,
		structurePending = false,
		detailsPending = false,
		loadError = '',
		detailsError = ''
	}: {
		payload: OtherLibraryPayload;
		structurePending?: boolean;
		detailsPending?: boolean;
		loadError?: string;
		detailsError?: string;
	} = $props();

	let query = $state('');
	let rootFilter = $state('all');
	let stateFilter = $state<'all' | 'ready' | 'blocked' | 'browse_only' | 'active'>('all');
	let sortMode = $state<'name' | 'size' | 'files' | 'reclaim'>('name');
	let selectedPrefix = $state('');

	const workUnits = $derived.by(() => {
		const normalizedQuery = query.trim().toLocaleLowerCase();
		const filtered = payload.work_units.filter((unit) => {
			if (rootFilter !== 'all' && unit.root !== rootFilter) return false;
			if (normalizedQuery) {
				const haystack = `${unit.title} ${unit.prefix} ${unit.library_label}`.toLocaleLowerCase();
				if (!haystack.includes(normalizedQuery)) return false;
			}
			if (stateFilter === 'ready') return unit.profile_readiness.state === 'ready';
			if (stateFilter === 'blocked') return unit.profile_readiness.state === 'blocked';
			if (stateFilter === 'browse_only') return unit.profile_readiness.state === 'browse_only';
			if (stateFilter === 'active') {
				return ['processing', 'validate', 'promote', 'attention'].includes(
					unit.workflow_state?.primary_lane ?? ''
				);
			}
			return true;
		});
		return [...filtered].sort((left, right) => compareUnits(left, right, sortMode));
	});

	const selectedUnit = $derived(
		workUnits.find((unit) => unit.prefix === selectedPrefix) ?? workUnits[0] ?? null
	);
	const totalFiles = $derived(
		payload.work_units.reduce((total, unit) => total + unit.item_count, 0)
	);
	const totalSize = $derived(
		payload.work_units.reduce((total, unit) => total + unit.total_size_bytes, 0)
	);
	const projectedReclaim = $derived(
		payload.work_units.reduce((total, unit) => total + (unit.projected_reclaim_bytes ?? 0), 0)
	);
	const reclaimCoverage = $derived(
		payload.work_units.filter((unit) => unit.projected_reclaim_bytes != null).length
	);
	const reclaimHasUnknowns = $derived(
		payload.work_units.some(
			(unit) => unit.projected_reclaim_bytes == null || (unit.estimate_unavailable_count ?? 0) > 0
		)
	);
	const totalOutputLabel = $derived(
		detailsPending
			? '…'
			: reclaimCoverage === 0
				? 'No estimate'
				: `${reclaimHasUnknowns ? 'At most ' : ''}${formatBytes(Math.max(0, totalSize - projectedReclaim))}`
	);
	const totalReclaimLabel = $derived(
		detailsPending
			? '…'
			: reclaimCoverage === 0
				? 'No estimate'
				: `${reclaimHasUnknowns ? 'At least ' : ''}${formatBytes(projectedReclaim)}`
	);

	$effect(() => {
		if (selectedUnit && selectedPrefix !== selectedUnit.prefix)
			selectedPrefix = selectedUnit.prefix;
	});

	function compareUnits(left: OtherWorkUnit, right: OtherWorkUnit, mode: typeof sortMode): number {
		if (mode === 'size') return right.total_size_bytes - left.total_size_bytes;
		if (mode === 'files') return right.item_count - left.item_count;
		if (mode === 'reclaim')
			return (right.projected_reclaim_bytes ?? -1) - (left.projected_reclaim_bytes ?? -1);
		return left.title.localeCompare(right.title, undefined, { numeric: true, sensitivity: 'base' });
	}

	function formatBytes(value: number | null | undefined): string {
		return formatFileSize(value, 'Pending');
	}

	function workflowTone(unit: OtherWorkUnit): string {
		if (unit.profile_readiness.state === 'blocked') return 'fail';
		if (unit.profile_readiness.state === 'browse_only') return 'wait';
		const lane = unit.workflow_state?.primary_lane;
		if (lane === 'processing') return 'active';
		if (lane === 'validate' || lane === 'promote' || lane === 'encode') return 'ready';
		if (lane === 'attention' || lane === 'blocked') return 'fail';
		return 'idle';
	}

	function statusLabel(unit: OtherWorkUnit): string {
		if (unit.profile_readiness.state !== 'ready') return unit.profile_readiness.label;
		return otherWorkflowLabel(unit.workflow_state, unit.details_loading);
	}

	function estimatedOutput(unit: OtherWorkUnit): number | null {
		return unit.projected_reclaim_bytes == null
			? null
			: Math.max(0, unit.total_size_bytes - unit.projected_reclaim_bytes);
	}

	function formatEstimate(unit: OtherWorkUnit, value: number | null | undefined): string {
		return unit.details_loading ? 'Estimating…' : formatFileSize(value, 'No estimate');
	}
</script>

<svelte:head>
	<title>Other Library · Mediaforce</title>
</svelte:head>

<main class="other-library">
	<LibraryModeNav active="other" />

	<header class="page-heading">
		<div class="page-heading__copy">
			<h1>Other Library</h1>
			<p>Files and folders you choose directly. Mediaforce never guesses what they are.</p>
		</div>
		<div class="library-totals" aria-label="Other library totals">
			<div><strong>{payload.work_units.length} scopes</strong><span>{totalFiles} files</span></div>
			<div><strong>{formatBytes(totalSize)}</strong><span>Current size</span></div>
			<div><strong>{totalOutputLabel}</strong><span>Estimated output</span></div>
			<div><strong>{totalReclaimLabel}</strong><span>Estimated space saved</span></div>
		</div>
	</header>

	{#if loadError}
		<div class="notice notice--danger" role="alert">
			<strong>Other Library could not open</strong><span>{loadError}</span>
		</div>
	{/if}
	{#if detailsError}
		<div class="notice" role="status">
			<strong>Workflow details are unavailable</strong>
			<span>The library index remains usable. {detailsError}</span>
		</div>
	{/if}
	{#if payload.catalog_truncated}
		<div class="notice" role="status">
			<strong>Safe catalog window reached</strong>
			<span
				>Showing at most {payload.catalog_work_unit_limit} folders and files from the first {payload.catalog_item_limit.toLocaleString()}
				indexed items. Narrow the configured roots to expose more bounded work; hidden items cannot be
				queued from this view.</span
			>
		</div>
	{/if}

	<section class="library-workstation" aria-label="Other library folders and files">
		<div class="toolbar">
			<label class="search-field">
				<span class="sr-only">Search folders and files</span>
				<input bind:value={query} type="search" placeholder="Search folders and files" />
			</label>
			<label>
				<span>Library</span>
				<select bind:value={rootFilter}>
					<option value="all">All roots</option>
					{#each payload.libraries as library (library.key)}
						<option value={library.key}>{library.label}</option>
					{/each}
				</select>
			</label>
			<label>
				<span>State</span>
				<select bind:value={stateFilter}>
					<option value="all">All states</option>
					<option value="ready">Profile ready</option>
					<option value="blocked">Blocked</option>
					<option value="browse_only">Browse only</option>
					<option value="active">Active work</option>
				</select>
			</label>
			<label>
				<span>Sort</span>
				<select bind:value={sortMode}>
					<option value="name">Name</option>
					<option value="size">Current size</option>
					<option value="files">File count</option>
					<option value="reclaim">Estimated space saved</option>
				</select>
			</label>
		</div>

		{#if structurePending && payload.work_units.length === 0}
			<div class="empty-state" role="status">
				<strong>Loading Other Library…</strong>
				<span>Folders and files appear first; workflow details follow.</span>
			</div>
		{:else if payload.catalog_empty || payload.work_units.length === 0}
			<div class="empty-state">
				<strong>No Other media found.</strong>
				<span
					>Add or scan an Other root in Settings. Root-level files and nested folders will appear
					here.</span
				>
				<a href={resolve('/settings')}>Open Settings</a>
			</div>
		{:else if workUnits.length === 0}
			<div class="empty-state">
				<strong>No Other folders or files match these filters.</strong>
				<span>Clear search or choose a broader state and library filter.</span>
			</div>
		{:else}
			<div class="workbench">
				<div class="unit-list" aria-label="Other folders and files">
					<div class="unit-list__head" aria-hidden="true">
						<span>Scope</span><span>Files</span><span>Current size</span><span>Workflow</span>
					</div>
					{#each workUnits as unit (unit.prefix)}
						<button
							type="button"
							class="unit-row"
							class:is-selected={selectedUnit?.prefix === unit.prefix}
							aria-pressed={selectedUnit?.prefix === unit.prefix}
							onclick={() => (selectedPrefix = unit.prefix)}
						>
							<span class="unit-identity">
								<strong>{unit.title}</strong>
								<small
									>{unit.scope_mode === 'exact_file' ? 'One file' : 'Whole folder'} · {unit.library_label}</small
								>
							</span>
							<span data-label="Files"><span class="sr-only">Files: </span>{unit.item_count}</span>
							<span data-label="Current size"
								><span class="sr-only">Current size: </span>{formatBytes(
									unit.total_size_bytes
								)}</span
							>
							<span class="state-badge" data-tone={workflowTone(unit)}>{statusLabel(unit)}</span>
						</button>
					{/each}
				</div>

				{#if selectedUnit}
					<aside class="inspector" aria-label={`${selectedUnit.title} details`}>
						<header class="inspector-heading">
							<div>
								<span class="eyebrow">{selectedUnit.scope_label}</span>
								<h2>{selectedUnit.title}</h2>
								<p>{selectedUnit.prefix}</p>
							</div>
							<span class="state-badge" data-tone={workflowTone(selectedUnit)}
								>{statusLabel(selectedUnit)}</span
							>
						</header>

						<div class="inspector-facts">
							<div>
								<span>Membership</span><strong
									>{selectedUnit.item_count}
									{selectedUnit.item_count === 1 ? 'file' : 'files'}</strong
								>
							</div>
							<div>
								<span>Current size</span><strong
									>{formatBytes(selectedUnit.total_size_bytes)}</strong
								>
							</div>
							<div>
								<span>Estimated output</span><strong
									>{formatEstimate(selectedUnit, estimatedOutput(selectedUnit))}</strong
								>
							</div>
							<div>
								<span>Estimated space saved</span><strong
									>{formatEstimate(selectedUnit, selectedUnit.projected_reclaim_bytes)}</strong
								>
							</div>
							<div>
								<span>What is included</span><strong
									>{selectedUnit.scope_mode === 'exact_file'
										? 'Only this file'
										: 'Files in this folder and its subfolders'}</strong
								>
							</div>
						</div>

						<section class="readiness" data-state={selectedUnit.profile_readiness.state}>
							<span>Compression profile</span>
							<strong>{selectedUnit.profile_readiness.profile_label}</strong>
							<p>{selectedUnit.profile_readiness.detail}</p>
							{#if selectedUnit.profile_readiness.blockers.length}
								<ul>
									{#each selectedUnit.profile_readiness.blockers as blocker (blocker)}
										<li>{blocker}</li>
									{/each}
								</ul>
							{/if}
						</section>

						<div class="policy-strip" aria-label="Other library policy">
							<span
								>{selectedUnit.scope_mode === 'exact_file'
									? 'One file at a time'
									: 'Whole folder together'}</span
							>
							{#if selectedUnit.membership_requires_confirmation}<span
									>Needs your confirmation before work starts</span
								>{/if}
						</div>

						<footer class="inspector-actions">
							<a class="primary-link" href={resolve(folderRoutePath(selectedUnit.prefix))}>
								Open {selectedUnit.title}
							</a>
							<span>Studio lists every included and untouched file before work starts.</span>
						</footer>
					</aside>
				{/if}
			</div>
		{/if}

		<footer class="workstation-footer">
			<span>{workUnits.length} of {payload.work_units.length} shown</span>
			<span
				>{detailsPending || reclaimCoverage === 0
					? 'Refreshing workflow details…'
					: `${formatBytes(projectedReclaim)} estimated space saved${reclaimHasUnknowns ? ' · lower bound' : ''}`}</span
			>
		</footer>
	</section>
</main>

<style>
	.other-library {
		margin: 0 auto;
		max-width: 1400px;
		padding: 24px 28px 54px;
	}

	h1,
	h2,
	p {
		margin: 0;
	}

	.page-heading {
		align-items: center;
		display: flex;
		gap: 28px;
		justify-content: space-between;
		margin-bottom: 14px;
	}

	.page-heading__copy {
		max-width: 610px;
	}

	.eyebrow {
		color: var(--mf-fg-muted);
		font-size: 11px;
		font-weight: 800;
		letter-spacing: 0.12em;
		text-transform: uppercase;
	}

	h1 {
		font-size: clamp(24px, 3vw, 30px);
		letter-spacing: -0.03em;
		margin-top: 2px;
	}

	.page-heading p,
	.inspector-heading p,
	.inspector-actions span,
	.readiness p,
	.readiness li {
		color: var(--mf-fg-secondary);
		font-size: 12px;
		line-height: 1.55;
	}

	.library-totals {
		border-bottom: 1px solid var(--mf-line);
		border-top: 1px solid var(--mf-line);
		display: grid;
		grid-template-columns: repeat(4, minmax(88px, 1fr));
		min-width: min(100%, 430px);
	}

	.library-totals div {
		border-left: 1px solid var(--mf-line);
		padding: 9px 12px;
	}

	.library-totals div:first-child {
		border-left: 0;
	}

	.library-totals strong,
	.library-totals span {
		display: block;
	}

	.library-totals strong {
		font-size: 15px;
	}

	.library-totals span,
	.toolbar label > span,
	.inspector-facts span,
	.readiness > span {
		color: var(--mf-fg-muted);
		font-size: 10px;
		font-weight: 700;
		letter-spacing: 0.06em;
		margin-top: 3px;
		text-transform: uppercase;
	}

	.notice {
		background: var(--mf-bg-panel);
		border: 1px solid var(--mf-line);
		border-left: 3px solid var(--mf-wait-fg);
		display: grid;
		gap: 3px;
		margin-bottom: 14px;
		padding: 12px 14px;
	}

	.notice--danger {
		border-left-color: var(--mf-fail-fg);
	}

	.notice span {
		color: var(--mf-fg-secondary);
		font-size: 12px;
	}

	.library-workstation {
		background: var(--mf-bg-panel);
		border: 1px solid var(--mf-line);
	}

	.toolbar {
		align-items: end;
		border-bottom: 1px solid var(--mf-line);
		display: grid;
		gap: 12px;
		grid-template-columns: minmax(220px, 1.6fr) repeat(3, minmax(130px, 0.7fr));
		padding: 14px;
	}

	.toolbar label {
		display: grid;
		gap: 5px;
	}

	.toolbar input,
	.toolbar select {
		background: var(--mf-bg-input);
		border: 1px solid var(--mf-line-strong);
		border-radius: 0;
		color: var(--mf-fg-primary);
		font: inherit;
		font-size: 13px;
		height: 36px;
		padding: 0 10px;
	}

	.workbench {
		display: grid;
		grid-template-columns: minmax(0, 1.35fr) minmax(320px, 0.65fr);
		min-height: 480px;
	}

	.unit-list {
		border-right: 1px solid var(--mf-line);
		min-width: 0;
	}

	.unit-list__head,
	.unit-row {
		display: grid;
		gap: 12px;
		grid-template-columns: minmax(180px, 1fr) 64px 92px minmax(112px, 0.65fr);
	}

	.unit-list__head {
		background: var(--mf-bg-subtle);
		border-bottom: 1px solid var(--mf-line);
		color: var(--mf-fg-muted);
		font-size: 10px;
		font-weight: 800;
		letter-spacing: 0.06em;
		padding: 10px 14px;
		text-transform: uppercase;
	}

	.unit-row {
		align-items: center;
		background: transparent;
		border: 0;
		border-bottom: 1px solid var(--mf-line);
		color: var(--mf-fg-primary);
		cursor: pointer;
		font: inherit;
		padding: 13px 14px;
		text-align: left;
		width: 100%;
	}

	.unit-row:hover,
	.unit-row.is-selected {
		background: var(--mf-bg-subtle);
	}

	.unit-row.is-selected {
		box-shadow: inset 3px 0 0 var(--mf-lib-other);
	}

	.unit-identity,
	.unit-identity strong,
	.unit-identity small {
		display: block;
		min-width: 0;
	}

	.unit-identity strong,
	.unit-identity small,
	.inspector-heading p {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.unit-identity small {
		color: var(--mf-fg-secondary);
		font-size: 11px;
		margin-top: 3px;
	}

	.state-badge {
		border: 1px solid var(--mf-line-strong);
		color: var(--mf-fg-secondary);
		font-size: 10px;
		font-weight: 800;
		justify-self: start;
		letter-spacing: 0.04em;
		padding: 4px 6px;
		text-transform: uppercase;
	}

	.state-badge[data-tone='ready'] {
		border-color: var(--mf-ready-fg);
		color: var(--mf-ready-fg);
	}

	.state-badge[data-tone='active'] {
		background: var(--mf-active-bg-strong);
		border-color: var(--mf-active-fg);
		color: var(--mf-active-fg-bright);
	}

	.state-badge[data-tone='wait'] {
		border-color: var(--mf-wait-fg);
		color: var(--mf-wait-fg);
	}

	.state-badge[data-tone='fail'] {
		border-color: var(--mf-fail-fg);
		color: var(--mf-fail-fg);
	}

	.inspector {
		display: flex;
		flex-direction: column;
		min-width: 0;
		padding: 20px;
	}

	.inspector-heading {
		align-items: start;
		display: flex;
		gap: 16px;
		justify-content: space-between;
	}

	.inspector-heading > div {
		min-width: 0;
	}

	.inspector-heading h2 {
		font-size: 22px;
		letter-spacing: -0.025em;
		margin-top: 4px;
	}

	.inspector-facts {
		border: 1px solid var(--mf-line);
		display: grid;
		grid-template-columns: 1fr 1fr;
		margin-top: 18px;
	}

	.inspector-facts div {
		border-bottom: 1px solid var(--mf-line);
		padding: 12px;
	}

	.inspector-facts div:nth-child(odd) {
		border-right: 1px solid var(--mf-line);
	}

	.inspector-facts div:nth-last-child(-n + 2) {
		border-bottom: 0;
	}

	.inspector-facts strong,
	.inspector-facts span {
		display: block;
	}

	.inspector-facts strong {
		font-size: 13px;
		margin-top: 4px;
	}

	.readiness {
		border-left: 3px solid var(--mf-ready-fg);
		margin-top: 16px;
		padding: 4px 0 4px 12px;
	}

	.readiness[data-state='blocked'] {
		border-left-color: var(--mf-fail-fg);
	}

	.readiness[data-state='browse_only'] {
		border-left-color: var(--mf-wait-fg);
	}

	.readiness strong,
	.readiness p {
		display: block;
	}

	.readiness p {
		margin-top: 4px;
	}

	.readiness ul {
		margin: 8px 0 0;
		padding-left: 18px;
	}

	.policy-strip {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
		margin-top: 16px;
	}

	.policy-strip span {
		background: var(--mf-bg-subtle);
		border: 1px solid var(--mf-line);
		color: var(--mf-fg-secondary);
		font-size: 10px;
		font-weight: 700;
		padding: 5px 7px;
		text-transform: uppercase;
	}

	.inspector-actions {
		align-items: start;
		border-top: 1px solid var(--mf-line);
		display: grid;
		gap: 8px;
		margin-top: auto;
		padding-top: 18px;
	}

	.primary-link,
	.empty-state a {
		background: var(--mf-active-fg);
		color: var(--mf-active-contrast);
		font-size: 13px;
		font-weight: 800;
		justify-self: start;
		padding: 9px 12px;
		text-decoration: none;
	}

	.empty-state {
		align-items: center;
		display: grid;
		gap: 8px;
		justify-items: center;
		min-height: 360px;
		padding: 36px;
		text-align: center;
	}

	.empty-state span {
		color: var(--mf-fg-secondary);
		font-size: 13px;
		max-width: 520px;
	}

	.workstation-footer {
		border-top: 1px solid var(--mf-line);
		color: var(--mf-fg-muted);
		display: flex;
		font-size: 11px;
		justify-content: space-between;
		padding: 10px 14px;
	}

	.sr-only {
		height: 1px;
		overflow: hidden;
		position: absolute;
		width: 1px;
		clip: rect(0 0 0 0);
	}

	@media (max-width: 980px) {
		.page-heading {
			align-items: stretch;
			flex-direction: column;
		}

		.toolbar {
			grid-template-columns: 1fr 1fr;
		}

		.search-field {
			grid-column: 1 / -1;
		}

		.workbench {
			grid-template-columns: 1fr;
		}

		.unit-list {
			border-right: 0;
		}

		.inspector {
			border-top: 1px solid var(--mf-line);
			min-height: 420px;
		}
	}

	@media (max-width: 680px) {
		.other-library {
			padding: 22px 14px 40px;
		}

		.library-totals {
			grid-template-columns: 1fr 1fr;
			min-width: 0;
		}

		.library-totals div:nth-child(3) {
			border-left: 0;
			border-top: 1px solid var(--mf-line);
		}

		.library-totals div:nth-child(4) {
			border-top: 1px solid var(--mf-line);
		}

		.toolbar {
			grid-template-columns: 1fr;
		}

		.search-field {
			grid-column: auto;
		}

		.unit-list__head {
			display: none;
		}

		.unit-row {
			gap: 7px 12px;
			grid-template-columns: 1fr auto;
		}

		.unit-identity {
			grid-column: 1 / -1;
		}

		.unit-row > span[data-label]::before {
			color: var(--mf-fg-muted);
			content: attr(data-label) ' ';
			font-size: 9px;
			font-weight: 800;
			text-transform: uppercase;
		}

		.state-badge {
			grid-column: 1 / -1;
		}

		.inspector {
			padding: 16px;
		}

		.inspector-heading {
			align-items: stretch;
			flex-direction: column;
		}

		.workstation-footer {
			align-items: flex-start;
			flex-direction: column;
			gap: 4px;
		}
	}
</style>
