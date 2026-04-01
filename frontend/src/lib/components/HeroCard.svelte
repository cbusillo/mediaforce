<script lang="ts">
	import Panel from '$lib/components/Panel.svelte';
	import type { Snippet } from 'svelte';

	let {
		copy,
		meta,
		aside,
		asideText,
		padding = '1.6rem 1.75rem'
	}: {
		copy?: Snippet;
		meta?: Snippet;
		aside?: Snippet;
		asideText?: string;
		padding?: string;
	} = $props();
</script>

<Panel class="hero-card" {padding}>
	<div class="copy-block">{@render copy?.()}</div>
	{#if meta}
		<div class="meta-block">{@render meta()}</div>
	{/if}
	{#if aside}
		<aside class="aside-block">{@render aside()}</aside>
	{:else if asideText}
		<aside class="aside-block">
			<p>{asideText}</p>
		</aside>
	{/if}
</Panel>

<style>
	:global(.hero-card) {
		display: grid;
		grid-template-columns: minmax(0, 1.15fr) minmax(180px, 240px) minmax(240px, 0.85fr);
		grid-template-areas: 'copy meta aside';
		gap: var(--space-4);
		align-items: start;
		background: var(--surface-spotlight);
	}

	.copy-block,
	.meta-block,
	.aside-block {
		display: grid;
		gap: var(--space-3);
	}

	.copy-block {
		grid-area: copy;
	}

	.meta-block {
		grid-area: meta;
		align-content: start;
	}

	.aside-block {
		grid-area: aside;
		color: var(--ink-muted);
		font-size: 0.98rem;
		line-height: 1.55;
		padding: 1rem 1.05rem;
		border-radius: var(--radius-md);
		background: rgba(255, 255, 255, 0.52);
		border: 1px solid rgba(23, 35, 31, 0.08);
	}

	@media (max-width: 1180px) {
		:global(.hero-card) {
			grid-template-columns: minmax(0, 1fr) minmax(240px, 300px);
			grid-template-areas:
				'copy meta'
				'aside meta';
		}
	}

	@media (max-width: 760px) {
		:global(.hero-card) {
			grid-template-columns: 1fr;
			grid-template-areas:
				'copy'
				'meta'
				'aside';
			gap: var(--space-3);
		}
	}
</style>
