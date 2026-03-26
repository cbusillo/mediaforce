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
		padding: 0.92rem 1.18rem;
		border-radius: var(--radius-md);
		font-weight: 700;
		transition:
			transform 150ms ease,
			opacity 150ms ease,
			background-color 150ms ease,
			border-color 150ms ease;
	}

	.button:hover:not(:disabled) {
		transform: translateY(-1px);
	}

	.button:disabled {
		opacity: 0.7;
		cursor: default;
	}

	.primary {
		background: var(--accent);
		color: white;
	}

	.secondary {
		background: #d9ebe8;
		color: var(--accent-deep);
	}

	.ghost {
		border: 1px solid var(--border);
		background: transparent;
		color: var(--ink);
	}

	.danger {
		background: #efe2d8;
		color: #8a4413;
	}
</style>
