<script lang="ts">
	import { resolve } from '$app/paths';

	let {
		queueStateTone,
		workerStateTone,
		fleetSnapshotLabel,
		stopRequested,
		queuePaused,
		queueWorkersScheduledOffWindow,
		runningCount,
		queuedCount,
		nextWorkerWindow,
		etaCopy,
		actionState,
		resumeQueueFromHome,
		readyHosts,
		queueCapableHosts,
		reachableHosts,
		catalogScanActive,
		catalogScanHeading,
		catalogScanProgressHeadline,
		totalProjectedReclaimCopy,
		recoveryDetailCopy
	}: {
		queueStateTone: string;
		workerStateTone: string;
		fleetSnapshotLabel: string;
		stopRequested: boolean;
		queuePaused: boolean;
		queueWorkersScheduledOffWindow: boolean;
		runningCount: number;
		queuedCount: number;
		nextWorkerWindow: string | null;
		etaCopy: string | null | undefined;
		actionState: null | 'resume-queue' | 'queue-folder';
		resumeQueueFromHome: () => void;
		readyHosts: number;
		queueCapableHosts: number;
		reachableHosts: number;
		catalogScanActive: boolean;
		catalogScanHeading: string;
		catalogScanProgressHeadline: string;
		totalProjectedReclaimCopy: string;
		recoveryDetailCopy: string;
	} = $props();
</script>

<section class="system-strip" aria-label="Fleet system state">
	<div class={`system-cell queue-cell ${queueStateTone}`.trim()}>
		<p class="system-label">Queue state</p>
		<p class="system-value">{fleetSnapshotLabel}</p>
		<p class="system-detail">
			{#if stopRequested}
				Stop was requested across the fleet. Resume here when you are ready to restart queue work.
			{:else if queuePaused}
				Queue is paused. Resume it here or open Ops for full queue controls.
			{:else if queuedCount > 0 && readyHosts === 0 && queueWorkersScheduledOffWindow}
				{nextWorkerWindow
					? `Queued work is waiting for the next worker window at ${nextWorkerWindow}.`
					: 'Queued work is waiting for a scheduled worker window.'}
			{:else if queuedCount > 0 && readyHosts === 0}
				Queued work is waiting for a worker to become ready.
			{:else if queueWorkersScheduledOffWindow && runningCount === 0}
				{nextWorkerWindow
					? `Queue is idle. Next worker window opens at ${nextWorkerWindow}.`
					: 'Queue is idle. All queue workers are currently scheduled off-window.'}
			{:else}
				{etaCopy
					? `Estimated queue finish in ${etaCopy}.`
					: 'Queue is ready for the next operator decision.'}
			{/if}
		</p>
		{#if stopRequested || queuePaused}
			<div class="queue-cell-actions">
				<button
					type="button"
					class="system-action"
					onclick={resumeQueueFromHome}
					disabled={actionState !== null}
				>
					{actionState === 'resume-queue' ? 'Resuming queue...' : 'Resume queue'}
				</button>
				<a class="console-link" href={resolve('/ops')}>Open ops</a>
			</div>
		{/if}
	</div>

	<div class={`system-cell ${workerStateTone}`.trim()}>
		<p class="system-label">Workers</p>
		<p class="system-value">{readyHosts} ready / {queueCapableHosts} queue-capable</p>
		<p class="system-detail">
			{queueCapableHosts === 0
				? `${reachableHosts} mounted now. No queue-capable workers configured.`
				: queueWorkersScheduledOffWindow
					? nextWorkerWindow
						? `${reachableHosts} mounted now. Queue dispatch resumes at ${nextWorkerWindow}.`
						: `${reachableHosts} mounted now. Queue dispatch is outside the active schedule window.`
					: `${reachableHosts} mounted now.`}
		</p>
	</div>

	<div class="system-cell">
		<p class="system-label">Catalog</p>
		<p class="system-value">{catalogScanActive ? catalogScanHeading : 'Catalog standing by'}</p>
		<p class="system-detail">
			{catalogScanActive
				? catalogScanProgressHeadline
				: 'Folder ranking is ready for the next pick.'}
		</p>
	</div>

	<div class="system-cell accent-cell">
		<p class="system-label">Recovery</p>
		<p class="system-value">{totalProjectedReclaimCopy} visible reclaim</p>
		<p class="system-detail">{recoveryDetailCopy}</p>
	</div>
</section>

<style>
	.system-strip {
		display: grid;
		gap: 1rem;
		grid-template-columns: repeat(4, minmax(0, 1fr));
	}

	.system-cell {
		position: relative;
		min-height: 6.8rem;
		padding: 0.9rem 1rem;
		border: 1px solid rgba(148, 163, 184, 0.18);
		background: rgba(15, 20, 27, 0.94);
		box-shadow: 0 18px 38px rgba(2, 6, 23, 0.2);
		overflow: hidden;
	}

	.accent-cell {
		background: rgba(13, 33, 42, 0.94);
	}

	.queue-cell::before {
		content: '';
		position: absolute;
		inset: 0 0 auto;
		height: 2px;
		background: rgba(56, 189, 248, 0.85);
	}

	.queue-cell.warning-state {
		border-color: rgba(249, 115, 22, 0.3);
		background: rgba(58, 26, 13, 0.94);
	}

	.queue-cell.warning-state::before {
		background: rgba(251, 146, 60, 0.95);
	}

	.system-cell.schedule-state {
		border-color: rgba(245, 158, 11, 0.28);
		background: rgba(51, 28, 10, 0.94);
	}

	.system-cell.schedule-state::before {
		content: '';
		position: absolute;
		inset: 0 0 auto;
		height: 2px;
		background: rgba(245, 158, 11, 0.92);
	}

	.queue-cell.normal-state::before {
		background: rgba(56, 189, 248, 0.85);
	}

	.system-label {
		margin: 0;
		font-size: 0.72rem;
		font-weight: 800;
		letter-spacing: 0.16em;
		text-transform: uppercase;
		color: rgba(148, 163, 184, 0.88);
	}

	.system-value {
		margin: 0.4rem 0 0;
		font-size: 1.06rem;
		font-weight: 700;
		line-height: 1.25;
		color: #f8fafc;
	}

	.system-detail {
		margin: 0;
		color: rgba(226, 232, 240, 0.74);
		line-height: 1.5;
	}

	.queue-cell-actions {
		display: flex;
		gap: 0.65rem;
		flex-wrap: wrap;
		margin-top: 0.85rem;
	}

	.system-action,
	.console-link {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		padding: 0.72rem 0.95rem;
		border: 1px solid rgba(56, 189, 248, 0.22);
		background: rgba(15, 23, 42, 0.7);
		color: #e2e8f0;
		font-weight: 700;
		transition:
			border-color 150ms ease,
			background-color 150ms ease,
			color 150ms ease,
			transform 150ms ease;
	}

	.console-link:hover,
	.system-action:hover {
		transform: translateY(-1px);
		border-color: rgba(56, 189, 248, 0.5);
		background: rgba(30, 41, 59, 0.94);
	}

	.system-action {
		background: rgba(154, 52, 18, 0.84);
		border-color: rgba(251, 146, 60, 0.5);
		color: #fff7ed;
	}

	.system-action:hover {
		background: rgba(194, 65, 12, 0.9);
		border-color: rgba(253, 186, 116, 0.65);
	}

	.system-action:disabled {
		opacity: 0.62;
		cursor: default;
		transform: none;
	}

	@media (max-width: 980px) {
		.system-strip {
			grid-template-columns: 1fr 1fr;
		}
	}

	@media (max-width: 720px) {
		.system-strip {
			grid-template-columns: 1fr;
		}
	}
</style>
