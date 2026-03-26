<script lang="ts">
	let {
		eyebrow,
		heading,
		lede,
		serif = true,
		size = 'section'
	}: {
		eyebrow: string;
		heading: string;
		lede?: string;
		serif?: boolean;
		size?: 'display' | 'section' | 'compact';
	} = $props();
</script>

<header class="section-head">
	<p class="eyebrow-copy">{eyebrow}</p>
	<h2 class:serif class:display={size === 'display'} class:compact={size === 'compact'}>
		{heading}
	</h2>
	{#if lede}
		<p class="lede-copy">{lede}</p>
	{/if}
</header>

<style>
	.section-head {
		display: grid;
		gap: var(--space-2);
	}

	h2 {
		font-size: clamp(1.8rem, 2.6vw, 2.35rem);
		line-height: 1.06;
	}

	h2.display {
		font-size: clamp(2.4rem, 4.4vw, 4rem);
	}

	h2.compact {
		font-size: clamp(1.25rem, 2vw, 1.65rem);
		letter-spacing: -0.02em;
	}
</style>
