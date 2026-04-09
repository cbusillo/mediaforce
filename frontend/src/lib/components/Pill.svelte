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
		padding: 0.58rem 0.95rem;
		border-radius: var(--radius-pill);
		font-size: 0.88rem;
		font-weight: 700;
		letter-spacing: 0.01em;
		line-height: 1;
		border: 1px solid rgba(15, 118, 110, 0.2);
		background: rgba(15, 118, 110, 0.12);
		color: var(--accent-deep);
		box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.48);
	}

	.pill.ok {
		background: rgba(47, 107, 62, 0.14);
		border-color: rgba(47, 107, 62, 0.24);
		color: var(--ok);
	}

	.pill.warn {
		background: rgba(194, 65, 12, 0.14);
		border-color: rgba(194, 65, 12, 0.24);
		color: var(--warn);
	}

	.pill.neutral {
		background: rgba(255, 255, 255, 0.78);
		border-color: rgba(23, 35, 31, 0.14);
		color: var(--ink-muted);
	}

	.pill.ghost {
		background: rgba(255, 255, 255, 0.56);
		border-color: rgba(23, 35, 31, 0.1);
		color: var(--ink-soft);
	}

	.pill.wide {
		justify-content: flex-start;
	}
</style>
