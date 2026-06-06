<script lang="ts">
	import { resolve } from '$app/paths';
	import { browser } from '$app/environment';
	import { folderRoutePath } from '$lib/folder-display';
	import type {
		DashboardFoldersPayload,
		DashboardSummaryPayload,
		FolderCard,
		HostsPayload
	} from '$lib/api/types';
	import OperatorShell from './OperatorShell.svelte';
	import StateBadge from './StateBadge.svelte';
	import WorkstationPanel from './WorkstationPanel.svelte';
	import {
		buildQueueFooterSignals,
		buildQueueStatusTiles,
		codecSummary,
		folderStatusCopy,
		isQueueActionableFolder,
		queueFolderState,
		queueFolderTone,
		totalPendingItems,
		totalProjectedReclaim,
		workflowOpenItemCount
	} from './queue-workstation';
	import {
		clearStoredWorkbenchFilters,
		readStoredWorkbenchFilters,
		writeStoredWorkbenchFilters
	} from './home-workbench';
	import { formatBytes } from './folder-studio-view';

	let {
		dashboard,
		foldersPayload,
		hosts,
		crumb = '/',
		mode = 'queue'
	}: {
		dashboard: DashboardSummaryPayload;
		foldersPayload: DashboardFoldersPayload;
		hosts: HostsPayload;
		crumb?: string;
		mode?: 'queue' | 'folders';
	} = $props();

	type FolderScope = 'season' | 'series';

	let folderScope = $state<FolderScope>('season');
	const isFolderIndex = $derived(mode === 'folders');
	const seriesFolders = $derived(foldersPayload.series_folders ?? []);
	const canSelectSeriesScope = $derived(isFolderIndex && seriesFolders.length > 0);
	const queueFolders = $derived(foldersPayload.folders.filter(isQueueActionableFolder));
	const folders = $derived(
		isFolderIndex && folderScope === 'series'
			? seriesFolders
			: isFolderIndex
				? foldersPayload.folders
				: queueFolders
	);
	$effect(() => {
		if (folderScope === 'series' && !canSelectSeriesScope) {
			folderScope = 'season';
		}
	});
	let searchQuery = $state('');
	let libraryFilters = $state<Record<string, boolean>>({});
	let stateFilters = $state<Record<string, boolean>>({});
	let loadedFilterMode = $state<'queue' | 'folders' | null>(null);
	const libraryOptions = $derived(buildLibraryOptions(folders));
	const stateOptions = $derived(buildStateOptions(folders));
	const normalizedSearchQuery = $derived(searchQuery.trim().toLowerCase());
	const visibleFolders = $derived(
		folders.filter((folder) => {
			const searchMatches =
				normalizedSearchQuery.length === 0 ||
				[folder.title, folder.prefix, folder.subtitle, folderStatusCopy(folder)]
					.join(' ')
					.toLowerCase()
					.includes(normalizedSearchQuery);
			return (
				searchMatches &&
				libraryIncluded(libraryKey(folderLibrary(folder))) &&
				stateIncluded(stateKey(queueFolderState(folder)))
			);
		})
	);
	const visibleFoldersPayload = $derived({ ...foldersPayload, folders: visibleFolders });
	const nextFolder = $derived(visibleFolders[0] ?? null);
	let selectedPrefix = $state('');
	const selectedFolder = $derived(
		visibleFolders.find((folder) => folder.prefix === selectedPrefix) ?? nextFolder
	);
	const selectedFolderIndex = $derived(
		selectedFolder
			? visibleFolders.findIndex((folder) => folder.prefix === selectedFolder.prefix)
			: -1
	);
	const excludedLibraryCount = $derived(
		libraryOptions.filter((option) => !libraryIncluded(option.key)).length
	);
	const excludedStateCount = $derived(
		stateOptions.filter((option) => !stateIncluded(option.key)).length
	);
	const folderScopeLabel = $derived(folderScope === 'series' ? 'Whole shows' : 'Season folders');
	const primaryScopeUnit = $derived(
		isFolderIndex ? folderScopeLabel.toLowerCase() : 'work folders'
	);
	const folderScopeDetail = $derived(
		folderScope === 'series'
			? 'queue or inspect every season in a show together'
			: isFolderIndex
				? 'queue or inspect one season at a time'
				: 'review folders'
	);
	const statusTiles = $derived(
		buildQueueStatusTiles(
			dashboard,
			visibleFoldersPayload,
			hosts,
			isFolderIndex ? folderScopeLabel : 'Work folders'
		)
	);
	const footerSignals = $derived(buildQueueFooterSignals(dashboard, visibleFoldersPayload, hosts));
	const runningSamples = $derived(dashboard.calibration_queue.sample.running_count);
	const queuedSamples = $derived(dashboard.calibration_queue.sample.queued_count);
	const pendingReviews = $derived(dashboard.calibration_queue.sample.pending_review_count);
	const shellSubject = $derived(isFolderIndex ? 'Folders' : 'Queue');
	const shellRoute = $derived(isFolderIndex ? 'folders' : 'queue');
	const filterTitle = $derived(isFolderIndex ? 'Find folders' : 'Queue filters');
	const visibleSummaryLabel = $derived(
		isFolderIndex ? `Visible ${folderScopeLabel.toLowerCase()}` : 'Visible work'
	);
	const filterEmptyCopy = $derived(
		isFolderIndex ? 'all folders included' : 'all queue items included'
	);
	const scopeTitle = $derived(isFolderIndex ? 'Index status' : 'Work status');
	const primaryScopeTitle = $derived(
		isFolderIndex ? `Searchable ${folderScopeLabel.toLowerCase()}` : 'Review candidates'
	);
	const headerEyebrow = $derived(isFolderIndex ? 'Folder index' : 'Queue');
	const headerTitle = $derived(isFolderIndex ? 'Find media' : 'Choose next folder');
	const headerCopy = $derived(
		isFolderIndex
			? 'Choose season folders or whole shows, then search or filter the scope you want to inspect.'
			: 'Use the worklist to pick the next folder that needs review, then open Folder Studio when the action looks right.'
	);
	const workspaceLabel = $derived(
		isFolderIndex ? 'Folder index and selected folder' : 'Queue worklist and selected folder'
	);
	const activeEntityLabel = $derived(isFolderIndex && folderScope === 'series' ? 'Show' : 'Folder');
	const selectedPanelTitle = $derived(
		isFolderIndex ? `${activeEntityLabel} details` : 'Next action'
	);
	const tableEyebrow = $derived(
		isFolderIndex && folderScope === 'series'
			? 'Show index'
			: isFolderIndex
				? 'Folder index'
				: 'Ranked queue'
	);
	const tableTitle = $derived(
		isFolderIndex ? `Matching ${folderScopeLabel.toLowerCase()}` : 'Next folders to review'
	);
	const visibleScopeSummary = $derived(
		`${visibleFolders.length.toLocaleString('en-US')} / ${folders.length.toLocaleString(
			'en-US'
		)} ${isFolderIndex ? folderScopeLabel.toLowerCase() : 'folders'}`
	);

	type LibraryOption = {
		key: string;
		label: string;
		folders: number;
		pending: number;
		reclaim: number;
	};

	type StateOption = {
		key: string;
		label: string;
		folders: number;
		pending: number;
	};

	function rowKey(folder: FolderCard): string {
		return `${folder.prefix}:${folder.sort_score}`;
	}

	function rowRank(folder: FolderCard): string {
		const index = visibleFolders.findIndex((candidate) => candidate.prefix === folder.prefix);
		return index >= 0 ? String(index + 1) : '—';
	}

	function libraryKey(label: string): string {
		return label.trim().toLowerCase() || 'library';
	}

	function libraryLabel(label: string): string {
		return label.trim() || 'Library';
	}

	function stateKey(label: string): string {
		return label.trim().toLowerCase() || 'state';
	}

	function folderLibrary(folder: FolderCard): string {
		return folder.prefix.split('/')[0] || folder.scope_label || 'library';
	}

	function buildLibraryOptions(queue: FolderCard[]): LibraryOption[] {
		const byLibrary: Record<string, LibraryOption> = {};
		for (const folder of queue) {
			const library = folderLibrary(folder);
			const key = libraryKey(library);
			const existing = byLibrary[key] ?? {
				key,
				label: libraryLabel(library),
				folders: 0,
				pending: 0,
				reclaim: 0
			};
			existing.folders += 1;
			existing.pending += workflowOpenItemCount(folder);
			existing.reclaim += Math.max(folder.projected_reclaim_bytes ?? 0, 0);
			byLibrary[key] = existing;
		}
		return Object.values(byLibrary).sort((left, right) => right.pending - left.pending);
	}

	function buildStateOptions(queue: FolderCard[]): StateOption[] {
		const byState: Record<string, StateOption> = {};
		for (const folder of queue) {
			const label = queueFolderState(folder);
			const key = stateKey(label);
			const existing = byState[key] ?? {
				key,
				label,
				folders: 0,
				pending: 0
			};
			existing.folders += 1;
			existing.pending += workflowOpenItemCount(folder);
			byState[key] = existing;
		}
		return Object.values(byState).sort((left, right) => right.pending - left.pending);
	}

	function restoreFilters() {
		if (!browser || loadedFilterMode === mode) return;
		loadedFilterMode = mode;
		const filters = readStoredWorkbenchFilters(mode);
		if (!filters) {
			searchQuery = '';
			libraryFilters = {};
			stateFilters = {};
			clearStoredWorkbenchFilters(mode);
			return;
		}
		searchQuery = filters.searchQuery;
		libraryFilters = filters.libraryFilters;
		stateFilters = filters.stateFilters;
	}

	function persistFilters() {
		if (!browser || loadedFilterMode !== mode) return;
		writeStoredWorkbenchFilters(mode, { searchQuery, libraryFilters, stateFilters });
	}

	function libraryIncluded(key: string): boolean {
		return libraryFilters[key] ?? true;
	}

	function stateIncluded(key: string): boolean {
		return stateFilters[key] ?? true;
	}

	function setLibraryIncluded(key: string, included: boolean) {
		libraryFilters = { ...libraryFilters, [key]: included };
	}

	function setStateIncluded(key: string, included: boolean) {
		stateFilters = { ...stateFilters, [key]: included };
	}

	function setAllLibraries(included: boolean) {
		libraryFilters = Object.fromEntries(libraryOptions.map((option) => [option.key, included]));
	}

	function setAllStates(included: boolean) {
		stateFilters = Object.fromEntries(stateOptions.map((option) => [option.key, included]));
	}

	function handleSearchInput(event: Event) {
		searchQuery = (event.currentTarget as HTMLInputElement).value;
	}

	function handleLibraryFilterChange(key: string, event: Event) {
		setLibraryIncluded(key, (event.currentTarget as HTMLInputElement).checked);
	}

	function handleStateFilterChange(key: string, event: Event) {
		setStateIncluded(key, (event.currentTarget as HTMLInputElement).checked);
	}

	function setFolderScope(scope: FolderScope) {
		if (scope === folderScope) return;
		folderScope = scope;
		selectedPrefix = '';
	}

	function selectFolder(folder: FolderCard) {
		selectedPrefix = folder.prefix;
	}

	function selectedRecommendationCopy(folder: FolderCard | null, index: number): string {
		if (!folder) return 'No folder is selected.';
		const reclaim = formatBytes(folder.projected_reclaim_bytes);
		const openWork = workflowOpenItemCount(folder).toLocaleString('en-US');
		if (isFolderIndex && folderScope === 'series') {
			return `${folder.title} spans ${openWork} open workflow items across the whole show with about ${reclaim} of projected reclaim.`;
		}
		if (isFolderIndex) {
			return `${folder.title} has ${openWork} open workflow items and about ${reclaim} of projected reclaim.`;
		}
		const rankCopy =
			index === 0 ? 'the first visible work item' : `#${index + 1} in the visible worklist`;
		return `${folder.title} is ${rankCopy} with ${openWork} open workflow items and about ${reclaim} of projected reclaim.`;
	}

	$effect(restoreFilters);
	$effect(persistFilters);
</script>

<OperatorShell route={shellRoute} subject={shellSubject} {crumb} {statusTiles} {footerSignals}>
	<main class="home">
		<aside class="home__rail" aria-label="Queue filters and scope">
			<WorkstationPanel eyebrow="Filters" title={filterTitle}>
				<div class="queue-filter" aria-label="Queue filters">
					<div class="queue-filter__summary">
						<span>{visibleSummaryLabel}</span>
						<strong>{visibleScopeSummary}</strong>
						<small
							>{[
								excludedLibraryCount ? `${excludedLibraryCount} libraries excluded` : '',
								excludedStateCount ? `${excludedStateCount} states excluded` : '',
								searchQuery.trim() ? 'search active' : ''
							]
								.filter(Boolean)
								.join(' · ') || filterEmptyCopy}</small
						>
					</div>

					<label class="queue-filter__search">
						<span>Search</span>
						<input
							type="search"
							value={searchQuery}
							placeholder="Name, path, or state"
							oninput={handleSearchInput}
						/>
					</label>

					<div class="queue-filter__group">
						<div class="queue-filter__group-head">
							<span>Libraries</span>
							<div class="queue-filter__actions" aria-label="Bulk library filters">
								<button type="button" onclick={() => setAllLibraries(true)}>All</button>
								<button type="button" onclick={() => setAllLibraries(false)}>None</button>
							</div>
						</div>
						{#each libraryOptions as option (option.key)}
							<label
								class="queue-filter__row"
								class:queue-filter__row--excluded={!libraryIncluded(option.key)}
							>
								<input
									type="checkbox"
									checked={libraryIncluded(option.key)}
									onchange={(event) => handleLibraryFilterChange(option.key, event)}
								/>
								<span>
									<strong>{option.label}</strong>
									<small
										>{option.folders.toLocaleString('en-US')} folders · {option.pending.toLocaleString(
											'en-US'
										)}
										open</small
									>
								</span>
								<em>{formatBytes(option.reclaim)}</em>
							</label>
						{/each}
					</div>

					<div class="queue-filter__group">
						<div class="queue-filter__group-head">
							<span>States</span>
							<div class="queue-filter__actions" aria-label="Bulk state filters">
								<button type="button" onclick={() => setAllStates(true)}>All</button>
								<button type="button" onclick={() => setAllStates(false)}>None</button>
							</div>
						</div>
						{#each stateOptions as option (option.key)}
							<label
								class="queue-filter__row"
								class:queue-filter__row--excluded={!stateIncluded(option.key)}
							>
								<input
									type="checkbox"
									checked={stateIncluded(option.key)}
									onchange={(event) => handleStateFilterChange(option.key, event)}
								/>
								<span>
									<strong>{option.label}</strong>
									<small
										>{option.folders.toLocaleString('en-US')} folders · {option.pending.toLocaleString(
											'en-US'
										)}
										open</small
									>
								</span>
							</label>
						{/each}
					</div>
				</div>
			</WorkstationPanel>

			<WorkstationPanel eyebrow="Scope" title={scopeTitle}>
				<div class="scope-list">
					<div class="scope-row scope-row--active">
						<span>Primary</span>
						<strong
							>{isFolderIndex
								? `${primaryScopeTitle} · ${folderScopeLabel}`
								: primaryScopeTitle}</strong
						>
						<small
							>{visibleFolders.length.toLocaleString('en-US')} / {folders.length.toLocaleString(
								'en-US'
							)}
							{primaryScopeUnit}</small
						>
					</div>
					<div class="scope-row">
						<span>Samples</span>
						<strong>{runningSamples} running · {queuedSamples} queued</strong>
						<small>{pendingReviews} waiting for review</small>
					</div>
					<div class="scope-row">
						<span>Processing</span>
						<strong
							>{dashboard.encode_queue.running_count} running · {dashboard.encode_queue
								.queued_count}
							queued</strong
						>
						<small
							>{dashboard.encode_queue.state.scheduler_summary ??
								'work schedule unavailable'}</small
						>
					</div>
				</div>
			</WorkstationPanel>

			<WorkstationPanel eyebrow="Completed" title="Cleanup review">
				<dl class="kv">
					<dt>Route</dt>
					<dd>Completed</dd>
					<dt>Scope</dt>
					<dd>Archived originals</dd>
					<dt>Action</dt>
					<dd><a href={resolve('/completed')}>Review cleanup</a></dd>
				</dl>
			</WorkstationPanel>
		</aside>

		<section
			class="home__main"
			aria-label={isFolderIndex ? 'Folder index' : 'Operational review queue'}
		>
			<header class="queue-header">
				<div>
					<span class="mf-eyebrow">{headerEyebrow}</span>
					<h1>{headerTitle}</h1>
					<p>{headerCopy}</p>
				</div>
				<div class="queue-header__tools">
					{#if isFolderIndex}
						<div class="scope-switch" aria-label="Folder scope">
							<span>Scope</span>
							<div class="scope-toggle" role="group" aria-label="Folder scope">
								<button
									type="button"
									class:scope-toggle__button--active={folderScope === 'season'}
									aria-pressed={folderScope === 'season'}
									aria-current={folderScope === 'season' ? 'true' : undefined}
									onclick={() => setFolderScope('season')}
								>
									<span>Season folders</span>
									<small>{foldersPayload.folders.length.toLocaleString('en-US')}</small>
								</button>
								<button
									type="button"
									disabled={!canSelectSeriesScope}
									class:scope-toggle__button--active={folderScope === 'series'}
									aria-pressed={folderScope === 'series'}
									aria-current={folderScope === 'series' ? 'true' : undefined}
									onclick={() => setFolderScope('series')}
								>
									<span>Whole shows</span>
									<small>{seriesFolders.length.toLocaleString('en-US')}</small>
								</button>
							</div>
							<small>{folderScopeDetail}</small>
						</div>
					{/if}
					<div class="queue-header__facts">
						<div>
							<span>{isFolderIndex ? `Visible ${folderScopeLabel}` : 'Visible folders'}</span>
							<strong>{visibleFolders.length.toLocaleString('en-US')}</strong>
						</div>
						<div>
							<span>Open work</span>
							<strong>{totalPendingItems(visibleFolders).toLocaleString('en-US')}</strong>
						</div>
						<div>
							<span>Projected reclaim</span>
							<strong>{formatBytes(totalProjectedReclaim(visibleFolders))}</strong>
						</div>
					</div>
				</div>
			</header>

			<section class="queue-workspace" aria-label={workspaceLabel}>
				<WorkstationPanel
					eyebrow={selectedFolderIndex === 0 ? 'Recommended' : 'Selected'}
					title={selectedPanelTitle}
				>
					{#if selectedFolder}
						<div class="selected-folder">
							<div class="selected-folder__identity">
								<div class="selected-folder__head">
									<StateBadge
										tone={queueFolderTone(selectedFolder)}
										label={queueFolderState(selectedFolder)}
									/>
									<strong>#{selectedFolderIndex + 1}</strong>
								</div>
								<div>
									<h2>{selectedFolder.title}</h2>
									<p>{selectedFolder.prefix}</p>
								</div>
							</div>
							<div class="reason-block">
								<span>Why this row</span>
								<p>{selectedRecommendationCopy(selectedFolder, selectedFolderIndex)}</p>
							</div>
							<dl class="context-metrics">
								<dt>Open work</dt>
								<dd>{workflowOpenItemCount(selectedFolder).toLocaleString('en-US')}</dd>
								<dt>Projected reclaim</dt>
								<dd>{formatBytes(selectedFolder.projected_reclaim_bytes)}</dd>
								<dt>Source size</dt>
								<dd>{formatBytes(selectedFolder.total_size_bytes)}</dd>
								<dt>Codec mix</dt>
								<dd>{codecSummary(selectedFolder.video_codecs)}</dd>
								<dt>Catalog state</dt>
								<dd>{folderStatusCopy(selectedFolder)}</dd>
							</dl>
							<a
								class="control control--primary"
								href={resolve(folderRoutePath(selectedFolder.prefix))}>Open Folder Studio</a
							>
						</div>
					{:else}
						<div class="empty-note">Select a folder from the ranked queue.</div>
					{/if}
				</WorkstationPanel>

				<WorkstationPanel
					eyebrow={isFolderIndex ? tableEyebrow : 'Worklist'}
					title={tableTitle}
					meta={`${visibleFolders.length.toLocaleString('en-US')} visible`}
				>
					<div class="table-wrap">
						<table>
							<thead>
								<tr>
									<th>Rank</th>
									<th>State</th>
									<th>{activeEntityLabel}</th>
									<th>Open work</th>
									<th>Source</th>
									<th>Reclaim</th>
									<th>Codec</th>
									<th>Open</th>
								</tr>
							</thead>
							<tbody>
								{#each visibleFolders.slice(0, 32) as folder (rowKey(folder))}
									<tr class:row-selected={selectedFolder?.prefix === folder.prefix}>
										<td>
											<button
												type="button"
												class="rank-button"
												aria-label={`Select ${folder.title}`}
												aria-pressed={selectedFolder?.prefix === folder.prefix}
												onclick={() => selectFolder(folder)}
											>
												{rowRank(folder)}
											</button>
										</td>
										<td>
											<StateBadge
												compact
												tone={queueFolderTone(folder)}
												label={queueFolderState(folder)}
											/>
										</td>
										<td>
											<button
												type="button"
												class="folder-select"
												onclick={() => selectFolder(folder)}
											>
												<strong>{folder.title}</strong>
												<span>{folder.prefix}</span>
											</button>
										</td>
										<td>{workflowOpenItemCount(folder).toLocaleString('en-US')}</td>
										<td>{formatBytes(folder.total_size_bytes)}</td>
										<td>{formatBytes(folder.projected_reclaim_bytes)}</td>
										<td>{codecSummary(folder.video_codecs)}</td>
										<td>
											<a class="open-link" href={resolve(folderRoutePath(folder.prefix))}>Open</a>
										</td>
									</tr>
								{:else}
									<tr>
										<td colspan="8">No folders match the current filters.</td>
									</tr>
								{/each}
							</tbody>
						</table>
					</div>
				</WorkstationPanel>
			</section>
		</section>
	</main>
</OperatorShell>

<style>
	.home {
		display: grid;
		grid-template-columns: var(--mf-workstation-rail-width) minmax(0, 1fr);
		min-height: calc(100vh - 178px);
	}

	.home__rail {
		background: var(--mf-bg-shell);
		display: flex;
		flex-direction: column;
		gap: var(--mf-space-5);
		grid-column: 1;
		grid-row: 1;
		padding: var(--mf-space-5);
	}

	.home__rail {
		border-right: var(--mf-border);
	}

	.home__main {
		display: grid;
		gap: var(--mf-space-5);
		grid-column: 2;
		grid-row: 1;
		min-width: 0;
		padding: var(--mf-space-6);
	}

	.queue-header {
		align-items: start;
		border-bottom: var(--mf-border);
		display: grid;
		gap: var(--mf-space-6);
		grid-template-columns: minmax(320px, 0.95fr) minmax(360px, 1fr);
		padding-bottom: var(--mf-space-5);
	}

	.queue-workspace {
		align-items: start;
		display: grid;
		gap: var(--mf-space-5);
		grid-template-columns: minmax(0, 1fr);
	}

	.queue-header h1 {
		margin-top: var(--mf-space-3);
	}

	.queue-header p {
		max-width: 74ch;
		margin-top: var(--mf-space-3);
	}

	.queue-header__tools {
		display: grid;
		gap: var(--mf-space-4);
		justify-self: stretch;
		min-width: 0;
	}

	.queue-header__facts {
		display: grid;
		gap: var(--mf-space-4);
		grid-template-columns: repeat(3, minmax(0, 1fr));
	}

	.queue-header__facts div {
		display: grid;
		gap: var(--mf-space-2);
		min-width: 0;
	}

	.queue-header__facts span,
	.scope-row span,
	.kv dt {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-semibold);
		letter-spacing: 0.08em;
		text-transform: uppercase;
	}

	.queue-header__facts strong,
	.kv dd {
		font-family: var(--mf-font-mono), monospace;
		font-size: var(--mf-text-sm);
		font-weight: var(--mf-weight-medium);
	}

	.scope-list,
	.queue-filter,
	.selected-folder {
		display: grid;
		gap: var(--mf-space-4);
		padding: var(--mf-space-5);
	}

	.selected-folder {
		align-items: start;
		border-left: 2px solid var(--mf-wait-fg);
		grid-template-columns: minmax(0, 1.1fr) minmax(260px, 0.9fr);
	}

	.selected-folder :global(.state-badge) {
		justify-self: start;
	}

	.selected-folder .control {
		justify-self: start;
	}

	.selected-folder__head {
		align-items: center;
		display: flex;
		justify-content: space-between;
	}

	.selected-folder__identity {
		display: grid;
		gap: var(--mf-space-3);
		min-width: 0;
	}

	.selected-folder__head strong {
		color: var(--mf-fg-tertiary);
		font-family: var(--mf-font-mono), monospace;
		font-size: var(--mf-text-xs);
	}

	.scope-row {
		border-left: 2px solid var(--mf-line-strong);
		display: grid;
		gap: var(--mf-space-2);
		padding: var(--mf-space-4);
	}

	.scope-row--active {
		background: var(--mf-active-bg);
		border-left-color: var(--mf-active-fg);
	}

	.scope-row strong {
		font-size: var(--mf-text-sm);
		font-weight: var(--mf-weight-semibold);
	}

	.scope-row small,
	.selected-folder p {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-xs);
	}

	.reason-block {
		background: var(--mf-bg-panel-2);
		border: var(--mf-border-muted);
		border-left: 2px solid var(--mf-active-fg);
		display: grid;
		gap: var(--mf-space-2);
		padding: var(--mf-space-4);
	}

	.reason-block span {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-semibold);
		letter-spacing: 0.08em;
		text-transform: uppercase;
	}

	.reason-block p {
		color: var(--mf-fg-secondary);
		margin: 0;
	}

	.queue-filter__summary {
		border-left: 2px solid var(--mf-active-fg);
		display: grid;
		gap: var(--mf-space-2);
		padding: var(--mf-space-3) var(--mf-space-4);
	}

	.queue-filter__summary span,
	.queue-filter__search span,
	.queue-filter__group-head > span,
	.queue-filter__row small {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-semibold);
		letter-spacing: 0.08em;
		text-transform: uppercase;
	}

	.queue-filter__summary strong {
		font-family: var(--mf-font-mono), monospace;
		font-size: var(--mf-text-sm);
	}

	.queue-filter__summary small {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-xs);
	}

	.queue-filter__search {
		display: grid;
		gap: var(--mf-space-2);
	}

	.scope-switch {
		display: grid;
		gap: var(--mf-space-2);
	}

	.scope-switch > span,
	.scope-switch > small {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-semibold);
		letter-spacing: 0.08em;
		text-transform: uppercase;
	}

	.scope-toggle {
		display: grid;
		gap: var(--mf-space-2);
		grid-template-columns: 1fr 1fr;
	}

	.scope-toggle button {
		background: var(--mf-bg-panel-2);
		border: var(--mf-border);
		border-radius: var(--mf-radius-1);
		color: var(--mf-fg-secondary);
		display: grid;
		font: inherit;
		gap: var(--mf-space-1);
		min-height: 48px;
		min-width: 0;
		padding: var(--mf-space-2) var(--mf-space-3);
		text-align: left;
	}

	.scope-toggle button:hover:not(:disabled),
	.scope-toggle button.scope-toggle__button--active {
		background: var(--mf-active-bg);
		border-color: var(--mf-active-solid-hi);
		color: var(--mf-fg-primary);
	}

	.scope-toggle button.scope-toggle__button--active {
		box-shadow: inset 3px 0 0 var(--mf-active-fg);
	}

	.scope-toggle button.scope-toggle__button--active small,
	.scope-toggle button.scope-toggle__button--active span {
		color: var(--mf-active-fg-bright);
	}

	.scope-toggle button:disabled {
		cursor: not-allowed;
		opacity: 0.48;
	}

	.scope-toggle span {
		font-size: var(--mf-text-xs);
		font-weight: var(--mf-weight-semibold);
		overflow-wrap: anywhere;
	}

	.scope-toggle small {
		color: var(--mf-fg-tertiary);
		font-family: var(--mf-font-mono), monospace;
		font-size: var(--mf-text-2xs);
	}

	.queue-filter__search input {
		background: var(--mf-bg-input);
		border: var(--mf-border);
		border-radius: var(--mf-radius-1);
		color: var(--mf-fg-primary);
		font: inherit;
		min-height: var(--mf-control-lg);
		padding: 0 var(--mf-space-4);
		width: 100%;
	}

	.queue-filter__group {
		display: grid;
		gap: var(--mf-space-2);
	}

	.queue-filter__summary,
	.queue-filter__search,
	.queue-filter__group {
		min-width: 0;
	}

	.queue-filter__group-head {
		align-items: center;
		display: flex;
		gap: var(--mf-space-3);
		justify-content: space-between;
	}

	.queue-filter__actions {
		display: grid;
		gap: var(--mf-space-2);
		grid-template-columns: 1fr 1fr;
	}

	.queue-filter__actions button {
		background: var(--mf-bg-panel-2);
		border: var(--mf-border);
		border-radius: var(--mf-radius-1);
		color: var(--mf-fg-secondary);
		font-size: var(--mf-text-xs);
		font-weight: var(--mf-weight-semibold);
		min-height: var(--mf-control-sm);
		padding: 0 var(--mf-space-3);
	}

	.queue-filter__actions button:hover {
		background: var(--mf-bg-raised);
		color: var(--mf-fg-primary);
	}

	.queue-filter__row {
		align-items: center;
		background: var(--mf-bg-panel-2);
		border: var(--mf-border-muted);
		border-radius: var(--mf-radius-1);
		display: grid;
		gap: var(--mf-space-3);
		grid-template-columns: auto minmax(0, 1fr) auto;
		min-height: var(--mf-row-default);
		padding: var(--mf-space-3);
	}

	.queue-filter__row input {
		accent-color: var(--mf-active-solid);
		height: 16px;
		width: 16px;
	}

	.queue-filter__row > span {
		display: grid;
		gap: var(--mf-space-1);
		min-width: 0;
	}

	.queue-filter__row--excluded {
		opacity: 0.52;
	}

	.queue-filter__row--excluded strong {
		text-decoration: line-through;
		text-decoration-thickness: 1px;
	}

	.queue-filter__row small {
		letter-spacing: 0;
		text-transform: none;
	}

	.queue-filter__row em {
		color: var(--mf-fg-tertiary);
		font-family: var(--mf-font-mono), monospace;
		font-size: var(--mf-text-2xs);
		font-style: normal;
	}

	.table-wrap {
		overflow: auto;
	}

	table {
		border-collapse: collapse;
		min-width: 840px;
		width: 100%;
	}

	th,
	td {
		border-bottom: var(--mf-border-muted);
		font-size: var(--mf-text-xs);
		height: var(--mf-row-default);
		padding: 0 var(--mf-space-3);
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

	td:nth-child(4),
	td:nth-child(5),
	td:nth-child(6),
	td:nth-child(7) {
		font-family: var(--mf-font-mono), monospace;
	}

	tr.row-selected td {
		background: var(--mf-active-bg);
	}

	.rank-button,
	.folder-select {
		background: transparent;
		border: 0;
		color: inherit;
		cursor: pointer;
		font: inherit;
		padding: 0;
		text-align: left;
	}

	.rank-button {
		align-items: center;
		border: var(--mf-border);
		display: inline-flex;
		font-family: var(--mf-font-mono), monospace;
		height: 26px;
		justify-content: center;
		width: 34px;
	}

	.rank-button:hover,
	.rank-button[aria-pressed='true'] {
		background: var(--mf-active-solid);
		border-color: var(--mf-active-solid-hi);
		color: var(--mf-fg-on-accent);
	}

	.folder-select {
		display: grid;
		gap: var(--mf-space-1);
		min-width: 0;
		width: 100%;
	}

	.folder-select strong {
		font-size: var(--mf-text-sm);
		font-weight: var(--mf-weight-semibold);
	}

	.folder-select span {
		color: var(--mf-fg-tertiary);
		font-family: var(--mf-font-mono), monospace;
		font-size: var(--mf-text-2xs);
		overflow-wrap: anywhere;
	}

	.open-link {
		color: var(--mf-active-fg);
		font-weight: var(--mf-weight-semibold);
	}

	.selected-folder h2 {
		margin-top: var(--mf-space-2);
		overflow-wrap: anywhere;
	}

	.control {
		align-items: center;
		background: var(--mf-bg-panel-2);
		border: var(--mf-border-strong);
		border-radius: var(--mf-radius-1);
		color: var(--mf-fg-primary);
		display: inline-flex;
		font-weight: var(--mf-weight-semibold);
		justify-content: center;
		min-height: var(--mf-control-lg);
		padding: 0 var(--mf-space-5);
	}

	.control--primary {
		background: var(--mf-active-solid);
		border-color: var(--mf-active-solid-hi);
		color: var(--mf-fg-on-accent);
	}

	.kv {
		display: grid;
		grid-template-columns: minmax(88px, auto) minmax(0, 1fr);
		padding: var(--mf-space-5);
		row-gap: var(--mf-space-4);
	}

	.kv dd {
		overflow-wrap: anywhere;
	}

	.context-metrics {
		display: grid;
		grid-template-columns: minmax(112px, auto) minmax(0, 1fr);
		row-gap: var(--mf-space-3);
	}

	.context-metrics dt {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-semibold);
		letter-spacing: 0.08em;
		text-transform: uppercase;
	}

	.context-metrics dd {
		font-family: var(--mf-font-mono), monospace;
		font-size: var(--mf-text-xs);
		overflow-wrap: anywhere;
	}

	.empty-note {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-sm);
		padding: var(--mf-space-5);
	}

	@media (max-width: 1180px) {
		.home {
			grid-template-columns: var(--mf-workstation-rail-width) minmax(0, 1fr);
		}
	}

	@media (max-width: 820px) {
		.home {
			grid-template-columns: 1fr;
		}

		.home__main {
			grid-column: auto;
			grid-row: 1;
		}

		.home__rail {
			border-left: 0;
			border-right: 0;
			grid-column: auto;
			grid-row: 2;
		}

		.queue-header {
			grid-template-columns: 1fr;
		}

		.queue-header__facts {
			grid-template-columns: repeat(3, minmax(0, 1fr));
		}

		.queue-header__facts strong {
			overflow-wrap: anywhere;
		}

		.queue-workspace {
			grid-template-columns: 1fr;
		}

		.selected-folder {
			grid-template-columns: 1fr;
		}
	}

	@media (max-width: 680px) {
		.table-wrap {
			overflow: visible;
		}

		table {
			min-width: 0;
		}

		thead {
			display: none;
		}

		tbody,
		tr,
		td {
			display: block;
			width: 100%;
		}

		tr {
			border-bottom: var(--mf-border-muted);
			display: grid;
			gap: var(--mf-space-3);
			grid-template-columns: auto minmax(0, 1fr);
			padding: var(--mf-space-4);
		}

		td {
			border-bottom: 0;
			height: auto;
			min-width: 0;
			padding: 0;
		}

		td:nth-child(1) {
			grid-row: 1 / span 6;
			width: 40px;
		}

		td:nth-child(2),
		td:nth-child(3),
		td:nth-child(4),
		td:nth-child(5),
		td:nth-child(6),
		td:nth-child(7),
		td:nth-child(8) {
			grid-column: 2;
		}

		td:nth-child(4),
		td:nth-child(5),
		td:nth-child(6),
		td:nth-child(7) {
			align-items: baseline;
			display: grid;
			gap: var(--mf-space-3);
			grid-template-columns: 72px minmax(0, 1fr);
		}

		td:nth-child(4)::before,
		td:nth-child(5)::before,
		td:nth-child(6)::before,
		td:nth-child(7)::before {
			color: var(--mf-fg-tertiary);
			font-family: var(--mf-font-sans), sans-serif;
			font-size: var(--mf-text-2xs);
			font-weight: var(--mf-weight-semibold);
			letter-spacing: 0.08em;
			text-transform: uppercase;
		}

		td:nth-child(4)::before {
			content: 'Open work';
		}

		td:nth-child(5)::before {
			content: 'Source';
		}

		td:nth-child(6)::before {
			content: 'Reclaim';
		}

		td:nth-child(7)::before {
			content: 'Codec';
		}
	}
</style>
