<script lang="ts">
	import Panel from '$lib/components/Panel.svelte';
	import type { Snippet } from 'svelte';

	let {
		copy,
		meta,
		aside,
		padding = '1.6rem 1.75rem'
	}: {
		copy?: Snippet;
		meta?: Snippet;
		aside?: Snippet;
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
	{/if}
</Panel>

<style>
	:global(.hero-card) {
		display: grid;
		grid-template-columns: minmax(0, 1.15fr) minmax(180px, 240px) minmax(240px, 0.85fr);
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

	.meta-block {
		align-content: start;
	}

	.aside-block {
		color: var(--ink-muted);
		font-size: 0.98rem;
		line-height: 1.55;
		padding: 1rem 1.05rem;
		border-radius: var(--radius-md);
		background: rgba(255, 255, 255, 0.52);
		border: 1px solid rgba(23, 35, 31, 0.08);
	}

	@media (max-width: 980px) {
		:global(.hero-card) {
			grid-template-columns: 1fr;
			gap: var(--space-3);
		}
	}
</style>
