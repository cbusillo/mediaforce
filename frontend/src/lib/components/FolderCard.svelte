<script lang="ts">
	import { resolve } from '$app/paths';
	import Panel from '$lib/components/Panel.svelte';
	import type { FolderCard as FolderCardData } from '$lib/api/types';
	import { formatCounts, formatGiB } from '$lib/format';

	let { folder }: { folder: FolderCardData } = $props();
</script>

<a class="folder-link" href={resolve(`/folders/${folder.prefix}`)}>
	<Panel class="folder-card" variant="default" padding="1.2rem 1.2rem 1.25rem">
		<div class="topline">
			<span>{folder.prefix}</span>
			<span>{folder.scope_label}</span>
		</div>
		<h3 class="serif">{folder.title}</h3>
		<p class="muted-copy">{folder.pending_count} pending of {folder.item_count} items</p>
		<p class="emphasis">Estimated reclaim: {formatGiB(folder.estimated_savings_bytes)}</p>
		<p class="muted-copy">
			Current size: {formatGiB(folder.total_size_bytes, 2)} · avg age {folder.average_age_days.toFixed(
				0
			)} days
		</p>
		<p class="muted-copy">Statuses: {formatCounts(folder.statuses)}</p>
		<p class="muted-copy">Video: {formatCounts(folder.video_codecs)}</p>
	</Panel>
</a>

<style>
	.folder-link {
		display: block;
	}

	:global(.folder-card) {
		display: grid;
		gap: var(--space-2);
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
		background: linear-gradient(180deg, var(--accent), rgba(15, 118, 110, 0.25));
	}

	.folder-link:hover :global(.folder-card) {
		transform: translateY(-3px);
		box-shadow: var(--shadow-lg);
		border-color: var(--border-strong);
	}

	.topline {
		display: flex;
		justify-content: space-between;
		gap: var(--space-2);
		flex-wrap: wrap;
		padding-left: 0.55rem;
		font-size: 0.82rem;
		color: var(--ink-soft);
	}

	h3 {
		font-size: 1.65rem;
		padding-left: 0.55rem;
		line-height: 1.05;
	}

	.emphasis {
		padding-left: 0.55rem;
		font-weight: 700;
	}

	p {
		padding-left: 0.55rem;
	}
</style>
