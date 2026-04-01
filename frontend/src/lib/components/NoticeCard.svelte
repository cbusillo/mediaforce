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
		background:
			radial-gradient(circle at top right, rgba(255, 255, 255, 0.82), transparent 34%),
			rgba(255, 252, 246, 0.97);
		box-shadow: 0 22px 48px rgba(45, 30, 20, 0.16);
		backdrop-filter: blur(26px);
	}

	:global(.notice-card)::before {
		content: '';
		position: absolute;
		inset: 0 auto 0 0;
		width: 5px;
		border-radius: 999px;
		background: var(--notice-edge, rgba(15, 118, 110, 0.38));
	}

	:global(.notice-card.success) {
		--notice-edge: rgba(47, 107, 62, 0.58);
		border-color: rgba(47, 107, 62, 0.26);
		background:
			radial-gradient(circle at top right, rgba(222, 247, 228, 0.88), transparent 38%),
			rgba(247, 255, 249, 0.98);
	}

	:global(.notice-card.info) {
		--notice-edge: rgba(15, 118, 110, 0.5);
		border-color: rgba(15, 118, 110, 0.24);
		background:
			radial-gradient(circle at top right, rgba(214, 247, 239, 0.84), transparent 38%),
			rgba(246, 255, 252, 0.98);
	}

	:global(.notice-card.error) {
		--notice-edge: rgba(194, 65, 12, 0.62);
		border-color: rgba(194, 65, 12, 0.28);
		background:
			radial-gradient(circle at top right, rgba(255, 228, 217, 0.92), transparent 38%),
			rgba(255, 247, 242, 0.98);
	}

	:global(.notice-card.success)::after {
		background: linear-gradient(90deg, transparent, rgba(47, 107, 62, 0.18), transparent);
	}

	:global(.notice-card.info)::after {
		background: linear-gradient(90deg, transparent, rgba(15, 118, 110, 0.18), transparent);
	}

	:global(.notice-card.error)::after {
		background: linear-gradient(90deg, transparent, rgba(194, 65, 12, 0.18), transparent);
	}

	:global(.notice-card.success .eyebrow-copy) {
		color: var(--ok);
	}

	:global(.notice-card.info .eyebrow-copy) {
		color: var(--accent-deep);
	}

	:global(.notice-card.error .eyebrow-copy) {
		color: var(--warn);
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
		border: 1px solid var(--border-strong);
		background: rgba(255, 255, 255, 0.76);
		font-size: 0.78rem;
		font-weight: 700;
		color: var(--ink-soft);
	}

	.dismiss-button:hover {
		border-color: var(--border-focus);
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
