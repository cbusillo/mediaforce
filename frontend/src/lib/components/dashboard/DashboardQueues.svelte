<script lang="ts">
	import { resolve } from '$app/paths';
	import type { EncodeQueueJob, DashboardSummaryPayload } from '$lib/api/types';
	import Button from '$lib/components/Button.svelte';
	import Panel from '$lib/components/Panel.svelte';
	import SectionHead from '$lib/components/SectionHead.svelte';

	type QueueAction = 'pause' | 'resume' | 'retry' | 'stop';
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
		encodeRunningJobs,
		queueWorkersScheduledOffWindow,
		nextQueueWindowCopy
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
		queueWorkersScheduledOffWindow: boolean;
		nextQueueWindowCopy: string | null;
	} = $props();

	function attentionTimestampMs(job: EncodeQueueJob): number {
		const raw = job.last_failure_at ?? job.finished_at ?? null;
		if (!raw) {
			return 0;
		}
		const parsed = new Date(raw);
		return Number.isNaN(parsed.getTime()) ? 0 : parsed.getTime();
	}

	const recentAttentionJobs = $derived.by(() =>
		[...(dashboard.encode_queue.recent ?? [])]
			.filter((job) => job.status === 'needs_attention')
			.sort((left, right) => attentionTimestampMs(right) - attentionTimestampMs(left))
			.slice(0, 3)
	);
	const blockersSummary = $derived.by(() => {
		if (!recentAttentionJobs.length) return 'No recent blockers';
		if (recentAttentionJobs.length === 1) return '1 recent blocker';
		return `${recentAttentionJobs.length} recent blockers`;
	});
	const blockersLeadCopy = $derived.by(() => {
		if (!recentAttentionJobs.length) return 'Open details';
		if (!encodeQueueHasWork) return 'Review blocker';
		return 'Open details';
	});
	const runningTelemetrySummary = $derived.by(() => {
		if (!encodeRunningJobs.length) return 'No live encode detail';
		if (encodeRunningJobs.length === 1) return '1 live encode';
		return `${encodeRunningJobs.length} live encodes`;
	});
	const encodeQueueHasWork = $derived.by(
		() => dashboard.encode_queue.running_count > 0 || dashboard.encode_queue.queued_count > 0
	);
	const encodeQueueIdle = $derived.by(
		() =>
			!encodeQueueHasWork &&
			!dashboard.encode_queue.needs_attention_count &&
			!dashboard.encode_queue.state.stop_requested
	);
	const encodeQueueActionable = $derived.by(
		() =>
			encodeQueueHasWork ||
			Number(dashboard.encode_queue.needs_attention_count ?? 0) > 0 ||
			dashboard.encode_queue.state.is_paused ||
			dashboard.encode_queue.state.stop_requested
	);
	const encodeQueueGuidanceCopy = $derived.by(() => {
		if (encodeQueueIdle) {
			return 'Fleet is idle. Queue a folder from the main workflow when you want the next full run to start.';
		}
		if (
			dashboard.encode_queue.running_count === 0 &&
			dashboard.encode_queue.queued_count > 0 &&
			readyHosts === 0 &&
			queueWorkersScheduledOffWindow
		) {
			return nextQueueWindowCopy
				? `Queued now. Next worker window opens at ${nextQueueWindowCopy}.`
				: 'Queued now. Mediaforce is waiting for the next worker schedule window to open.';
		}
		if (
			!encodeQueueHasWork &&
			Number(dashboard.encode_queue.needs_attention_count ?? 0) > 0 &&
			!dashboard.encode_queue.state.stop_requested
		) {
			return 'No encodes are running right now. Review the recent blockers below before you queue the next folder.';
		}
		return '';
	});
	const encodeQueueActionHelp = $derived.by(() => {
		if (!encodeQueueActionable) return '';
		if (Number(dashboard.encode_queue.needs_attention_count ?? 0) > 0) {
			return 'Retry All Failed requeues approved failed folders. Anything that still needs review stays blocked.';
		}
		if (dashboard.encode_queue.state.is_paused) {
			return 'Resume keeps the queued folders in place. Stop + Clean clears the queue.';
		}
		if (encodeQueueHasWork || dashboard.encode_queue.state.stop_requested) {
			return 'Pause is reversible. Stop + Clean clears the queue and active work for these folders.';
		}
		return '';
	});

	function formatAttentionTimestamp(job: EncodeQueueJob): string | null {
		const timestamp = attentionTimestampMs(job);
		if (!timestamp) {
			return null;
		}
		return new Intl.DateTimeFormat(undefined, {
			month: 'short',
			day: 'numeric',
			hour: 'numeric',
			minute: '2-digit'
		}).format(new Date(timestamp));
	}

	function attentionReason(job: EncodeQueueJob): string {
		const failureKind = String(job.last_failure_kind ?? '').trim().replaceAll('_', ' ');
		const rawError = String(job.error ?? '').trim();
		const errorTail = rawError.includes('Error:')
			? rawError.slice(rawError.lastIndexOf('Error:'))
			: rawError.split(/\r?\n/).filter(Boolean).at(-1) ?? rawError;
		const error = errorTail.replace(/\s+/g, ' ').trim();
		const compactError = error.length > 180 ? `${error.slice(0, 177)}...` : error;
		if (failureKind && error) {
			return `${failureKind}: ${compactError}`;
		}
		if (compactError) {
			return compactError;
		}
		if (failureKind) {
			return failureKind;
		}
		return job.scheduler_status_copy || 'Needs operator attention before retrying.';
	}
</script>

<div class="queue-grid">
	{#each queueCards as card, index (card.eyebrow)}
		<Panel class={`ops-queue-panel ${index === 1 ? 'primary-ops-panel' : 'secondary-ops-panel'}`.trim()} variant={index === 1 ? 'accent' : 'default'}>
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
					{#if calibrationQueueHasWork}
						<div class="action-row">
							<Button
								variant="danger"
								loading={calibrationQueueAction === 'stop'}
								onclick={stopCalibrationQueue}>Stop + Clean</Button
							>
						</div>
					{:else if pendingReviewCount === 0}
						<p class="queue-empty-copy muted-copy">
							Calibration lanes are idle. <a href={resolve('/')}>Go to folders</a> when you want a new sample or proof encode.
						</p>
					{/if}
				{:else}
					<div class="queue-pill-row">
						<span class={`queue-pill ${encodeQueueStatus.tone}`.trim()}
							>{encodeQueueStatus.label}</span
						>
						{#if dashboard.encode_queue.needs_attention_count}
							<span class="queue-pill attention"
								>Needs attention: {dashboard.encode_queue.needs_attention_count}</span
							>
						{/if}
						{#if dashboard.encode_queue.state.stop_requested}
							<span class="queue-pill attention">Stop requested</span>
						{/if}
						<a class="queue-link-pill" href="#remote-hosts">Workers ready: {readyHosts}</a>
						{#if readyHosts < encodeCapableHosts}
							<span class="queue-pill neutral">{reachableHosts} mounted</span>
						{/if}
					</div>
					{#if encodeQueueActionable}
						<div class="action-row">
							{#if dashboard.encode_queue.needs_attention_count}
								<Button
									variant="ghost"
									loading={queueAction === 'retry'}
									onclick={() => runQueueAction('retry')}>Retry All Failed</Button
								>
							{/if}
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
							{#if encodeQueueHasWork || dashboard.encode_queue.state.stop_requested}
								<Button
									variant="ghost"
									loading={queueAction === 'stop'}
									disabled={dashboard.encode_queue.state.stop_requested}
									onclick={() => runQueueAction('stop')}>Stop + Clean</Button
								>
							{/if}
						</div>
					{/if}
					{#if encodeQueueActionHelp}
						<p class="queue-action-help muted-copy">{encodeQueueActionHelp}</p>
					{/if}
					{#if encodeQueueGuidanceCopy}
						<p class="queue-empty-copy muted-copy">
							{#if encodeQueueIdle}
								<a href={resolve('/')}>Go to folders</a> when you want the next full run to start.
							{:else}
								{encodeQueueGuidanceCopy}
							{/if}
						</p>
					{/if}
					{#if encodeQueueEtaCopy}
						<p class="queue-telemetry-note muted-copy">
							Estimated queue finish in {encodeQueueEtaCopy} at the current fleet pace.
						</p>
					{/if}
						{#if recentAttentionJobs.length > 0}
							<details class="queue-detail-shell attention-detail-shell" aria-label="Recent queue blockers" open={!encodeQueueHasWork}>
								<summary>
									<span>{blockersSummary}</span>
									<span class="muted-copy">{blockersLeadCopy}</span>
								</summary>
								<div class="attention-list">
									{#each recentAttentionJobs as job (job.job_id)}
										<div class="attention-row">
											<div>
												<p class="attention-title">{job.prefix}</p>
												<p class="attention-detail muted-copy">{attentionReason(job)}</p>
											</div>
											<div class="attention-meta muted-copy">
												{#if job.attempt_summary}
													<span>{job.attempt_summary}</span>
												{/if}
												{#if formatAttentionTimestamp(job)}
													<span>{formatAttentionTimestamp(job)}</span>
												{/if}
											</div>
										</div>
									{/each}
								</div>
							</details>
						{/if}
						{#if encodeRunningJobs.length > 0}
							<details class="queue-detail-shell" aria-label="Running encode telemetry">
								<summary>
									<span>{runningTelemetrySummary}</span>
									<span class="muted-copy">Open details</span>
								</summary>
								<div class="encode-telemetry-list">
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
							</details>
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

	:global(.ops-queue-panel) {
		background:
			radial-gradient(circle at top left, rgba(255, 255, 255, 0.38), transparent 32%),
			linear-gradient(145deg, rgba(255, 253, 247, 0.88), rgba(245, 236, 220, 0.8));
	}

	:global(.primary-ops-panel) {
		background:
			radial-gradient(circle at top left, rgba(15, 118, 110, 0.18), transparent 34%),
			linear-gradient(145deg, rgba(255, 253, 247, 0.9), rgba(239, 235, 225, 0.82));
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

	.queue-empty-copy {
		margin: 0;
	}

	.queue-action-help {
		margin: -0.15rem 0 0;
	}

	.queue-telemetry-note,
	.encode-telemetry-detail,
	.encode-telemetry-title,
	.encode-telemetry-summary {
		margin: 0;
	}

	.queue-detail-shell {
		display: grid;
		gap: 0.75rem;
		padding: 0.78rem 0.88rem;
		border-radius: var(--radius-md);
		background: rgba(255, 255, 255, 0.54);
		border: 1px solid rgba(23, 35, 31, 0.08);
	}

	.queue-detail-shell summary {
		cursor: pointer;
		display: flex;
		justify-content: space-between;
		gap: 0.8rem;
		align-items: center;
		font-weight: 700;
		color: var(--ink);
	}

	.queue-detail-shell summary::-webkit-details-marker {
		display: none;
	}

	.attention-detail-shell {
		background: rgba(180, 83, 9, 0.06);
		border-color: rgba(180, 83, 9, 0.14);
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

	.attention-list {
		display: grid;
		gap: 0.75rem;
	}

	.attention-title,
	.attention-title,
	.attention-detail,
	.attention-meta {
		margin: 0;
	}

	.attention-row {
		display: flex;
		justify-content: space-between;
		gap: 1rem;
		padding-top: 0.75rem;
		border-top: 1px solid rgba(180, 83, 9, 0.14);
	}

	.attention-row:first-of-type {
		padding-top: 0;
		border-top: 0;
	}

	.attention-title {
		font-weight: 700;
		color: var(--ink);
	}

	.attention-detail {
		max-width: 40rem;
	}

	.attention-meta {
		display: grid;
		gap: 0.25rem;
		text-align: right;
		flex-shrink: 0;
	}

	@media (max-width: 860px) {
		.queue-grid {
			grid-template-columns: 1fr;
		}

		.encode-telemetry-row,
		.attention-row {
			flex-direction: column;
		}

		.encode-telemetry-summary,
		.attention-meta {
			text-align: left;
		}
	}
</style>
