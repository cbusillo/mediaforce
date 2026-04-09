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
	const reviewBadgeTone = $derived(folder.review_badge_tone ?? 'neutral');
	const progressVisible = $derived(completedCount > 0 && completedCount < folder.item_count);
	const folderStateCopy = $derived.by(() => {
		if (completedCount === 0) {
			return `${folder.item_count} ${folder.item_count === 1 ? 'item' : 'items'} waiting`;
		}
		if (folder.pending_count === 0) {
			return `${folder.item_count} ${folder.item_count === 1 ? 'item' : 'items'} need no more discovery work`;
		}
		return `${folder.pending_count} pending of ${folder.item_count} items`;
	});
	const statusSummaryCopy = $derived.by(() => formatCounts(folder.statuses));
	const statusLineCopy = $derived.by(() => {
		const discoveredCount = Number(folder.statuses?.discovered ?? 0);
		if (discoveredCount === folder.item_count && Object.keys(folder.statuses ?? {}).length === 1) {
			return 'Ready to assess';
		}
		return statusSummaryCopy;
	});
	const cardActionLabel = $derived.by(() => {
		if (folder.review_badge_label) return 'Continue in studio';
		return 'Open studio';
	});
	const folderFacts = $derived.by(() => [
		{ label: 'Estimated reclaim', value: formatGiB(folder.estimated_savings_bytes) },
		{ label: 'Current size', value: formatGiB(folder.total_size_bytes, 2) }
	]);
	const secondaryMetaCopy = $derived.by(() => {
		const parts: string[] = [];
		const videoMix = formatCounts(folder.video_codecs);
		if (videoMix && videoMix !== 'None') parts.push(videoMix);
		if (!folder.details_loading && folder.average_age_days > 0) {
			parts.push(`${folder.average_age_days.toFixed(0)} days old`);
		}
		return parts.join(' · ');
	});
</script>

<a class="folder-link" href={resolve(`/folders/${folder.prefix}`)} style={cardThemeStyle} title={folder.prefix}>
	<Panel class="folder-card" variant="default" padding="1.2rem 1.2rem 1.25rem">
		<div class="card-shell">
			<div class="card-header">
				<div class="badge-row">
					<span class="folder-badge library">{libraryLabel}</span>
					<span class="folder-badge scope">{scopeBadgeLabel}</span>
					{#if folder.review_badge_label}
						<span class={`folder-badge review ${reviewBadgeTone}`.trim()}
							>{folder.review_badge_label}</span
						>
					{/if}
				</div>
			</div>

			<div class="card-body">
					<h3 class="serif">{folder.title}</h3>
					<p class="summary-line">{folderStateCopy}</p>
					{#if progressVisible}
					<div class="progress-cluster" aria-label={`${completionPercent}% complete`}>
					<div class="progress-rail" aria-hidden="true">
						<span class="progress-fill" style={`width: ${completionPercent}%;`}></span>
					</div>
					<p class="progress-copy">{completedCount} complete · {completionPercent}%</p>
				</div>
					{/if}
					<p class="status-line">{statusLineCopy}</p>
					{#if secondaryMetaCopy}
						<p class="secondary-meta-line">{secondaryMetaCopy}</p>
					{/if}
			</div>

			<div class="fact-grid" aria-label={`${folder.title} details`}>
					{#each folderFacts as fact (fact.label)}
						<div class={`fact-card ${fact.label === 'Estimated reclaim' ? 'highlight' : ''}`.trim()}>
							<p class="fact-label">{fact.label}</p>
							<p class="fact-value">{fact.value}</p>
						</div>
					{/each}
				</div>

			<div class="card-footer" aria-hidden="true">
				<span class="card-footer-copy">{cardActionLabel}</span>
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
		grid-template-rows: auto 1fr auto auto;
		gap: 1rem;
		height: 100%;
		padding-left: 0.55rem;
	}

	.card-header {
		display: grid;
		grid-template-rows: auto auto;
		gap: 0.55rem;
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

	.folder-badge.review {
		border-color: rgba(15, 118, 110, 0.18);
		background: rgba(15, 118, 110, 0.09);
		color: var(--accent-deep);
	}

	.folder-badge.review.ok {
		background: rgba(47, 107, 62, 0.12);
		border-color: rgba(47, 107, 62, 0.2);
		color: var(--ok);
	}

	.folder-badge.review.warning {
		background: rgba(194, 65, 12, 0.12);
		border-color: rgba(194, 65, 12, 0.2);
		color: var(--warn);
	}

	.folder-badge.review.attention {
		background: rgba(15, 118, 110, 0.09);
		border-color: rgba(15, 118, 110, 0.18);
		color: var(--accent-deep);
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

	.secondary-meta-line {
		margin: -0.1rem 0 0;
		font-size: 0.8rem;
		line-height: 1.4;
		color: var(--library-soft-text);
	}

	.fact-grid {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: 0.72rem;
		align-self: end;
	}

	.card-footer {
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: 0.75rem;
		padding-top: 0.15rem;
		border-top: 1px solid rgba(23, 35, 31, 0.08);
		color: var(--library-text);
	}

	.card-footer-copy {
		font-size: 0.84rem;
		font-weight: 700;
		letter-spacing: 0.01em;
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

	.fact-card.pending {
		background: rgba(255, 255, 255, 0.48);
	}

	.fact-label,
	.fact-value {
		margin: 0;
	}

	.fact-label {
		font-size: 0.72rem;
		font-weight: 800;
		letter-spacing: 0.01em;
		color: var(--library-text);
	}

	.fact-value {
		font-size: 0.9rem;
		line-height: 1.4;
		font-weight: 600;
		color: var(--ink);
		text-wrap: pretty;
	}

	.fact-card.highlight .fact-value {
		font-size: 1.14rem;
		font-weight: 800;
		color: var(--library-text);
	}

	.fact-card.pending .fact-value {
		color: var(--ink-soft);
		font-weight: 600;
	}

	.fact-skeleton {
		display: block;
		width: 72%;
		height: 1rem;
		border-radius: 999px;
		background: linear-gradient(90deg, rgba(23, 35, 31, 0.08), rgba(23, 35, 31, 0.16), rgba(23, 35, 31, 0.08));
		background-size: 200% 100%;
		animation: fact-skeleton-shift 1.6s ease-in-out infinite;
	}

	.visually-hidden {
		position: absolute;
		width: 1px;
		height: 1px;
		padding: 0;
		margin: -1px;
		overflow: hidden;
		clip: rect(0, 0, 0, 0);
		white-space: nowrap;
		border: 0;
	}

	@keyframes fact-skeleton-shift {
		0% {
			background-position: 100% 0;
		}

		100% {
			background-position: -100% 0;
		}
	}

	@media (max-width: 720px) {
		.fact-grid {
			grid-template-columns: 1fr;
		}
	}
</style>
