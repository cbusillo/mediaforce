<script lang="ts">
	import NoticeCard from '$lib/components/NoticeCard.svelte';
	import { toasts } from '$lib/stores/toasts';
</script>

<div class="toast-stack" aria-live="polite" aria-atomic="false">
	{#each $toasts as toast (toast.id)}
		<div
			class="toast-frame"
			role={toast.kind === 'error' ? 'alert' : 'status'}
			aria-live={toast.kind === 'error' ? 'assertive' : 'polite'}
		>
			<NoticeCard
				kind={toast.kind}
				eyebrow={toast.eyebrow}
				heading={toast.heading}
				lede={toast.lede}
				detail={toast.detail}
				dismissLabel={toast.dismissLabel ?? 'Dismiss'}
				onDismiss={() => toasts.dismiss(toast.id)}
			/>
		</div>
	{/each}
</div>

<style>
	.toast-stack {
		position: fixed;
		top: clamp(1rem, 2vw, 1.35rem);
		right: clamp(1rem, 2vw, 1.35rem);
		z-index: 60;
		display: grid;
		gap: var(--space-2);
		width: min(28rem, calc(100vw - 2rem));
	}

	.toast-frame {
		width: 100%;
	}

	@media (max-width: 640px) {
		.toast-stack {
			left: 1rem;
			right: 1rem;
			width: auto;
		}
	}
</style>
