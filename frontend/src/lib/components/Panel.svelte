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
		position: relative;
		overflow: clip;
		isolation: isolate;
	}

	.panel.accent {
		background: var(--surface-accent);
	}

	.panel.inset {
		background: var(--surface-3);
	}

	.panel::after {
		content: '';
		position: absolute;
		inset: auto 0 0;
		height: 1px;
		background: linear-gradient(90deg, transparent, rgba(15, 118, 110, 0.12), transparent);
		pointer-events: none;
	}

	.panel::before {
		content: '';
		position: absolute;
		inset: 0;
		pointer-events: none;
		background:
			radial-gradient(circle at top left, rgba(255, 255, 255, 0.55), transparent 30%),
			linear-gradient(180deg, rgba(255, 255, 255, 0.26), transparent 28%);
		opacity: 0.8;
	}
</style>
