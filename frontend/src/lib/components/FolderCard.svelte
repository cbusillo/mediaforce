<script lang="ts">
	import { resolve } from '$app/paths';
	import Panel from '$lib/components/Panel.svelte';
	import type { FolderCard as FolderCardData } from '$lib/api/types';
	import { formatCounts, formatGiB } from '$lib/format';
	import {
		folderLibraryKey,
		folderLibraryLabel,
		folderLibraryThemeStyle,
		folderScopeLabel
	} from '$lib/folder-display';

	let {
		folder,
		libraryColor = undefined
	}: {
		folder: FolderCardData;
		libraryColor?: string;
	} = $props();

	const libraryKey = $derived(folderLibraryKey(folder.prefix));
	const libraryLabel = $derived(folderLibraryLabel(libraryKey));
	const scopeBadgeLabel = $derived(folderScopeLabel(folder.prefix, folder.scope_label));
	const completedCount = $derived(Math.max(folder.item_count - folder.pending_count, 0));
	const completionPercent = $derived(
		folder.item_count > 0 ? Math.round((completedCount / folder.item_count) * 100) : 0
	);
	const cardThemeStyle = $derived(folderLibraryThemeStyle(libraryColor));
	const folderFacts = $derived.by(() => [
		{ label: 'Estimated reclaim', value: formatGiB(folder.estimated_savings_bytes) },
		{ label: 'Current size', value: formatGiB(folder.total_size_bytes, 2) },
		{
			label: 'Avg age',
			value: folder.details_loading ? 'Calculating' : `${folder.average_age_days.toFixed(0)} days`
		},
		{ label: 'Video mix', value: formatCounts(folder.video_codecs) }
	]);
</script>

<a class="folder-link" href={resolve(`/folders/${folder.prefix}`)} style={cardThemeStyle}>
	<Panel class="folder-card" variant="default" padding="1.2rem 1.2rem 1.25rem">
		<div class="card-shell">
			<div class="card-header">
				<div class="badge-row">
					<span class="folder-badge library">{libraryLabel}</span>
					<span class="folder-badge scope">{scopeBadgeLabel}</span>
				</div>
				<p class="path-line">{folder.prefix}</p>
			</div>

			<div class="card-body">
				<h3 class="serif">{folder.title}</h3>
				<p class="summary-line">{folder.pending_count} pending of {folder.item_count} items</p>
				{#if folder.details_loading}
					<p class="loading-line">Ranking details are still loading.</p>
				{/if}
				<div class="progress-cluster" aria-label={`${completionPercent}% complete`}>
					<div class="progress-rail" aria-hidden="true">
						<span class="progress-fill" style={`width: ${completionPercent}%;`}></span>
					</div>
					<p class="progress-copy">{completedCount} complete · {completionPercent}%</p>
				</div>
				<p class="status-line">Statuses: {formatCounts(folder.statuses)}</p>
			</div>

			<div class="fact-grid" aria-label={`${folder.title} details`}>
				{#each folderFacts as fact (fact.label)}
					<div class={`fact-card ${fact.label === 'Estimated reclaim' ? 'highlight' : ''}`.trim()}>
						<p class="fact-label">{fact.label}</p>
						<p class="fact-value">{fact.value}</p>
					</div>
				{/each}
			</div>
		</div>
	</Panel>
</a>

<style>
	.folder-link {
		display: block;
		height: 100%;
	}

	:global(.folder-card) {
		height: 100%;
		position: relative;
		overflow: hidden;
		transition:
			transform 180ms ease,
			box-shadow 180ms ease,
			border-color 180ms ease;
	}

	:global(.folder-card)::before {
		content: '';
		position: absolute;
		inset: 1rem auto 1rem 0;
		width: 4px;
		border-radius: var(--radius-pill);
		background: linear-gradient(180deg, var(--library-base), var(--library-glow));
	}

	.folder-link:hover :global(.folder-card) {
		transform: translateY(-3px);
		box-shadow: var(--shadow-lg);
		border-color: var(--border-strong);
	}

	.card-shell {
		display: grid;
		grid-template-rows: auto 1fr auto;
		gap: 1rem;
		height: 100%;
		padding-left: 0.55rem;
	}

	.card-header {
		display: grid;
		grid-template-rows: auto auto;
		gap: 0.55rem;
	}

	.path-line {
		margin: 0;
		font-size: 0.8rem;
		line-height: 1.35;
		color: var(--library-soft-text);
		max-width: min(20rem, 100%);
		word-break: break-word;
	}

	.badge-row {
		display: inline-flex;
		gap: 0.45rem;
		flex-wrap: wrap;
		justify-content: flex-start;
		min-height: 2rem;
	}

	.folder-badge {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		padding: 0.4rem 0.72rem;
		border-radius: var(--radius-pill);
		font-size: 0.76rem;
		font-weight: 800;
		letter-spacing: 0.02em;
		line-height: 1;
		border: 1px solid var(--library-border);
	}

	.folder-badge.library {
		background: var(--library-base);
		border-color: color-mix(in srgb, var(--library-base) 80%, white);
		box-shadow: 0 10px 24px var(--library-glow);
		color: #f8fcfb;
	}

	.folder-badge.scope {
		background: var(--library-surface);
		color: var(--library-text);
	}

	.card-body {
		display: grid;
		align-content: start;
		gap: 0.55rem;
	}

	h3 {
		margin: 0;
		font-size: 1.65rem;
		line-height: 1.05;
		text-wrap: balance;
	}

	.summary-line {
		margin: 0;
		font-size: 0.98rem;
		font-weight: 700;
		color: var(--ink-soft);
	}

	.loading-line {
		margin: 0;
		font-size: 0.78rem;
		font-weight: 700;
		color: var(--library-soft-text);
	}

	.progress-cluster {
		display: grid;
		gap: 0.32rem;
	}

	.progress-rail {
		height: 0.26rem;
		border-radius: 999px;
		background: rgba(23, 35, 31, 0.08);
		overflow: hidden;
	}

	.progress-fill {
		display: block;
		height: 100%;
		border-radius: inherit;
		background: linear-gradient(
			90deg,
			var(--library-base),
			color-mix(in srgb, var(--library-base) 68%, white)
		);
	}

	.progress-copy {
		margin: 0;
		font-size: 0.76rem;
		font-weight: 700;
		color: var(--library-soft-text);
	}

	.status-line {
		margin: 0;
		font-size: 0.88rem;
		line-height: 1.45;
		color: var(--ink-soft);
	}

	.fact-grid {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: 0.72rem;
		align-self: end;
	}

	.fact-card {
		display: grid;
		gap: 0.2rem;
		padding: 0.72rem 0.82rem;
		border-radius: var(--radius-md);
		background: rgba(255, 255, 255, 0.56);
		border: 1px solid rgba(23, 35, 31, 0.08);
	}

	.fact-card.highlight {
		background: linear-gradient(180deg, var(--library-surface), rgba(255, 255, 255, 0.62));
		border-color: var(--library-border);
	}

	.fact-label,
	.fact-value {
		margin: 0;
	}

	.fact-label {
		font-size: 0.72rem;
		font-weight: 800;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--library-text);
	}

	.fact-value {
		font-size: 0.95rem;
		line-height: 1.4;
		font-weight: 650;
		color: var(--ink);
		text-wrap: pretty;
	}

	@media (max-width: 720px) {
		.fact-grid {
			grid-template-columns: 1fr;
		}
	}
</style>
