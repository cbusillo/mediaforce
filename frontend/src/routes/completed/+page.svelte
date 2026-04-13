<script lang="ts">
	import { onMount } from 'svelte';
	import '$lib/design/workstation-shell.css';
	import { resolve } from '$app/paths';
	import type { CompletedBackupsClearResponse, CompletedPagePayload } from '$lib/api/types';
	import { fetchJson, postJson } from '$lib/api/client';
	import Button from '$lib/components/Button.svelte';
	import Panel from '$lib/components/Panel.svelte';
	import Pill from '$lib/components/Pill.svelte';
	import SectionHead from '$lib/components/SectionHead.svelte';
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

<Panel class="completed-hero" padding="1.25rem 1.3rem 1.35rem">
	<div class="hero-shell">
		<div class="hero-copy">
			<SectionHead
				eyebrow="Completed"
				heading={`${completed.completed_count} promoted folders, ${formatGiB(completed.archive_cleanup.total_size_bytes, 1)} in archived originals.`}
				lede="Review successful folder promotions, keep the history visible, and clear rollback copies only when you are confident the AV1 outputs are solid."
				size="display"
			/>
			<div class="hero-pills">
				<Pill
					label={`${completed.folders_with_backups_count} folders with backups`}
					variant="warn"
				/>
				<Pill label={`${completed.archive_cleanup.file_count} archived originals`} variant="warn" />
				<Pill
					label={`${formatGiB(completed.archive_cleanup.total_size_bytes, 1)} reclaimable`}
					variant="ghost"
				/>
			</div>
		</div>
		<div class="hero-meta">
			<p class="hero-meta-label">Archive root</p>
			<p class="hero-meta-value">{completed.archive_cleanup.archive_root}</p>
			<div class="hero-links">
				<a href={resolve('/')}>Back to folders</a>
				<a href={resolve('/ops')}>Open ops</a>
			</div>
		</div>
	</div>
</Panel>

{#if loadState === 'error'}
	<Panel class="empty-panel" padding="1.1rem 1.15rem 1.2rem">
		<p class="eyebrow-copy">Completed</p>
		<h2>Completed page failed to load.</h2>
		<p>{loadError ?? 'Unknown completed page error.'}</p>
		<Button variant="ghost" onclick={loadCompletedPage}>Try again</Button>
	</Panel>
{:else}
	<Panel class="cleanup-panel" padding="1.05rem 1.15rem 1.2rem">
		<div class="cleanup-header">
			<div>
				<p class="eyebrow-copy">Archived Originals</p>
				<h2>Folder-level cleanup for rollback copies</h2>
				<p class="cleanup-copy">
					Select completed folders with archived originals, or clear the whole archive when you are
					ready.
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
		{#if cleanupAvailable && selectedFolders.length > 0}
			<p class="selection-summary">
				{selectedFolders.length} folder{selectedFolders.length === 1 ? '' : 's'} selected · {selectedBackupCount}
				archived original{selectedBackupCount === 1 ? '' : 's'} · {formatGiB(
					selectedBackupSizeBytes,
					1
				)}
			</p>
		{/if}
	</Panel>

	{#if loadState === 'loading'}
		<Panel class="empty-panel" padding="1.1rem 1.15rem 1.2rem">
			<p class="eyebrow-copy">Completed</p>
			<h2>Loading completed folders…</h2>
			<p>Pulling promoted-folder history and archived backup totals now.</p>
		</Panel>
	{:else if folders.length > 0}
		<div class="completed-grid">
			{#each folders as folder (folder.prefix)}
				{@const hasBackups = folder.archived_backup_count > 0}
				{@const isSelected = selectedPrefixes.includes(folder.prefix)}
				<Panel
					class={`completed-card ${isSelected ? 'selected' : ''}`.trim()}
					padding="1rem 1rem 1.05rem"
				>
					<div class="card-header">
						<div>
							<p class="eyebrow-copy">{folder.scope_label}</p>
							<h2>{folder.title}</h2>
							<p class="card-subtitle">{folder.subtitle}</p>
						</div>
						{#if hasBackups && cleanupAvailable}
							<label class="select-toggle">
								<input
									type="checkbox"
									checked={isSelected}
									onchange={() => toggleFolder(folder.prefix)}
								/>
								<span>Select</span>
							</label>
						{/if}
					</div>
					<div class="card-pills">
						<Pill label={`${folder.promoted_item_count} promoted`} variant="ok" />
						{#if hasBackups}
							<Pill label={`${folder.archived_backup_count} archived backups`} variant="warn" />
						{:else}
							<Pill label="Backups cleared" variant="ghost" />
						{/if}
					</div>
					<div class="card-stats">
						<div>
							<p class="card-stat-label">Archived size</p>
							<p class="card-stat-value">{formatGiB(folder.archived_backup_size_bytes, 1)}</p>
						</div>
						<div>
							<p class="card-stat-label">Space recovered</p>
							<p class="card-stat-value">{formatGiB(folder.total_bytes_saved, 1)}</p>
						</div>
						<div>
							<p class="card-stat-label">Latest promotion</p>
							<p class="card-stat-value time">{formatPromotionTime(folder.latest_promoted_at)}</p>
						</div>
					</div>
					<div class="card-actions">
						<a href={resolve(folderRoutePath(folder.prefix))}>Open folder</a>
					</div>
				</Panel>
			{/each}
		</div>
	{:else}
		<Panel class="empty-panel" padding="1.1rem 1.15rem 1.2rem">
			<p class="eyebrow-copy">Completed</p>
			<h2>No completed folders are available yet.</h2>
			<p>
				Folders appear here once staged outputs are promoted into the library. Archived originals
				will stay visible until you clear them.
			</p>
		</Panel>
	{/if}
{/if}

<style>
	.hero-shell,
	.cleanup-header {
		display: flex;
		justify-content: space-between;
		gap: 1rem;
		align-items: start;
		flex-wrap: wrap;
	}

	:global(.completed-hero) {
		border: 1px solid rgba(148, 163, 184, 0.18) !important;
		background: rgba(15, 20, 27, 0.94) !important;
		box-shadow: 0 18px 38px rgba(2, 6, 23, 0.2) !important;
	}

	:global(.completed-hero)::before,
	:global(.completed-hero)::after,
	:global(.cleanup-panel)::before,
	:global(.cleanup-panel)::after,
	:global(.empty-panel)::before,
	:global(.empty-panel)::after,
	:global(.completed-card)::before,
	:global(.completed-card)::after {
		display: none !important;
	}

	.hero-copy,
	.hero-meta {
		display: grid;
		gap: 0.8rem;
	}

	.hero-copy {
		max-width: 48rem;
	}

	.hero-pills,
	.card-pills,
	.hero-links,
	.cleanup-actions {
		display: flex;
		gap: 0.7rem;
		flex-wrap: wrap;
	}

	.hero-meta {
		justify-items: start;
		max-width: 22rem;
	}

	.hero-meta-label,
	.card-stat-label,
	.selection-summary,
	.cleanup-copy {
		margin: 0;
		color: rgba(226, 232, 240, 0.68);
	}

	.hero-meta-value {
		margin: 0;
		font-size: 0.95rem;
		line-height: 1.55;
		color: #f8fafc;
		word-break: break-word;
	}

	.hero-links a,
	.card-actions a {
		font-weight: 700;
		color: #7dd3fc;
	}

	:global(.cleanup-panel) h2,
	:global(.empty-panel) h2,
	:global(.completed-card) h2 {
		margin: 0;
		font-size: 1.2rem;
		line-height: 1.15;
		color: #f8fafc;
	}

	:global(.completed-hero h1),
	:global(.completed-hero h2),
	:global(.completed-hero .eyebrow-copy),
	:global(.cleanup-panel .eyebrow-copy),
	:global(.empty-panel .eyebrow-copy),
	:global(.completed-card .eyebrow-copy) {
		color: rgba(125, 211, 252, 0.84) !important;
	}

	:global(.completed-hero .lede-copy),
	:global(.empty-panel p),
	.card-subtitle {
		color: rgba(226, 232, 240, 0.72) !important;
	}

	.cleanup-copy,
	.card-subtitle,
	:global(.empty-panel) p {
		max-width: 46rem;
	}

	.completed-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
		gap: var(--space-4);
	}

	:global(.completed-card) {
		display: grid;
		gap: 0.95rem;
		border: 1px solid rgba(148, 163, 184, 0.18) !important;
		background: rgba(15, 20, 27, 0.94) !important;
		box-shadow: 0 18px 38px rgba(2, 6, 23, 0.2) !important;
	}

	:global(.completed-card.selected) {
		border-color: rgba(56, 189, 248, 0.32) !important;
		box-shadow: 0 16px 34px rgba(8, 47, 73, 0.22) !important;
	}

	.card-header {
		display: flex;
		justify-content: space-between;
		gap: 1rem;
		align-items: start;
	}

	.card-subtitle {
		margin: 0.2rem 0 0;
		color: rgba(148, 163, 184, 0.82);
	}

	.select-toggle {
		display: inline-flex;
		align-items: center;
		gap: 0.45rem;
		font-weight: 700;
		color: rgba(226, 232, 240, 0.72);
	}

	.card-stats {
		display: grid;
		grid-template-columns: repeat(3, minmax(0, 1fr));
		gap: 0.7rem;
	}

	.card-stat-value {
		margin: 0.2rem 0 0;
		font-size: 1rem;
		font-weight: 700;
		color: #f8fafc;
	}

	.card-stat-value.time {
		font-size: 0.92rem;
		line-height: 1.45;
	}

	.selection-summary {
		font-weight: 700;
	}

	:global(.cleanup-panel),
	:global(.empty-panel) {
		border: 1px solid rgba(148, 163, 184, 0.18) !important;
		background: rgba(15, 20, 27, 0.94) !important;
		box-shadow: 0 18px 38px rgba(2, 6, 23, 0.2) !important;
	}

	:global(.cleanup-panel .button.ghost),
	:global(.empty-panel .button.ghost) {
		border: 1px solid rgba(56, 189, 248, 0.22) !important;
		background: rgba(15, 23, 42, 0.72) !important;
		color: #e2e8f0 !important;
	}

	:global(.cleanup-panel .button.danger) {
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

	@media (max-width: 720px) {
		.card-stats {
			grid-template-columns: 1fr;
		}
	}
</style>
