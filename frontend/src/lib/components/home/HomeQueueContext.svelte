<script lang="ts">
	import { resolve } from '$app/paths';
	import type { FolderCard as FolderCardData } from '$lib/api/types';
	import { folderLibraryKey, folderLibraryLabel, folderRoutePath } from '$lib/folder-display';
	import { formatGiB } from '$lib/format';

	let {
		activeWorkspaceFolder,
		libraryColors,
		metricsReady,
		metricStatusCopy,
		catalogEmpty,
		activeWorkspaceQueued,
		queueActionDisabled,
		queueActionPending,
		queueActiveWorkspaceFolder,
		formatTopCounts
	}: {
		activeWorkspaceFolder: FolderCardData | null;
		libraryColors: Record<string, string>;
		metricsReady: boolean;
		metricStatusCopy: string;
		catalogEmpty: boolean;
		activeWorkspaceQueued: boolean;
		queueActionDisabled: boolean;
		queueActionPending: boolean;
		queueActiveWorkspaceFolder: () => void;
		formatTopCounts: (counts: Record<string, number>, limit?: number, emptyCopy?: string) => string;
	} = $props();
</script>

<section class="station-card active-workspace" aria-label="Queue context">
	<div class="section-head compact">
		<div>
			<p class="section-label">Queue context</p>
			<h2 class="section-title small">Top-ranked folder</h2>
		</div>
	</div>

	{#if activeWorkspaceFolder}
		<div class="workspace-header">
			<div>
				<div class="workspace-badges">
					<span
						class="workspace-badge library"
						style={`--library-chip: ${libraryColors[folderLibraryKey(activeWorkspaceFolder.prefix)] ?? '#4b5563'};`}
					>
						{folderLibraryLabel(folderLibraryKey(activeWorkspaceFolder.prefix))}
					</span>
					<span class="workspace-badge neutral">{activeWorkspaceFolder.scope_label}</span>
					{#if activeWorkspaceFolder.review_badge_label}
						<span
							class={`workspace-badge review ${activeWorkspaceFolder.review_badge_tone ?? 'neutral'}`.trim()}
						>
							{activeWorkspaceFolder.review_badge_label}
						</span>
					{/if}
				</div>
				<h3 class="workspace-title">{activeWorkspaceFolder.title}</h3>
				<p class="workspace-subtitle">
					{activeWorkspaceFolder.subtitle || activeWorkspaceFolder.prefix}
				</p>
				<p class="workspace-summary">
					{activeWorkspaceFolder.pending_count} pending of {activeWorkspaceFolder.item_count} items. Projected
					reclaim {formatGiB(activeWorkspaceFolder.projected_reclaim_bytes, 1)}.
				</p>
			</div>
		</div>

		<div class="workspace-actions">
			<a class="workspace-primary" href={resolve(folderRoutePath(activeWorkspaceFolder.prefix))}>
				Open folder
			</a>
			<button
				type="button"
				class="workspace-secondary"
				onclick={queueActiveWorkspaceFolder}
				disabled={queueActionDisabled ||
					activeWorkspaceQueued ||
					activeWorkspaceFolder.pending_count <= 0}
			>
				{#if queueActionPending}
					Queueing folder...
				{:else if activeWorkspaceQueued}
					Already queued
				{:else if activeWorkspaceFolder.pending_count <= 0}
					Nothing to queue
				{:else}
					Queue folder
				{/if}
			</button>
		</div>

		<div class="fact-grid compact-grid" aria-label="Active workspace facts">
			<div class="fact-cell highlight">
				<p class="fact-label">Projected reclaim</p>
				<p class="fact-value">{formatGiB(activeWorkspaceFolder.projected_reclaim_bytes, 1)}</p>
			</div>
			<div class="fact-cell">
				<p class="fact-label">Pending</p>
				<p class="fact-value">{activeWorkspaceFolder.pending_count}</p>
			</div>
			<div class="fact-cell">
				<p class="fact-label">Current size</p>
				<p class="fact-value">{formatGiB(activeWorkspaceFolder.total_size_bytes, 1)}</p>
			</div>
			<div class="fact-cell">
				<p class="fact-label">Average age</p>
				<p class="fact-value">{Math.round(activeWorkspaceFolder.average_age_days)} days</p>
			</div>
		</div>

		<div class="signal-grid compact-grid">
			<div class="signal-panel">
				<p class="signal-label">Status mix</p>
				<p class="signal-value">{formatTopCounts(activeWorkspaceFolder.statuses, 4)}</p>
			</div>
			<div class="signal-panel">
				<p class="signal-label">Codec mix</p>
				<p class="signal-value">{formatTopCounts(activeWorkspaceFolder.video_codecs, 3)}</p>
			</div>
			<div class="signal-panel full-span">
				<p class="signal-label">Metric readiness</p>
				<p class="signal-value">{metricsReady ? 'Full metric stack online' : metricStatusCopy}</p>
			</div>
		</div>
	{:else if catalogEmpty}
		<div class="empty-shell">
			<h3 class="workspace-title">No folders ranked yet</h3>
			<p class="workspace-summary">
				The catalog is still empty. Start a library refresh or check Settings before queueing work.
			</p>
		</div>
	{:else}
		<div class="empty-shell">
			<h3 class="workspace-title">Loading queue context</h3>
			<p class="workspace-summary">
				Mediaforce is pulling the full folder payload for the active console view.
			</p>
		</div>
	{/if}
</section>

<style>
	.station-card {
		position: relative;
		padding: 1.1rem;
		border: 1px solid rgba(148, 163, 184, 0.18);
		background: rgba(15, 20, 27, 0.94);
		box-shadow: 0 18px 38px rgba(2, 6, 23, 0.2);
		overflow: hidden;
	}

	.active-workspace::before {
		content: '';
		position: absolute;
		inset: 0 0 auto;
		height: 2px;
		background: linear-gradient(90deg, rgba(56, 189, 248, 0.85), rgba(34, 197, 94, 0.22));
	}

	.section-head,
	.workspace-header,
	.fact-grid,
	.signal-grid {
		display: grid;
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

	.section-label,
	.fact-label,
	.signal-label {
		margin: 0;
		font-size: 0.72rem;
		font-weight: 800;
		letter-spacing: 0.16em;
		text-transform: uppercase;
		color: rgba(148, 163, 184, 0.88);
	}

	.section-title,
	.workspace-title,
	.fact-value {
		margin: 0;
		color: #f8fafc;
	}

	.section-title {
		margin-top: 0.3rem;
		font-size: 1.25rem;
		font-weight: 700;
	}

	.section-title.small {
		font-size: 1rem;
	}

	.workspace-actions,
	.workspace-badges {
		display: flex;
		gap: 0.65rem;
		flex-wrap: wrap;
	}

	.workspace-header {
		grid-template-columns: 1fr;
		gap: 1rem;
		align-items: start;
	}

	.workspace-title {
		margin-top: 0.7rem;
		font-size: clamp(1.18rem, 1.55vw, 1.55rem);
		font-weight: 700;
		line-height: 1.16;
	}

	.workspace-subtitle,
	.workspace-summary {
		margin: 0;
		color: rgba(226, 232, 240, 0.74);
		line-height: 1.5;
	}

	.workspace-subtitle {
		margin-top: 0.55rem;
		font-size: 0.95rem;
	}

	.workspace-summary {
		margin-top: 0.8rem;
		max-width: 38rem;
	}

	.workspace-actions {
		justify-content: flex-start;
	}

	.workspace-primary,
	.workspace-secondary {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: 0.45rem;
		min-height: 2.6rem;
		padding: 0.72rem 1rem;
		border: 1px solid rgba(148, 163, 184, 0.2);
		font-weight: 700;
		color: #f8fafc;
		transition:
			transform 150ms ease,
			border-color 150ms ease,
			background 150ms ease;
	}

	.workspace-primary {
		background: rgba(8, 47, 73, 0.92);
		border-color: rgba(56, 189, 248, 0.45);
	}

	.workspace-secondary {
		background: rgba(15, 23, 42, 0.72);
	}

	.workspace-primary:hover,
	.workspace-secondary:hover {
		transform: translateY(-1px);
		border-color: rgba(125, 211, 252, 0.48);
	}

	.workspace-secondary:disabled {
		transform: none;
		cursor: not-allowed;
		opacity: 0.64;
	}

	.workspace-badge {
		display: inline-flex;
		align-items: center;
		gap: 0.45rem;
		padding: 0.34rem 0.56rem;
		border: 1px solid rgba(148, 163, 184, 0.28);
		background: rgba(30, 41, 59, 0.84);
		color: #e2e8f0;
		font-size: 0.72rem;
		font-weight: 800;
		letter-spacing: 0.04em;
		text-transform: uppercase;
	}

	.workspace-badge.library {
		border-color: color-mix(in srgb, var(--library-chip) 58%, rgba(148, 163, 184, 0.35));
	}

	.workspace-badge.library::before {
		content: '';
		width: 0.55rem;
		height: 0.55rem;
		border-radius: 999px;
		background: var(--library-chip);
	}

	.workspace-badge.review.ok {
		background: rgba(20, 83, 45, 0.84);
		color: #dcfce7;
	}

	.workspace-badge.review.warning,
	.workspace-badge.review.warn {
		background: rgba(120, 53, 15, 0.84);
		color: #ffedd5;
	}

	.workspace-badge.review.attention,
	.workspace-badge.review.neutral {
		background: rgba(8, 47, 73, 0.86);
		color: #dbeafe;
	}

	.fact-grid,
	.signal-grid {
		grid-template-columns: repeat(4, minmax(0, 1fr));
		gap: 0.75rem;
		margin-top: 1rem;
	}

	.fact-grid.compact-grid {
		grid-template-columns: repeat(2, minmax(0, 1fr));
	}

	.signal-grid {
		grid-template-columns: repeat(3, minmax(0, 1fr));
		margin-top: 0.75rem;
	}

	.signal-grid.compact-grid {
		grid-template-columns: repeat(2, minmax(0, 1fr));
	}

	.signal-panel.full-span {
		grid-column: 1 / -1;
	}

	.fact-cell,
	.signal-panel,
	.empty-shell {
		padding: 0.85rem 0.9rem;
		border: 1px solid rgba(148, 163, 184, 0.18);
		background: rgba(15, 23, 42, 0.54);
	}

	.fact-cell.highlight {
		background: rgba(8, 47, 73, 0.82);
		border-color: rgba(56, 189, 248, 0.36);
	}

	.fact-value {
		margin-top: 0.45rem;
		font-size: 1.1rem;
		font-weight: 700;
		font-family: 'SFMono-Regular', 'Menlo', monospace;
		letter-spacing: -0.01em;
	}

	.signal-value {
		margin: 0.45rem 0 0;
		color: rgba(241, 245, 249, 0.92);
		line-height: 1.5;
	}

	@media (max-width: 1100px) {
		.section-head,
		.workspace-header {
			grid-template-columns: 1fr;
		}

		.workspace-actions {
			justify-content: flex-start;
		}
	}

	@media (max-width: 720px) {
		.fact-grid,
		.signal-grid {
			grid-template-columns: 1fr;
		}
	}
</style>
