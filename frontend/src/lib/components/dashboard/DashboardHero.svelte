<script lang="ts">
	import type { DashboardSummaryPayload } from '$lib/api/types';
	import HeroCard from '$lib/components/HeroCard.svelte';
	import Pill from '$lib/components/Pill.svelte';
	import SectionHead from '$lib/components/SectionHead.svelte';
	import { formatGiB } from '$lib/format';

	type HeroFact = { label: string; value: string };

	let {
		foldersCount,
		totalEstimatedSavings,
		heroFacts,
		metricsReady,
		metricSupport,
		metricStatusCopy
	}: {
		foldersCount: number;
		totalEstimatedSavings: number;
		heroFacts: HeroFact[];
		metricsReady: boolean;
		metricSupport: DashboardSummaryPayload['metric_support'];
		metricStatusCopy: string;
	} = $props();
</script>

<HeroCard>
	{#snippet copy()}
		<SectionHead
			eyebrow="Current Strategy"
			heading={`${foldersCount} folders ready for tuning with ${formatGiB(totalEstimatedSavings, 0)} on the table.`}
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

	{#snippet aside()}
		<p>{metricStatusCopy}</p>
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
		padding: 0.6rem 0.7rem;
		border-radius: var(--radius-md);
		background: rgba(255, 255, 255, 0.52);
		border: 1px solid rgba(23, 35, 31, 0.08);
	}

	.hero-stat-value {
		margin: 0;
		font-size: 1.3rem;
		font-weight: 700;
		color: var(--ink);
	}
</style>
