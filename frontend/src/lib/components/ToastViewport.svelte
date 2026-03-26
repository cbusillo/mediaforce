<script lang="ts">
	import { toasts } from '$lib/stores/toasts';
</script>

<div class="toast-stack">
	{#each $toasts as toast (toast.id)}
		<div class={`toast ${toast.kind}`}>
			<div>
				<p class="eyebrow-copy toast-title">{toast.title}</p>
				<p class="toast-body">{toast.body}</p>
			</div>
			<button aria-label="Dismiss notification" onclick={() => toasts.remove(toast.id)}>×</button>
		</div>
	{/each}
</div>

<style>
	.toast-stack {
		position: fixed;
		top: 1.2rem;
		right: 1.2rem;
		z-index: 50;
		display: grid;
		gap: var(--space-2);
		width: min(380px, calc(100vw - 2rem));
	}

	.toast {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		gap: var(--space-2);
		align-items: start;
		padding: 0.95rem 1rem;
		border-radius: 1.1rem;
		border: 1px solid var(--border);
		background: rgba(255, 252, 246, 0.97);
		box-shadow: var(--shadow-md);
		backdrop-filter: blur(20px);
	}

	.toast.success {
		border-color: rgba(47, 107, 62, 0.2);
	}

	.toast.error {
		border-color: rgba(194, 65, 12, 0.24);
	}

	.toast-title {
		margin-bottom: 0.2rem;
	}

	.toast-body {
		color: var(--ink-muted);
		line-height: 1.45;
	}

	button {
		width: 2rem;
		height: 2rem;
		border-radius: 999px;
		color: var(--ink-soft);
	}
</style>
