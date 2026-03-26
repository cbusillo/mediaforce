<script lang="ts">
	type Variant = 'default' | 'ok' | 'warn' | 'neutral' | 'ghost';

	let {
		label,
		variant = 'default',
		wide = false
	}: {
		label: string;
		variant?: Variant;
		wide?: boolean;
	} = $props();
</script>

<span class={`pill ${variant} ${wide ? 'wide' : ''}`.trim()}>{label}</span>

<style>
	.pill {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		padding: 0.55rem 0.9rem;
		border-radius: var(--radius-pill);
		font-size: 0.9rem;
		line-height: 1;
		border: 1px solid rgba(15, 118, 110, 0.18);
		background: rgba(15, 118, 110, 0.09);
		color: var(--accent-deep);
	}

	.pill.ok {
		background: rgba(47, 107, 62, 0.12);
		border-color: rgba(47, 107, 62, 0.2);
		color: var(--ok);
	}

	.pill.warn {
		background: rgba(194, 65, 12, 0.12);
		border-color: rgba(194, 65, 12, 0.2);
		color: var(--warn);
	}

	.pill.neutral {
		background: rgba(255, 255, 255, 0.72);
		border-color: rgba(23, 35, 31, 0.12);
		color: var(--ink-muted);
	}

	.pill.ghost {
		background: rgba(255, 255, 255, 0.48);
		border-color: rgba(23, 35, 31, 0.08);
		color: var(--ink-soft);
	}

	.pill.wide {
		justify-content: flex-start;
	}
</style>
