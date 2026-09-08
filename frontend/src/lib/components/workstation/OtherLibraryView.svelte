<script lang="ts">
	import { resolve } from '$app/paths';
	import { onMount } from 'svelte';

	import type { OtherLibraryPayload, OtherWorkUnit } from '$lib/api/types';
	import { folderRoutePath } from '$lib/folder-display';
	import { formatFileSize } from '$lib/format';
	import {
		otherLibraryStateGroup,
		otherWorkflowLabel,
		type OtherLibraryStateKey
	} from '$lib/other/library';
	import LibraryLayout from './LibraryLayout.svelte';
	import StateBadge from './StateBadge.svelte';
	import { summarizeWorkStates } from './library-layout';
	import type {
		LibraryMetric,
		LibraryNotice,
		LibraryTone,
		LibraryWorkSegment
	} from './library-layout';

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
	let stateFilter = $state<'all' | OtherLibraryStateKey>('all');
	let sortMode = $state<'name' | 'size' | 'files' | 'reclaim'>('name');
	let selectedPrefix = $state('');
	let detailExpanded = $state(true);

	onMount(() => {
		if (window.matchMedia('(max-width: 680px)').matches) detailExpanded = false;
	});

	const workUnits = $derived.by(() => {
		const normalizedQuery = query.trim().toLocaleLowerCase();
		const filtered = payload.work_units.filter((unit) => {
			if (rootFilter !== 'all' && unit.root !== rootFilter) return false;
			if (normalizedQuery) {
				const haystack = `${unit.title} ${unit.prefix} ${unit.library_label}`.toLocaleLowerCase();
				if (!haystack.includes(normalizedQuery)) return false;
			}
			return stateFilter === 'all' || otherLibraryStateGroup(unit).key === stateFilter;
		});
		return [...filtered].sort((left, right) => compareUnits(left, right, sortMode));
	});

	const selectedUnit = $derived(
		workUnits.find((unit) => unit.prefix === selectedPrefix) ?? workUnits[0] ?? null
	);
	const selectedUnitIndex = $derived(
		selectedUnit ? workUnits.findIndex((unit) => unit.prefix === selectedUnit.prefix) : -1
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
	const totalOutputValue = $derived(
		detailsPending
			? '…'
			: reclaimCoverage === 0
				? '—'
				: formatBytes(Math.max(0, totalSize - projectedReclaim))
	);
	const totalOutputDetail = $derived(
		detailsPending
			? undefined
			: reclaimCoverage === 0
				? 'No estimate'
				: reclaimHasUnknowns
					? `At most · ${reclaimCoverage} of ${payload.work_units.length}`
					: undefined
	);
	const totalReclaimValue = $derived(
		detailsPending ? '…' : reclaimCoverage === 0 ? '—' : formatBytes(projectedReclaim)
	);
	const totalReclaimDetail = $derived(
		detailsPending
			? undefined
			: reclaimCoverage === 0
				? 'No estimate'
				: reclaimHasUnknowns
					? `At least · ${reclaimCoverage} of ${payload.work_units.length}`
					: undefined
	);
	const libraryMetrics = $derived<LibraryMetric[]>([
		{
			value: `${payload.work_units.length}`,
			label: payload.work_units.length === 1 ? 'Scope' : 'Scopes',
			detail: `${totalFiles} ${totalFiles === 1 ? 'file' : 'files'}`
		},
		{ value: formatBytes(totalSize), label: 'Current size' },
		{
			value: totalOutputValue,
			label: 'Estimated output',
			detail: totalOutputDetail,
			pending: detailsPending
		},
		{
			value: totalReclaimValue,
			label: 'Estimated space saved',
			detail: totalReclaimDetail,
			pending: detailsPending
		}
	]);
	const libraryWorkSegments = $derived<LibraryWorkSegment[]>(
		summarizeWorkStates(payload.work_units, otherLibraryStateGroup)
	);
	const libraryNotices = $derived.by<LibraryNotice[]>(() => {
		const notices: LibraryNotice[] = [];
		if (loadError)
			notices.push({ title: 'Other Library could not open', detail: loadError, tone: 'fail' });
		if (detailsError) {
			notices.push({
				title: 'Workflow details are unavailable',
				detail: `The library index remains usable. ${detailsError}`
			});
		}
		if (payload.catalog_truncated) {
			notices.push({
				title: 'Safe catalog window reached',
				detail: `Showing at most ${payload.catalog_work_unit_limit} folders and files from the first ${payload.catalog_item_limit.toLocaleString()} indexed items. Narrow the configured roots to expose more bounded work; hidden items cannot be queued from this view.`,
				tone: 'wait'
			});
		}
		return notices;
	});

	$effect(() => {
		if (selectedUnit && selectedPrefix !== selectedUnit.prefix)
			selectedPrefix = selectedUnit.prefix;
	});

	function selectUnit(prefix: string) {
		selectedPrefix = prefix;
		detailExpanded = !window.matchMedia('(max-width: 680px)').matches;
	}

	function toggleUnitDetail(prefix: string) {
		if (selectedUnit?.prefix === prefix) detailExpanded = !detailExpanded;
		else {
			selectedPrefix = prefix;
			detailExpanded = true;
		}
		requestAnimationFrame(() => {
			document
				.querySelector<HTMLElement>(`[data-other-unit-row="${CSS.escape(prefix)}"]`)
				?.scrollIntoView({ block: 'nearest' });
		});
	}

	function clearFilters() {
		query = '';
		rootFilter = 'all';
		stateFilter = 'all';
	}

	function selectWorkSegment(key: string) {
		query = '';
		rootFilter = 'all';
		stateFilter = stateFilter === key ? 'all' : (key as OtherLibraryStateKey);
	}

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

	function workflowTone(unit: OtherWorkUnit): LibraryTone {
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

<LibraryLayout
	active="other"
	title="Other Library"
	metrics={libraryMetrics}
	workSegments={libraryWorkSegments}
	activeWorkSegment={stateFilter === 'all' ? '' : stateFilter}
	onWorkSegmentSelect={selectWorkSegment}
	notices={libraryNotices}
	toolbarSummary={`${workUnits.length} of ${payload.work_units.length} scopes`}
	loading={structurePending || detailsPending}
>
	{#snippet toolbar()}
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
				<option value="attention">Needs attention</option>
				<option value="blocked">Cannot start</option>
				<option value="processing">Compressing</option>
				<option value="ready">Ready to act on</option>
				<option value="browse_only">Browse only</option>
				<option value="idle">No active work</option>
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
	{/snippet}

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
			<button class="empty-state__action" type="button" onclick={clearFilters}>Clear filters</button
			>
		</div>
	{:else}
		<div class="library-register">
			<div class="unit-list__head library-register__header" aria-hidden="true">
				<span>Scope</span><span>Files</span><span>Current size</span><span>Workflow</span>
			</div>
			<div class="library-workbench">
				<div class="unit-list library-index">
					{#each workUnits as unit, unitIndex (unit.prefix)}
						<div class="ledger-row">
							<button
								type="button"
								class="unit-row"
								class:is-selected={selectedUnit?.prefix === unit.prefix}
								data-other-unit-row={unit.prefix}
								data-library-state={otherLibraryStateGroup(unit).key}
								aria-pressed={selectedUnit?.prefix === unit.prefix}
								onclick={() => selectUnit(unit.prefix)}
							>
								<span class="unit-identity">
									<strong>{unit.title}</strong>
									<small>
										{unit.scope_mode === 'exact_file'
											? 'One file'
											: 'Whole folder'}{#if payload.libraries.length > 1}
											· {unit.library_label}{/if}
									</small>
								</span>
								<span data-label="Files"><span class="sr-only">Files: </span>{unit.item_count}</span
								>
								<span data-label="Current size"
									><span class="sr-only">Current size: </span>{formatBytes(
										unit.total_size_bytes
									)}</span
								>
								<StateBadge
									tone={workflowTone(unit)}
									label={statusLabel(unit)}
									compact
									quiet={selectedUnit?.prefix !== unit.prefix &&
										(unit.profile_readiness.state === 'browse_only' ||
											workflowTone(unit) === 'ready')}
								/>
							</button>
							<button
								class="row-inspect"
								type="button"
								aria-controls={`other-unit-detail-${unitIndex}`}
								aria-expanded={selectedUnit?.prefix === unit.prefix && detailExpanded}
								aria-label={`${selectedUnit?.prefix === unit.prefix && detailExpanded ? 'Close' : 'Inspect'} ${unit.title}`}
								onclick={() => toggleUnitDetail(unit.prefix)}
							>
								<span aria-hidden="true"
									>{selectedUnit?.prefix === unit.prefix && detailExpanded
										? 'Close'
										: 'Inspect'}</span
								>
							</button>
						</div>
					{/each}
				</div>

				{#if selectedUnit && detailExpanded}
					<aside
						id={`other-unit-detail-${selectedUnitIndex}`}
						class="inspector library-inspector"
						style:grid-row={selectedUnitIndex + 2}
						aria-label={`${selectedUnit.title} details`}
					>
						<header class="inspector-heading">
							<div>
								<span class="eyebrow">{selectedUnit.scope_label}</span>
								<h2>{selectedUnit.title}</h2>
								<p>{selectedUnit.prefix}</p>
							</div>
							<StateBadge
								tone={workflowTone(selectedUnit)}
								label={statusLabel(selectedUnit)}
								compact
							/>
							<button
								class="inspector-collapse"
								type="button"
								onclick={() => (detailExpanded = false)}
							>
								Collapse ↑
							</button>
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
								Open in Studio
							</a>
							<span>Studio lists every included and untouched file before work starts.</span>
						</footer>
					</aside>
				{/if}
			</div>
		</div>
	{/if}
</LibraryLayout>

<style>
	h2,
	p {
		margin: 0;
	}

	.eyebrow {
		color: var(--mf-fg-muted);
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-semibold);
		letter-spacing: 0.08em;
		text-transform: uppercase;
	}

	.inspector-heading p,
	.inspector-actions span,
	.readiness p,
	.readiness li {
		color: var(--mf-fg-secondary);
		font-size: 12px;
		line-height: 1.55;
	}

	.inspector-facts span,
	.readiness > span {
		color: var(--mf-fg-muted);
		font-size: 10px;
		font-weight: 700;
		letter-spacing: 0.06em;
		margin-top: 3px;
		text-transform: uppercase;
	}

	.unit-list {
		display: contents;
	}

	.unit-list__head,
	.unit-row {
		display: grid;
		gap: 12px;
		grid-template-columns: minmax(320px, 1fr) 80px 128px 180px;
	}

	.unit-list__head {
		background: var(--mf-bg-panel);
		border-bottom: 1px solid var(--mf-line-strong);
		color: var(--mf-fg-muted);
		font-size: 10px;
		font-weight: var(--mf-weight-semibold);
		letter-spacing: 0.1em;
		padding: var(--mf-space-4) var(--mf-space-6);
		text-transform: uppercase;
	}

	.ledger-row {
		position: relative;
	}

	.unit-row {
		align-items: center;
		background: transparent;
		border: 0;
		border-bottom: 1px solid var(--mf-line-muted);
		color: var(--mf-fg-primary);
		cursor: pointer;
		font: inherit;
		min-height: 56px;
		padding: var(--mf-space-4) var(--mf-space-6);
		text-align: left;
		width: 100%;
	}

	.unit-row:hover {
		background: var(--mf-bg-panel-2);
	}

	.unit-row.is-selected {
		background: var(--mf-bg-panel-2);
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

	.unit-identity strong {
		font-size: 14px;
		font-weight: var(--mf-weight-semibold);
	}

	.unit-identity small {
		color: var(--mf-fg-secondary);
		font-size: 12px;
		margin-top: 3px;
	}

	.unit-row :global(.state-badge) {
		max-width: 100%;
		white-space: nowrap;
	}

	.row-inspect {
		display: none;
	}

	.inspector {
		display: grid;
		gap: var(--mf-space-7);
		grid-column: 1;
		grid-template-columns: minmax(230px, 0.75fr) minmax(390px, 1.45fr) minmax(260px, 0.8fr);
		min-width: 0;
		padding: var(--mf-space-7) var(--mf-space-6) var(--mf-space-8);
		box-shadow:
			inset 3px 0 0 var(--mf-lib-other),
			0 10px 24px color-mix(in srgb, var(--mf-fg-primary) 8%, transparent);
	}

	.inspector-heading {
		align-items: start;
		display: grid;
		grid-column: 1;
		grid-row: 1;
		gap: var(--mf-space-6);
		padding-right: var(--mf-space-8);
		position: relative;
	}

	.inspector-heading :global(.state-badge) {
		justify-self: start;
	}

	.inspector-heading > div {
		min-width: 0;
	}

	.inspector-heading h2 {
		font-family: inherit;
		font-size: 22px;
		font-weight: var(--mf-weight-semibold);
		line-height: var(--mf-leading-snug);
		letter-spacing: -0.025em;
		margin-top: 4px;
		overflow-wrap: anywhere;
	}

	.inspector-facts {
		display: grid;
		gap: var(--mf-space-6) var(--mf-space-8);
		grid-column: 2;
		grid-row: 1;
		grid-template-columns: 1fr 1fr;
	}

	.inspector-facts div {
		padding: 0;
	}

	.inspector-facts div:last-child {
		grid-column: 1 / -1;
	}

	.inspector-facts strong,
	.inspector-facts span {
		display: block;
	}

	.inspector-facts strong {
		font-size: 15px;
		font-variant-numeric: tabular-nums;
		font-weight: var(--mf-weight-semibold);
		margin-top: var(--mf-space-2);
	}

	.readiness {
		border-left: 3px solid var(--mf-ready-fg);
		grid-column: 2;
		margin-top: var(--mf-space-6);
		padding: var(--mf-space-2) 0 var(--mf-space-2) var(--mf-space-5);
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
		gap: var(--mf-space-3);
		margin-top: var(--mf-space-6);
	}

	.policy-strip span {
		background: var(--mf-bg-subtle);
		border: 1px solid var(--mf-line);
		color: var(--mf-fg-secondary);
		font-size: 10px;
		font-weight: var(--mf-weight-semibold);
		padding: var(--mf-space-2) var(--mf-space-3);
		text-transform: uppercase;
	}

	.inspector-actions {
		align-items: start;
		border-left: 1px solid var(--mf-line-strong);
		display: grid;
		gap: var(--mf-space-4);
		grid-column: 3;
		grid-row: 1 / span 6;
		padding-left: var(--mf-space-7);
	}

	.primary-link,
	.empty-state a {
		background: var(--mf-active-solid);
		color: var(--mf-active-contrast);
		font-size: 13px;
		border-radius: var(--mf-radius-2);
		font-weight: var(--mf-weight-semibold);
		justify-self: start;
		padding: 9px 12px;
		text-decoration: none;
	}

	.inspector-collapse {
		background: transparent;
		border: 0;
		border-radius: var(--mf-radius-2);
		color: var(--mf-fg-secondary);
		font-size: var(--mf-text-xs);
		padding: var(--mf-space-2) var(--mf-space-3);
		position: absolute;
		right: 0;
		top: 0;
	}

	.empty-state {
		align-items: center;
		display: grid;
		gap: 8px;
		justify-items: center;
		min-height: 180px;
		padding: 28px;
		text-align: center;
	}

	.empty-state__action {
		background: var(--mf-bg-panel);
		border: 1px solid var(--mf-line-strong);
		border-radius: var(--mf-radius-1);
		color: var(--mf-fg-primary);
		cursor: pointer;
		font: inherit;
		font-weight: var(--mf-weight-semibold);
		padding: 8px 12px;
	}

	.empty-state span {
		color: var(--mf-fg-secondary);
		font-size: 13px;
		max-width: 520px;
	}

	.sr-only {
		height: 1px;
		overflow: hidden;
		position: absolute;
		width: 1px;
		clip: rect(0 0 0 0);
	}

	@media (max-width: 980px) {
		.unit-list__head,
		.unit-row {
			grid-template-columns: minmax(260px, 1fr) 140px 190px;
		}

		.unit-list__head > :nth-child(2),
		.unit-row > :nth-child(2) {
			display: none;
		}

		.inspector {
			grid-template-columns: minmax(210px, 0.75fr) minmax(320px, 1.25fr);
		}

		.inspector-actions {
			border-left: 0;
			border-top: 1px solid var(--mf-line-strong);
			grid-column: 1 / -1;
			grid-row: auto;
			padding: var(--mf-space-6) 0 0;
		}
	}

	@media (max-width: 680px) {
		.unit-list__head {
			border: 0;
			height: 0;
			overflow: hidden;
			padding: 0;
			visibility: hidden;
		}

		.unit-row {
			gap: 7px 12px;
			grid-template-columns: 1fr auto;
			min-height: 68px;
			padding-right: 86px;
		}

		.unit-row > span[data-label='Files'] {
			display: none;
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

		.inspector {
			grid-template-columns: minmax(0, 1fr);
			padding: var(--mf-space-6) var(--mf-space-5) var(--mf-space-7);
		}

		.inspector-heading {
			align-items: stretch;
			flex-direction: column;
		}

		.inspector-heading,
		.inspector-facts,
		.readiness,
		.policy-strip,
		.inspector-actions {
			grid-column: 1;
			grid-row: auto;
		}

		.inspector-actions {
			padding-top: var(--mf-space-6);
		}

		.row-inspect {
			background: var(--mf-bg-panel);
			border: 1px solid var(--mf-line-strong);
			border-radius: var(--mf-radius-2);
			bottom: var(--mf-space-3);
			color: var(--mf-fg-secondary);
			display: inline-flex;
			font-size: 11px;
			min-height: 26px;
			padding: 0 var(--mf-space-2);
			position: absolute;
			right: var(--mf-space-3);
		}
	}
</style>
