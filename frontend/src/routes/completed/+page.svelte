<script lang="ts">
	import { onMount } from 'svelte';
	import '$lib/design/workstation-shell.css';
	import { resolve } from '$app/paths';
	import type { CompletedBackupsClearResponse, CompletedPagePayload } from '$lib/api/types';
	import { fetchJson, postJson } from '$lib/api/client';
	import Button from '$lib/components/Button.svelte';
	import Pill from '$lib/components/Pill.svelte';
	import { folderRoutePath } from '$lib/folder-display';
	import { formatGiB } from '$lib/format';
	import { toasts } from '$lib/stores/toasts';

	const EMPTY_COMPLETED_PAGE: CompletedPagePayload = {
		folders: [],
		completed_count: 0,
		folders_with_backups_count: 0,
		archive_cleanup: {
			archive_root: '',
			file_count: 0,
			total_size_bytes: 0,
			has_cleanup: false
		}
	};

	let completedPayload = $state<CompletedPagePayload>(EMPTY_COMPLETED_PAGE);
	let selectedPrefixes = $state<string[]>([]);
	let actionState = $state<'selected' | 'all' | null>(null);
	let loadState = $state<'loading' | 'ready' | 'error'>('loading');
	let loadError = $state<string | null>(null);

	onMount(() => {
		void loadCompletedPage();
	});

	const completed = $derived(completedPayload);
	const folders = $derived(completed.folders);
	const foldersWithBackups = $derived(folders.filter((folder) => folder.archived_backup_count > 0));
	const cleanupAvailable = $derived(completed.archive_cleanup.has_cleanup);
	const selectedFolders = $derived(
		cleanupAvailable
			? foldersWithBackups.filter((folder) => selectedPrefixes.includes(folder.prefix))
			: []
	);
	const selectedBackupCount = $derived(
		selectedFolders.reduce((total, folder) => total + folder.archived_backup_count, 0)
	);
	const selectedBackupSizeBytes = $derived(
		selectedFolders.reduce((total, folder) => total + folder.archived_backup_size_bytes, 0)
	);
	const allFoldersSelected = $derived(
		cleanupAvailable &&
			foldersWithBackups.length > 0 &&
			selectedPrefixes.length === foldersWithBackups.length
	);

	function toggleFolder(prefix: string): void {
		if (!cleanupAvailable) {
			return;
		}
		selectedPrefixes = nextSelection(prefix, selectedPrefixes);
	}

	function toggleAllFolders(): void {
		if (!cleanupAvailable) {
			return;
		}
		if (allFoldersSelected) {
			selectedPrefixes = [];
			return;
		}
		selectedPrefixes = foldersWithBackups.map((folder) => folder.prefix);
	}

	async function clearSelectedBackups(): Promise<void> {
		if (!cleanupAvailable || selectedFolders.length <= 0) {
			return;
		}
		const confirmed = window.confirm(
			`Delete ${selectedBackupCount} archived original${selectedBackupCount === 1 ? '' : 's'} (${formatGiB(selectedBackupSizeBytes, 1)}) from ${selectedFolders.length} completed folder${selectedFolders.length === 1 ? '' : 's'}?\n\nArchive root: ${completed.archive_cleanup.archive_root}\nThis cannot be undone.`
		);
		if (!confirmed) {
			return;
		}
		await clearBackups({
			prefixes: selectedFolders.map((folder) => folder.prefix),
			mode: 'selected'
		});
	}

	async function clearAllBackups(): Promise<void> {
		if (!cleanupAvailable) {
			return;
		}
		const confirmed = window.confirm(
			`Delete all ${completed.archive_cleanup.file_count} archived original${completed.archive_cleanup.file_count === 1 ? '' : 's'} (${formatGiB(completed.archive_cleanup.total_size_bytes, 1)})?\n\nArchive root: ${completed.archive_cleanup.archive_root}\nThis cannot be undone.`
		);
		if (!confirmed) {
			return;
		}
		await clearBackups({ prefixes: null, mode: 'all' });
	}

	async function clearBackups({
		prefixes,
		mode
	}: {
		prefixes: string[] | null;
		mode: 'selected' | 'all';
	}): Promise<void> {
		actionState = mode;
		try {
			const response = await postJson<CompletedBackupsClearResponse>(
				'/api/completed/backups/clear',
				prefixes ? { prefixes } : {}
			);
			completedPayload = response.completed;
			selectedPrefixes = [];
			toasts.success('Archived backups removed', response.message);
		} catch (error) {
			toasts.error(
				'Backup cleanup failed',
				error instanceof Error ? error.message : 'Unexpected completed-backup cleanup error'
			);
		} finally {
			actionState = null;
		}
	}

	async function loadCompletedPage(): Promise<void> {
		loadState = 'loading';
		try {
			completedPayload = await fetchJson<CompletedPagePayload>('/api/completed');
			loadError = null;
			loadState = 'ready';
		} catch (error) {
			loadError =
				error instanceof Error ? error.message : 'Unexpected completed page loading error';
			loadState = 'error';
		}
	}

	function formatPromotionTime(value: string | null): string {
		if (!value) return 'Promotion time unavailable';
		const parsed = new Date(value);
		if (Number.isNaN(parsed.getTime())) {
			return value;
		}
		return new Intl.DateTimeFormat(undefined, {
			dateStyle: 'medium',
			timeStyle: 'short'
		}).format(parsed);
	}

	function nextSelection(prefix: string, current: string[]): string[] {
		return current.includes(prefix)
			? current.filter((value) => value !== prefix)
			: [...current, prefix];
	}

	$effect(() => {
		if (cleanupAvailable || selectedPrefixes.length === 0) {
			return;
		}

		selectedPrefixes = [];
	});
</script>

<svelte:head>
	<title>Completed · Mediaforce</title>
</svelte:head>

<div class="completed-screen">
	<section class="system-strip" aria-label="Completed archive state">
		<div class="system-cell accent-cell">
			<p class="system-label">Completed archive</p>
			<p class="system-value">{completed.completed_count} promoted folders</p>
			<p class="system-detail">
				Successful AV1 promotions stay visible here so rollback state and cleanup timing remain
				explicit.
			</p>
		</div>

		<div class={`system-cell ${cleanupAvailable ? 'warning-state' : 'normal-state'}`.trim()}>
			<p class="system-label">Rollback copies</p>
			<p class="system-value">{completed.archive_cleanup.file_count} archived originals</p>
			<p class="system-detail">
				{#if cleanupAvailable}
					{completed.folders_with_backups_count} folder{completed.folders_with_backups_count === 1
						? ''
						: 's'} still keep rollback media.
				{:else}
					No archived originals are waiting for cleanup.
				{/if}
			</p>
		</div>

		<div class="system-cell normal-state">
			<p class="system-label">Reclaimable</p>
			<p class="system-value">{formatGiB(completed.archive_cleanup.total_size_bytes, 1)}</p>
			<p class="system-detail">
				Delete backups only after the promoted outputs are confirmed and rollback copies are no
				longer needed.
			</p>
		</div>

		<div
			class={`system-cell ${selectedFolders.length > 0 ? 'selection-state' : 'normal-state'}`.trim()}
		>
			<p class="system-label">Selection</p>
			<p class="system-value">
				{#if selectedFolders.length > 0}
					{selectedFolders.length} folder{selectedFolders.length === 1 ? '' : 's'} armed
				{:else}
					No folders selected
				{/if}
			</p>
			<p class="system-detail">
				{#if selectedFolders.length > 0}
					{selectedBackupCount} archived original{selectedBackupCount === 1 ? '' : 's'} · {formatGiB(
						selectedBackupSizeBytes,
						1
					)} queued for removal.
				{:else if cleanupAvailable}
					Select specific folders or clear the whole archive in one pass.
				{:else}
					Cleanup controls activate automatically when archived originals exist.
				{/if}
			</p>
		</div>
	</section>

	<div class="completed-console-grid">
		<div class="completed-main-column">
			<section class="station-card cleanup-card" aria-label="Archive cleanup controls">
				<div class="section-head compact">
					<div>
						<p class="section-label">Rollback cleanup</p>
						<h2 class="section-title small">
							Clear archived originals only when the new encode is trusted
						</h2>
						<p class="section-copy">
							Use folder selection for surgical cleanup, or clear the whole archive once the
							promoted outputs are stable.
						</p>
					</div>
					<div class="cleanup-actions">
						<Button
							variant="ghost"
							onclick={toggleAllFolders}
							disabled={!cleanupAvailable || foldersWithBackups.length <= 0}
						>
							{allFoldersSelected ? 'Clear selection' : 'Select all folders with backups'}
						</Button>
						<Button
							variant="danger"
							onclick={clearSelectedBackups}
							disabled={!cleanupAvailable || selectedFolders.length <= 0}
							loading={actionState === 'selected'}
						>
							Delete selected backups
						</Button>
						<Button
							variant="danger"
							onclick={clearAllBackups}
							disabled={!cleanupAvailable}
							loading={actionState === 'all'}
						>
							Delete all backups
						</Button>
					</div>
				</div>

				<div class="pill-row cleanup-pill-row">
					<Pill
						label={`${completed.folders_with_backups_count} folders with backups`}
						variant="warn"
					/>
					<Pill
						label={`${completed.archive_cleanup.file_count} archived originals`}
						variant="warn"
					/>
					<Pill
						label={`${formatGiB(completed.archive_cleanup.total_size_bytes, 1)} reclaimable`}
						variant="ghost"
					/>
				</div>

				{#if selectedFolders.length > 0}
					<p class="selection-summary">
						{selectedFolders.length} folder{selectedFolders.length === 1 ? '' : 's'} selected · {selectedBackupCount}
						archived original{selectedBackupCount === 1 ? '' : 's'} · {formatGiB(
							selectedBackupSizeBytes,
							1
						)}
					</p>
				{/if}
			</section>

			<section class="station-card completed-table-card" aria-label="Completed folder history">
				<div class="section-head compact">
					<div>
						<p class="section-label">Completed history</p>
						<h2 class="section-title small">Promoted folders and retained rollback media</h2>
					</div>
					<p class="table-count">
						{folders.length} visible folder{folders.length === 1 ? '' : 's'}
					</p>
				</div>

				{#if loadState === 'error'}
					<div class="table-message error-message">
						<p>Completed page failed to load.</p>
						<p>{loadError ?? 'Unknown completed page error.'}</p>
						<Button variant="ghost" onclick={loadCompletedPage}>Try again</Button>
					</div>
				{:else if loadState === 'loading'}
					<div class="table-message">
						<p>Loading completed folders…</p>
						<p>Pulling promoted-folder history and archived backup totals now.</p>
					</div>
				{:else if folders.length > 0}
					<div class="table-shell">
						<table>
							<thead>
								<tr>
									<th scope="col">Folder</th>
									<th scope="col">Rollback media</th>
									<th scope="col">Savings</th>
									<th scope="col">Latest promotion</th>
									<th scope="col">Actions</th>
								</tr>
							</thead>
							<tbody>
								{#each folders as folder (folder.prefix)}
									{@const hasBackups = folder.archived_backup_count > 0}
									{@const isSelected = selectedPrefixes.includes(folder.prefix)}
									<tr class:selected-row={isSelected}>
										<td data-label="Folder">
											<div class="row-copy">
												<p class="row-eyebrow">{folder.scope_label}</p>
												<a class="row-title-link" href={resolve(folderRoutePath(folder.prefix))}
													>{folder.title}</a
												>
												<p class="row-subcopy">{folder.subtitle}</p>
											</div>
										</td>
										<td data-label="Rollback media">
											<span class={`status-tag ${hasBackups ? 'warn' : 'neutral'}`.trim()}>
												{hasBackups ? `${folder.archived_backup_count} retained` : 'Archive clear'}
											</span>
											<span class="table-subcopy"
												>{formatGiB(folder.archived_backup_size_bytes, 1)} archived size</span
											>
										</td>
										<td data-label="Savings">
											<span class="table-value">{formatGiB(folder.total_bytes_saved, 1)}</span>
											<span class="table-subcopy"
												>{folder.promoted_item_count} promoted item{folder.promoted_item_count === 1
													? ''
													: 's'}</span
											>
										</td>
										<td data-label="Latest promotion">
											<span class="table-value time-value"
												>{formatPromotionTime(folder.latest_promoted_at)}</span
											>
											<span class="table-subcopy"
												>Rollback archive remains visible until cleared</span
											>
										</td>
										<td data-label="Actions">
											<div class="row-actions">
												<a class="console-link" href={resolve(folderRoutePath(folder.prefix))}
													>Open folder</a
												>
												{#if hasBackups && cleanupAvailable}
													<label class="select-toggle">
														<input
															type="checkbox"
															checked={isSelected}
															onchange={() => toggleFolder(folder.prefix)}
														/>
														<span>{isSelected ? 'Selected' : 'Select'}</span>
													</label>
												{/if}
											</div>
										</td>
									</tr>
								{/each}
							</tbody>
						</table>
					</div>
				{:else}
					<div class="table-message">
						<p>No completed folders are available yet.</p>
						<p>
							Folders appear here once staged outputs are promoted into the library. Archived
							originals stay visible until you clear them.
						</p>
					</div>
				{/if}
			</section>
		</div>

		<aside class="completed-side-rail">
			<section class="station-card rail-card" aria-label="Archive root">
				<div class="section-head compact">
					<div>
						<p class="section-label">Archive root</p>
						<h2 class="section-title small">Rollback media location</h2>
					</div>
				</div>
				<p class="rail-copy">All archived originals live under this root until you clear them.</p>
				<p class="mono-block">
					{completed.archive_cleanup.archive_root || 'Archive root not configured'}
				</p>
				<div class="rail-actions">
					<a class="console-link" href={resolve('/')}>Back to folders</a>
					<a class="console-link" href={resolve('/ops')}>Open ops</a>
				</div>
			</section>

			<section class="station-card rail-card" aria-label="Selection guidance">
				<div class="section-head compact">
					<div>
						<p class="section-label">Selection state</p>
						<h2 class="section-title small">Cleanup arm/disarm</h2>
					</div>
				</div>
				{#if selectedFolders.length > 0}
					<p class="rail-copy strong-copy">
						{selectedFolders.length} folder{selectedFolders.length === 1 ? '' : 's'} armed for cleanup.
					</p>
					<p class="rail-copy">
						The selection covers {selectedBackupCount} archived original{selectedBackupCount === 1
							? ''
							: 's'} and {formatGiB(selectedBackupSizeBytes, 1)}.
					</p>
				{:else}
					<p class="rail-copy strong-copy">No folder is armed for cleanup.</p>
					<p class="rail-copy">
						Use the table checkboxes for surgical cleanup, or use the global delete action only when
						the archive should be cleared in one pass.
					</p>
				{/if}
			</section>

			<section class="station-card rail-card" aria-label="Cleanup rules">
				<div class="section-head compact">
					<div>
						<p class="section-label">Cleanup rules</p>
						<h2 class="section-title small">Operator reminders</h2>
					</div>
				</div>
				<ul class="fact-list">
					<li>Keep rollback copies until the promoted encode is fully trusted.</li>
					<li>Use folder selection when only a few promotions are ready to be finalized.</li>
					<li>Use archive-wide deletion only after the whole backlog has been reviewed.</li>
				</ul>
			</section>
		</aside>
	</div>
</div>

<style>
	.completed-screen {
		display: grid;
		gap: 1rem;
		padding: 0.25rem 0 1rem;
	}

	.system-strip {
		display: grid;
		grid-template-columns: repeat(4, minmax(0, 1fr));
		gap: 1rem;
	}

	.system-cell,
	.station-card,
	.table-message {
		position: relative;
		border: 1px solid rgba(148, 163, 184, 0.18);
		background: rgba(15, 20, 27, 0.94);
		box-shadow: 0 18px 38px rgba(2, 6, 23, 0.2);
		overflow: hidden;
	}

	.system-cell::before {
		content: '';
		position: absolute;
		inset: 0 0 auto;
		height: 2px;
		background: rgba(56, 189, 248, 0.82);
	}

	.system-cell {
		padding: 1rem 1.1rem;
		min-height: 8.4rem;
	}

	.accent-cell {
		background: rgba(13, 33, 42, 0.94);
	}

	.warning-state {
		border-color: rgba(249, 115, 22, 0.28);
		background: rgba(58, 26, 13, 0.94);
	}

	.warning-state::before {
		background: rgba(251, 146, 60, 0.92);
	}

	.selection-state {
		border-color: rgba(45, 212, 191, 0.24);
		background: rgba(10, 36, 38, 0.94);
	}

	.selection-state::before {
		background: rgba(45, 212, 191, 0.88);
	}

	.system-label,
	.section-label {
		margin: 0;
		font-size: 0.72rem;
		font-weight: 800;
		letter-spacing: 0.16em;
		text-transform: uppercase;
		color: rgba(148, 163, 184, 0.88);
	}

	.system-value,
	.section-title {
		margin: 0;
		color: #f8fafc;
	}

	.system-value {
		margin-top: 0.4rem;
		font-size: 1.2rem;
		font-weight: 700;
		line-height: 1.25;
	}

	.system-detail,
	.section-copy,
	.table-count,
	.table-subcopy,
	.rail-copy,
	.fact-list,
	.fact-list li {
		margin: 0;
		color: rgba(226, 232, 240, 0.74);
		line-height: 1.5;
	}

	.completed-console-grid {
		display: grid;
		grid-template-columns: minmax(0, 1.7fr) minmax(21rem, 0.92fr);
		gap: 1rem;
		align-items: start;
	}

	.completed-main-column,
	.completed-side-rail {
		display: grid;
		gap: 1rem;
		min-width: 0;
	}

	.station-card {
		padding: 1.1rem;
	}

	.section-head {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		gap: 1rem;
		align-items: start;
		margin-bottom: 0.95rem;
	}

	.section-head.compact {
		margin-bottom: 0.8rem;
	}

	.section-title {
		margin-top: 0.28rem;
		font-size: 1rem;
		font-weight: 700;
		line-height: 1.28;
	}

	.cleanup-actions,
	.rail-actions,
	.row-actions,
	.cleanup-pill-row {
		display: flex;
		gap: 0.65rem;
		flex-wrap: wrap;
	}

	.selection-summary {
		margin: 0;
		padding: 0.8rem 0.95rem;
		border: 1px solid rgba(45, 212, 191, 0.22);
		background: rgba(10, 36, 38, 0.7);
		color: #ccfbf1;
		font-weight: 700;
	}

	.table-message {
		display: grid;
		gap: 0.35rem;
		padding: 1rem;
		border-color: rgba(148, 163, 184, 0.16);
		background: rgba(15, 23, 42, 0.46);
	}

	.error-message {
		border-color: rgba(249, 115, 22, 0.28);
		background: rgba(67, 20, 7, 0.72);
	}

	.table-shell {
		min-width: 0;
		max-width: 100%;
		max-height: min(72vh, 60rem);
		overflow: auto;
		border: 1px solid rgba(148, 163, 184, 0.16);
		background: rgba(9, 14, 22, 0.88);
	}

	table {
		width: max(100%, 70rem);
		border-collapse: collapse;
	}

	thead {
		background: rgba(15, 23, 42, 0.96);
	}

	thead th {
		position: sticky;
		top: 0;
		z-index: 2;
		background: rgba(15, 23, 42, 0.98);
	}

	th,
	td {
		padding: 0.85rem 0.9rem;
		text-align: left;
		vertical-align: top;
		border-bottom: 1px solid rgba(148, 163, 184, 0.14);
	}

	th {
		font-size: 0.72rem;
		font-weight: 800;
		letter-spacing: 0.12em;
		text-transform: uppercase;
		color: rgba(148, 163, 184, 0.88);
	}

	td {
		color: rgba(226, 232, 240, 0.86);
	}

	tbody tr {
		background: rgba(11, 15, 20, 0.76);
	}

	tbody tr:nth-child(even) {
		background: rgba(15, 23, 42, 0.82);
	}

	tbody tr.selected-row {
		background: rgba(8, 47, 73, 0.34);
	}

	.row-copy {
		display: grid;
		gap: 0.15rem;
	}

	.row-eyebrow {
		margin: 0;
		font-size: 0.72rem;
		font-weight: 800;
		letter-spacing: 0.12em;
		text-transform: uppercase;
		color: rgba(125, 211, 252, 0.84);
	}

	.row-title-link,
	.console-link {
		color: #f8fafc;
		font-weight: 700;
	}

	.row-title-link {
		font-size: 1rem;
	}

	.row-subcopy,
	.mono-block {
		margin: 0;
		color: rgba(203, 213, 225, 0.76);
	}

	.table-value,
	.mono-block {
		font-family: 'SFMono-Regular', 'Menlo', monospace;
	}

	.time-value {
		font-size: 0.92rem;
		line-height: 1.45;
	}

	.status-tag {
		display: inline-flex;
		align-items: center;
		gap: 0.45rem;
		padding: 0.34rem 0.56rem;
		font-size: 0.72rem;
		font-weight: 800;
		letter-spacing: 0.04em;
		text-transform: uppercase;
	}

	.status-tag.warn {
		background: rgba(120, 53, 15, 0.84);
		color: #ffedd5;
	}

	.status-tag.neutral {
		background: rgba(30, 41, 59, 0.84);
		color: #cbd5e1;
	}

	.select-toggle {
		display: inline-flex;
		align-items: center;
		gap: 0.45rem;
		font-weight: 700;
		color: rgba(226, 232, 240, 0.78);
	}

	.select-toggle input {
		accent-color: #38bdf8;
	}

	.console-link {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		padding: 0.72rem 0.95rem;
		border: 1px solid rgba(56, 189, 248, 0.22);
		background: rgba(15, 23, 42, 0.7);
		color: #e2e8f0;
		transition:
			border-color 150ms ease,
			background-color 150ms ease,
			color 150ms ease;
	}

	.console-link:hover {
		border-color: rgba(56, 189, 248, 0.5);
		background: rgba(30, 41, 59, 0.94);
	}

	.strong-copy {
		color: #f8fafc;
		font-weight: 700;
	}

	.fact-list {
		display: grid;
		gap: 0.45rem;
		padding-left: 1rem;
	}

	.mono-block {
		padding: 0.85rem 0.9rem;
		border: 1px solid rgba(148, 163, 184, 0.16);
		background: rgba(15, 23, 42, 0.58);
		line-height: 1.5;
		word-break: break-word;
	}

	:global(.cleanup-card .button.ghost),
	:global(.completed-table-card .button.ghost) {
		border: 1px solid rgba(56, 189, 248, 0.22) !important;
		background: rgba(15, 23, 42, 0.72) !important;
		color: #e2e8f0 !important;
	}

	:global(.cleanup-card .button.danger) {
		background: rgba(120, 53, 15, 0.82) !important;
		color: #ffedd5 !important;
	}

	:global(.pill.ok) {
		background: rgba(20, 83, 45, 0.82) !important;
		border-color: rgba(34, 197, 94, 0.28) !important;
		color: #dcfce7 !important;
	}

	:global(.pill.warn) {
		background: rgba(120, 53, 15, 0.82) !important;
		border-color: rgba(249, 115, 22, 0.28) !important;
		color: #ffedd5 !important;
	}

	:global(.pill.ghost),
	:global(.pill.neutral) {
		background: rgba(30, 41, 59, 0.78) !important;
		border-color: rgba(148, 163, 184, 0.18) !important;
		color: rgba(226, 232, 240, 0.78) !important;
	}

	@media (max-width: 1100px) {
		.system-strip {
			grid-template-columns: 1fr 1fr;
		}

		.section-head {
			grid-template-columns: 1fr;
		}
	}

	@media (max-width: 960px) {
		.completed-console-grid {
			grid-template-columns: 1fr;
		}
	}

	@media (max-width: 720px) {
		.system-strip {
			grid-template-columns: 1fr;
		}

		thead {
			display: none;
		}

		table,
		tbody,
		tr,
		td {
			display: block;
			width: 100%;
		}

		tr {
			padding: 0.4rem 0;
		}

		td {
			padding: 0.6rem 0.9rem;
		}

		td::before {
			content: attr(data-label);
			display: block;
			margin-bottom: 0.22rem;
			font-size: 0.68rem;
			font-weight: 800;
			letter-spacing: 0.12em;
			text-transform: uppercase;
			color: rgba(148, 163, 184, 0.8);
		}
	}
</style>
