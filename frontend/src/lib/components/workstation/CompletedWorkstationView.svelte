<script lang="ts">
	import { resolve } from '$app/paths';
	import { postJson } from '$lib/api/client';
	import type {
		ArchiveCleanupSummary,
		CompletedCleanupResult,
		CompletedFolderRow,
		CompletedHistoryEvent,
		CompletedOriginalsResolvedResult,
		CompletedPayload
	} from '$lib/api/types';
	import { folderRoutePath } from '$lib/folder-display';
	import { formatBytes as formatTechnicalBytes } from './folder-studio-view';
	import OperatorShell from './OperatorShell.svelte';
	import StateBadge from './StateBadge.svelte';
	import WorkstationPanel from './WorkstationPanel.svelte';
	import {
		buildCompletedFooterSignals,
		buildCompletedHistoryRows,
		buildCompletedReadinessSummary,
		buildCompletedStatusTiles,
		cleanupDetail,
		cleanupLabel,
		cleanupState,
		cleanupStateCounts,
		cleanupTone,
		completedHistoryDetail,
		completedHistoryLabel,
		completedHistorySearchText,
		completedLibraryOptions,
		completedStateOptions,
		folderCanBeMarkedHandled,
		folderCanBeSelected,
		folderSearchText,
		readyFolders,
		totalArchivedBackupSize,
		totalSavedSize,
		type CleanupState
	} from './completed-workstation';
	import type { ShellTone } from './shell-types';

	type WorkstationMode = 'completed' | 'history';
	type CleanupScope = 'selected' | 'global';
	type ReviewScope = 'already-removed';

	let {
		completed: loadedCompleted,
		loadError,
		onArchiveRefresh = async () => null,
		onCompletedUpdate = () => {}
	}: {
		completed: CompletedPayload | null;
		loadError: string | null;
		onArchiveRefresh?: () => Promise<ArchiveCleanupSummary | null>;
		onCompletedUpdate?: (completed: CompletedPayload) => void;
	} = $props();

	let completed = $state<CompletedPayload | null>(null);
	let mode = $state<WorkstationMode>('completed');
	let searchQuery = $state('');
	let historyQuery = $state('');
	let libraryFilters = $state<Record<string, boolean>>({});
	let stateFilters = $state<Record<string, boolean>>({});
	let selectedPrefixes = $state<Record<string, boolean>>({});
	let reviewPrefixes = $state<Record<string, boolean>>({});
	let armedScope = $state<CleanupScope | null>(null);
	let armedReview = $state<ReviewScope | null>(null);
	let cleanupPending = $state(false);
	let reviewPending = $state(false);
	let actionMessage = $state('');
	let actionError = $state('');
	let lastCleanupResult = $state<CompletedCleanupResult | null>(null);
	let localHistory = $state<CompletedHistoryEvent[]>([]);

	$effect(() => {
		completed = loadedCompleted;
		actionError = loadError ?? '';
	});

	const folders = $derived(completed?.folders ?? []);
	const archive = $derived(
		completed?.archive_cleanup ?? {
			archive_root: '',
			file_count: 0,
			total_size_bytes: 0,
			has_cleanup: false
		}
	);
	const statusTiles = $derived(buildCompletedStatusTiles(completed, actionError || loadError));
	const footerSignals = $derived(buildCompletedFooterSignals(completed));
	const readiness = $derived(buildCompletedReadinessSummary(completed, actionError || loadError));
	const libraryOptions = $derived(completedLibraryOptions(folders));
	const stateOptions = $derived(completedStateOptions(folders, archive));
	const normalizedSearchQuery = $derived(searchQuery.trim().toLowerCase());
	const filteredFolders = $derived(
		folders.filter((folder) => {
			const searchMatches =
				normalizedSearchQuery.length === 0 ||
				folderSearchText(folder, archive).includes(normalizedSearchQuery);
			return (
				searchMatches &&
				libraryIncluded(libraryKey(folder.scope_label || folder.prefix.split('/')[0])) &&
				stateIncluded(cleanupState(folder, archive))
			);
		})
	);
	const cleanupReadyFolders = $derived(readyFolders(folders, archive));
	const filteredReadyFolders = $derived(
		filteredFolders.filter((folder) => folderCanBeSelected(folder, archive))
	);
	const filteredReviewFolders = $derived(
		filteredFolders.filter((folder) => folderCanBeMarkedHandled(folder, archive))
	);
	const selectedFolders = $derived(
		folders.filter(
			(folder) => selectedPrefixes[folder.prefix] && folderCanBeSelected(folder, archive)
		)
	);
	const selectedBackupSize = $derived(totalArchivedBackupSize(selectedFolders));
	const selectedBackupCount = $derived(
		selectedFolders.reduce((total, folder) => total + Math.max(folder.archived_backup_count, 0), 0)
	);
	const reviewFolders = $derived(
		folders.filter(
			(folder) => reviewPrefixes[folder.prefix] && folderCanBeMarkedHandled(folder, archive)
		)
	);
	const reviewItemCount = $derived(
		reviewFolders.reduce(
			(total, folder) => total + Math.max(folder.missing_backup_count ?? 0, 0),
			0
		)
	);
	const counts = $derived(cleanupStateCounts(folders, archive));
	const recommendedFolder = $derived(cleanupReadyFolders[0] ?? null);
	const cleanupWorkAvailable = $derived(archive.has_cleanup || cleanupReadyFolders.length > 0);
	const cleanupReviewCount = $derived(counts.blocked + counts.unknown);
	const cleanupNeedsAction = $derived(cleanupWorkAvailable || cleanupReviewCount > 0);
	const cleanupStatusTone = $derived(
		cleanupReadyFolders.length > 0 ? 'ready' : cleanupReviewCount > 0 ? 'wait' : 'idle'
	);
	const cleanupStatusLabel = $derived(
		cleanupReadyFolders.length > 0
			? 'Originals waiting'
			: cleanupReviewCount > 0
				? 'Review needed'
				: 'Settled'
	);
	const cleanupStatusTitle = $derived(
		cleanupReadyFolders.length > 0
			? `${cleanupReadyFolders.length.toLocaleString('en-US')} finished seasons have originals waiting.`
			: cleanupReviewCount > 0
				? `${cleanupReviewCount.toLocaleString('en-US')} finished seasons need review.`
				: 'No originals are waiting.'
	);
	const cleanupStatusDetail = $derived(
		cleanupReadyFolders.length > 0
			? 'Review the seasons below before removing their originals.'
			: cleanupReviewCount > 0
				? 'Check the new files, then confirm which originals are already gone.'
				: 'Past changes remain available in History.'
	);
	const historyRows = $derived([
		...localHistory.map((event) => ({ ...event, source: 'api' as const })),
		...(completed ? buildCompletedHistoryRows(completed) : [])
	]);
	const showMainHistorySummary = $derived(!cleanupNeedsAction && historyRows.length > 0);
	const normalizedHistoryQuery = $derived(historyQuery.trim().toLowerCase());
	const visibleHistory = $derived(
		historyRows.filter((event) => {
			if (normalizedHistoryQuery.length === 0) return true;
			return completedHistorySearchText(event).includes(normalizedHistoryQuery);
		})
	);
	const selectedSummary = $derived(
		selectedFolders.length
			? `${selectedFolders.length.toLocaleString('en-US')} folders · ${selectedBackupCount.toLocaleString('en-US')} files · ${formatBytes(selectedBackupSize)}`
			: 'No folders with originals waiting are selected'
	);
	const reviewSummary = $derived(
		reviewFolders.length
			? `${reviewFolders.length.toLocaleString('en-US')} folders · ${reviewItemCount.toLocaleString('en-US')} originals already gone`
			: 'No folders selected for review'
	);
	const globalRemovalSummary = $derived(
		`This removes ${archive.file_count.toLocaleString('en-US')} originals from ${archive.archive_root || 'the cleanup folder'} and reclaims ${formatBytes(archive.total_size_bytes)}.`
	);

	function selectedRemovalSummary(): string {
		if (!selectedFolders.length) return selectedSummary;
		return `This removes originals for ${selectedFolders.length.toLocaleString('en-US')} selected folders from ${archive.archive_root || 'the cleanup folder'} and reclaims ${formatBytes(selectedBackupSize)}.`;
	}

	function libraryKey(label: string): string {
		return label.trim().toLowerCase() || 'library';
	}

	function libraryIncluded(key: string): boolean {
		return libraryFilters[key] ?? true;
	}

	function stateIncluded(state: CleanupState): boolean {
		return stateFilters[state] ?? true;
	}

	function handleSearchInput(event: Event) {
		searchQuery = (event.currentTarget as HTMLInputElement).value;
	}

	function handleHistorySearchInput(event: Event) {
		historyQuery = (event.currentTarget as HTMLInputElement).value;
	}

	function handleLibraryFilterChange(key: string, event: Event) {
		libraryFilters = {
			...libraryFilters,
			[key]: (event.currentTarget as HTMLInputElement).checked
		};
	}

	function handleStateFilterChange(key: string, event: Event) {
		stateFilters = { ...stateFilters, [key]: (event.currentTarget as HTMLInputElement).checked };
	}

	function setAllLibraries(included: boolean) {
		libraryFilters = Object.fromEntries(libraryOptions.map((option) => [option.key, included]));
	}

	function setAllStates(included: boolean) {
		stateFilters = Object.fromEntries(stateOptions.map((option) => [option.key, included]));
	}

	function selected(folder: CompletedFolderRow): boolean {
		return Boolean(selectedPrefixes[folder.prefix]);
	}

	function selectedForReview(folder: CompletedFolderRow): boolean {
		return Boolean(reviewPrefixes[folder.prefix]);
	}

	function setFolderSelected(folder: CompletedFolderRow, value: boolean) {
		if (!folderCanBeSelected(folder, archive)) return;
		selectedPrefixes = { ...selectedPrefixes, [folder.prefix]: value };
		armedScope = null;
	}

	function setFolderReviewSelected(folder: CompletedFolderRow, value: boolean) {
		if (!folderCanBeMarkedHandled(folder, archive)) return;
		reviewPrefixes = { ...reviewPrefixes, [folder.prefix]: value };
		armedReview = null;
	}

	function handleRowSelection(folder: CompletedFolderRow, event: Event) {
		setFolderSelected(folder, (event.currentTarget as HTMLInputElement).checked);
	}

	function handleReviewSelection(folder: CompletedFolderRow, event: Event) {
		setFolderReviewSelected(folder, (event.currentTarget as HTMLInputElement).checked);
	}

	function selectVisibleReady() {
		selectedPrefixes = {
			...selectedPrefixes,
			...Object.fromEntries(filteredReadyFolders.map((folder) => [folder.prefix, true]))
		};
		armedScope = null;
	}

	function selectVisibleReview() {
		reviewPrefixes = {
			...reviewPrefixes,
			...Object.fromEntries(filteredReviewFolders.map((folder) => [folder.prefix, true]))
		};
		armedReview = null;
	}

	function clearSelection() {
		selectedPrefixes = {};
		reviewPrefixes = {};
		armedScope = null;
		armedReview = null;
	}

	function cleanupDisabled(scope: CleanupScope): boolean {
		if (!completed || cleanupPending) return true;
		if (scope === 'selected') return selectedFolders.length === 0;
		return !archive.has_cleanup || archive.file_count <= 0 || !archive.archive_root;
	}

	function armCleanup(scope: CleanupScope) {
		actionMessage = '';
		actionError = '';
		lastCleanupResult = null;
		armedScope = armedScope === scope ? null : scope;
		armedReview = null;
	}

	function armReview(scope: ReviewScope) {
		actionMessage = '';
		actionError = '';
		lastCleanupResult = null;
		armedScope = null;
		armedReview = armedReview === scope ? null : scope;
	}

	function applyCompleted(nextCompleted: CompletedPayload) {
		completed = nextCompleted;
		onCompletedUpdate(nextCompleted);
	}

	async function refreshArchiveSummary() {
		if (!completed) return;
		const archiveCleanup = await onArchiveRefresh();
		if (!archiveCleanup) return;
		applyCompleted({ ...completed, archive_cleanup: archiveCleanup });
	}

	async function confirmAlreadyRemoved() {
		if (!completed || reviewFolders.length === 0 || reviewPending) return;
		reviewPending = true;
		actionMessage = '';
		actionError = '';
		try {
			const foldersToResolve = reviewFolders;
			const result = await postJson<CompletedOriginalsResolvedResult>(
				`${resolve('/')}api/completed/originals/confirm-removed`,
				{ prefixes: foldersToResolve.map((folder) => folder.prefix) }
			);
			actionMessage = result.message || 'Marked as already handled.';
			if (result.completed) {
				applyCompleted(result.completed);
			}
			localHistory = [
				{
					id: Date.now(),
					event_type: 'originals_removed_confirmed',
					label: 'Originals marked removed',
					tone: 'ready',
					prefix: `${foldersToResolve.length} selected folders`,
					title: 'Originals already removed',
					subtitle: result.message,
					scope_label: 'review',
					created_at: new Date().toISOString(),
					detail: `${result.resolved_count.toLocaleString('en-US')} items marked handled · no files deleted`,
					size_bytes: 0
				},
				...localHistory
			];
			reviewPrefixes = {};
			armedReview = null;
		} catch (error) {
			actionError = error instanceof Error ? error.message : 'Review update failed.';
		} finally {
			reviewPending = false;
		}
	}

	async function confirmCleanup(scope: CleanupScope) {
		if (cleanupDisabled(scope)) return;
		cleanupPending = true;
		actionMessage = '';
		actionError = '';
		try {
			const cleanupFolders = selectedFolders;
			const body =
				scope === 'selected' ? { prefixes: cleanupFolders.map((folder) => folder.prefix) } : {};
			const result = await postJson<CompletedCleanupResult>(
				`${resolve('/')}api/completed/backups/clear`,
				body
			);
			lastCleanupResult = result;
			actionMessage = result.message || 'Cleanup completed.';
			if (result.completed) {
				applyCompleted(result.completed);
			}
			try {
				await refreshArchiveSummary();
			} catch {
				// Cleanup already completed; keep that result visible if the follow-up scan fails.
			}
			localHistory = [
				{
					id: Date.now(),
					event_type: 'cleanup_completed',
					label: scope === 'selected' ? 'Selected cleanup completed' : 'Global cleanup completed',
					tone: 'ready',
					prefix:
						scope === 'selected' ? `${cleanupFolders.length} selected folders` : 'cleanup folder',
					title: scope === 'selected' ? 'Selected cleanup' : 'Global cleanup',
					subtitle: result.message,
					scope_label: 'cleanup',
					created_at: new Date().toISOString(),
					detail: `${result.removed_count.toLocaleString('en-US')} files removed · ${formatBytes(result.removed_size_bytes)}`,
					size_bytes: result.removed_size_bytes
				},
				...localHistory
			];
			selectedPrefixes = {};
			armedScope = null;
		} catch (error) {
			actionError = error instanceof Error ? error.message : 'Cleanup failed.';
		} finally {
			cleanupPending = false;
		}
	}

	function formatTimestamp(value: string | null | undefined): string {
		if (!value) return '—';
		const date = new Date(value);
		if (Number.isNaN(date.getTime())) return value;
		return date.toLocaleString([], {
			month: 'short',
			day: '2-digit',
			hour: '2-digit',
			minute: '2-digit'
		});
	}

	function formatBytes(value: number | null | undefined): string {
		return formatTechnicalBytes(value)
			.replace('GiB', 'GB')
			.replace('MiB', 'MB')
			.replace('KiB', 'KB');
	}

	function eventTone(value: string): ShellTone {
		if (value === 'active' || value === 'ready' || value === 'wait' || value === 'fail') {
			return value;
		}
		return 'idle';
	}
</script>

<OperatorShell
	route="completed"
	subject="Finished"
	crumb="/completed"
	{statusTiles}
	{footerSignals}
>
	<main class="completed">
		<section class="completed__main" aria-label="Finished seasons">
			<header class="completed-header">
				<div>
					<span class="mf-eyebrow">Finished</span>
					<h1>Finished seasons</h1>
					<p>{readiness.detail}</p>
				</div>
				<div class="completed-header__facts" aria-label="Completed cleanup totals">
					<div>
						<span>Finished</span>
						<strong>{folders.length.toLocaleString('en-US')}</strong>
					</div>
					<div>
						<span>Originals waiting</span>
						<strong>{counts.ready.toLocaleString('en-US')}</strong>
					</div>
					<div>
						<span>Needs review</span>
						<strong>{(counts.blocked + counts.unknown).toLocaleString('en-US')}</strong>
					</div>
					<div>
						<span>Space saved</span>
						<strong>{formatBytes(totalSavedSize(folders))}</strong>
					</div>
				</div>
			</header>

			<div class="modebar" role="tablist" aria-label="Finished season views">
				<button
					type="button"
					role="tab"
					aria-selected={mode === 'completed'}
					class:active={mode === 'completed'}
					onclick={() => (mode = 'completed')}>Originals</button
				>
				<button
					type="button"
					role="tab"
					aria-selected={mode === 'history'}
					class:active={mode === 'history'}
					onclick={() => (mode = 'history')}>History</button
				>
			</div>

			{#if !completed}
				<WorkstationPanel eyebrow="Status" title="Finished seasons are unavailable">
					<div class="empty-note empty-note--error">
						{actionError || 'Finished season data is loading or unavailable.'}
					</div>
				</WorkstationPanel>
			{:else if mode === 'completed'}
				<WorkstationPanel eyebrow="Originals" title="What is waiting">
					<div class="cleanup-status cleanup-status--{cleanupStatusTone}">
						<StateBadge tone={cleanupStatusTone} label={cleanupStatusLabel} />
						<div>
							<strong>{cleanupStatusTitle}</strong>
							<span>{cleanupStatusDetail}</span>
						</div>
					</div>
				</WorkstationPanel>

				{#if showMainHistorySummary}
					<WorkstationPanel
						eyebrow="History"
						title="Recent changes"
						meta={`${historyRows.length.toLocaleString('en-US')} events`}
					>
						<div class="history-list history-list--summary">
							{#each historyRows.slice(0, 5) as event (`summary:${event.source}:${event.id}:${event.created_at}`)}
								<div class="history-row">
									<StateBadge
										compact
										tone={eventTone(event.tone)}
										label={completedHistoryLabel(event)}
									/>
									<div>
										<strong>{event.title}</strong>
										<span>{event.prefix}</span>
									</div>
									<p>{completedHistoryDetail(event.detail, event)}</p>
									<time>{formatTimestamp(event.created_at)}</time>
								</div>
							{/each}
						</div>
					</WorkstationPanel>
				{/if}

				<WorkstationPanel eyebrow="Library" title="Find a finished season">
					<div class="completed-filter" aria-label="Completed cleanup filters">
						<div class="completed-filter__summary">
							<span>Visible seasons</span>
							<strong
								>{filteredFolders.length.toLocaleString('en-US')} / {folders.length.toLocaleString(
									'en-US'
								)}
								seasons</strong
							>
							<small>{reviewFolders.length > 0 ? reviewSummary : selectedSummary}</small>
						</div>

						<label class="completed-filter__search">
							<span>Search</span>
							<input
								type="search"
								value={searchQuery}
								placeholder="Show, season, or status"
								oninput={handleSearchInput}
							/>
						</label>

						<div class="completed-filter__group">
							<div class="completed-filter__group-head">
								<span>Libraries</span>
								<div class="completed-filter__actions" aria-label="Bulk library filters">
									<button type="button" onclick={() => setAllLibraries(true)}>All</button>
									<button type="button" onclick={() => setAllLibraries(false)}>None</button>
								</div>
							</div>
							{#each libraryOptions as option (option.key)}
								<label class="completed-filter__row" class:excluded={!libraryIncluded(option.key)}>
									<input
										type="checkbox"
										checked={libraryIncluded(option.key)}
										onchange={(event) => handleLibraryFilterChange(option.key, event)}
									/>
									<span>
										<strong>{option.label}</strong>
										<small
											>{option.folders.toLocaleString('en-US')} folders · {option.backups.toLocaleString(
												'en-US'
											)}
											waiting originals</small
										>
									</span>
									<em>{formatBytes(option.size)}</em>
								</label>
							{/each}
						</div>

						<div class="completed-filter__group">
							<div class="completed-filter__group-head">
								<span>Folder state</span>
								<div class="completed-filter__actions" aria-label="Bulk cleanup state filters">
									<button type="button" onclick={() => setAllStates(true)}>All</button>
									<button type="button" onclick={() => setAllStates(false)}>None</button>
								</div>
							</div>
							{#each stateOptions as option (option.key)}
								<label
									class="completed-filter__row"
									class:excluded={!stateIncluded(option.key as CleanupState)}
								>
									<input
										type="checkbox"
										checked={stateIncluded(option.key as CleanupState)}
										onchange={(event) => handleStateFilterChange(option.key, event)}
									/>
									<span>
										<strong>{option.label}</strong>
										<small
											>{option.folders.toLocaleString('en-US')} folders · {option.backups.toLocaleString(
												'en-US'
											)}
											waiting originals</small
										>
									</span>
									<em>{formatBytes(option.size)}</em>
								</label>
							{/each}
						</div>
					</div>
				</WorkstationPanel>

				{#if cleanupNeedsAction}
					<WorkstationPanel
						eyebrow="Originals"
						title={cleanupWorkAvailable
							? 'Choose what to do with originals'
							: cleanupReviewCount > 0
								? 'Review originals that are already gone'
								: 'No cleanup action needed'}
					>
						<div class="cleanup-command">
							<div class="cleanup-command__state">
								<StateBadge
									tone={cleanupStatusTone}
									label={recommendedFolder
										? 'Originals waiting'
										: cleanupReviewCount > 0
											? 'Needs review'
											: 'No action'}
								/>
								<div>
									<strong
										>{recommendedFolder
											? `${recommendedFolder.title} is ready for your decision`
											: cleanupReviewCount > 0
												? 'Some originals were already removed'
												: 'Completed work is handled'}</strong
									>
									<span
										>{recommendedFolder
											? cleanupDetail(recommendedFolder, archive)
											: cleanupReviewCount > 0
												? 'After checking the new files, mark these folders handled so they leave review.'
												: archive.archive_root
													? `Nothing is waiting in ${archive.archive_root}.`
													: 'Cleanup folder is not configured, and no completed folder is asking for action.'}</span
									>
								</div>
							</div>

							{#if cleanupWorkAvailable || cleanupReviewCount > 0}
								<div class="cleanup-command__actions" aria-label="Cleanup actions">
									<button
										type="button"
										class="control"
										disabled={filteredReadyFolders.length === 0}
										onclick={selectVisibleReady}>Select visible originals</button
									>
									<button
										type="button"
										class="control"
										disabled={filteredReviewFolders.length === 0}
										onclick={selectVisibleReview}>Select visible review items</button
									>
									<button
										type="button"
										class="control"
										disabled={selectedFolders.length === 0 && reviewFolders.length === 0}
										onclick={clearSelection}>Clear selection</button
									>
									<button
										type="button"
										class="control control--danger"
										class:armed={armedScope === 'selected'}
										disabled={cleanupDisabled('selected')}
										onclick={() => armCleanup('selected')}
										>{armedScope === 'selected'
											? 'Selected armed'
											: 'Delete selected originals'}</button
									>
									<button
										type="button"
										class="control control--danger"
										class:armed={armedScope === 'global'}
										disabled={cleanupDisabled('global')}
										onclick={() => armCleanup('global')}
										>{armedScope === 'global'
											? 'Global armed'
											: 'Delete all waiting originals'}</button
									>
									<button
										type="button"
										class="control control--primary"
										class:armed={armedReview === 'already-removed'}
										disabled={reviewFolders.length === 0 || reviewPending}
										onclick={() => armReview('already-removed')}
										>{armedReview === 'already-removed'
											? 'Ready to mark handled'
											: 'Confirm originals are already gone'}</button
									>
								</div>
							{:else}
								<div class="cleanup-standby" aria-label="Cleanup standby state">
									<span>No action needed</span>
									<strong
										>{cleanupReviewCount > 0
											? `${cleanupReviewCount.toLocaleString('en-US')} completed folders need review.`
											: `${folders.length.toLocaleString('en-US')} completed folders are handled and shown only as history.`}</strong
									>
									<small
										>{cleanupReviewCount > 0
											? 'Check the new files, then mark the originals as already removed.'
											: 'When originals are waiting, scoped cleanup controls return here.'}</small
									>
								</div>
							{/if}

							{#if armedScope}
								{@const scope = armedScope}
								<div class="confirm-panel" role="alertdialog" aria-label="Confirm original removal">
									<div>
										<strong
											>{scope === 'selected'
												? 'Review selected originals before removal'
												: 'Review entire cleanup folder before removal'}</strong
										>
										<span
											>{scope === 'selected'
												? selectedRemovalSummary()
												: globalRemovalSummary}</span
										>
										<small>
											This deletes originals from the cleanup folder. It does not remove the new
											processed files.
										</small>
									</div>
									<div class="confirm-panel__actions">
										<button
											type="button"
											class="control control--danger armed"
											disabled={cleanupPending}
											onclick={() => confirmCleanup(scope)}
											>{cleanupPending ? 'Working' : 'Delete originals'}</button
										>
										<button
											type="button"
											class="control"
											disabled={cleanupPending}
											onclick={() => (armedScope = null)}>Cancel</button
										>
									</div>
								</div>
							{/if}

							{#if armedReview === 'already-removed'}
								<div
									class="confirm-panel confirm-panel--review"
									role="alertdialog"
									aria-label="Confirm originals already removed"
								>
									<div>
										<strong>Mark originals as already removed</strong>
										<span
											>{reviewSummary}. This does not delete files; it only clears the review state.</span
										>
									</div>
									<div class="confirm-panel__actions">
										<button
											type="button"
											class="control control--primary armed"
											disabled={reviewPending}
											onclick={confirmAlreadyRemoved}
											>{reviewPending ? 'Working' : 'Mark handled'}</button
										>
										<button
											type="button"
											class="control"
											disabled={reviewPending}
											onclick={() => (armedReview = null)}>Cancel</button
										>
									</div>
								</div>
							{/if}

							{#if actionMessage}
								<p class="action-message">{actionMessage}</p>
							{/if}
							{#if actionError}
								<p class="action-error">{actionError}</p>
							{/if}
							{#if lastCleanupResult}
								<div class="result-strip">
									<span
										>{lastCleanupResult.removed_count.toLocaleString('en-US')} files removed</span
									>
									<span>{formatBytes(lastCleanupResult.removed_size_bytes)}</span>
									<span
										>{lastCleanupResult.removed_prefix_count.toLocaleString('en-US')} folders affected</span
									>
								</div>
							{/if}
						</div>
					</WorkstationPanel>
				{/if}

				<WorkstationPanel
					eyebrow="Seasons"
					title="Finished season list"
					meta={`${filteredFolders.length.toLocaleString('en-US')} visible`}
				>
					<div class="table-wrap">
						<table class="completed-table">
							<colgroup>
								<col class="completed-table__select" />
								<col class="completed-table__season" />
								<col class="completed-table__originals" />
								<col class="completed-table__saved" />
								<col class="completed-table__latest" />
							</colgroup>
							<thead>
								<tr>
									<th aria-label="Choose"></th>
									<th>Season and state</th>
									<th>Originals</th>
									<th>Saved</th>
									<th>Latest</th>
								</tr>
							</thead>
							<tbody>
								{#each filteredFolders as folder (folder.prefix)}
									{@const state = cleanupState(folder, archive)}
									<tr
										class:selected={selected(folder) || selectedForReview(folder)}
										class:blocked={state === 'blocked'}
									>
										<td class="select-cell">
											{#if folderCanBeSelected(folder, archive)}
												<input
													type="checkbox"
													checked={selected(folder)}
													aria-label={`Select ${folder.title} for cleanup`}
													onchange={(event) => handleRowSelection(folder, event)}
												/>
											{:else if folderCanBeMarkedHandled(folder, archive)}
												<input
													type="checkbox"
													checked={selectedForReview(folder)}
													aria-label={`Select ${folder.title} to mark handled`}
													onchange={(event) => handleReviewSelection(folder, event)}
												/>
											{/if}
										</td>
										<td class="season-state-cell">
											<a class="folder-link" href={resolve(folderRoutePath(folder.prefix))}>
												<strong>{folder.title}</strong>
												<span>{folder.prefix}</span>
											</a>
											<div class="row-state">
												<StateBadge compact tone={cleanupTone(state)} label={cleanupLabel(state)} />
												<span class="state-detail">{cleanupDetail(folder, archive)}</span>
											</div>
										</td>
										<td class="originals-cell" data-label="Originals">
											<strong>{folder.archived_backup_count.toLocaleString('en-US')} waiting</strong
											>
											<span
												>{formatBytes(folder.archived_backup_size_bytes)} reclaim · {folder.promoted_item_count.toLocaleString(
													'en-US'
												)} promoted</span
											>
										</td>
										<td class="saved-cell" data-label="Saved"
											>{formatBytes(folder.total_bytes_saved)}</td
										>
										<td class="latest-cell" data-label="Latest"
											>{formatTimestamp(folder.latest_promoted_at)}</td
										>
									</tr>
								{:else}
									<tr>
										<td colspan="5">No finished seasons match this search.</td>
									</tr>
								{/each}
							</tbody>
						</table>
					</div>
				</WorkstationPanel>
			{:else}
				<WorkstationPanel
					eyebrow="History"
					title="Handled history"
					meta={`${visibleHistory.length.toLocaleString('en-US')} visible`}
				>
					<div class="history-workspace">
						<label class="history-search">
							<span>Search history</span>
							<input
								type="search"
								value={historyQuery}
								placeholder="Event, path, folder, or detail"
								oninput={handleHistorySearchInput}
							/>
						</label>
						<div class="history-list history-list--wide">
							{#each visibleHistory as event (`${event.source}:${event.id}:${event.created_at}`)}
								<div class="history-row">
									<StateBadge
										compact
										tone={eventTone(event.tone)}
										label={completedHistoryLabel(event)}
									/>
									<div>
										<strong>{event.title}</strong>
										<span>{event.prefix}</span>
									</div>
									<p>{completedHistoryDetail(event.detail, event)}</p>
									<time>{formatTimestamp(event.created_at)}</time>
								</div>
							{:else}
								<div class="empty-note">No history events match the active search.</div>
							{/each}
						</div>
					</div>
				</WorkstationPanel>
			{/if}
		</section>

		<aside class="completed__rail" aria-label="Originals and history">
			<WorkstationPanel eyebrow="Originals" title="Backup area">
				<dl class="kv">
					<dt>Folder</dt>
					<dd>{archive.archive_root || 'not configured'}</dd>
					<dt>Files</dt>
					<dd>{archive.file_count.toLocaleString('en-US')}</dd>
					<dt>Size</dt>
					<dd>{formatBytes(archive.total_size_bytes)}</dd>
					<dt>State</dt>
					<dd>{archive.has_cleanup ? 'originals waiting' : 'nothing waiting'}</dd>
				</dl>
			</WorkstationPanel>

			<WorkstationPanel eyebrow="Summary" title="Originals by state">
				<div class="scope-list">
					<div class="scope-row scope-row--ready">
						<span>Waiting originals</span>
						<strong>{counts.ready.toLocaleString('en-US')} folders</strong>
						<small>{formatBytes(totalArchivedBackupSize(cleanupReadyFolders))} waiting</small>
					</div>
					<div class="scope-row" class:scope-row--fail={counts.blocked > 0}>
						<span>Check settings</span>
						<strong>{counts.blocked.toLocaleString('en-US')} folders</strong>
						<small>Mediaforce cannot check these originals</small>
					</div>
					<div class="scope-row" class:scope-row--wait={counts.unknown > 0}>
						<span>Already removed</span>
						<strong>{counts.unknown.toLocaleString('en-US')} folders</strong>
						<small>originals already gone; confirm after review</small>
					</div>
					<div class="scope-row">
						<span>Handled</span>
						<strong>{counts.cleaned.toLocaleString('en-US')} folders</strong>
						<small>nothing waiting to remove</small>
					</div>
				</div>
			</WorkstationPanel>

			{#if cleanupNeedsAction || mode === 'history'}
				<WorkstationPanel
					eyebrow="History"
					title="Recent changes"
					meta={`${historyRows.length.toLocaleString('en-US')} events`}
				>
					<div class="history-list">
						{#each historyRows.slice(0, 8) as event (`rail:${event.source}:${event.id}:${event.created_at}`)}
							<div class="history-row history-row--rail">
								<StateBadge
									compact
									tone={eventTone(event.tone)}
									label={completedHistoryLabel(event)}
								/>
								<div>
									<strong>{event.title}</strong>
									<span>{event.prefix}</span>
								</div>
								<time>{formatTimestamp(event.created_at)}</time>
							</div>
						{:else}
							<div class="empty-note">No completed-history events are available yet.</div>
						{/each}
					</div>
				</WorkstationPanel>
			{:else}
				<WorkstationPanel eyebrow="History" title="Audit trail">
					<div class="audit-note">
						<strong>Handled history is available in the History tab.</strong>
						<span>No cleanup action is waiting.</span>
					</div>
				</WorkstationPanel>
			{/if}
		</aside>
	</main>
</OperatorShell>

<style>
	.completed {
		display: grid;
		grid-template-columns: minmax(0, 1fr) var(--mf-workstation-rail-width);
		min-height: calc(100vh - 178px);
	}

	.completed__main {
		align-content: start;
		display: grid;
		gap: var(--mf-space-5);
		min-width: 0;
		padding: var(--mf-space-6);
	}

	.completed__rail {
		align-content: start;
		background: var(--mf-bg-shell);
		border-left: var(--mf-border);
		display: flex;
		flex-direction: column;
		gap: var(--mf-space-5);
		min-width: 0;
		padding: var(--mf-space-5);
	}

	.completed-header {
		align-items: end;
		border-bottom: var(--mf-border);
		display: grid;
		gap: var(--mf-space-6);
		grid-template-columns: minmax(0, 1fr) auto;
		padding-bottom: var(--mf-space-5);
	}

	.completed-header h1 {
		margin-top: var(--mf-space-3);
	}

	.completed-header p {
		margin-top: var(--mf-space-3);
		max-width: 78ch;
	}

	.completed-header__facts {
		display: flex;
		flex-wrap: wrap;
		gap: var(--mf-space-5);
	}

	.completed-header__facts div {
		display: grid;
		gap: var(--mf-space-2);
		min-width: 82px;
	}

	.completed-header__facts span,
	.completed-filter__summary span,
	.completed-filter__search span,
	.completed-filter__group-head > span,
	.completed-filter__row small,
	.history-search span,
	.scope-row span,
	.kv dt {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-semibold);
		letter-spacing: 0.08em;
		text-transform: uppercase;
	}

	.completed-header__facts strong,
	.completed-filter__summary strong,
	.kv dd {
		font-family: var(--mf-font-mono), monospace;
		font-size: var(--mf-text-sm);
		font-weight: var(--mf-weight-medium);
	}

	.modebar {
		background: var(--mf-bg-panel);
		border: var(--mf-border);
		display: inline-grid;
		grid-template-columns: repeat(2, minmax(110px, 1fr));
		justify-self: start;
	}

	.modebar button {
		border-right: var(--mf-border);
		color: var(--mf-fg-secondary);
		font-size: var(--mf-text-xs);
		font-weight: var(--mf-weight-semibold);
		min-height: var(--mf-control-md);
		padding: 0 var(--mf-space-5);
		text-transform: uppercase;
	}

	.modebar button:last-child {
		border-right: 0;
	}

	.modebar button.active {
		background: var(--mf-active-bg);
		color: var(--mf-active-fg-bright);
	}

	.completed-filter,
	.cleanup-status,
	.cleanup-command,
	.scope-list,
	.history-workspace,
	.history-list,
	.audit-note {
		display: grid;
		gap: var(--mf-space-4);
		padding: var(--mf-space-5);
	}

	.completed-filter {
		align-items: start;
		grid-template-columns:
			minmax(170px, 0.7fr) minmax(240px, 1fr) minmax(220px, 0.85fr)
			minmax(220px, 0.85fr);
	}

	.completed-filter__summary,
	.completed-filter__search,
	.completed-filter__group,
	.cleanup-status > div,
	.cleanup-command__state > div {
		min-width: 0;
	}

	.completed-filter__summary {
		border-left: 2px solid var(--mf-active-fg);
		display: grid;
		gap: var(--mf-space-2);
		padding: var(--mf-space-3) var(--mf-space-4);
	}

	.completed-filter__summary small,
	.cleanup-status span,
	.cleanup-command__state span,
	.scope-row small,
	.folder-link span,
	.state-detail,
	.history-row span,
	.history-row p,
	.audit-note span {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-xs);
	}

	.completed-filter__search,
	.completed-filter__group,
	.history-search {
		display: grid;
		gap: var(--mf-space-2);
	}

	.completed-filter__search input,
	.history-search input {
		background: var(--mf-bg-input);
		border: var(--mf-border);
		border-radius: var(--mf-radius-1);
		color: var(--mf-fg-primary);
		font: inherit;
		min-height: var(--mf-control-lg);
		padding: 0 var(--mf-space-4);
		width: 100%;
	}

	.completed-filter__group-head {
		align-items: center;
		display: flex;
		gap: var(--mf-space-3);
		justify-content: space-between;
	}

	.completed-filter__actions {
		display: grid;
		gap: var(--mf-space-2);
		grid-template-columns: 1fr 1fr;
	}

	.completed-filter__actions button {
		background: var(--mf-bg-panel-2);
		border: var(--mf-border);
		border-radius: var(--mf-radius-1);
		color: var(--mf-fg-secondary);
		font-size: var(--mf-text-xs);
		font-weight: var(--mf-weight-semibold);
		min-height: var(--mf-control-sm);
		padding: 0 var(--mf-space-3);
	}

	.completed-filter__row {
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

	.completed-filter__row.excluded {
		opacity: 0.52;
	}

	.completed-filter__row input,
	td input {
		accent-color: var(--mf-active-solid);
		height: 16px;
		width: 16px;
	}

	.completed-filter__row > span {
		display: grid;
		gap: var(--mf-space-1);
		min-width: 0;
	}

	.completed-filter__row small {
		letter-spacing: 0;
		text-transform: none;
	}

	.completed-filter__row em {
		color: var(--mf-fg-tertiary);
		font-family: var(--mf-font-mono), monospace;
		font-size: var(--mf-text-2xs);
		font-style: normal;
	}

	.cleanup-command__state {
		align-items: center;
		display: grid;
		gap: var(--mf-space-4);
		grid-template-columns: auto minmax(0, 1fr);
	}

	.cleanup-status {
		align-items: center;
		background: var(--mf-bg-strip);
		border-left: 2px solid var(--mf-line-strong);
		grid-template-columns: auto minmax(0, 1fr);
		min-height: var(--mf-row-comfy);
	}

	.cleanup-status--ready {
		background: var(--mf-ready-bg);
		border-left-color: var(--mf-ready-fg);
	}

	.cleanup-status--wait {
		background: var(--mf-wait-bg);
		border-left-color: var(--mf-wait-fg);
	}

	.cleanup-status > div {
		display: grid;
		gap: var(--mf-space-1);
	}

	.cleanup-status strong,
	.cleanup-command__state strong,
	.scope-row strong,
	.history-row strong,
	.audit-note strong {
		font-size: var(--mf-text-sm);
		font-weight: var(--mf-weight-semibold);
		overflow-wrap: anywhere;
	}

	.audit-note {
		background: var(--mf-bg-strip);
		border-left: 2px solid var(--mf-line-strong);
		gap: var(--mf-space-1);
	}

	.cleanup-command__actions,
	.confirm-panel__actions {
		display: flex;
		flex-wrap: wrap;
		gap: var(--mf-space-3);
	}

	.cleanup-standby {
		background: var(--mf-bg-panel-2);
		border: var(--mf-border-muted);
		border-left: 2px solid var(--mf-line-strong);
		display: grid;
		gap: var(--mf-space-2);
		padding: var(--mf-space-4);
	}

	.cleanup-standby span {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-semibold);
		letter-spacing: 0.08em;
		text-transform: uppercase;
	}

	.cleanup-standby strong {
		font-size: var(--mf-text-sm);
		font-weight: var(--mf-weight-semibold);
	}

	.cleanup-standby small {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-xs);
	}

	.confirm-panel {
		align-items: center;
		background: var(--mf-fail-bg);
		border: 1px solid var(--mf-fail-line);
		border-left: 3px solid var(--mf-fail-fg);
		display: grid;
		gap: var(--mf-space-4);
		grid-template-columns: minmax(0, 1fr) auto;
		padding: var(--mf-space-4);
	}

	.confirm-panel > div:first-child {
		display: grid;
		gap: var(--mf-space-1);
		min-width: 0;
	}

	.confirm-panel span {
		color: var(--mf-fg-secondary);
		font-family: var(--mf-font-mono), monospace;
		font-size: var(--mf-text-xs);
		overflow-wrap: anywhere;
	}

	.confirm-panel small {
		color: var(--mf-fail-fg);
		font-size: var(--mf-text-xs);
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

	.control--danger {
		border-color: var(--mf-fail-line);
		color: var(--mf-fail-fg);
	}

	.control--danger.armed {
		background: var(--mf-fail-bg-strong);
		border-color: var(--mf-fail-fg);
		color: var(--mf-fail-fg-bright);
	}

	.action-message,
	.action-error {
		border-left: 2px solid var(--mf-ready-fg);
		color: var(--mf-fg-secondary);
		font-size: var(--mf-text-xs);
		padding-left: var(--mf-space-4);
	}

	.action-error,
	.empty-note--error {
		border-left-color: var(--mf-fail-fg);
		color: var(--mf-fail-fg);
	}

	.result-strip {
		background: var(--mf-ready-bg);
		border: 1px solid var(--mf-ready-line);
		color: var(--mf-ready-fg);
		display: flex;
		flex-wrap: wrap;
		font-family: var(--mf-font-mono), monospace;
		font-size: var(--mf-text-xs);
		gap: var(--mf-space-5);
		padding: var(--mf-space-3) var(--mf-space-4);
	}

	.table-wrap {
		overflow: hidden;
	}

	.completed-table {
		border-collapse: collapse;
		min-width: 0;
		table-layout: fixed;
		width: 100%;
	}

	.completed-table__select {
		width: 42px;
	}

	.completed-table__season {
		width: 48%;
	}

	.completed-table__originals {
		width: 18%;
	}

	.completed-table__saved {
		width: 13%;
	}

	.completed-table__latest {
		width: 21%;
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

	tr.selected {
		background: var(--mf-active-bg);
	}

	tr.blocked {
		background: var(--mf-fail-bg);
	}

	.season-state-cell {
		min-width: 0;
	}

	.originals-cell,
	.saved-cell,
	.latest-cell {
		font-family: var(--mf-font-mono), monospace;
	}

	.originals-cell {
		font-size: var(--mf-text-xs);
	}

	.originals-cell span {
		display: block;
		color: var(--mf-fg-tertiary);
		font-family: var(--mf-font-sans), sans-serif;
	}

	.row-state {
		align-items: center;
		display: flex;
		gap: var(--mf-space-3);
		min-width: 0;
		margin-top: var(--mf-space-2);
	}

	.row-state .state-detail {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.completed-table th {
		height: 34px;
	}

	.completed-table td {
		height: auto;
		padding-block: 9px;
		vertical-align: top;
	}

	.completed-table .select-cell {
		text-align: center;
	}

	.completed-table .saved-cell,
	.completed-table .latest-cell {
		white-space: nowrap;
	}

	.folder-link {
		display: grid;
		gap: var(--mf-space-1);
		min-width: 0;
	}

	.folder-link strong {
		font-size: var(--mf-text-sm);
		font-weight: var(--mf-weight-semibold);
		overflow-wrap: anywhere;
	}

	.folder-link span {
		font-family: var(--mf-font-mono), monospace;
		font-size: var(--mf-text-2xs);
		overflow-wrap: anywhere;
	}

	.kv {
		display: grid;
		grid-template-columns: minmax(62px, auto) minmax(0, 1fr);
		padding: var(--mf-space-5);
		row-gap: var(--mf-space-4);
	}

	.kv dd {
		overflow-wrap: anywhere;
	}

	.scope-row {
		border-left: 2px solid var(--mf-line-strong);
		display: grid;
		gap: var(--mf-space-2);
		padding: var(--mf-space-4);
	}

	.scope-row--ready {
		background: var(--mf-ready-bg);
		border-left-color: var(--mf-ready-fg);
	}

	.scope-row--fail {
		background: var(--mf-fail-bg);
		border-left-color: var(--mf-fail-fg);
	}

	.scope-row--wait {
		background: var(--mf-wait-bg);
		border-left-color: var(--mf-wait-fg);
	}

	.history-list {
		padding: var(--mf-space-4);
	}

	.history-list--wide {
		max-height: none;
		padding: 0;
	}

	.history-list--summary {
		padding: var(--mf-space-5);
	}

	.history-row {
		background: var(--mf-bg-panel-2);
		border: var(--mf-border-muted);
		display: grid;
		gap: var(--mf-space-3);
		grid-template-columns: minmax(120px, auto) minmax(0, 1fr) auto;
		min-height: var(--mf-row-comfy);
		padding: var(--mf-space-4);
	}

	.history-list--wide .history-row {
		grid-template-columns: minmax(150px, auto) minmax(0, 1fr) minmax(180px, 0.7fr) auto;
	}

	.history-row--rail {
		gap: var(--mf-space-2);
		grid-template-columns: 1fr;
		min-height: 0;
		padding: var(--mf-space-3);
	}

	.history-row--rail time {
		white-space: normal;
	}

	.history-row > div {
		display: grid;
		gap: var(--mf-space-1);
		min-width: 0;
	}

	.history-row p {
		margin: 0;
		min-width: 0;
		overflow-wrap: anywhere;
	}

	.history-row time {
		color: var(--mf-fg-tertiary);
		font-family: var(--mf-font-mono), monospace;
		font-size: var(--mf-text-2xs);
		white-space: nowrap;
	}

	.empty-note {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-sm);
		padding: var(--mf-space-5);
	}

	@media (max-width: 1080px) {
		.completed {
			grid-template-columns: 1fr;
		}

		.completed__rail {
			border-left: 0;
			border-top: var(--mf-border);
		}

		.completed-header,
		.confirm-panel {
			grid-template-columns: 1fr;
		}

		.completed-filter {
			grid-template-columns: 1fr;
		}
	}

	@media (max-width: 680px) {
		.completed__main,
		.completed__rail {
			padding: var(--mf-space-4);
		}

		.cleanup-command__state,
		.cleanup-status,
		.history-row,
		.history-list--wide .history-row {
			align-items: start;
			grid-template-columns: 1fr;
		}

		.modebar {
			justify-self: stretch;
		}

		.table-wrap {
			overflow: visible;
		}

		.completed-table {
			min-width: 0;
		}

		.completed-table thead,
		.completed-table colgroup {
			display: none;
		}

		.completed-table tbody,
		.completed-table tr,
		.completed-table td {
			display: block;
			width: 100%;
		}

		.completed-table tr {
			border-bottom: var(--mf-border-muted);
			display: grid;
			gap: var(--mf-space-3);
			grid-template-columns: auto minmax(0, 1fr);
			padding: var(--mf-space-4);
		}

		.completed-table td {
			border-bottom: 0;
			height: auto;
			min-width: 0;
			padding: 0;
		}

		.completed-table .select-cell {
			grid-row: 1 / span 4;
			padding-top: var(--mf-space-1);
			width: 32px;
		}

		.completed-table .season-state-cell,
		.completed-table .originals-cell,
		.completed-table .saved-cell,
		.completed-table .latest-cell {
			grid-column: 2;
		}

		.completed-table td[data-label] {
			align-items: baseline;
			display: grid;
			gap: var(--mf-space-3);
			grid-template-columns: 68px minmax(0, 1fr);
		}

		.completed-table td[data-label]::before {
			color: var(--mf-fg-tertiary);
			content: attr(data-label);
			font-family: var(--mf-font-sans), sans-serif;
			font-size: var(--mf-text-2xs);
			font-weight: var(--mf-weight-semibold);
			letter-spacing: 0.08em;
			text-transform: uppercase;
		}

		.completed-table .originals-cell > * {
			grid-column: 2;
		}
	}

	/* Human finished-seasons surface */
	.completed {
		display: grid;
		gap: 18px;
		grid-template-columns: minmax(0, 1fr);
		margin: 0 auto;
		max-width: 900px;
		min-height: 0;
		padding: 34px 24px 64px;
	}

	.completed__main {
		gap: 16px;
		padding: 0;
	}

	.completed__rail {
		display: none;
	}

	.completed-header {
		align-items: flex-end;
		border: 0;
		display: flex;
		gap: 20px;
		justify-content: space-between;
		padding: 0 0 8px;
	}

	.completed-header > div:first-child {
		display: grid;
		gap: 5px;
	}

	.completed-header h1 {
		font-size: clamp(22px, 2.5vw, 28px);
		font-weight: 600;
		letter-spacing: -0.02em;
	}

	.completed-header p {
		color: var(--mf-fg-secondary);
		font-size: 14px;
	}

	:global(.completed .mf-eyebrow) {
		background: var(--mf-ready-bg);
		border-radius: 999px;
		color: var(--mf-ready-fg);
		font-family: var(--mf-font-sans);
		font-size: 11px;
		font-weight: 700;
		letter-spacing: 0.02em;
		padding: 6px 9px;
		text-transform: none;
		width: fit-content;
	}

	.completed-header__facts {
		background: var(--mf-bg-panel);
		border: 1px solid var(--mf-line);
		border-radius: var(--mf-radius-3);
		display: grid;
		grid-template-columns: repeat(4, minmax(74px, 1fr));
		overflow: hidden;
	}

	.completed-header__facts div {
		border-right: 1px solid var(--mf-line-muted);
		display: grid;
		gap: 2px;
		padding: 9px 11px;
	}

	.completed-header__facts div:last-child {
		border-right: 0;
	}

	.completed-header__facts span {
		color: var(--mf-fg-tertiary);
		font-family: var(--mf-font-sans);
		font-size: 10px;
		letter-spacing: 0.02em;
		text-transform: none;
	}

	.completed-header__facts strong {
		color: var(--mf-fg-primary);
		font-family: var(--mf-font-sans);
		font-size: 15px;
	}

	.modebar {
		background: transparent;
		border-bottom: 1px solid var(--mf-line);
		border-radius: 0;
		gap: 8px;
		justify-self: start;
		padding: 0;
	}

	.modebar button {
		border: 0;
		border-bottom: 2px solid transparent;
		border-radius: 0;
		color: var(--mf-fg-secondary);
		font-family: var(--mf-font-sans);
		font-size: 13px;
		font-weight: 600;
		min-height: 34px;
		padding: 0 14px;
	}

	.modebar button.active {
		background: transparent;
		border-bottom-color: var(--mf-active-fg);
		box-shadow: none;
		color: var(--mf-active-fg);
	}

	.cleanup-status,
	.cleanup-command__state,
	.cleanup-standby,
	.scope-row,
	.audit-note,
	.history-row,
	.empty-note {
		background: var(--mf-bg-panel-2);
		border: 1px solid var(--mf-line-muted);
		border-radius: var(--mf-radius-2);
		color: var(--mf-fg-primary);
	}

	.cleanup-status {
		border-left: 0;
		gap: 12px;
		margin: 12px;
		padding: 12px;
	}

	.cleanup-status strong,
	.cleanup-command strong,
	.scope-row strong,
	.audit-note strong,
	.history-row strong {
		color: var(--mf-fg-primary);
		font-family: var(--mf-font-sans);
	}

	.cleanup-status span,
	.cleanup-command span,
	.cleanup-command small,
	.scope-row span,
	.scope-row small,
	.audit-note span,
	.history-row span,
	.history-row p,
	.history-row time {
		color: var(--mf-fg-secondary);
		font-family: var(--mf-font-sans);
	}

	.history-list--summary .history-row > div span,
	.history-row--rail > div span {
		display: none;
	}

	.completed-filter {
		display: grid;
		gap: 12px;
		grid-template-columns: minmax(190px, 0.7fr) minmax(260px, 1.3fr);
		padding: 12px;
	}

	.completed-filter__group {
		display: none;
	}

	.completed-filter__summary,
	.completed-filter__search {
		background: var(--mf-bg-panel-2);
		border: 1px solid var(--mf-line-muted);
		border-radius: var(--mf-radius-2);
		padding: 11px 12px;
	}

	.completed-filter__summary span,
	.completed-filter__search span {
		color: var(--mf-fg-tertiary);
		font-family: var(--mf-font-sans);
		font-size: 11px;
	}

	.completed-filter__summary strong {
		color: var(--mf-fg-primary);
		font-family: var(--mf-font-sans);
	}

	.completed-filter input,
	.history-search input {
		background: var(--mf-bg-input);
		border: 1px solid var(--mf-line-strong);
		border-radius: var(--mf-radius-2);
		color: var(--mf-fg-primary);
		font-family: var(--mf-font-sans);
		min-height: 36px;
	}

	.cleanup-command,
	.history-workspace,
	.kv,
	.scope-list,
	.history-list,
	.audit-note {
		padding: 12px;
	}

	.cleanup-command__state {
		border-left: 0;
		padding: 12px;
	}

	.cleanup-command__actions {
		background: transparent;
		border: 0;
		gap: 7px;
		padding: 0;
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

	.control--primary {
		background: var(--mf-active-bg);
		border-color: var(--mf-active-line);
		color: var(--mf-active-fg);
	}

	.control--danger {
		background: var(--mf-bg-panel);
		border-color: var(--mf-fail-line);
		color: var(--mf-fail-fg);
	}

	.confirm-panel {
		background: var(--mf-bg-panel);
		border: 1px solid var(--mf-fail-line);
		border-radius: var(--mf-radius-3);
		box-shadow: var(--mf-shadow-modal);
		color: var(--mf-fg-primary);
		margin-top: 10px;
		padding: 16px;
	}

	.confirm-panel .control--danger.armed {
		background: var(--mf-fail-solid);
		border-color: var(--mf-fail-solid);
		color: var(--mf-fg-on-accent);
	}

	.confirm-panel--review {
		border-color: var(--mf-active-line);
	}

	.confirm-panel--review .control--primary.armed {
		background: var(--mf-active-solid);
		border-color: var(--mf-active-solid);
		color: var(--mf-fg-on-accent);
	}

	.table-wrap {
		background: var(--mf-bg-panel);
		border: 0;
		overflow-x: hidden;
	}

	.completed-table {
		background: var(--mf-bg-panel);
		color: var(--mf-fg-primary);
	}

	th {
		background: var(--mf-bg-panel-2);
		border-bottom: 1px solid var(--mf-line);
		color: var(--mf-fg-tertiary);
		font-family: var(--mf-font-sans);
		font-size: 11px;
		letter-spacing: 0.03em;
		text-transform: none;
	}

	td {
		border-bottom: 1px solid var(--mf-line-muted);
		color: var(--mf-fg-secondary);
		font-family: var(--mf-font-sans);
		font-size: 13px;
	}

	.folder-link strong,
	.state-cell strong {
		color: var(--mf-fg-primary);
		font-family: var(--mf-font-sans);
	}

	.folder-link span,
	.state-detail {
		color: var(--mf-fg-tertiary);
		font-family: var(--mf-font-sans);
	}

	.kv {
		grid-template-columns: auto minmax(0, 1fr);
	}

	.kv dt,
	.kv dd {
		color: var(--mf-fg-secondary);
		font-family: var(--mf-font-sans);
	}

	.scope-row {
		border-left: 0;
		padding: 10px;
	}

	button:focus-visible,
	a:focus-visible,
	input:focus-visible {
		box-shadow: var(--mf-ring-focus);
		outline: none;
	}

	@media (max-width: 680px) {
		.completed {
			padding: 26px 12px 48px;
		}

		.completed-header {
			align-items: stretch;
			flex-direction: column;
		}

		.completed-header__facts {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}

		.completed-header__facts div:nth-child(2) {
			border-right: 0;
		}

		.completed-header__facts div:nth-child(-n + 2) {
			border-bottom: 1px solid var(--mf-line-muted);
		}

		.completed-filter {
			grid-template-columns: 1fr;
		}
	}
</style>
