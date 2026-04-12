<script lang="ts">
	import { onMount } from 'svelte';
	import { resolve } from '$app/paths';
	import type {
		CompletedBackupsClearResponse,
		CompletedFolder,
		CompletedPagePayload
	} from '$lib/api/types';
	import { fetchJson, postJson } from '$lib/api/client';
	import Button from '$lib/components/Button.svelte';
	import Panel from '$lib/components/Panel.svelte';
	import Pill from '$lib/components/Pill.svelte';
	import SectionHead from '$lib/components/SectionHead.svelte';
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
	let selectedPrefixes = $state<Set<string>>(new Set());
	let actionState = $state<'selected' | 'all' | null>(null);
	let loadState = $state<'loading' | 'ready' | 'error'>('loading');
	let loadError = $state<string | null>(null);

	onMount(() => {
		void loadCompletedPage();
	});

	const completed = $derived(completedPayload);
	const folders = $derived(completed.folders);
	const foldersWithBackups = $derived(
		folders.filter((folder) => folder.archived_backup_count > 0)
	);
	const selectedFolders = $derived(
		foldersWithBackups.filter((folder) => selectedPrefixes.has(folder.prefix))
	);
	const selectedBackupCount = $derived(
		selectedFolders.reduce((total, folder) => total + folder.archived_backup_count, 0)
	);
	const selectedBackupSizeBytes = $derived(
		selectedFolders.reduce((total, folder) => total + folder.archived_backup_size_bytes, 0)
	);
	const allFoldersSelected = $derived(
		foldersWithBackups.length > 0 && selectedPrefixes.size === foldersWithBackups.length
	);

	function folderHref(prefix: string): string {
		const encodedPrefix = prefix
			.split('/')
			.map((segment) => encodeURIComponent(segment))
			.join('/');
		return resolve(`/folders/${encodedPrefix}`);
	}

	function toggleFolder(prefix: string): void {
		selectedPrefixes = nextSelection(prefix, selectedPrefixes);
	}

	function toggleAllFolders(): void {
		if (allFoldersSelected) {
			selectedPrefixes = new Set();
			return;
		}
		selectedPrefixes = new Set(foldersWithBackups.map((folder) => folder.prefix));
	}

	async function clearSelectedBackups(): Promise<void> {
		if (selectedFolders.length <= 0) {
			return;
		}
		const confirmed = window.confirm(
			`Delete ${selectedBackupCount} archived original${selectedBackupCount === 1 ? '' : 's'} (${formatGiB(selectedBackupSizeBytes, 1)}) from ${selectedFolders.length} completed folder${selectedFolders.length === 1 ? '' : 's'}?\n\nArchive root: ${completed.archive_cleanup.archive_root}\nThis cannot be undone.`
		);
		if (!confirmed) {
			return;
		}
		await clearBackups({ prefixes: selectedFolders.map((folder) => folder.prefix), mode: 'selected' });
	}

	async function clearAllBackups(): Promise<void> {
		if (!completed.archive_cleanup.has_cleanup) {
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
			selectedPrefixes = new Set();
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
			loadError = error instanceof Error ? error.message : 'Unexpected completed page loading error';
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

	function nextSelection(prefix: string, current: Set<string>): Set<string> {
		const next = new Set(current);
		if (next.has(prefix)) {
			next.delete(prefix);
		} else {
			next.add(prefix);
		}
		return next;
	}
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
				<Pill label={`${completed.folders_with_backups_count} folders with backups`} variant="warn" />
				<Pill label={`${completed.archive_cleanup.file_count} archived originals`} variant="warn" />
				<Pill label={`${formatGiB(completed.archive_cleanup.total_size_bytes, 1)} reclaimable`} variant="ghost" />
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
				Select completed folders with archived originals, or clear the whole archive when you are ready.
			</p>
		</div>
		<div class="cleanup-actions">
			<Button variant="ghost" onclick={toggleAllFolders} disabled={foldersWithBackups.length <= 0}>
				{allFoldersSelected ? 'Clear selection' : 'Select all folders with backups'}
			</Button>
			<Button
				variant="danger"
				onclick={clearSelectedBackups}
				disabled={selectedFolders.length <= 0}
				loading={actionState === 'selected'}
			>
				Delete selected backups
			</Button>
			<Button
				variant="danger"
				onclick={clearAllBackups}
				disabled={!completed.archive_cleanup.has_cleanup}
				loading={actionState === 'all'}
			>
				Delete all backups
			</Button>
		</div>
	</div>
	{#if selectedFolders.length > 0}
		<p class="selection-summary">
			{selectedFolders.length} folder{selectedFolders.length === 1 ? '' : 's'} selected · {selectedBackupCount} archived original{selectedBackupCount === 1 ? '' : 's'} · {formatGiB(selectedBackupSizeBytes, 1)}
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
			{@const isSelected = selectedPrefixes.has(folder.prefix)}
			<Panel class={`completed-card ${isSelected ? 'selected' : ''}`.trim()} padding="1rem 1rem 1.05rem">
				<div class="card-header">
					<div>
						<p class="eyebrow-copy">{folder.scope_label}</p>
						<h2>{folder.title}</h2>
						<p class="card-subtitle">{folder.subtitle}</p>
					</div>
					{#if hasBackups}
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
					<a href={folderHref(folder.prefix)}>Open folder</a>
				</div>
			</Panel>
		{/each}
	</div>
	{:else}
	<Panel class="empty-panel" padding="1.1rem 1.15rem 1.2rem">
		<p class="eyebrow-copy">Completed</p>
		<h2>No completed folders are available yet.</h2>
		<p>
			Folders appear here once staged outputs are promoted into the library. Archived originals will stay visible until you clear them.
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
		background:
			linear-gradient(180deg, rgba(255, 251, 243, 0.96), rgba(247, 241, 229, 0.92)),
			radial-gradient(circle at top left, rgba(15, 118, 110, 0.12), transparent 34%);
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
		color: var(--ink-soft);
	}

	.hero-meta-value {
		margin: 0;
		font-size: 0.95rem;
		line-height: 1.55;
		color: var(--ink);
		word-break: break-word;
	}

	.hero-links a,
	.card-actions a {
		font-weight: 700;
		color: var(--accent-deep);
	}

	:global(.cleanup-panel) h2,
	:global(.empty-panel) h2,
	:global(.completed-card) h2 {
		margin: 0;
		font-size: 1.2rem;
		line-height: 1.15;
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
		border: 1px solid rgba(15, 118, 110, 0.08);
		background:
			linear-gradient(180deg, rgba(255, 255, 255, 0.9), rgba(252, 248, 241, 0.92)),
			radial-gradient(circle at top right, rgba(15, 118, 110, 0.08), transparent 26%);
	}

	:global(.completed-card.selected) {
		border-color: rgba(15, 118, 110, 0.34);
		box-shadow: 0 16px 34px rgba(15, 118, 110, 0.1);
	}

	.card-header {
		display: flex;
		justify-content: space-between;
		gap: 1rem;
		align-items: start;
	}

	.card-subtitle {
		margin: 0.2rem 0 0;
		color: var(--ink-soft);
	}

	.select-toggle {
		display: inline-flex;
		align-items: center;
		gap: 0.45rem;
		font-weight: 700;
		color: var(--ink-soft);
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
		color: var(--ink);
	}

	.card-stat-value.time {
		font-size: 0.92rem;
		line-height: 1.45;
	}

	.selection-summary {
		font-weight: 700;
	}

	@media (max-width: 720px) {
		.card-stats {
			grid-template-columns: 1fr;
		}
	}
</style>
