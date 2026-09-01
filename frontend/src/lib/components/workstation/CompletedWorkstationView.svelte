<script lang="ts">
	import { resolve } from '$app/paths';
	import { postJson } from '$lib/api/client';
	import { tick } from 'svelte';
	import type {
		ArchiveCleanupSummary,
		CompletedCleanupResult,
		CompletedFolderRow,
		CompletedHistoryEvent,
		CompletedOriginalsResolvedResult,
		CompletedPayload
	} from '$lib/api/types';
	import { folderRoutePath } from '$lib/folder-display';
	import { formatFileSize } from '$lib/format';
	import OperatorShell from './OperatorShell.svelte';
	import StateBadge from './StateBadge.svelte';
	import WorkstationPanel from './WorkstationPanel.svelte';
	import {
		buildDeleteConfirmCopy,
		buildMarkHandledConfirmCopy,
		buildCompletedFooterSignals,
		buildCompletedHistoryRows,
		buildCompletedStatusTiles,
		cleanupDetail,
		cleanupActionBlockers,
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
		type CompletedCleanupScope,
		type CleanupState
	} from './completed-workstation';
	import type { ShellTone } from './shell-types';

	type WorkstationMode = 'completed' | 'history';
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
	let armedScope = $state<CompletedCleanupScope | null>(null);
	let armedReview = $state<ReviewScope | null>(null);
	let selectedCleanupTrigger = $state<HTMLButtonElement | null>(null);
	let globalCleanupTrigger = $state<HTMLButtonElement | null>(null);
	let reviewCleanupTrigger = $state<HTMLButtonElement | null>(null);
	let deleteConfirmPanel = $state<HTMLElement | null>(null);
	let reviewConfirmPanel = $state<HTMLElement | null>(null);
	let actionStatusMessage = $state<HTMLParagraphElement | null>(null);
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
	const cleanupDeleteRelevant = $derived(cleanupWorkAvailable || counts.blocked > 0);
	const cleanupReviewCount = $derived(counts.blocked + counts.unknown);
	const cleanupNeedsAction = $derived(cleanupWorkAvailable || cleanupReviewCount > 0);
	const cleanupStatusTone = $derived(
		cleanupReadyFolders.length > 0 ? 'ready' : cleanupReviewCount > 0 ? 'wait' : 'idle'
	);
	const historyRows = $derived([
		...localHistory.map((event) => ({ ...event, source: 'api' as const })),
		...(completed ? buildCompletedHistoryRows(completed) : [])
	]);
	const normalizedHistoryQuery = $derived(historyQuery.trim().toLowerCase());
	const visibleHistory = $derived(
		historyRows.filter((event) => {
			if (normalizedHistoryQuery.length === 0) return true;
			return completedHistorySearchText(event).includes(normalizedHistoryQuery);
		})
	);
	const selectedSummary = $derived(
		selectedFolders.length
			? `${countLabel(selectedFolders.length, 'folder')} · ${countLabel(selectedBackupCount, 'file')} · ${formatBytes(selectedBackupSize)}`
			: 'None selected'
	);
	const reviewSummary = $derived(
		reviewFolders.length
			? `${countLabel(reviewFolders.length, 'folder')} · ${countLabel(reviewItemCount, 'original backup')} already gone`
			: 'None selected'
	);
	const actionBlockers = $derived(
		cleanupActionBlockers({
			completedAvailable: Boolean(completed),
			archive,
			selectedFolderCount: selectedFolders.length,
			reviewFolderCount: reviewFolders.length,
			cleanupPending,
			reviewPending
		})
	);
	$effect(() => {
		if (armedScope && !cleanupPending && cleanupDisabled(armedScope)) {
			armedScope = null;
		}
		if (armedReview && !reviewPending && actionBlockers.review !== null) {
			armedReview = null;
		}
	});
	const deleteConfirmCopy = $derived.by(() =>
		armedScope
			? buildDeleteConfirmCopy(armedScope, {
					folderCount:
						armedScope === 'selected' ? selectedFolders.length : cleanupReadyFolders.length,
					backupCount: armedScope === 'selected' ? selectedBackupCount : archive.file_count,
					backupSizeBytes:
						armedScope === 'selected' ? selectedBackupSize : archive.total_size_bytes,
					archiveRoot: archive.archive_root
				})
			: null
	);
	const markHandledConfirmCopy = $derived(
		buildMarkHandledConfirmCopy({
			folderCount: reviewFolders.length,
			backupCount: reviewItemCount,
			archiveRoot: archive.archive_root
		})
	);

	function libraryKey(label: string): string {
		return label.trim().toLowerCase() || 'library';
	}

	function countLabel(count: number, singular: string, plural = `${singular}s`): string {
		return `${count.toLocaleString('en-US')} ${count === 1 ? singular : plural}`;
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

	function cleanupDisabled(scope: CompletedCleanupScope): boolean {
		return scope === 'selected' ? actionBlockers.selected !== null : actionBlockers.global !== null;
	}

	async function armCleanup(scope: CompletedCleanupScope) {
		if (cleanupDisabled(scope)) return;
		actionMessage = '';
		actionError = '';
		lastCleanupResult = null;
		const shouldArm = armedScope !== scope;
		armedScope = shouldArm ? scope : null;
		armedReview = null;
		await tick();
		(shouldArm
			? deleteConfirmPanel
			: scope === 'selected'
				? selectedCleanupTrigger
				: globalCleanupTrigger
		)?.focus();
	}

	async function armReview(scope: ReviewScope) {
		if (actionBlockers.review !== null) return;
		actionMessage = '';
		actionError = '';
		lastCleanupResult = null;
		armedScope = null;
		const shouldArm = armedReview !== scope;
		armedReview = shouldArm ? scope : null;
		await tick();
		(shouldArm ? reviewConfirmPanel : reviewCleanupTrigger)?.focus();
	}

	async function cancelCleanupConfirmation(scope: CompletedCleanupScope) {
		armedScope = null;
		await tick();
		(scope === 'selected' ? selectedCleanupTrigger : globalCleanupTrigger)?.focus();
	}

	async function cancelReviewConfirmation() {
		armedReview = null;
		await tick();
		reviewCleanupTrigger?.focus();
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
			actionMessage = result.message || 'Marked folders as handled. Nothing was deleted.';
			if (result.completed) {
				applyCompleted(result.completed);
			}
			localHistory = [
				{
					id: Date.now(),
					event_type: 'originals_removed_confirmed',
					label: 'Marked handled',
					tone: 'ready',
					prefix: `${foldersToResolve.length} selected folders`,
					title: 'Original backups already gone',
					subtitle: result.message,
					scope_label: 'review',
					created_at: new Date().toISOString(),
					detail: `${result.resolved_count.toLocaleString('en-US')} items marked handled · nothing deleted`,
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
			await tick();
			(armedReview ? reviewConfirmPanel : actionStatusMessage)?.focus();
		}
	}

	async function confirmCleanup(scope: CompletedCleanupScope) {
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
			actionMessage = result.message || 'Original backups deleted.';
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
					label: 'Original backups deleted',
					tone: 'ready',
					prefix:
						scope === 'selected' ? `${cleanupFolders.length} selected folders` : 'Cleanup folder',
					title:
						scope === 'selected'
							? 'Selected original backups deleted'
							: 'All original backups deleted',
					subtitle: result.message,
					scope_label: 'cleanup',
					created_at: new Date().toISOString(),
					detail: `${result.removed_count.toLocaleString('en-US')} original backups deleted · ${formatBytes(result.removed_size_bytes)}`,
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
			await tick();
			(armedScope ? deleteConfirmPanel : actionStatusMessage)?.focus();
		}
	}

	function formatTimestamp(value: string | null | undefined): string {
		if (!value) return '—';
		const date = new Date(value);
		if (Number.isNaN(date.getTime())) return 'Unknown time';
		return date.toLocaleString([], {
			month: 'short',
			day: '2-digit',
			hour: '2-digit',
			minute: '2-digit'
		});
	}

	function formatBytes(value: number | null | undefined): string {
		return formatFileSize(value);
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
		<section class="completed__main" aria-label="Finished media">
			<h1 class="sr-only">Finished media</h1>
			<div class="completed-metrics" aria-label="Finished media summary">
				<div>
					<span>Finished</span>
					<strong>{folders.length.toLocaleString('en-US')}</strong>
				</div>
				<div class:completed-metrics__attention={counts.ready > 0}>
					<span>Backups to delete</span>
					<strong>{counts.ready.toLocaleString('en-US')}</strong>
				</div>
				<div class:completed-metrics__attention={counts.blocked + counts.unknown > 0}>
					<span>Needs review</span>
					<strong>{(counts.blocked + counts.unknown).toLocaleString('en-US')}</strong>
				</div>
				<div>
					<span>Space saved</span>
					<strong>{formatBytes(totalSavedSize(folders))}</strong>
				</div>
			</div>
			<div class="modebar" role="tablist" aria-label="Finished media views">
				<button
					type="button"
					role="tab"
					aria-selected={mode === 'completed'}
					class:active={mode === 'completed'}
					onclick={() => (mode = 'completed')}>Backups</button
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
				<WorkstationPanel eyebrow="Status" title="Finished media is unavailable">
					<div class="empty-note empty-note--error">
						{actionError || 'Finished media data is loading or unavailable.'}
					</div>
				</WorkstationPanel>
			{:else if mode === 'completed'}
				<WorkstationPanel
					eyebrow="Register"
					title="Finished media"
					meta={`${filteredFolders.length.toLocaleString('en-US')} of ${folders.length.toLocaleString('en-US')} visible`}
				>
					<div class="completed-filter" aria-label="Finished backup filters">
						<label class="completed-filter__search">
							<span>Search</span>
							<input
								type="search"
								value={searchQuery}
								placeholder="Title, season, file, or status"
								oninput={handleSearchInput}
							/>
						</label>

						<details class="completed-filter__advanced">
							<summary>
								<span>Filters</span>
								<small
									>{countLabel(libraryOptions.length, 'library', 'libraries')} · {countLabel(
										stateOptions.length,
										'state'
									)}</small
								>
							</summary>
							<div class="completed-filter__advanced-body">
								<div class="completed-filter__group">
									<div class="completed-filter__group-head">
										<span>Libraries</span>
										<div class="completed-filter__actions" aria-label="Bulk library filters">
											<button type="button" onclick={() => setAllLibraries(true)}>All</button>
											<button type="button" onclick={() => setAllLibraries(false)}>None</button>
										</div>
									</div>
									{#each libraryOptions as option (option.key)}
										<label
											class="completed-filter__row"
											class:excluded={!libraryIncluded(option.key)}
										>
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
													original backups</small
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
													original backups</small
												>
											</span>
											<em>{formatBytes(option.size)}</em>
										</label>
									{/each}
								</div>
							</div>
						</details>

						<div class="selection-bar" aria-label="Finished media selection">
							<div class="selection-bar__summary">
								<span>Selected for deletion</span>
								<strong>{selectedSummary}</strong>
							</div>
							<div class="selection-bar__summary">
								<span>Selected for review</span>
								<strong>{reviewSummary}</strong>
							</div>
							<div class="selection-bar__actions">
								<button
									type="button"
									class="control"
									disabled={filteredReadyFolders.length === 0}
									onclick={selectVisibleReady}>Select visible backups</button
								>
								<button
									type="button"
									class="control"
									disabled={filteredReviewFolders.length === 0}
									onclick={selectVisibleReview}>Select visible already-gone folders</button
								>
								<button
									type="button"
									class="control"
									disabled={selectedFolders.length === 0 && reviewFolders.length === 0}
									onclick={clearSelection}>Clear selection</button
								>
							</div>
						</div>
					</div>

					{#if cleanupNeedsAction}
						<div class="cleanup-command">
							<div class="cleanup-command__state">
								<StateBadge
									tone={cleanupStatusTone}
									label={recommendedFolder
										? 'Backups ready to delete'
										: counts.blocked > 0
											? 'Check before deleting'
											: counts.unknown > 0
												? 'Needs review'
												: 'No action'}
								/>
								<div>
									<strong
										>{recommendedFolder
											? `${recommendedFolder.title} is ready for your decision`
											: counts.blocked > 0
												? 'Some original backups need a location check'
												: counts.unknown > 0
													? 'Some original backups are already gone'
													: 'Completed work is handled'}</strong
									>
									<span
										>{recommendedFolder
											? cleanupDetail(recommendedFolder, archive)
											: counts.blocked > 0
												? 'Mediaforce cannot verify these original backups until their Cleanup folder is available.'
												: counts.unknown > 0
													? 'After checking the finished files, mark these folders handled so they leave review.'
													: archive.archive_root
														? `No original backups are waiting in ${archive.archive_root}.`
														: 'Cleanup folder is not set, and no finished folder needs action.'}</span
									>
								</div>
							</div>

							<div class="cleanup-actions" aria-label="Original backup actions">
								{#if cleanupReviewCount > 0}
									<section class="action-command action-command--review">
										<div class="action-command__copy">
											<span>Review resolution</span>
											<strong>Record backups that are already gone</strong>
											<small>This updates the audit state. It does not delete files.</small>
										</div>
										<div class="action-command__control">
											<button
												type="button"
												class="control control--primary"
												class:armed={armedReview === 'already-removed'}
												disabled={actionBlockers.review !== null}
												aria-describedby={actionBlockers.review
													? 'review-cleanup-blocker'
													: undefined}
												aria-expanded={armedReview === 'already-removed'}
												aria-controls={armedReview === 'already-removed'
													? 'review-cleanup-confirm'
													: undefined}
												bind:this={reviewCleanupTrigger}
												onclick={() => armReview('already-removed')}
												>Mark backups already gone as handled</button
											>
											{#if actionBlockers.review}
												<span id="review-cleanup-blocker" class="action-blocker"
													>{actionBlockers.review}</span
												>
											{/if}
										</div>
									</section>
								{/if}

								{#if cleanupDeleteRelevant}
									<section class="action-command action-command--danger">
										<div class="action-command__copy">
											<span>Destructive cleanup</span>
											<strong>Delete original backups</strong>
											<small>Deletion is permanent and always requires confirmation.</small>
										</div>
										<div class="action-command__options">
											<div class="action-command__control">
												<button
													type="button"
													class="control control--danger"
													class:armed={armedScope === 'selected'}
													disabled={cleanupDisabled('selected')}
													aria-describedby={actionBlockers.selected
														? 'selected-cleanup-blocker'
														: undefined}
													aria-expanded={armedScope === 'selected'}
													aria-controls={armedScope === 'selected'
														? 'selected-cleanup-confirm'
														: undefined}
													bind:this={selectedCleanupTrigger}
													onclick={() => armCleanup('selected')}
													>Delete selected original backups</button
												>
												{#if actionBlockers.selected}
													<span id="selected-cleanup-blocker" class="action-blocker"
														>{actionBlockers.selected}</span
													>
												{/if}
											</div>
											<div class="action-command__control">
												<button
													type="button"
													class="control control--danger"
													class:armed={armedScope === 'global'}
													disabled={cleanupDisabled('global')}
													aria-describedby={actionBlockers.global
														? 'global-cleanup-blocker'
														: undefined}
													aria-expanded={armedScope === 'global'}
													aria-controls={armedScope === 'global'
														? 'global-cleanup-confirm'
														: undefined}
													bind:this={globalCleanupTrigger}
													onclick={() => armCleanup('global')}>Delete all original backups</button
												>
												{#if actionBlockers.global}
													<span id="global-cleanup-blocker" class="action-blocker"
														>{actionBlockers.global}</span
													>
												{/if}
											</div>
										</div>
									</section>
								{/if}
							</div>

							{#if armedScope && deleteConfirmCopy}
								{@const scope = armedScope}
								<div
									id={`${scope}-cleanup-confirm`}
									class="confirm-panel"
									role="alertdialog"
									aria-label="Confirm original backup deletion"
									tabindex="-1"
									bind:this={deleteConfirmPanel}
									onkeydown={(event) => {
										if (event.key === 'Escape' && !cleanupPending) {
											event.preventDefault();
											void cancelCleanupConfirmation(scope);
										}
									}}
								>
									<div>
										<strong>{deleteConfirmCopy.title}</strong>
										<span>{deleteConfirmCopy.scope}</span>
										<small>{deleteConfirmCopy.safety}</small>
									</div>
									<div class="confirm-panel__actions">
										<strong class="irreversible-warning">{deleteConfirmCopy.warning}</strong>
										<button
											type="button"
											class="control control--danger armed"
											disabled={cleanupPending}
											onclick={() => confirmCleanup(scope)}
											>{cleanupPending ? 'Deleting…' : deleteConfirmCopy.confirmLabel}</button
										>
										<button
											type="button"
											class="control"
											disabled={cleanupPending}
											onclick={() => cancelCleanupConfirmation(scope)}>Cancel</button
										>
									</div>
								</div>
							{/if}

							{#if armedReview === 'already-removed'}
								<div
									id="review-cleanup-confirm"
									class="confirm-panel confirm-panel--review"
									role="alertdialog"
									aria-label="Confirm already-gone original backups"
									tabindex="-1"
									bind:this={reviewConfirmPanel}
									onkeydown={(event) => {
										if (event.key === 'Escape' && !reviewPending) {
											event.preventDefault();
											void cancelReviewConfirmation();
										}
									}}
								>
									<div>
										<strong>{markHandledConfirmCopy.title}</strong>
										<span>{markHandledConfirmCopy.scope}</span>
										<small>{markHandledConfirmCopy.safety}</small>
									</div>
									<div class="confirm-panel__actions">
										<button
											type="button"
											class="control control--primary armed"
											disabled={reviewPending}
											onclick={confirmAlreadyRemoved}
											>{reviewPending
												? 'Marking handled…'
												: markHandledConfirmCopy.confirmLabel}</button
										>
										<button
											type="button"
											class="control"
											disabled={reviewPending}
											onclick={cancelReviewConfirmation}>Cancel</button
										>
									</div>
								</div>
							{/if}

							{#if actionMessage}
								<p
									class="action-message"
									role="status"
									aria-live="polite"
									tabindex="-1"
									bind:this={actionStatusMessage}
								>
									{actionMessage}
								</p>
							{/if}
							{#if actionError}
								<p class="action-error" role="alert" tabindex="-1" bind:this={actionStatusMessage}>
									{actionError}
								</p>
							{/if}
							{#if lastCleanupResult}
								<div class="result-strip">
									<span
										>{lastCleanupResult.removed_count.toLocaleString('en-US')} original backups deleted</span
									>
									<span>{formatBytes(lastCleanupResult.removed_size_bytes)}</span>
									<span
										>{lastCleanupResult.removed_prefix_count.toLocaleString('en-US')} folders affected</span
									>
								</div>
							{/if}
						</div>
					{/if}

					<div class="table-wrap">
						<table class="completed-table">
							<colgroup>
								<col class="completed-table__select" />
								<col class="completed-table__media" />
								<col class="completed-table__originals" />
								<col class="completed-table__saved" />
								<col class="completed-table__latest" />
							</colgroup>
							<thead>
								<tr>
									<th aria-label="Choose"></th>
									<th>Media and state</th>
									<th>Original backups</th>
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
													aria-label={`Select ${folder.title} to delete its original backups`}
													onchange={(event) => handleRowSelection(folder, event)}
												/>
											{:else if folderCanBeMarkedHandled(folder, archive)}
												<input
													type="checkbox"
													checked={selectedForReview(folder)}
													aria-label={`Select ${folder.title} to mark already-gone original backups handled`}
													onchange={(event) => handleReviewSelection(folder, event)}
												/>
											{/if}
										</td>
										<td class="media-state-cell">
											<a class="folder-link" href={resolve(folderRoutePath(folder.prefix))}>
												<strong>{folder.title}</strong>
												<span>{folder.prefix}</span>
											</a>
											<div class="row-state">
												<StateBadge compact tone={cleanupTone(state)} label={cleanupLabel(state)} />
												<span class="state-detail">{cleanupDetail(folder, archive)}</span>
											</div>
										</td>
										<td class="originals-cell" data-label="Original backups">
											<strong>{countLabel(folder.archived_backup_count, 'backup')}</strong>
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
										<td colspan="5">No finished media match this search.</td>
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
					meta={`${visibleHistory.length.toLocaleString('en-US')} of ${historyRows.length.toLocaleString('en-US')} events`}
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

		<aside class="completed__rail" aria-label="Original backups and history">
			<WorkstationPanel eyebrow="Backups" title="Cleanup folder">
				<dl class="kv">
					<dt>Folder</dt>
					<dd>{archive.archive_root || 'Not set'}</dd>
					<dt>Original backups</dt>
					<dd>{archive.file_count.toLocaleString('en-US')}</dd>
					<dt>Size</dt>
					<dd>{formatBytes(archive.total_size_bytes)}</dd>
					<dt>State</dt>
					<dd>{archive.has_cleanup ? 'Backups waiting' : 'Nothing waiting'}</dd>
				</dl>
			</WorkstationPanel>

			<WorkstationPanel eyebrow="Summary" title="Backups by state">
				<div class="scope-list">
					<div class="scope-row scope-row--ready">
						<span>Ready to delete</span>
						<strong>{counts.ready.toLocaleString('en-US')} folders</strong>
						<small
							>{formatBytes(totalArchivedBackupSize(cleanupReadyFolders))} in the Cleanup folder</small
						>
					</div>
					<div class="scope-row" class:scope-row--fail={counts.blocked > 0}>
						<span>Check before deleting</span>
						<strong>{counts.blocked.toLocaleString('en-US')} folders</strong>
						<small>Mediaforce cannot verify these original backups</small>
					</div>
					<div class="scope-row" class:scope-row--wait={counts.unknown > 0}>
						<span>Already gone</span>
						<strong>{counts.unknown.toLocaleString('en-US')} folders</strong>
						<small>nothing to delete; mark handled after checking</small>
					</div>
					<div class="scope-row">
						<span>Nothing to delete</span>
						<strong>{counts.cleaned.toLocaleString('en-US')} folders</strong>
						<small>no original backups are waiting</small>
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
						<span>No original-backup action is waiting.</span>
					</div>
				</WorkstationPanel>
			{/if}
		</aside>
	</main>
</OperatorShell>

<style>
	.sr-only {
		clip: rect(0, 0, 0, 0);
		clip-path: inset(50%);
		height: 1px;
		overflow: hidden;
		position: absolute;
		white-space: nowrap;
		width: 1px;
	}

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

	.completed-metrics span,
	.completed-filter__search span,
	.completed-filter__group-head > span,
	.completed-filter__row small,
	.selection-bar__summary span,
	.action-command__copy > span,
	.history-search span,
	.scope-row span,
	.kv dt {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-semibold);
		letter-spacing: 0.08em;
		text-transform: uppercase;
	}

	.completed-metrics strong,
	.selection-bar__summary strong,
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
		grid-template-columns: minmax(260px, 1fr) minmax(220px, 0.7fr);
	}

	.completed-filter__search,
	.completed-filter__group,
	.cleanup-command__state > div {
		min-width: 0;
	}

	.cleanup-command__state span,
	.action-command__copy small,
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

	.completed-filter__advanced {
		background: var(--mf-bg-panel-2);
		border: var(--mf-border-muted);
		border-radius: var(--mf-radius-1);
		min-width: 0;
	}

	.completed-filter__advanced summary {
		align-items: center;
		cursor: pointer;
		display: flex;
		gap: var(--mf-space-3);
		justify-content: space-between;
		min-height: var(--mf-control-lg);
		padding: 0 var(--mf-space-4);
	}

	.completed-filter__advanced summary span {
		font-size: var(--mf-text-xs);
		font-weight: var(--mf-weight-semibold);
	}

	.completed-filter__advanced summary small {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-xs);
	}

	.completed-filter__advanced-body {
		border-top: var(--mf-border-muted);
		display: grid;
		gap: var(--mf-space-4);
		grid-template-columns: repeat(2, minmax(0, 1fr));
		padding: var(--mf-space-4);
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

	.selection-bar {
		align-items: start;
		background: var(--mf-bg-strip);
		border: var(--mf-border-muted);
		border-radius: var(--mf-radius-1);
		display: grid;
		gap: var(--mf-space-4);
		grid-column: 1 / -1;
		grid-template-columns: repeat(2, minmax(0, 1fr)) auto;
		padding: var(--mf-space-4);
	}

	.selection-bar__summary {
		display: grid;
		gap: var(--mf-space-1);
		min-width: 0;
	}

	.selection-bar__summary strong {
		overflow-wrap: anywhere;
	}

	.selection-bar__actions {
		display: flex;
		flex-wrap: wrap;
		gap: var(--mf-space-2);
		justify-content: flex-end;
	}

	.cleanup-command__state {
		align-items: center;
		display: grid;
		gap: var(--mf-space-4);
		grid-template-columns: auto minmax(0, 1fr);
	}

	.cleanup-command__state > div {
		display: grid;
		gap: var(--mf-space-1);
	}

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

	.confirm-panel__actions {
		display: flex;
		flex-wrap: wrap;
		gap: var(--mf-space-3);
	}

	.cleanup-actions {
		display: grid;
		gap: var(--mf-space-3);
	}

	.action-command {
		align-items: start;
		background: var(--mf-bg-panel-2);
		border: var(--mf-border-muted);
		border-left: 3px solid var(--mf-line-strong);
		display: grid;
		gap: var(--mf-space-4);
		grid-template-columns: minmax(220px, 0.8fr) minmax(0, 1.2fr);
		padding: var(--mf-space-4);
	}

	.action-command--review {
		border-left-color: var(--mf-active-fg);
	}

	.action-command--danger {
		border-left-color: var(--mf-fail-fg);
	}

	.action-command__copy,
	.action-command__control {
		display: grid;
		gap: var(--mf-space-2);
		min-width: 0;
	}

	.action-command__copy strong {
		font-size: var(--mf-text-sm);
		font-weight: var(--mf-weight-semibold);
	}

	.action-command__options {
		display: grid;
		gap: var(--mf-space-3);
		grid-template-columns: repeat(2, minmax(0, 1fr));
		min-width: 0;
	}

	.action-command__control .control {
		justify-self: stretch;
		white-space: normal;
		width: 100%;
	}

	.action-blocker {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-xs);
		overflow-wrap: anywhere;
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

	.irreversible-warning {
		align-self: center;
		color: var(--mf-fail-fg);
		font-size: var(--mf-text-xs);
		font-weight: var(--mf-weight-bold);
	}

	.confirm-panel small {
		color: var(--mf-fg-secondary);
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

	.completed-table__media {
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

	.media-state-cell {
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
		align-items: flex-start;
		display: flex;
		gap: var(--mf-space-3);
		min-width: 0;
		margin-top: var(--mf-space-2);
	}

	.row-state .state-detail {
		line-height: 1.4;
		min-width: 0;
		overflow-wrap: anywhere;
		white-space: normal;
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
		.action-command,
		.action-command__options,
		.completed-filter__advanced-body,
		.selection-bar,
		.history-row,
		.history-list--wide .history-row {
			align-items: start;
			grid-template-columns: 1fr;
		}

		.confirm-panel__actions {
			align-items: stretch;
			flex-direction: column;
		}

		.confirm-panel__actions .control {
			justify-content: center;
			white-space: normal;
			width: 100%;
		}

		.selection-bar__actions {
			justify-content: stretch;
		}

		.selection-bar__actions .control,
		.action-command__control .control {
			justify-self: stretch;
			width: 100%;
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

		.completed-table .media-state-cell,
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

	/* Human finished-media surface */
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

	.completed-metrics {
		background: var(--mf-bg-panel);
		border: 1px solid var(--mf-line);
		border-radius: var(--mf-radius-3);
		display: grid;
		grid-template-columns: repeat(4, minmax(74px, 1fr));
		overflow: hidden;
	}

	.completed-metrics div {
		border-right: 1px solid var(--mf-line-muted);
		display: grid;
		gap: 2px;
		padding: 9px 11px;
	}

	.completed-metrics div:last-child {
		border-right: 0;
	}

	.completed-metrics span {
		color: var(--mf-fg-tertiary);
		font-family: var(--mf-font-sans);
		font-size: 10px;
		letter-spacing: 0.02em;
		text-transform: none;
	}

	.completed-metrics strong {
		color: var(--mf-fg-primary);
		font-family: var(--mf-font-sans);
		font-size: 15px;
	}

	.completed-metrics__attention {
		background: var(--mf-wait-bg);
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

	.cleanup-command__state,
	.scope-row,
	.audit-note,
	.history-row,
	.empty-note {
		background: var(--mf-bg-panel-2);
		border: 1px solid var(--mf-line-muted);
		border-radius: var(--mf-radius-2);
		color: var(--mf-fg-primary);
	}

	.cleanup-command strong,
	.scope-row strong,
	.audit-note strong,
	.history-row strong {
		color: var(--mf-fg-primary);
		font-family: var(--mf-font-sans);
	}

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

	.history-row--rail > div span {
		display: none;
	}

	.completed-filter {
		display: grid;
		gap: 12px;
		grid-template-columns: minmax(190px, 0.7fr) minmax(260px, 1.3fr);
		padding: 12px;
	}

	.completed-filter__search {
		background: var(--mf-bg-panel-2);
		border: 1px solid var(--mf-line-muted);
		border-radius: var(--mf-radius-2);
		padding: 11px 12px;
	}

	.completed-filter__search span {
		color: var(--mf-fg-tertiary);
		font-family: var(--mf-font-sans);
		font-size: 11px;
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

	.control:not(.control--danger):hover:not(:disabled) {
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

	.control--danger:hover:not(:disabled) {
		background: var(--mf-fail-bg);
		border-color: var(--mf-fail-fg);
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

	.folder-link strong {
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

		.completed-metrics {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}

		.completed-metrics div:nth-child(2) {
			border-right: 0;
		}

		.completed-metrics div:nth-child(-n + 2) {
			border-bottom: 1px solid var(--mf-line-muted);
		}

		.completed-filter {
			grid-template-columns: 1fr;
		}
	}
</style>
