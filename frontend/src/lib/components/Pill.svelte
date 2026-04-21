<script lang="ts">
	type Variant = 'default' | 'ok' | 'warn' | 'fail' | 'neutral' | 'ghost';

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
		padding: 0.38rem 0.7rem;
		border-radius: var(--radius-pill);
		font-size: 0.78rem;
		font-weight: 700;
		letter-spacing: 0.02em;
		line-height: 1;
		text-transform: uppercase;
		border: 1px solid rgba(44, 137, 217, 0.22);
		background: rgba(15, 24, 32, 0.92);
		color: var(--accent-deep);
	}

	.pill.ok {
		background: rgba(19, 59, 40, 0.92);
		border-color: rgba(55, 166, 107, 0.28);
		color: var(--ok);
	}

	.pill.warn {
		background: rgba(76, 41, 14, 0.94);
		border-color: rgba(214, 132, 54, 0.28);
		color: var(--warn);
	}

	.pill.fail {
		background: rgba(70, 28, 24, 0.94);
		border-color: rgba(208, 92, 79, 0.28);
		color: var(--fail);
	}

	.pill.neutral {
		background: rgba(27, 36, 45, 0.94);
		border-color: rgba(192, 204, 216, 0.14);
		color: var(--ink-muted);
	}

	.pill.ghost {
		background: rgba(20, 26, 32, 0.7);
		border-color: rgba(192, 204, 216, 0.1);
		color: var(--ink-soft);
	}

	.pill.wide {
		justify-content: flex-start;
	}
</style>
