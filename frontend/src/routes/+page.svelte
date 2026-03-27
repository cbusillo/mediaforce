<script lang="ts">
	import type { DashboardPayload, HostsPayload } from '$lib/api/types';
	import { invalidateAll } from '$app/navigation';
	import { resolve } from '$app/paths';
	import { postJson } from '$lib/api/client';
	import Button from '$lib/components/Button.svelte';
	import FolderCard from '$lib/components/FolderCard.svelte';
	import { formatGiB } from '$lib/format';
	import HeroCard from '$lib/components/HeroCard.svelte';
	import HostCard from '$lib/components/HostCard.svelte';
	import Panel from '$lib/components/Panel.svelte';
	import Pill from '$lib/components/Pill.svelte';
	import SectionHead from '$lib/components/SectionHead.svelte';
	import { toasts } from '$lib/stores/toasts';

	let {
		data
	}: {
		data: {
			dashboard: DashboardPayload;
			hosts: HostsPayload;
		};
	} = $props();

	const dashboard = $derived(data.dashboard);
	const hosts = $derived(data.hosts);
	const totalEstimatedSavings = $derived.by(() =>
		dashboard.folders.reduce((total, folder) => total + folder.estimated_savings_bytes, 0)
	);
	const foldersPending = $derived.by(() =>
		dashboard.folders.reduce((total, folder) => total + folder.pending_count, 0)
	);
	const encodeCapableHosts = $derived.by(
		() => hosts.hosts.filter((host) => host.capabilities.includes('encode_queue')).length
	);
	const readyHosts = $derived.by(() => hosts.hosts.filter((host) => host.queue_active).length);
	const reachableHosts = $derived.by(() => hosts.hosts.filter((host) => host.available).length);
	const pendingReviewCount = $derived.by(
		() =>
			dashboard.calibration_queue.sample.pending_review_count +
			dashboard.calibration_queue.full.pending_review_count
	);
	const heroFacts = $derived.by(() => [
		{ label: 'Top folders', value: String(dashboard.folders.length) },
		{ label: 'Pending items', value: String(foldersPending) },
		{ label: 'Potential reclaim', value: formatGiB(totalEstimatedSavings, 1) }
	]);
	const metricsReady = $derived(
		dashboard.metric_support.vmaf && dashboard.metric_support.xpsnr && dashboard.metric_support.ssim
	);

	const queueCards = $derived.by(() => [
		{
			eyebrow: 'Calibration',
			heading: `${dashboard.calibration_queue.sample.running_count + dashboard.calibration_queue.full.running_count} running · ${dashboard.calibration_queue.sample.queued_count + dashboard.calibration_queue.full.queued_count} queued`,
			lede: 'Sample calibrations and proof encodes stay separate so you can tune before committing to a full folder run.'
		},
		{
			eyebrow: 'Encode Queue',
			heading: `${dashboard.encode_queue.running_count} running · ${dashboard.encode_queue.queued_count} queued`,
			lede: 'Queue health, worker availability, and controls for whole-folder runs.'
		}
	]);

	let queueAction = $state<string | null>(null);

	async function runQueueAction(action: 'pause' | 'resume' | 'stop') {
		queueAction = action;
		try {
			const response = await postJson<{ message: string }>(`/api/encode-queue/${action}`, {});
			toasts.success('Queue updated', response.message);
			await invalidateAll();
		} catch (error) {
			toasts.error(
				'Queue update failed',
				error instanceof Error ? error.message : 'Unexpected queue error'
			);
		} finally {
			queueAction = null;
		}
	}
</script>

<svelte:head>
	<title>Folders · Mediaforce</title>
</svelte:head>

<div class="page-stack">
	<HeroCard>
		{#snippet copy()}
			<SectionHead
				eyebrow="Current Strategy"
				heading={`${dashboard.folders.length} folders ready for tuning with ${formatGiB(totalEstimatedSavings, 0)} on the table.`}
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
							label={`VMAF ${dashboard.metric_support.vmaf ? 'ready' : 'missing'}`}
							variant={dashboard.metric_support.vmaf ? 'ok' : 'warn'}
						/>
						<Pill
							label={`XPSNR ${dashboard.metric_support.xpsnr ? 'ready' : 'missing'}`}
							variant={dashboard.metric_support.xpsnr ? 'ok' : 'warn'}
						/>
						<Pill
							label={`SSIM ${dashboard.metric_support.ssim ? 'ready' : 'missing'}`}
							variant={dashboard.metric_support.ssim ? 'ok' : 'warn'}
						/>
					{/if}
				</div>
			</div>
		{/snippet}

		{#snippet aside()}
			<p>{dashboard.metric_status_copy}</p>
		{/snippet}
	</HeroCard>

	<div class="queue-grid">
		{#each queueCards as card, index (card.eyebrow)}
			<Panel variant={index === 0 ? 'accent' : 'default'}>
				<div class="panel-stack">
					<SectionHead
						eyebrow={card.eyebrow}
						heading={card.heading}
						lede={card.lede}
						size="compact"
					/>
					{#if index === 0}
						{#if pendingReviewCount > 0}
							<div class="queue-pill-row">
								<span class="queue-pill attention">Pending review: {pendingReviewCount}</span>
							</div>
						{/if}
					{:else}
						<div class="queue-pill-row">
							<span
								class={`queue-pill ${dashboard.encode_queue.state.is_paused ? 'neutral' : 'ok'}`.trim()}
							>
								{dashboard.encode_queue.state.is_paused ? 'Paused' : 'Running'}
							</span>
							{#if dashboard.encode_queue.state.stop_requested}
								<span class="queue-pill attention">Stop requested</span>
							{/if}
							<a class="queue-link-pill" href="#remote-hosts">
								Workers ready: {readyHosts}
							</a>
							{#if readyHosts < encodeCapableHosts}
								<span class="queue-pill neutral">{reachableHosts} mounted</span>
							{/if}
						</div>
						<div class="action-row">
							<Button
								variant="secondary"
								loading={queueAction === 'pause'}
								disabled={dashboard.encode_queue.state.is_paused}
								onclick={() => runQueueAction('pause')}>Pause Queue</Button
							>
							<Button
								variant="primary"
								loading={queueAction === 'resume'}
								disabled={!dashboard.encode_queue.state.is_paused}
								onclick={() => runQueueAction('resume')}>Resume Queue</Button
							>
							<Button
								variant="danger"
								loading={queueAction === 'stop'}
								onclick={() => runQueueAction('stop')}>Stop + Clean</Button
							>
						</div>
					{/if}
				</div>
			</Panel>
		{/each}
	</div>

	<Panel variant="inset" class="folder-section" padding="1.2rem 1.3rem 1.4rem">
		<div class="section-stack">
			<div class="section-header-row">
				<SectionHead
					eyebrow="Remote Hosts"
					heading="Where encodes can run"
					lede="Worker availability stays close to the queue controls so scheduling decisions are easy to read."
					size="compact"
				/>
				<div class="section-header-tools">
					<span class="section-summary-chip">{readyHosts} ready</span>
					<a class="section-action-link" href={resolve('/settings')}> Manage in Settings </a>
				</div>
			</div>
			<div id="remote-hosts" class="host-grid">
				{#each hosts.hosts as host (host.key)}
					<HostCard {host} />
				{/each}
			</div>
		</div>
	</Panel>

	<Panel variant="inset" class="folder-section" padding="1.2rem 1.3rem 1.4rem">
		<div class="section-stack">
			<div class="section-header-row">
				<SectionHead
					eyebrow="Folders"
					heading="Candidate folders"
					lede="Sorted by estimated reclaim so the strongest space-saving bets stay first."
					size="compact"
				/>
				<p class="section-kicker muted-copy">Open a folder to start representative-file tuning.</p>
			</div>
			<div class="folder-grid">
				{#each dashboard.folders as folder (folder.prefix)}
					<FolderCard {folder} />
				{/each}
			</div>
		</div>
	</Panel>
</div>

<style>
	.page-stack,
	.panel-stack,
	.section-stack {
		display: grid;
		gap: var(--space-3);
	}

	.queue-grid {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: var(--space-4);
	}

	.folder-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
		gap: var(--space-4);
	}

	.host-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
		gap: var(--space-4);
	}

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

	.queue-pill-row {
		display: flex;
		flex-wrap: wrap;
		gap: 0.65rem;
		align-items: center;
	}

	.queue-pill,
	.queue-link-pill,
	.section-summary-chip {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		padding: 0.48rem 0.82rem;
		border-radius: var(--radius-pill);
		font-size: 0.84rem;
		font-weight: 700;
		line-height: 1;
		border: 1px solid rgba(23, 35, 31, 0.1);
		background: rgba(255, 255, 255, 0.78);
		color: var(--ink-soft);
	}

	.queue-pill.ok,
	.section-summary-chip {
		background: rgba(47, 107, 62, 0.12);
		border-color: rgba(47, 107, 62, 0.18);
		color: var(--ok);
	}

	.queue-pill.attention {
		background: rgba(180, 83, 9, 0.12);
		border-color: rgba(180, 83, 9, 0.18);
		color: #9a5b00;
	}

	.queue-pill.neutral {
		background: rgba(255, 255, 255, 0.76);
		border-color: rgba(23, 35, 31, 0.12);
		color: var(--ink-soft);
	}

	.queue-link-pill,
	.section-action-link {
		text-decoration: none;
		transition:
			transform 150ms ease,
			border-color 150ms ease,
			background-color 150ms ease,
			color 150ms ease;
	}

	.queue-link-pill {
		background: rgba(15, 118, 110, 0.09);
		border-color: rgba(15, 118, 110, 0.18);
		color: var(--accent-deep);
	}

	.queue-link-pill:hover,
	.section-action-link:hover {
		transform: translateY(-1px);
	}

	.section-header-tools {
		display: flex;
		align-items: center;
		justify-content: flex-end;
		gap: 0.7rem;
		flex-wrap: wrap;
	}

	.section-action-link {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		padding: 0.48rem 0.82rem;
		border-radius: var(--radius-pill);
		border: 1px solid rgba(23, 35, 31, 0.1);
		background: rgba(255, 255, 255, 0.72);
		color: var(--ink-soft);
		font-size: 0.84rem;
		font-weight: 700;
	}

	.hero-stat-row :global(.eyebrow-copy) {
		font-size: 0.72rem;
	}

	.hero-stat-value {
		font-size: 1.02rem;
		font-weight: 700;
		line-height: 1.2;
		text-align: right;
	}

	.action-row {
		display: flex;
		gap: var(--space-2);
		flex-wrap: wrap;
	}

	.section-header-row {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		gap: var(--space-3);
		align-items: end;
		padding-bottom: 0.95rem;
		border-bottom: 1px solid rgba(23, 35, 31, 0.08);
	}

	.section-kicker {
		max-width: 18rem;
		font-size: 0.88rem;
		line-height: 1.45;
		text-align: right;
	}

	@media (max-width: 860px) {
		.queue-grid {
			grid-template-columns: 1fr;
		}

		.section-header-row {
			grid-template-columns: 1fr;
			align-items: start;
		}

		.section-header-tools {
			justify-content: flex-start;
		}

		.section-kicker {
			max-width: none;
			text-align: left;
		}
	}

	@media (max-width: 720px) {
		.folder-grid,
		.host-grid {
			grid-template-columns: 1fr;
		}
	}
</style>
