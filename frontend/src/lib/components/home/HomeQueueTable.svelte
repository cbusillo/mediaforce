<script lang="ts">
	import { resolve } from '$app/paths';
	import type { FolderCard as FolderCardData } from '$lib/api/types';
	import { folderLibraryKey, folderLibraryLabel, folderRoutePath } from '$lib/folder-display';
	import { formatGiB } from '$lib/format';

	type FolderLibraryOption = { key: string; label: string; count: number };
	type QueueRow = { folder: FolderCardData; priority: number };
	type FolderSortKey = 'priority' | 'title' | 'library' | 'pending' | 'reclaim' | 'age' | 'review';
	type FolderSortDirection = 'asc' | 'desc';

	let {
		folderLibraries,
		libraryColors,
		disabledLibraries,
		libraryFiltersActive,
		filterHintCopy,
		folderLoadState,
		folderLoadError,
		showInitialLoadingMessage,
		catalogEmpty,
		sortedVisibleFolders,
		sortedVisibleFolderRows,
		activeWorkspacePrefix,
		activeWorkspaceFolder,
		activeWorkspaceQueued,
		queueActionDisabled,
		queueActionPending,
		queueActiveWorkspaceFolder,
		folderSortKey,
		folderSortDirection,
		enableAllLibraries,
		toggleLibraryFilter,
		toggleFolderSort,
		formatTopCounts
	}: {
		folderLibraries: FolderLibraryOption[];
		libraryColors: Record<string, string>;
		disabledLibraries: string[];
		libraryFiltersActive: boolean;
		filterHintCopy: string;
		folderLoadState: 'loading' | 'ready' | 'error';
		folderLoadError: string | null;
		showInitialLoadingMessage: boolean;
		catalogEmpty: boolean;
		sortedVisibleFolders: FolderCardData[];
		sortedVisibleFolderRows: QueueRow[];
		activeWorkspacePrefix: string | null;
		activeWorkspaceFolder: FolderCardData | null;
		activeWorkspaceQueued: boolean;
		queueActionDisabled: boolean;
		queueActionPending: boolean;
		queueActiveWorkspaceFolder: () => void;
		folderSortKey: FolderSortKey;
		folderSortDirection: FolderSortDirection;
		enableAllLibraries: () => void;
		toggleLibraryFilter: (libraryKey: string) => void;
		toggleFolderSort: (key: FolderSortKey) => void;
		formatTopCounts: (counts: Record<string, number>, limit?: number, emptyCopy?: string) => string;
	} = $props();

	function folderSortAria(key: FolderSortKey): 'ascending' | 'descending' | 'none' {
		if (folderSortKey !== key) {
			return 'none';
		}
		return folderSortDirection === 'asc' ? 'ascending' : 'descending';
	}

	function folderNeedsAttention(folder: FolderCardData): boolean {
		return (
			folder.review_badge_label === 'Needs attention' && folder.review_badge_tone === 'warning'
		);
	}

	function folderReviewDetail(folder: FolderCardData): string {
		return String(folder.review_badge_detail ?? '').trim();
	}
</script>

<section class="station-card table-deck" aria-label="Folder work queue">
	<div class="section-head compact">
		<div>
			<p class="section-label">Work queue</p>
			<h2 class="section-title small">Ranked folders</h2>
		</div>
		<div class="table-head-meta">
			<p class="queue-count">
				{sortedVisibleFolders.length} visible folders
				<span class="sort-summary">
					Sorted by {folderSortKey}
					{folderSortDirection === 'asc' ? 'ascending' : 'descending'}
				</span>
			</p>
			<p class="filter-hint inline">{filterHintCopy}</p>
		</div>
	</div>

	{#if folderLibraries.length > 0}
		<div class="table-toolbar" aria-label="Queue filters">
			<div class="filter-row compact-filters">
				<button
					type="button"
					class:active={!libraryFiltersActive}
					class="filter-chip all-chip"
					onclick={enableAllLibraries}
				>
					Show all
				</button>

				{#each folderLibraries as library (library.key)}
					<button
						type="button"
						class:active={!disabledLibraries.includes(library.key)}
						class="filter-chip"
						style={`--library-chip: ${libraryColors[library.key] ?? '#4b5563'};`}
						onclick={() => toggleLibraryFilter(library.key)}
					>
						<span class="filter-dot"></span>
						{library.label}
						<span class="filter-count">{library.count}</span>
					</button>
				{/each}
			</div>
		</div>
	{/if}

	{#if activeWorkspaceFolder}
		<div class="queue-context-bar" aria-label="Top-ranked folder actions">
			<div class="queue-context-copy">
				<p class="queue-context-label">Top-ranked folder</p>
				<p class="queue-context-title">{activeWorkspaceFolder.title}</p>
				<p class="queue-context-detail">
					{activeWorkspaceFolder.pending_count} pending · {formatGiB(
						activeWorkspaceFolder.projected_reclaim_bytes,
						1
					)} reclaim · {formatTopCounts(activeWorkspaceFolder.statuses, 3)}
				</p>
			</div>
			<div class="queue-context-actions">
				<a class="queue-context-link" href={resolve(folderRoutePath(activeWorkspaceFolder.prefix))}>
					Open folder
				</a>
				<button
					type="button"
					class="queue-context-button"
					onclick={queueActiveWorkspaceFolder}
					disabled={queueActionDisabled ||
						activeWorkspaceQueued ||
						activeWorkspaceFolder.pending_count <= 0}
				>
					{#if queueActionPending}
						Queueing...
					{:else if activeWorkspaceQueued}
						Already queued
					{:else if activeWorkspaceFolder.pending_count <= 0}
						Nothing to queue
					{:else}
						Queue folder
					{/if}
				</button>
			</div>
		</div>
	{/if}

	<div class="mobile-sort-row" aria-label="Mobile sort controls">
		<button
			type="button"
			class={`mobile-sort-chip ${folderSortKey === 'priority' ? 'active' : ''}`.trim()}
			onclick={() => toggleFolderSort('priority')}
		>
			Priority {folderSortKey === 'priority' ? (folderSortDirection === 'asc' ? '↑' : '↓') : ''}
		</button>
		<button
			type="button"
			class={`mobile-sort-chip ${folderSortKey === 'pending' ? 'active' : ''}`.trim()}
			onclick={() => toggleFolderSort('pending')}
		>
			Pending {folderSortKey === 'pending' ? (folderSortDirection === 'asc' ? '↑' : '↓') : ''}
		</button>
		<button
			type="button"
			class={`mobile-sort-chip ${folderSortKey === 'reclaim' ? 'active' : ''}`.trim()}
			onclick={() => toggleFolderSort('reclaim')}
		>
			Reclaim {folderSortKey === 'reclaim' ? (folderSortDirection === 'asc' ? '↑' : '↓') : ''}
		</button>
		<button
			type="button"
			class={`mobile-sort-chip ${folderSortKey === 'review' ? 'active' : ''}`.trim()}
			onclick={() => toggleFolderSort('review')}
		>
			Review {folderSortKey === 'review' ? (folderSortDirection === 'asc' ? '↑' : '↓') : ''}
		</button>
	</div>

	{#if showInitialLoadingMessage}
		<p class="muted-block">Loading the full folder queue from the active catalog snapshot.</p>
	{:else if folderLoadState === 'error'}
		<div class="table-message error-message">
			<p>Could not load the ranked folder queue.</p>
			<p>{folderLoadError}</p>
		</div>
	{:else if catalogEmpty}
		<div class="table-message">
			<p>No folders are available yet.</p>
			<p>Run a scan or confirm your library configuration in Settings.</p>
		</div>
	{:else if sortedVisibleFolders.length === 0}
		<div class="table-message">
			<p>All visible libraries are currently filtered out.</p>
			<p>Restore one hidden library or choose Show all to resume the queue view.</p>
		</div>
	{:else}
		<div class="table-shell">
			<table>
				<thead>
					<tr>
						<th scope="col" aria-sort={folderSortAria('priority')}>
							<button
								type="button"
								class={`sort-header ${folderSortKey === 'priority' ? 'active' : ''}`.trim()}
								onclick={() => toggleFolderSort('priority')}
							>
								Priority
								<span class="sort-indicator"
									>{folderSortKey === 'priority'
										? folderSortDirection === 'asc'
											? '↑'
											: '↓'
										: '↕'}</span
								>
							</button>
						</th>
						<th scope="col" aria-sort={folderSortAria('title')}>
							<button
								type="button"
								class={`sort-header ${folderSortKey === 'title' ? 'active' : ''}`.trim()}
								onclick={() => toggleFolderSort('title')}
							>
								Folder
								<span class="sort-indicator"
									>{folderSortKey === 'title'
										? folderSortDirection === 'asc'
											? '↑'
											: '↓'
										: '↕'}</span
								>
							</button>
						</th>
						<th scope="col" aria-sort={folderSortAria('library')}>
							<button
								type="button"
								class={`sort-header ${folderSortKey === 'library' ? 'active' : ''}`.trim()}
								onclick={() => toggleFolderSort('library')}
							>
								Library
								<span class="sort-indicator"
									>{folderSortKey === 'library'
										? folderSortDirection === 'asc'
											? '↑'
											: '↓'
										: '↕'}</span
								>
							</button>
						</th>
						<th scope="col" aria-sort={folderSortAria('pending')}>
							<button
								type="button"
								class={`sort-header ${folderSortKey === 'pending' ? 'active' : ''}`.trim()}
								onclick={() => toggleFolderSort('pending')}
							>
								Pending
								<span class="sort-indicator"
									>{folderSortKey === 'pending'
										? folderSortDirection === 'asc'
											? '↑'
											: '↓'
										: '↕'}</span
								>
							</button>
						</th>
						<th scope="col" aria-sort={folderSortAria('reclaim')}>
							<button
								type="button"
								class={`sort-header ${folderSortKey === 'reclaim' ? 'active' : ''}`.trim()}
								onclick={() => toggleFolderSort('reclaim')}
							>
								Reclaim
								<span class="sort-indicator"
									>{folderSortKey === 'reclaim'
										? folderSortDirection === 'asc'
											? '↑'
											: '↓'
										: '↕'}</span
								>
							</button>
						</th>
						<th scope="col" aria-sort={folderSortAria('age')}>
							<button
								type="button"
								class={`sort-header ${folderSortKey === 'age' ? 'active' : ''}`.trim()}
								onclick={() => toggleFolderSort('age')}
							>
								Age
								<span class="sort-indicator"
									>{folderSortKey === 'age'
										? folderSortDirection === 'asc'
											? '↑'
											: '↓'
										: '↕'}</span
								>
							</button>
						</th>
						<th scope="col" aria-sort={folderSortAria('review')}>
							<button
								type="button"
								class={`sort-header ${folderSortKey === 'review' ? 'active' : ''}`.trim()}
								onclick={() => toggleFolderSort('review')}
							>
								Review
								<span class="sort-indicator"
									>{folderSortKey === 'review'
										? folderSortDirection === 'asc'
											? '↑'
											: '↓'
										: '↕'}</span
								>
							</button>
						</th>
						<th scope="col">Status</th>
					</tr>
				</thead>
				<tbody>
					{#each sortedVisibleFolderRows as row (row.folder.prefix)}
						<tr
							class:active-row={row.folder.prefix === activeWorkspacePrefix}
							class:attention-row={folderNeedsAttention(row.folder)}
						>
							<td data-label="Priority">#{row.priority}</td>
							<td data-label="Folder">
								<a class="folder-link" href={resolve(folderRoutePath(row.folder.prefix))}>
									<span class="folder-title">{row.folder.title}</span>
									<span class="folder-meta">{row.folder.subtitle || row.folder.prefix}</span>
								</a>
								{#if folderNeedsAttention(row.folder)}
									<span class="folder-attention-inline">
										<span>Needs attention</span>
										{folderReviewDetail(row.folder) || 'Open folder to inspect the stalled encode.'}
									</span>
								{/if}
							</td>
							<td data-label="Library">
								<span
									class="library-tag"
									style={`--library-chip: ${libraryColors[folderLibraryKey(row.folder.prefix)] ?? '#4b5563'};`}
								>
									<span class="filter-dot"></span>
									{folderLibraryLabel(folderLibraryKey(row.folder.prefix))}
								</span>
								<span class="table-subcopy">{row.folder.scope_label}</span>
							</td>
							<td data-label="Pending">
								<span class="table-value">{row.folder.pending_count} / {row.folder.item_count}</span
								>
								{#if Math.max(row.folder.item_count - row.folder.pending_count, 0) > 0}
									<span class="table-subcopy"
										>{Math.max(row.folder.item_count - row.folder.pending_count, 0)} complete</span
									>
								{/if}
							</td>
							<td data-label="Reclaim">
								<span class="table-value">{formatGiB(row.folder.projected_reclaim_bytes, 1)}</span>
								<span class="table-subcopy"
									>{formatGiB(row.folder.total_size_bytes, 1)} on disk</span
								>
							</td>
							<td data-label="Age">
								<span class="table-value"
									>{Math.max(0, Math.round(row.folder.average_age_days))}d</span
								>
								<span class="table-subcopy">
									{row.folder.average_age_days > 0 ? 'Average item age' : 'Freshly ranked'}
								</span>
							</td>
							<td data-label="Review">
								<span class={`review-tag ${row.folder.review_badge_tone ?? 'neutral'}`.trim()}>
									{row.folder.review_badge_label || 'Ready to inspect'}
								</span>
								<span
									class:attention-detail={folderNeedsAttention(row.folder)}
									class="table-subcopy"
								>
									{folderReviewDetail(row.folder) || row.folder.scope_label}
								</span>
							</td>
							<td data-label="Status">
								<span class="table-value">{formatTopCounts(row.folder.statuses, 3)}</span>
								<span class="table-subcopy"
									>{formatTopCounts(row.folder.video_codecs, 2, 'Codec mix pending')}</span
								>
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{/if}
</section>

<style>
	.station-card {
		padding: 1.1rem;
		min-width: 0;
		width: 100%;
	}

	.table-deck {
		padding-top: 1rem;
		min-width: 0;
		width: 100%;
		overflow: hidden;
	}

	.section-head,
	.filter-row,
	.filter-chip,
	.library-tag {
		display: flex;
	}

	.section-head {
		grid-template-columns: minmax(0, 1fr) auto;
		gap: 1rem;
		align-items: start;
		margin-bottom: 1rem;
	}

	.section-head.compact {
		margin-bottom: 0.85rem;
	}

	.section-title {
		margin-top: 0.3rem;
		font-size: 1.25rem;
		font-weight: 700;
	}

	.section-title.small {
		font-size: 1rem;
	}

	.table-head-meta {
		display: grid;
		gap: 0.2rem;
		justify-items: end;
	}

	.queue-count,
	.filter-hint,
	.table-subcopy,
	.table-message,
	.muted-block {
		color: rgba(203, 213, 225, 0.76);
	}

	.filter-hint.inline {
		font-size: 0.84rem;
		text-align: right;
	}

	.table-toolbar {
		margin-bottom: 0.95rem;
		padding-bottom: 0.95rem;
		border-bottom: 1px solid rgba(148, 163, 184, 0.14);
	}

	.queue-context-bar {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		gap: 0.9rem;
		align-items: start;
		margin-bottom: 0.95rem;
		padding: 0.8rem 0.9rem;
		border: 1px solid rgba(148, 163, 184, 0.16);
		background: rgba(15, 23, 42, 0.62);
	}

	.queue-context-copy,
	.queue-context-actions {
		display: grid;
		gap: 0.28rem;
	}

	.queue-context-label {
		margin: 0;
		font-size: 0.72rem;
		font-weight: 800;
		letter-spacing: 0.12em;
		text-transform: uppercase;
		color: rgba(125, 211, 252, 0.84);
	}

	.queue-context-title {
		margin: 0;
		font-size: 1rem;
		font-weight: 700;
		color: #f8fafc;
	}

	.queue-context-detail {
		margin: 0;
		color: rgba(226, 232, 240, 0.74);
		line-height: 1.45;
	}

	.queue-context-actions {
		grid-auto-flow: column;
		grid-auto-columns: max-content;
		align-content: start;
		gap: 0.65rem;
	}

	.queue-context-link,
	.queue-context-button {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-height: 2.45rem;
		padding: 0.65rem 0.9rem;
		border: 1px solid rgba(148, 163, 184, 0.18);
		background: rgba(9, 14, 22, 0.9);
		font: inherit;
		font-size: 0.84rem;
		font-weight: 700;
		color: #f8fafc;
	}

	.queue-context-button:disabled {
		color: rgba(148, 163, 184, 0.72);
		cursor: not-allowed;
	}

	.mobile-sort-row {
		display: none;
		gap: 0.65rem;
		margin-bottom: 0.95rem;
		flex-wrap: wrap;
	}

	.mobile-sort-chip {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		padding: 0.55rem 0.75rem;
		border: 1px solid rgba(148, 163, 184, 0.18);
		background: rgba(15, 23, 42, 0.62);
		font: inherit;
		font-size: 0.8rem;
		font-weight: 700;
		color: rgba(226, 232, 240, 0.78);
	}

	.mobile-sort-chip.active {
		border-color: rgba(56, 189, 248, 0.38);
		color: #f8fafc;
	}

	.compact-filters {
		align-items: center;
	}

	.filter-row {
		gap: 0.65rem;
		flex-wrap: wrap;
		grid-template-columns: repeat(auto-fit, minmax(10rem, max-content));
		align-items: stretch;
	}

	.filter-chip {
		display: inline-flex;
		gap: 0.5rem;
		padding: 0.7rem 0.85rem;
		border: 1px solid rgba(148, 163, 184, 0.2);
		background: rgba(15, 23, 42, 0.62);
		color: rgba(226, 232, 240, 0.76);
	}

	.filter-chip.active {
		background: rgba(30, 41, 59, 0.88);
		color: #f8fafc;
		border-color: color-mix(in srgb, var(--library-chip, #38bdf8) 58%, rgba(148, 163, 184, 0.35));
	}

	.all-chip.active {
		border-color: rgba(56, 189, 248, 0.45);
	}

	.filter-dot {
		width: 0.55rem;
		height: 0.55rem;
		border-radius: 999px;
		background: var(--library-chip, #38bdf8);
		flex: 0 0 auto;
		align-self: center;
	}

	.filter-count {
		margin-left: auto;
		color: rgba(148, 163, 184, 0.8);
	}

	.empty-shell,
	.table-message,
	.muted-block {
		padding: 0.95rem 1rem;
		border: 1px solid rgba(148, 163, 184, 0.16);
		background: rgba(15, 23, 42, 0.46);
	}

	.table-shell {
		min-width: 0;
		max-width: 100%;
		width: 100%;
		max-height: min(74vh, 64rem);
		overflow-x: auto;
		overflow-y: auto;
		border: 1px solid rgba(148, 163, 184, 0.16);
		background: rgba(9, 14, 22, 0.88);
	}

	table {
		width: 100%;
		min-width: 64rem;
		border-collapse: collapse;
		table-layout: fixed;
	}

	th:nth-child(1),
	td:nth-child(1) {
		width: 7%;
	}

	th:nth-child(2),
	td:nth-child(2) {
		width: 28%;
	}

	th:nth-child(3),
	td:nth-child(3) {
		width: 11%;
	}

	th:nth-child(4),
	td:nth-child(4),
	th:nth-child(5),
	td:nth-child(5),
	th:nth-child(6),
	td:nth-child(6) {
		width: 10%;
	}

	th:nth-child(7),
	td:nth-child(7) {
		width: 13%;
	}

	th:nth-child(8),
	td:nth-child(8) {
		width: 11%;
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

	.sort-header {
		display: inline-flex;
		align-items: center;
		gap: 0.45rem;
		padding: 0;
		border: 0;
		background: transparent;
		color: inherit;
		font: inherit;
		letter-spacing: inherit;
		text-transform: inherit;
	}

	.sort-header.active {
		color: #f8fafc;
	}

	.sort-indicator {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-width: 0.8rem;
		color: rgba(125, 211, 252, 0.9);
	}

	.sort-summary {
		display: block;
		margin-top: 0.15rem;
		font-size: 0.82rem;
		color: rgba(148, 163, 184, 0.84);
	}

	tbody tr {
		background: rgba(11, 15, 20, 0.76);
	}

	tbody tr:nth-child(even) {
		background: rgba(15, 23, 42, 0.82);
	}

	tbody tr.active-row {
		background: rgba(8, 47, 73, 0.34);
	}

	tbody tr.attention-row {
		background: rgba(67, 20, 7, 0.46);
		box-shadow: inset 3px 0 0 rgba(249, 115, 22, 0.72);
	}

	tbody tr.attention-row.active-row {
		background: rgba(88, 36, 10, 0.56);
	}

	.folder-link {
		display: grid;
		gap: 0.18rem;
		color: inherit;
	}

	.folder-title {
		font-weight: 700;
		color: #f8fafc;
		overflow-wrap: anywhere;
	}

	.folder-meta,
	.table-subcopy {
		display: block;
		margin-top: 0.18rem;
		font-size: 0.84rem;
	}

	.folder-attention-inline {
		display: grid;
		gap: 0.18rem;
		margin-top: 0.45rem;
		max-width: 34rem;
		padding: 0.48rem 0.58rem;
		border: 1px solid rgba(249, 115, 22, 0.26);
		background: rgba(67, 20, 7, 0.48);
		color: #fed7aa;
		font-size: 0.82rem;
		font-weight: 700;
		line-height: 1.35;
	}

	.folder-attention-inline span {
		font-size: 0.68rem;
		font-weight: 900;
		letter-spacing: 0.1em;
		text-transform: uppercase;
		color: #fdba74;
	}

	.table-subcopy.attention-detail {
		color: #fed7aa;
		font-weight: 700;
	}

	.library-tag,
	.review-tag {
		display: inline-flex;
		align-items: center;
		gap: 0.45rem;
		padding: 0.34rem 0.56rem;
		font-size: 0.72rem;
		font-weight: 800;
		letter-spacing: 0.04em;
		text-transform: uppercase;
	}

	.library-tag {
		border: 1px solid rgba(148, 163, 184, 0.28);
		color: #e2e8f0;
		background: rgba(30, 41, 59, 0.84);
	}

	.review-tag {
		justify-content: flex-start;
	}

	.review-tag.ok {
		background: rgba(20, 83, 45, 0.84);
		color: #dcfce7;
	}

	.review-tag.warning,
	.review-tag.warn {
		background: rgba(120, 53, 15, 0.84);
		color: #ffedd5;
	}

	.review-tag.attention,
	.review-tag.neutral {
		background: rgba(8, 47, 73, 0.86);
		color: #dbeafe;
	}

	.table-value {
		font-family: 'SFMono-Regular', 'Menlo', monospace;
		letter-spacing: -0.01em;
	}

	.error-message {
		border-color: rgba(249, 115, 22, 0.3);
		background: rgba(67, 20, 7, 0.72);
	}

	@media (max-width: 1100px) {
		.queue-context-bar,
		.section-head {
			grid-template-columns: 1fr;
		}

		.table-head-meta {
			justify-items: start;
		}

		.queue-context-actions {
			grid-auto-flow: row;
			grid-auto-columns: unset;
		}

		.filter-hint.inline {
			text-align: left;
		}
	}

	@media (max-width: 900px) {
		.mobile-sort-row {
			display: flex;
		}

		.filter-row {
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
			min-width: 0;
		}

		td:nth-child(n) {
			width: 100%;
		}

		table {
			min-width: 0;
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
