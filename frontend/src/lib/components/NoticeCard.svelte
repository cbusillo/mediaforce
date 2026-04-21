<script lang="ts">
	import Panel from '$lib/components/Panel.svelte';
	import SectionHead from '$lib/components/SectionHead.svelte';
	import type { ToastKind } from '$lib/stores/toasts';

	let {
		kind,
		eyebrow,
		heading,
		lede,
		detail,
		dismissLabel = 'Dismiss',
		onDismiss
	}: {
		kind: ToastKind;
		eyebrow?: string;
		heading: string;
		lede: string;
		detail?: string;
		dismissLabel?: string;
		onDismiss?: (() => void) | undefined;
	} = $props();

	const resolvedEyebrow = $derived.by(() => {
		if (eyebrow?.trim()) return eyebrow.trim();
		if (kind === 'success') return 'All Set';
		if (kind === 'info') return 'Heads Up';
		if (kind === 'warning') return 'Check This';
		return 'Needs Attention';
	});
	const cardClassName = $derived(`notice-card ${kind}`);
</script>

<Panel class={cardClassName} padding="1rem 1.05rem 0.95rem">
	<div class="notice-layout">
		<SectionHead eyebrow={resolvedEyebrow} {heading} {lede} size="compact" serif={false} />
		<button
			type="button"
			class="dismiss-button"
			aria-label={dismissLabel}
			onclick={() => onDismiss?.()}
		>
			{dismissLabel}
		</button>
	</div>
	{#if detail}
		<p class="notice-detail">{detail}</p>
	{/if}
</Panel>

<style>
	:global(.notice-card) {
		position: relative;
		isolation: isolate;
		overflow: hidden;
		border-color: rgba(148, 163, 184, 0.22);
		background:
			linear-gradient(135deg, rgba(20, 26, 32, 0.96), rgba(10, 15, 21, 0.98)), var(--surface-1);
		box-shadow: 0 22px 44px rgba(0, 0, 0, 0.32);
		backdrop-filter: blur(24px);
	}

	:global(.notice-card)::before {
		content: '';
		position: absolute;
		inset: 0 auto 0 0;
		width: 5px;
		border-radius: 999px;
		background: var(--notice-edge, rgba(145, 205, 253, 0.62));
	}

	:global(.notice-card)::after {
		content: '';
		position: absolute;
		inset: 0 0 auto;
		height: 1px;
		background: linear-gradient(
			90deg,
			transparent,
			var(--notice-glint, rgba(145, 205, 253, 0.36)),
			transparent
		);
		pointer-events: none;
	}

	:global(.notice-card.success) {
		--notice-edge: rgba(55, 166, 107, 0.72);
		--notice-glint: rgba(55, 166, 107, 0.42);
		border-color: rgba(55, 166, 107, 0.28);
		background:
			linear-gradient(135deg, rgba(20, 36, 29, 0.98), rgba(10, 15, 21, 0.98)), var(--surface-1);
	}

	:global(.notice-card.info) {
		--notice-edge: rgba(145, 205, 253, 0.72);
		--notice-glint: rgba(145, 205, 253, 0.44);
		border-color: rgba(145, 205, 253, 0.26);
		background:
			linear-gradient(135deg, rgba(13, 32, 45, 0.98), rgba(10, 15, 21, 0.98)), var(--surface-1);
	}

	:global(.notice-card.warning) {
		--notice-edge: rgba(214, 132, 54, 0.78);
		--notice-glint: rgba(214, 132, 54, 0.44);
		border-color: rgba(214, 132, 54, 0.3);
		background:
			linear-gradient(135deg, rgba(42, 27, 13, 0.98), rgba(10, 15, 21, 0.98)), var(--surface-1);
	}

	:global(.notice-card.error) {
		--notice-edge: rgba(208, 92, 79, 0.82);
		--notice-glint: rgba(208, 92, 79, 0.46);
		border-color: rgba(208, 92, 79, 0.32);
		background:
			linear-gradient(135deg, rgba(46, 18, 16, 0.98), rgba(10, 15, 21, 0.98)), var(--surface-1);
	}

	:global(.notice-card.success .eyebrow-copy) {
		color: var(--ok);
	}

	:global(.notice-card.info .eyebrow-copy) {
		color: var(--accent-deep);
	}

	:global(.notice-card.warning .eyebrow-copy) {
		color: var(--warn);
	}

	:global(.notice-card.error .eyebrow-copy) {
		color: var(--fail);
	}

	.notice-layout {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		gap: 0.75rem;
		align-items: start;
	}

	.notice-detail {
		margin: 0.2rem 0 0;
		padding-left: 0.05rem;
		font-size: 0.94rem;
		line-height: 1.5;
		color: var(--ink-muted);
	}

	.dismiss-button {
		min-width: 2.45rem;
		border-radius: var(--radius-pill);
		padding: 0.42rem 0.78rem;
		border: 1px solid rgba(148, 163, 184, 0.26);
		background: rgba(15, 23, 42, 0.62);
		font-size: 0.78rem;
		font-weight: 700;
		color: var(--ink-muted);
	}

	.dismiss-button:hover {
		border-color: rgba(145, 205, 253, 0.42);
		background: rgba(30, 41, 59, 0.72);
		color: var(--ink);
	}

	@media (max-width: 640px) {
		.notice-layout {
			grid-template-columns: minmax(0, 1fr);
		}

		.dismiss-button {
			justify-self: start;
		}
	}
</style>
