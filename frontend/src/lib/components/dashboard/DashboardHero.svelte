<script lang="ts">
	import type { DashboardSummaryPayload } from '$lib/api/types';
	import HeroCard from '$lib/components/HeroCard.svelte';
	import Pill from '$lib/components/Pill.svelte';
	import SectionHead from '$lib/components/SectionHead.svelte';
	import { formatGiB } from '$lib/format';

	type HeroFact = { label: string; value: string };

	let {
		foldersCount,
		totalProjectedReclaim,
		heroFacts,
		metricsReady,
		metricSupport,
		metricStatusCopy
	}: {
		foldersCount: number;
		totalProjectedReclaim: number;
		heroFacts: HeroFact[];
		metricsReady: boolean;
		metricSupport: DashboardSummaryPayload['metric_support'];
		metricStatusCopy: string;
	} = $props();
</script>

<HeroCard asideText={metricStatusCopy}>
	{#snippet copy()}
		<SectionHead
			eyebrow="Current Strategy"
			heading={`${foldersCount} folders ready for tuning with ${formatGiB(totalProjectedReclaim, 0)} projected.`}
			lede="Start with the biggest reclaim, validate with a sample, then send the full folder only when the draft looks right."
			size="display"
		/>
	{/snippet}

	{#snippet meta()}
		<div class="hero-support-column">
			<div class="hero-stat-list" aria-label="Strategy summary">
				{#each heroFacts as fact (fact.label)}
					<div class="hero-stat-row">
						<p class="eyebrow-copy">{fact.label}</p>
						<p class="hero-stat-value">{fact.value}</p>
					</div>
				{/each}
			</div>
			<div class="pill-column">
				{#if metricsReady}
					<Pill label="All metrics ready" variant="ok" wide />
				{:else}
					<Pill
						label={`VMAF ${metricSupport.vmaf ? 'ready' : 'missing'}`}
						variant={metricSupport.vmaf ? 'ok' : 'warn'}
					/>
					<Pill
						label={`XPSNR ${metricSupport.xpsnr ? 'ready' : 'missing'}`}
						variant={metricSupport.xpsnr ? 'ok' : 'warn'}
					/>
					<Pill
						label={`SSIM ${metricSupport.ssim ? 'ready' : 'missing'}`}
						variant={metricSupport.ssim ? 'ok' : 'warn'}
					/>
				{/if}
			</div>
		</div>
	{/snippet}
</HeroCard>

<style>
	.pill-column {
		display: grid;
		gap: var(--space-2);
		align-content: start;
	}

	.hero-support-column {
		display: grid;
		gap: var(--space-3);
		align-content: start;
	}

	.hero-stat-list {
		display: grid;
		gap: 0.65rem;
	}

	.hero-stat-row {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
		gap: 0.8rem;
		padding: 0.72rem 0.8rem;
		border-radius: var(--radius-md);
		background: rgba(255, 255, 255, 0.64);
		border: 1px solid rgba(15, 118, 110, 0.1);
		box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.5);
	}

	.hero-stat-row:nth-child(1) {
		border-left: 4px solid rgba(15, 118, 110, 0.55);
	}

	.hero-stat-row:nth-child(2) {
		border-left: 4px solid rgba(79, 111, 166, 0.55);
	}

	.hero-stat-row:nth-child(3) {
		border-left: 4px solid rgba(161, 98, 7, 0.55);
	}

	.hero-stat-value {
		margin: 0;
		font-size: 1.3rem;
		font-weight: 700;
		color: var(--ink);
	}
</style>
