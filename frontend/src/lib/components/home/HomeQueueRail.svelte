<script lang="ts">
	import { resolve } from '$app/paths';
	import type { DashboardSummaryPayload, FolderCard as FolderCardData } from '$lib/api/types';
	import {
		folderSubtitleForPrefix,
		folderTitleForPrefix,
		queueJobDetailCopy
	} from '$lib/components/home/home-queue-display';

	let {
		folderLookup,
		queueWatchJobs,
		recentQueueIssues,
		fleetSnapshotLabel,
		pendingReviewCount,
		catalogScanStatusCopy,
		catalogScanProgressFacts,
		metricsReady,
		metricStatusCopy,
		archiveRoot
	}: {
		folderLookup: Record<string, FolderCardData>;
		queueWatchJobs: DashboardSummaryPayload['encode_queue']['running'];
		recentQueueIssues: DashboardSummaryPayload['encode_queue']['recent'];
		fleetSnapshotLabel: string;
		pendingReviewCount: number;
		catalogScanStatusCopy: string;
		catalogScanProgressFacts: string[];
		metricsReady: boolean;
		metricStatusCopy: string;
		archiveRoot: string;
	} = $props();

	const visibleRecentQueueIssues = $derived(recentQueueIssues ?? []);
</script>

<div class="side-rail">
	<section class="station-card rail-card control-card" aria-label="Console controls">
		<div class="section-head compact">
			<div>
				<p class="section-label">Console controls</p>
				<h2 class="section-title small">Global actions</h2>
			</div>
		</div>

		<div class="rail-actions">
			<a class="console-link" href={resolve('/ops')}>Open ops</a>
			<a class="console-link" href={resolve('/settings')}>Settings</a>
			<a class="console-link" href={resolve('/completed')}>Completed</a>
		</div>
	</section>

	<section class="station-card rail-card" aria-label="Queue monitor">
		<div class="section-head compact">
			<div>
				<p class="section-label">Queue monitor</p>
				<h2 class="section-title small">Active encodes</h2>
			</div>
		</div>

		<div class="rail-summary">
			<p>{fleetSnapshotLabel}</p>
			<p>{pendingReviewCount} pending review</p>
		</div>

		{#if queueWatchJobs.length > 0}
			<ul class="watch-list">
				{#each queueWatchJobs as job (job.job_id)}
					<li>
						<div class="watch-row">
							<div>
								<p class="watch-title">{folderTitleForPrefix(job.prefix, folderLookup)}</p>
								<p class="watch-path mono-copy">
									{folderSubtitleForPrefix(job.prefix, folderLookup)}
								</p>
								<p class="watch-copy">{queueJobDetailCopy(job)}</p>
							</div>
							<span class="watch-state">{job.status}</span>
						</div>
					</li>
				{/each}
			</ul>
		{:else}
			<p class="muted-block">
				No encode jobs are active. The next folder pick will define the queue.
			</p>
		{/if}
	</section>

	<section class="station-card rail-card" aria-label="Catalog status">
		<div class="section-head compact">
			<div>
				<p class="section-label">Catalog status</p>
				<h2 class="section-title small">Scan and cleanup</h2>
			</div>
		</div>

		<p class="catalog-headline">{catalogScanStatusCopy}</p>
		{#if catalogScanProgressFacts.length > 0}
			<ul class="fact-list">
				{#each catalogScanProgressFacts as fact (fact)}
					<li>{fact}</li>
				{/each}
			</ul>
		{/if}

		<div class="rail-summary stack">
			<p>Metrics: {metricsReady ? 'VMAF, XPSNR, and SSIM online' : metricStatusCopy}</p>
			<p>
				Archive root: <span class="mono-copy">{archiveRoot || 'not set'}</span>
			</p>
		</div>

		{#if visibleRecentQueueIssues.length > 0}
			<div class="issue-block">
				<p class="signal-label">Recent blockers</p>
				<ul class="watch-list compact-list">
					{#each visibleRecentQueueIssues as job (job.job_id)}
						<li>
							<p class="watch-title">{folderTitleForPrefix(job.prefix, folderLookup)}</p>
							<p class="watch-path mono-copy">
								{folderSubtitleForPrefix(job.prefix, folderLookup)}
							</p>
							<p class="watch-copy">{job.error || queueJobDetailCopy(job)}</p>
						</li>
					{/each}
				</ul>
			</div>
		{/if}
	</section>
</div>

<style>
	.side-rail {
		display: grid;
		gap: 1rem;
	}

	.station-card {
		position: relative;
		padding: 1.1rem;
		border: 1px solid rgba(148, 163, 184, 0.18);
		background: rgba(15, 20, 27, 0.94);
		box-shadow: 0 18px 38px rgba(2, 6, 23, 0.2);
		overflow: hidden;
	}

	.control-card {
		padding-bottom: 1rem;
	}

	.section-head {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		gap: 1rem;
		align-items: start;
		margin-bottom: 1rem;
	}

	.section-head.compact {
		margin-bottom: 0.85rem;
	}

	.section-label,
	.signal-label {
		margin: 0;
		font-size: 0.72rem;
		font-weight: 800;
		letter-spacing: 0.16em;
		text-transform: uppercase;
		color: rgba(148, 163, 184, 0.88);
	}

	.section-title,
	.watch-title {
		margin: 0;
		color: #f8fafc;
	}

	.section-title {
		margin-top: 0.3rem;
		font-size: 1rem;
		font-weight: 700;
	}

	.rail-actions {
		display: flex;
		gap: 0.65rem;
		flex-wrap: wrap;
	}

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

	.console-link:hover {
		transform: translateY(-1px);
		border-color: rgba(56, 189, 248, 0.5);
		background: rgba(30, 41, 59, 0.94);
	}

	.rail-summary,
	.rail-summary p,
	.catalog-headline,
	.watch-copy {
		margin: 0;
		color: rgba(226, 232, 240, 0.74);
		line-height: 1.5;
	}

	.rail-summary {
		display: flex;
		gap: 0.75rem;
		justify-content: space-between;
		padding: 0.75rem 0.85rem;
		border: 1px solid rgba(148, 163, 184, 0.16);
		background: rgba(15, 23, 42, 0.64);
	}

	.rail-summary.stack {
		display: grid;
		gap: 0.45rem;
		justify-content: stretch;
	}

	.watch-list,
	.fact-list {
		margin: 0.9rem 0 0;
		padding: 0;
		list-style: none;
	}

	.watch-list {
		display: grid;
		gap: 0.7rem;
	}

	.watch-row {
		display: flex;
		justify-content: space-between;
		gap: 0.75rem;
		padding: 0.75rem 0.85rem;
		border: 1px solid rgba(148, 163, 184, 0.16);
		background: rgba(15, 23, 42, 0.6);
	}

	.watch-title {
		font-size: 0.8rem;
	}

	.watch-copy {
		margin-top: 0.25rem;
		font-size: 0.88rem;
	}

	.watch-path {
		margin-top: 0.18rem;
		font-size: 0.76rem;
		color: rgba(148, 163, 184, 0.82);
	}

	.watch-state {
		display: inline-flex;
		align-self: start;
		align-items: center;
		gap: 0.45rem;
		padding: 0.34rem 0.56rem;
		border: 1px solid rgba(56, 189, 248, 0.22);
		background: rgba(30, 41, 59, 0.88);
		color: #cbd5e1;
		font-size: 0.72rem;
		font-weight: 800;
		letter-spacing: 0.04em;
		text-transform: uppercase;
	}

	.fact-list {
		display: grid;
		gap: 0.45rem;
		padding-left: 1rem;
		color: rgba(226, 232, 240, 0.74);
	}

	.issue-block {
		margin-top: 1rem;
	}

	.compact-list {
		margin-top: 0.55rem;
	}

	@media (max-width: 1100px) {
		.section-head {
			grid-template-columns: 1fr;
		}
	}
</style>
