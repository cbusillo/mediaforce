<script lang="ts">
	type Variant = 'primary' | 'secondary' | 'ghost' | 'danger';

	let {
		variant = 'primary',
		type = 'button',
		disabled = false,
		loading = false,
		onclick,
		children
	}: {
		variant?: Variant;
		type?: 'button' | 'submit';
		disabled?: boolean;
		loading?: boolean;
		onclick?: () => void;
		children?: import('svelte').Snippet;
	} = $props();
</script>

<button class={`button ${variant}`} {type} disabled={disabled || loading} {onclick}>
	{#if loading}Working...{:else}{@render children?.()}{/if}
</button>

<style>
	.button {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: 0.35rem;
		padding: 0.82rem 1.05rem;
		border-radius: var(--radius-md);
		font-weight: 700;
		letter-spacing: -0.01em;
		transition:
			transform 150ms ease,
			opacity 150ms ease,
			background-color 150ms ease,
			border-color 150ms ease,
			box-shadow 150ms ease;
	}

	.button:hover:not(:disabled) {
		transform: translateY(-1px);
	}

	.button:disabled {
		opacity: 0.7;
		cursor: default;
	}

	.primary {
		background: linear-gradient(135deg, var(--accent), #167d73);
		color: white;
		box-shadow: 0 10px 24px rgba(15, 118, 110, 0.18);
	}

	.secondary {
		background: #dcecea;
		color: var(--accent-deep);
	}

	.ghost {
		border: 1px solid var(--border);
		background: rgba(255, 255, 255, 0.64);
		color: var(--ink);
	}

	.danger {
		background: #efe2d8;
		color: #8a4413;
	}
</style>
