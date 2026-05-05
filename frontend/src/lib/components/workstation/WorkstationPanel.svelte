<script lang="ts">
	import type { Snippet } from 'svelte';

	let {
		eyebrow,
		title,
		meta,
		children
	}: {
		eyebrow?: string;
		title?: string;
		meta?: string;
		children: Snippet;
	} = $props();
</script>

<section class="panel">
	{#if eyebrow || title || meta}
		<header class="panel__header">
			{#if eyebrow && !title}
				<span class="mf-eyebrow">{eyebrow}</span>
			{/if}
			{#if title}
				<h3>{title}</h3>
			{/if}
			{#if meta}
				<span class="panel__meta">{meta}</span>
			{/if}
		</header>
	{/if}
	{@render children()}
</section>

<style>
	.panel {
		background: var(--mf-bg-panel);
		border: var(--mf-border);
		min-width: 0;
		overflow: hidden;
	}

	.panel__header {
		align-items: center;
		border-bottom: var(--mf-border);
		display: flex;
		gap: var(--mf-space-4);
		min-height: 38px;
		padding: 0 var(--mf-space-5);
	}

	.panel__header h3 {
		font-size: var(--mf-text-md);
	}

	.panel__meta {
		color: var(--mf-fg-tertiary);
		font-family: var(--mf-font-mono);
		font-size: var(--mf-text-xs);
		margin-left: auto;
	}
</style>
