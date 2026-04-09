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
		gap: 0.8rem;
	}

	.section-head :global(.eyebrow-copy) {
		display: inline-flex;
		align-items: center;
		gap: 0.55rem;
	}

	.section-head :global(.eyebrow-copy)::before {
		content: '';
		width: 1.35rem;
		height: 1px;
		background: currentColor;
		opacity: 0.8;
	}

	h2 {
		font-size: clamp(1.8rem, 2.6vw, 2.3rem);
		line-height: 1.04;
		font-weight: 700;
		text-wrap: balance;
	}

	h2.display {
		font-size: clamp(2.45rem, 4.3vw, 3.55rem);
	}

	h2.compact {
		font-size: clamp(1.28rem, 1.9vw, 1.7rem);
		letter-spacing: -0.02em;
	}

	.section-head :global(.lede-copy) {
		max-width: 52ch;
		color: var(--ink-muted);
	}
</style>
