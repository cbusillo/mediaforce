<script lang="ts">
	type Variant = 'primary' | 'secondary' | 'ghost' | 'danger' | 'approve';

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
		padding: 0.72rem 0.92rem;
		border-radius: var(--radius-md);
		font-weight: 700;
		line-height: 1.2;
		min-height: 2.9rem;
		text-align: center;
		letter-spacing: 0;
		border: 1px solid transparent;
		transition:
			opacity 150ms ease,
			background-color 150ms ease,
			border-color 150ms ease,
			box-shadow 150ms ease;
	}

	.button:hover:not(:disabled) {
		box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.04) inset;
	}

	.button:disabled {
		cursor: default;
		box-shadow: none;
		opacity: 1;
		color: rgba(183, 193, 203, 0.64);
	}

	.primary {
		background: #1e6fb8;
		border-color: rgba(145, 205, 253, 0.3);
		color: #f4f9fd;
	}

	.primary:disabled {
		background: rgba(44, 137, 217, 0.2);
		border-color: rgba(145, 205, 253, 0.12);
		color: rgba(244, 249, 253, 0.54);
	}

	.approve {
		background: rgba(24, 84, 54, 0.96);
		border-color: rgba(55, 166, 107, 0.28);
		color: #e7f7ee;
	}

	.approve:disabled {
		background: rgba(24, 84, 54, 0.32);
		border-color: rgba(55, 166, 107, 0.12);
		color: rgba(231, 247, 238, 0.56);
	}

	.secondary {
		background: rgba(32, 43, 53, 0.96);
		border-color: rgba(192, 204, 216, 0.18);
		color: var(--ink);
	}

	.secondary:disabled,
	.ghost:disabled,
	.danger:disabled {
		border-color: rgba(192, 204, 216, 0.12);
		background: rgba(20, 26, 32, 0.58);
	}

	.ghost {
		border-color: var(--border);
		background: rgba(20, 26, 32, 0.78);
		color: var(--ink-muted);
	}

	.danger {
		background: #5a261f;
		border-color: rgba(208, 92, 79, 0.24);
		color: #ffe8e3;
	}
</style>
