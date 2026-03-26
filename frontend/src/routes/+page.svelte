<script lang="ts">
	import type { DashboardPayload, HostsPayload } from '$lib/api/types';
	import { invalidateAll } from '$app/navigation';
	import { postJson } from '$lib/api/client';
	import Button from '$lib/components/Button.svelte';
	import FolderCard from '$lib/components/FolderCard.svelte';
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

	const queueCards = $derived.by(() => [
		{
			eyebrow: 'Calibration',
			heading: `${dashboard.calibration_queue.sample.running_count + dashboard.calibration_queue.full.running_count} running · ${dashboard.calibration_queue.sample.queued_count + dashboard.calibration_queue.full.queued_count} queued`,
			lede: 'Sample calibrations and proof encodes stay separate so you can tune before committing to a full folder run.'
		},
		{
			eyebrow: 'Encode Queue',
			heading: `${dashboard.encode_queue.running_count} running · ${dashboard.encode_queue.queued_count} queued`,
			lede:
				dashboard.encode_queue.state.scheduler_summary ?? 'Whole-folder queue controls and status.'
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
	<title>Folders · Media Harness</title>
</svelte:head>

<div class="page-stack">
	<HeroCard>
		{#snippet copy()}
			<SectionHead
				eyebrow="Current Strategy"
				heading="Slightly prefer smaller files when the visual difference is hard to spot."
				lede="Start with a folder, generate hard-scene samples, and tune the profile before the real batch."
				size="display"
			/>
		{/snippet}

		{#snippet meta()}
			<div class="pill-column">
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
						<p class="muted-copy">
							Pending review: {dashboard.calibration_queue.sample.pending_review_count +
								dashboard.calibration_queue.full.pending_review_count}
						</p>
					{:else}
						<p class="muted-copy">
							State: {dashboard.encode_queue.state.is_paused
								? 'paused'
								: 'running'}{#if dashboard.encode_queue.state.stop_requested}
								· stop requested{/if}
						</p>
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

	<div class="section-stack">
		<SectionHead
			eyebrow="Folders"
			heading="Start with the biggest wins"
			lede="Pick a library slice and move from candidate folders into representative-file tuning."
			size="section"
		/>
		<div class="folder-grid">
			{#each dashboard.folders as folder (folder.prefix)}
				<FolderCard {folder} />
			{/each}
		</div>
	</div>

	<Panel>
		<div class="panel-stack">
			<SectionHead
				eyebrow="Remote Hosts"
				heading="Where encodes can run"
				lede="Keep worker status visible, but secondary to the folder decision-making flow."
				size="section"
			/>
			<div class="host-grid">
				{#each hosts.hosts as host (host.key)}
					<HostCard {host} />
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
		gap: var(--space-4);
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
	}

	.action-row {
		display: flex;
		gap: var(--space-2);
		flex-wrap: wrap;
	}

	@media (max-width: 860px) {
		.queue-grid {
			grid-template-columns: 1fr;
		}
	}
</style>
