<script lang="ts">
	import type { EncodeQueueJob, DashboardSummaryPayload } from '$lib/api/types';
	import Button from '$lib/components/Button.svelte';
	import Panel from '$lib/components/Panel.svelte';
	import SectionHead from '$lib/components/SectionHead.svelte';

	type QueueAction = 'pause' | 'resume' | 'stop';
	type QueueCard = { eyebrow: string; heading: string; lede: string };
	type EncodeQueueStatus = { label: string; tone: 'attention' | 'neutral' | 'ok' };

	let {
		queueCards,
		pendingReviewCount,
		calibrationQueueAction,
		calibrationQueueHasWork,
		stopCalibrationQueue,
		encodeQueueStatus,
		queueAction,
		dashboard,
		readyHosts,
		encodeCapableHosts,
		reachableHosts,
		runQueueAction,
		encodeQueueEtaCopy,
		encodeRunningJobs
	}: {
		queueCards: QueueCard[];
		pendingReviewCount: number;
		calibrationQueueAction: string | null;
		calibrationQueueHasWork: boolean;
		stopCalibrationQueue: () => Promise<void>;
		encodeQueueStatus: EncodeQueueStatus;
		queueAction: string | null;
		dashboard: DashboardSummaryPayload;
		readyHosts: number;
		encodeCapableHosts: number;
		reachableHosts: number;
		runQueueAction: (action: QueueAction) => Promise<void>;
		encodeQueueEtaCopy: string | null;
		encodeRunningJobs: EncodeQueueJob[];
	} = $props();
</script>

<div class="queue-grid">
	{#each queueCards as card, index (card.eyebrow)}
		<Panel variant={index === 1 ? 'accent' : 'default'}>
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
					<div class="action-row">
						<Button
							variant="danger"
							loading={calibrationQueueAction === 'stop'}
							disabled={!calibrationQueueHasWork}
							onclick={stopCalibrationQueue}>Stop + Clean</Button
						>
					</div>
				{:else}
					<div class="queue-pill-row">
						<span class={`queue-pill ${encodeQueueStatus.tone}`.trim()}
							>{encodeQueueStatus.label}</span
						>
						{#if dashboard.encode_queue.state.stop_requested}
							<span class="queue-pill attention">Stop requested</span>
						{/if}
						<a class="queue-link-pill" href="#remote-hosts">Workers ready: {readyHosts}</a>
						{#if readyHosts < encodeCapableHosts}
							<span class="queue-pill neutral">{reachableHosts} mounted</span>
						{/if}
					</div>
					<div class="action-row">
						{#if dashboard.encode_queue.state.is_paused}
							<Button
								variant="primary"
								loading={queueAction === 'resume'}
								onclick={() => runQueueAction('resume')}>Resume Queue</Button
							>
						{:else}
							<Button
								variant="primary"
								loading={queueAction === 'pause'}
								onclick={() => runQueueAction('pause')}>Pause Queue</Button
							>
						{/if}
						<Button
							variant="danger"
							loading={queueAction === 'stop'}
							disabled={(!dashboard.encode_queue.running_count &&
								!dashboard.encode_queue.queued_count) ||
								dashboard.encode_queue.state.stop_requested}
							onclick={() => runQueueAction('stop')}>Stop + Clean</Button
						>
					</div>
					{#if encodeQueueEtaCopy}
						<p class="queue-telemetry-note muted-copy">
							Estimated queue finish in {encodeQueueEtaCopy} at the current fleet pace.
						</p>
					{/if}
					{#if encodeRunningJobs.length > 0}
						<div class="encode-telemetry-list" aria-label="Running encode telemetry">
							{#each encodeRunningJobs as job (job.job_id)}
								<div class="encode-telemetry-row">
									<div>
										<p class="encode-telemetry-title">{job.prefix}</p>
										<p class="muted-copy encode-telemetry-detail">
											{String(job.host?.label ?? job.host?.key ?? 'Worker')}
											{#if job.progress?.current_item_rel_path}
												· {job.progress.current_item_rel_path}
											{/if}
										</p>
									</div>
									<p class="encode-telemetry-summary">
										{job.telemetry_summary || job.scheduler_status_copy || 'Running now'}
									</p>
								</div>
							{/each}
						</div>
					{/if}
				{/if}
			</div>
		</Panel>
	{/each}
</div>

<style>
	.panel-stack {
		display: grid;
		gap: var(--space-3);
	}

	.queue-grid {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: var(--space-4);
	}

	.queue-pill-row {
		display: flex;
		flex-wrap: wrap;
		gap: 0.65rem;
		align-items: center;
	}

	.queue-pill,
	.queue-link-pill {
		display: inline-flex;
		align-items: center;
		gap: 0.35rem;
		padding: 0.42rem 0.72rem;
		border-radius: var(--radius-pill);
		font-size: 0.78rem;
		font-weight: 700;
		letter-spacing: 0.03em;
		text-transform: uppercase;
	}

	.queue-pill.attention {
		background: rgba(180, 83, 9, 0.14);
		color: #9a4b0b;
	}

	.queue-pill.ok {
		background: rgba(15, 118, 110, 0.14);
		color: #0f5f59;
	}

	.queue-pill.neutral,
	.queue-link-pill {
		background: rgba(23, 35, 31, 0.08);
		color: var(--ink-soft);
	}

	.queue-link-pill {
		text-decoration: none;
	}

	.action-row {
		display: flex;
		flex-wrap: wrap;
		gap: 0.75rem;
	}

	.queue-telemetry-note,
	.encode-telemetry-detail,
	.encode-telemetry-title,
	.encode-telemetry-summary {
		margin: 0;
	}

	.encode-telemetry-list {
		display: grid;
		gap: 0.75rem;
	}

	.encode-telemetry-row {
		display: flex;
		justify-content: space-between;
		gap: 1rem;
		padding: 0.8rem 0.9rem;
		border-radius: var(--radius-md);
		background: rgba(255, 255, 255, 0.5);
		border: 1px solid rgba(23, 35, 31, 0.08);
	}

	.encode-telemetry-title {
		font-weight: 700;
		color: var(--ink);
	}

	.encode-telemetry-summary {
		font-weight: 600;
		color: var(--accent-deep);
		text-align: right;
	}

	@media (max-width: 860px) {
		.queue-grid {
			grid-template-columns: 1fr;
		}

		.encode-telemetry-row {
			flex-direction: column;
		}

		.encode-telemetry-summary {
			text-align: left;
		}
	}
</style>
