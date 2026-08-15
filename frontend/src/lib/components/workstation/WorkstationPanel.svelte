<script lang="ts">
	import type { Snippet } from 'svelte';

	let {
		eyebrow,
		title,
		meta,
		hidden = false,
		children
	}: {
		eyebrow?: string;
		title?: string;
		meta?: string;
		hidden?: boolean;
		children: Snippet;
	} = $props();
</script>

<section class="panel" {hidden}>
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
		border-radius: var(--mf-radius-3);
		box-shadow: var(--mf-shadow-popover);
		min-width: 0;
		overflow: hidden;
	}

	.panel__header {
		align-items: center;
		background: var(--mf-bg-panel);
		border-bottom: var(--mf-border-muted);
		display: flex;
		gap: var(--mf-space-4);
		min-height: 48px;
		padding: 0 var(--mf-space-6);
	}

	.panel__header h3 {
		font-size: var(--mf-text-lg);
		font-weight: var(--mf-weight-semibold);
		letter-spacing: -0.01em;
	}

	.panel__meta {
		color: var(--mf-fg-tertiary);
		font-family: var(--mf-font-sans);
		font-size: var(--mf-text-xs);
		margin-left: auto;
	}
</style>
