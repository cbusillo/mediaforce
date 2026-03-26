<script lang="ts">
	import Panel from '$lib/components/Panel.svelte';
	import SectionHead from '$lib/components/SectionHead.svelte';

	let {
		eyebrow,
		heading,
		lede,
		variant = 'default',
		detail
	}: {
		eyebrow: string;
		heading: string;
		lede: string;
		variant?: 'default' | 'accent' | 'error';
		detail?: string;
	} = $props();
</script>

<Panel
	variant={variant === 'accent' ? 'accent' : 'default'}
	class={variant === 'error' ? 'error-banner' : ''}
	padding="1.25rem 1.35rem"
>
	<div class="banner-stack">
		<SectionHead {eyebrow} {heading} {lede} />
		{#if detail}
			<p class="muted-copy">{detail}</p>
		{/if}
	</div>
</Panel>

<style>
	.banner-stack {
		display: grid;
		gap: var(--space-2);
	}

	:global(.error-banner) {
		border-color: rgba(194, 65, 12, 0.24);
		background: rgba(194, 65, 12, 0.07);
	}

	:global(.error-banner) :global(.eyebrow-copy) {
		color: var(--warn);
	}
</style>
