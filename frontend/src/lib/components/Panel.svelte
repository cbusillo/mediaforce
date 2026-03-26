<script lang="ts">
	import type { Snippet } from 'svelte';

	type Variant = 'default' | 'accent' | 'inset';

	let {
		children,
		variant = 'default',
		padding = '1.45rem',
		class: className = ''
	}: {
		children?: Snippet;
		variant?: Variant;
		padding?: string;
		class?: string;
	} = $props();
</script>

<section class={`panel ${variant} ${className}`.trim()} style={`--panel-padding: ${padding};`}>
	{@render children?.()}
</section>

<style>
	.panel {
		padding: var(--panel-padding);
		border-radius: var(--radius-lg);
		background: var(--surface-1);
		border: 1px solid var(--border);
		box-shadow: var(--shadow-md);
		backdrop-filter: blur(20px);
	}

	.panel.accent {
		background: var(--surface-accent);
	}

	.panel.inset {
		background: var(--surface-3);
	}
</style>
