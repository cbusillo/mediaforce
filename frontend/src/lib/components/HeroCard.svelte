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
		grid-template-columns: minmax(0, 1.3fr) auto minmax(260px, 0.9fr);
		gap: var(--space-5);
		align-items: start;
	}

	.copy-block,
	.meta-block,
	.aside-block {
		display: grid;
		gap: var(--space-3);
	}

	.meta-block {
		align-content: center;
	}

	.aside-block {
		color: var(--ink-muted);
		font-size: 1rem;
		line-height: 1.55;
	}

	@media (max-width: 980px) {
		:global(.hero-card) {
			grid-template-columns: 1fr;
		}
	}
</style>
