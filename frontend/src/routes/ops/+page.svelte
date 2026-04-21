<script lang="ts">
	import { browser } from '$app/environment';
	import '$lib/design/workstation-shell.css';
	import { resolve } from '$app/paths';
	import { onMount } from 'svelte';
	import { fetchJson } from '$lib/api/client';
	import type { DashboardSummaryPayload, HostsPayload } from '$lib/api/types';
	import { goto } from '$app/navigation';
	import DashboardHostGrid from '$lib/components/dashboard/DashboardHostGrid.svelte';
	import DashboardQueues from '$lib/components/dashboard/DashboardQueues.svelte';
	import { hostSettingsAnchor } from '$lib/format';
	import { hostsStatusPending } from '$lib/hosts/runtime';
	import { allQueueWorkersScheduledOffWindow, nextQueueWindowCopy } from '$lib/hosts/schedule';
	import { toasts } from '$lib/stores/toasts';

	const EMPTY_DASHBOARD: DashboardSummaryPayload = {
		folders_preview: [],
		library_colors: {},
		scan_job: null,
		calibration_queue: {
			sample: {
				running: [],
				queued: [],
				pending_review: [],
				running_count: 0,
				queued_count: 0,
				pending_review_count: 0
			},
			full: {
				running: [],
				queued: [],
				pending_review: [],
				running_count: 0,
				queued_count: 0,
				pending_review_count: 0
			},
			active_count: 0
		},
		encode_queue: {
			queued_count: 0,
			running_count: 0,
			state: { is_paused: false, stop_requested: false },
			running: [],
			telemetry: undefined,
			queued: []
		},
		archive_cleanup: {
			archive_root: '',
			file_count: 0,
			total_size_bytes: 0,
			has_cleanup: false
		},
		catalog_empty: true,
		folder_cache_key: '',
		metric_support: { vmaf: false, xpsnr: false, ssim: false },
		metric_status_copy: ''
	};
	const EMPTY_HOSTS: HostsPayload = { compact: true, hosts: [] };

	let dashboardPayload = $state<DashboardSummaryPayload | null>(null);
	let hostsPayload = $state<HostsPayload | null>(null);
	let isLoadingOps = $state(true);
	let opsLoadError = $state<string | null>(null);
	let hostStatusRetryTimer: number | null = null;
	let opsRefreshTimer: number | null = null;
	let dashboardRefreshController: AbortController | null = null;
	let hostsRefreshController: AbortController | null = null;
	let activeDashboardRefreshRequest = 0;
	let activeHostsRefreshRequest = 0;
	let opsRefreshPromise: Promise<void> | null = null;

	const dashboard = $derived(dashboardPayload ?? EMPTY_DASHBOARD);
	const hosts = $derived(hostsPayload ?? EMPTY_HOSTS);
	const encodeCapableHosts = $derived.by(
		() => hosts.hosts.filter((host) => host.capabilities.includes('encode_queue')).length
	);
	const readyHosts = $derived.by(() => hosts.hosts.filter((host) => host.queue_active).length);
	const queueWorkersScheduledOffWindow = $derived.by(() =>
		allQueueWorkersScheduledOffWindow(hosts.hosts)
	);
	const nextWorkerWindow = $derived.by(() => nextQueueWindowCopy(hosts.hosts));
	const rankedHosts = $derived.by(() =>
		[...hosts.hosts].sort(
			(left, right) =>
				Number(right.priority) - Number(left.priority) || left.label.localeCompare(right.label)
		)
	);
	const reachableHosts = $derived.by(() => hosts.hosts.filter((host) => host.available).length);
	const pendingReviewCount = $derived.by(
		() =>
			dashboard.calibration_queue.sample.pending_review_count +
			dashboard.calibration_queue.full.pending_review_count
	);
	const calibrationQueueHasWork = $derived.by(
		() =>
			dashboard.calibration_queue.sample.running_count +
				dashboard.calibration_queue.sample.queued_count +
				dashboard.calibration_queue.full.running_count +
				dashboard.calibration_queue.full.queued_count >
			0
	);
	const encodeRunningJobs = $derived(dashboard.encode_queue.running ?? []);
	const encodeQueueEtaCopy = $derived(dashboard.encode_queue.telemetry?.eta_copy ?? null);
	const encodeQueueStatus = $derived.by(() => {
		if (dashboard.encode_queue.state.stop_requested) {
			return { label: 'Stopping', tone: 'attention' as const };
		}

		if (dashboard.encode_queue.state.is_paused) {
			return { label: 'Paused', tone: 'neutral' as const };
		}

		if (dashboard.encode_queue.running_count > 0) {
			return { label: 'Running', tone: 'ok' as const };
		}

		if (dashboard.encode_queue.queued_count > 0) {
			return { label: 'Queued', tone: 'attention' as const };
		}

		return { label: 'Idle', tone: 'neutral' as const };
	});
	const fleetSnapshotLabel = $derived.by(() => {
		if (dashboard.encode_queue.running_count > 0) {
			return `${dashboard.encode_queue.running_count} encodes running`;
		}
		if (dashboard.encode_queue.queued_count > 0) {
			return `${dashboard.encode_queue.queued_count} folder${dashboard.encode_queue.queued_count === 1 ? '' : 's'} queued`;
		}
		return 'Fleet idle';
	});
	const queueCards = $derived.by(() => [
		{
			eyebrow: 'Calibration',
			heading: `${dashboard.calibration_queue.sample.running_count + dashboard.calibration_queue.full.running_count} running · ${dashboard.calibration_queue.sample.queued_count + dashboard.calibration_queue.full.queued_count} queued`,
			lede: 'Sample lanes stay separate so tuning work does not obscure the full encode queue.'
		},
		{
			eyebrow: 'Encode Queue',
			heading: `${dashboard.encode_queue.running_count} running · ${dashboard.encode_queue.queued_count} queued`,
			lede: 'Run-state, blockers, and queue controls for full-folder encodes.'
		}
	]);
	const queueStateTone = $derived.by(() => {
		if (dashboard.encode_queue.state.stop_requested || dashboard.encode_queue.state.is_paused) {
			return 'warning-state';
		}
		if (queueWorkersScheduledOffWindow) {
			return 'schedule-state';
		}
		if (dashboard.encode_queue.queued_count > 0 && readyHosts === 0) {
			return 'warning-state';
		}
		return 'normal-state';
	});
	const workerStateTone = $derived.by(() => {
		if (encodeCapableHosts === 0) {
			return 'warning-state';
		}
		if (queueWorkersScheduledOffWindow) {
			return 'schedule-state';
		}
		if (readyHosts === 0 && dashboard.encode_queue.queued_count > 0) {
			return 'warning-state';
		}
		return 'normal-state';
	});
	const calibrationStateTone = $derived.by(() => {
		if (pendingReviewCount > 0) {
			return 'warning-state';
		}
		return calibrationQueueHasWork ? 'normal-state' : 'muted-state';
	});
	const cleanupStateTone = $derived.by(() =>
		Number(dashboard.archive_cleanup?.file_count ?? 0) > 0 ? 'warning-state' : 'normal-state'
	);
	const queueStateDetail = $derived.by(() => {
		if (dashboard.encode_queue.state.stop_requested) {
			return 'Stop was requested. Review queue details below before restarting fleet work.';
		}
		if (dashboard.encode_queue.state.is_paused) {
			return 'Queue is paused. Resume from the queue controls when the fleet is ready.';
		}
		if (queueWorkersScheduledOffWindow && dashboard.encode_queue.queued_count > 0) {
			return nextWorkerWindow
				? `Queued now. Next worker window opens at ${nextWorkerWindow}.`
				: 'Queued now. All queue workers are currently scheduled off-window.';
		}
		if (queueWorkersScheduledOffWindow) {
			return nextWorkerWindow
				? `Queue is idle. Next worker window opens at ${nextWorkerWindow}.`
				: 'Queue is idle. All queue workers are currently scheduled off-window.';
		}
		if (dashboard.encode_queue.queued_count > 0 && readyHosts === 0) {
			return 'Queued work is waiting for a ready host.';
		}
		return encodeQueueEtaCopy
			? `Estimated queue finish in ${encodeQueueEtaCopy} at the current fleet pace.`
			: 'Queue state and recent blockers stay visible below.';
	});
	const workerStateDetail = $derived.by(() => {
		if (encodeCapableHosts === 0) {
			return `${reachableHosts} mounted now. No encode-queue workers are configured.`;
		}
		if (readyHosts === 0 && dashboard.encode_queue.queued_count > 0) {
			return queueWorkersScheduledOffWindow
				? 'Workers exist, but none are inside the current queue schedule window.'
				: 'Workers exist, but none are ready to take queued work right now.';
		}
		if (queueWorkersScheduledOffWindow) {
			return nextWorkerWindow
				? `${reachableHosts} mounted now. Queue dispatch resumes at ${nextWorkerWindow}.`
				: `${reachableHosts} mounted now. Queue dispatch is outside the active schedule window.`;
		}
		return `${reachableHosts} mounted now across ${encodeCapableHosts} queue-capable host${encodeCapableHosts === 1 ? '' : 's'}.`;
	});
	const calibrationDetail = $derived.by(() => {
		if (pendingReviewCount > 0) {
			return `${pendingReviewCount} calibration result${pendingReviewCount === 1 ? '' : 's'} need review before more tuning decisions.`;
		}
		if (calibrationQueueHasWork) {
			return 'Sample and proof jobs are active without mixing into the full encode queue.';
		}
		return 'Calibration lanes are idle. Launch new sample work from folders when needed.';
	});
	const cleanupDetail = $derived.by(() => {
		const cleanupCount = Number(dashboard.archive_cleanup?.file_count ?? 0);
		if (cleanupCount > 0) {
			return `${cleanupCount} archived original${cleanupCount === 1 ? '' : 's'} are waiting in completed cleanup.`;
		}
		return 'No archived originals are waiting for cleanup.';
	});
	let queueAction = $state<string | null>(null);
	let calibrationQueueAction = $state<string | null>(null);

	async function refreshOpsData({
		silent = false,
		force = false
	}: { silent?: boolean; force?: boolean } = {}) {
		if (opsRefreshPromise !== null && !force) {
			return opsRefreshPromise;
		}
		if (force) {
			dashboardRefreshController?.abort();
			hostsRefreshController?.abort();
		}
		const refreshPromise = (async () => {
			isLoadingOps = true;
			const dashboardRequestId = ++activeDashboardRefreshRequest;
			const hostsRequestId = ++activeHostsRefreshRequest;
			try {
				const nextDashboardRefreshController = new AbortController();
				const nextHostsRefreshController = new AbortController();
				dashboardRefreshController = nextDashboardRefreshController;
				hostsRefreshController = nextHostsRefreshController;
				const [nextDashboard, nextHosts] = await Promise.all([
					fetchJson<DashboardSummaryPayload>('/api/dashboard', fetch, {
						signal: nextDashboardRefreshController.signal
					}),
					fetchJson<HostsPayload>('/api/hosts?compact=1', fetch, {
						signal: nextHostsRefreshController.signal
					})
				]);
				if (
					dashboardRequestId !== activeDashboardRefreshRequest ||
					hostsRequestId !== activeHostsRefreshRequest
				) {
					return;
				}
				dashboardPayload = nextDashboard;
				hostsPayload = nextHosts;
				opsLoadError = null;
				if (browser && hostsStatusPending(nextHosts)) {
					hostStatusRetryTimer ??= window.setTimeout(() => {
						hostStatusRetryTimer = null;
						void refreshOpsData({ silent: true });
					}, 1000);
				} else if (hostStatusRetryTimer !== null) {
					clearTimeout(hostStatusRetryTimer);
					hostStatusRetryTimer = null;
				}
			} catch (error) {
				if (error instanceof DOMException && error.name === 'AbortError') {
					return;
				}
				opsLoadError = error instanceof Error ? error.message : 'Unexpected ops loading error';
				if (!silent) {
					toasts.error('Ops load failed', opsLoadError);
				}
			} finally {
				if (dashboardRequestId === activeDashboardRefreshRequest) {
					dashboardRefreshController = null;
				}
				if (hostsRequestId === activeHostsRefreshRequest) {
					hostsRefreshController = null;
				}
				if (
					dashboardRequestId === activeDashboardRefreshRequest &&
					hostsRequestId === activeHostsRefreshRequest
				) {
					isLoadingOps = false;
				}
			}
		})();
		opsRefreshPromise = refreshPromise;
		await refreshPromise;
		if (opsRefreshPromise === refreshPromise) {
			opsRefreshPromise = null;
		}
	}

	async function runQueueAction(action: 'pause' | 'resume' | 'retry' | 'stop') {
		if (
			action === 'stop' &&
			browser &&
			!window.confirm('Stop active encodes and clear the queue? This cannot be undone.')
		) {
			return;
		}
		if (
			action === 'retry' &&
			browser &&
			!window.confirm(
				'Retry all approved failed folder encodes? Folders that still need review will be skipped.'
			)
		) {
			return;
		}

		queueAction = action;
		try {
			const actionPath = action === 'retry' ? 'retry-failed' : action;
			const response = await fetch(`/api/encode-queue/${actionPath}`, { method: 'POST' });
			const payload = (await response.json()) as { message?: string };
			if (!response.ok) {
				toasts.error('Queue update failed', payload.message ?? `Could not ${action} queue.`);
				return;
			}
			toasts.success('Queue updated', payload.message ?? 'Queue updated.');
			await refreshOpsData({ silent: true, force: true });
		} catch (error) {
			toasts.error(
				'Queue update failed',
				error instanceof Error ? error.message : 'Unexpected queue error'
			);
		} finally {
			queueAction = null;
		}
	}

	async function stopCalibrationQueue() {
		if (
			browser &&
			!window.confirm(
				'Stop active calibrations and clear queued calibration jobs? This cannot be undone.'
			)
		) {
			return;
		}

		calibrationQueueAction = 'stop';
		try {
			const response = await fetch('/api/calibration-queue/stop', { method: 'POST' });
			const payload = (await response.json()) as { message?: string };
			if (!response.ok) {
				toasts.error(
					'Calibration queue update failed',
					payload.message ?? 'Could not stop calibration queue.'
				);
				return;
			}
			toasts.success('Calibration queue updated', payload.message ?? 'Calibration queue updated.');
			await refreshOpsData({ silent: true, force: true });
		} catch (error) {
			toasts.error(
				'Calibration queue update failed',
				error instanceof Error ? error.message : 'Unexpected calibration queue error'
			);
		} finally {
			calibrationQueueAction = null;
		}
	}

	async function openHostSettings(hostKey: string) {
		await goto(resolve('/settings'));
		if (browser) {
			window.location.hash = hostSettingsAnchor(hostKey);
		}
	}

	onMount(() => {
		void refreshOpsData({ silent: true });
		if (browser) {
			opsRefreshTimer = window.setInterval(() => {
				void refreshOpsData({ silent: true });
			}, 4000);
		}
		return () => {
			dashboardRefreshController?.abort();
			hostsRefreshController?.abort();
			if (opsRefreshTimer !== null) {
				clearInterval(opsRefreshTimer);
				opsRefreshTimer = null;
			}
			if (hostStatusRetryTimer !== null) {
				clearTimeout(hostStatusRetryTimer);
				hostStatusRetryTimer = null;
			}
		};
	});
</script>

<svelte:head>
	<title>Ops · Mediaforce</title>
</svelte:head>

<div class="workstation-screen ops-screen">
	{#if !dashboardPayload}
		<section class="loading-shell" aria-label="Ops loading state">
			<div>
				<p class="system-label">Operations</p>
				<h1 class="loading-title">{isLoadingOps ? 'Loading fleet status' : 'Ops unavailable'}</h1>
				<p class="loading-copy">
					{opsLoadError ??
						'The ops console is waiting for queue state, host readiness, and cleanup totals from the runtime.'}
				</p>
			</div>
			<div class="loading-actions">
				<a class="console-link" href={resolve('/')}>Back to folders</a>
				{#if opsLoadError}
					<button
						type="button"
						class="alert-action"
						onclick={() => refreshOpsData({ force: true })}
					>
						Retry ops load
					</button>
				{/if}
			</div>
		</section>
	{:else}
		<section class="system-strip" aria-label="Operations fleet state">
			<div class={`system-cell queue-cell ${queueStateTone}`.trim()}>
				<p class="system-label">Queue state</p>
				<p class="system-value">{fleetSnapshotLabel}</p>
				<p class="system-detail">{queueStateDetail}</p>
			</div>

			<div class={`system-cell ${workerStateTone}`.trim()}>
				<p class="system-label">Workers</p>
				<p class="system-value">{readyHosts} ready / {encodeCapableHosts} queue-capable</p>
				<p class="system-detail">{workerStateDetail}</p>
			</div>

			<div class={`system-cell ${calibrationStateTone}`.trim()}>
				<p class="system-label">Calibration</p>
				<p class="system-value">
					{dashboard.calibration_queue.sample.running_count +
						dashboard.calibration_queue.full.running_count} running ·
					{dashboard.calibration_queue.sample.queued_count +
						dashboard.calibration_queue.full.queued_count}
					queued
				</p>
				<p class="system-detail">{calibrationDetail}</p>
			</div>

			<div class={`system-cell ${cleanupStateTone}`.trim()}>
				<p class="system-label">Cleanup and routes</p>
				<p class="system-value">
					{Number(dashboard.archive_cleanup?.file_count ?? 0)} archived original{Number(
						dashboard.archive_cleanup?.file_count ?? 0
					) === 1
						? ''
						: 's'}
				</p>
				<p class="system-detail">{cleanupDetail}</p>
				<div class="ops-control-row">
					<a class="console-link" href={resolve('/')}>Back to folders</a>
					<a class="console-link" href={resolve('/settings')}>Settings</a>
					{#if Number(dashboard.archive_cleanup?.file_count ?? 0) > 0}
						<a class="console-link" href={resolve('/completed')}>Open completed</a>
					{/if}
				</div>
			</div>
		</section>

		{#if opsLoadError}
			<section class="alert-strip" aria-label="Ops runtime issue">
				<div>
					<p class="alert-label">Ops runtime issue</p>
					<p class="alert-copy">{opsLoadError}</p>
				</div>
				<button type="button" class="alert-action" onclick={() => refreshOpsData({ force: true })}>
					Retry ops load
				</button>
			</section>
		{/if}

		<DashboardQueues
			{queueCards}
			{pendingReviewCount}
			{calibrationQueueAction}
			{calibrationQueueHasWork}
			{stopCalibrationQueue}
			{encodeQueueStatus}
			{queueAction}
			{dashboard}
			{readyHosts}
			{encodeCapableHosts}
			{reachableHosts}
			{runQueueAction}
			{encodeQueueEtaCopy}
			{encodeRunningJobs}
			{queueWorkersScheduledOffWindow}
			nextQueueWindowCopy={nextWorkerWindow}
		/>

		<DashboardHostGrid {rankedHosts} {readyHosts} onOpenHostSettings={openHostSettings} />
	{/if}
</div>

<style>
	.workstation-screen {
		display: grid;
		gap: 1rem;
		padding: 0.25rem 0 1rem;
		position: relative;
		isolation: isolate;
		z-index: 0;
	}

	.workstation-screen::before {
		content: '';
		position: fixed;
		inset: 0;
		z-index: -2;
		pointer-events: none;
		background: #0b1014;
	}

	.workstation-screen::after {
		display: none;
	}

	.loading-shell {
		display: grid;
		gap: 1rem;
		padding: 1.1rem;
		border: 1px solid rgba(148, 163, 184, 0.18);
		background: rgba(15, 20, 27, 0.94);
		box-shadow: 0 18px 38px rgba(2, 6, 23, 0.2);
	}

	.loading-title {
		margin: 0.3rem 0 0;
		color: #f8fafc;
		font-size: 1.35rem;
		font-weight: 700;
	}

	.loading-copy {
		margin: 0.7rem 0 0;
		max-width: 42rem;
		color: rgba(226, 232, 240, 0.74);
		line-height: 1.5;
	}

	.loading-actions {
		display: flex;
		gap: 0.65rem;
		flex-wrap: wrap;
	}

	.system-strip {
		display: grid;
		gap: 1rem;
		grid-template-columns: repeat(4, minmax(0, 1fr));
	}

	.system-cell {
		position: relative;
		display: grid;
		gap: 0.55rem;
		min-height: 8.5rem;
		padding: 1rem 1.1rem;
		border: 1px solid rgba(148, 163, 184, 0.18);
		background: rgba(15, 20, 27, 0.94);
		box-shadow: 0 18px 38px rgba(2, 6, 23, 0.2);
		overflow: hidden;
	}

	.queue-cell::before,
	.alert-strip::before {
		content: '';
		position: absolute;
		inset: 0 0 auto;
		height: 2px;
		background: rgba(56, 189, 248, 0.85);
	}

	.system-cell.warning-state {
		border-color: rgba(249, 115, 22, 0.3);
		background: rgba(58, 26, 13, 0.94);
	}

	.system-cell.warning-state::before {
		content: '';
		position: absolute;
		inset: 0 0 auto;
		height: 2px;
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

	.system-cell.muted-state {
		border-color: rgba(71, 85, 105, 0.2);
		background: rgba(12, 17, 23, 0.94);
	}

	.system-label,
	.alert-label {
		margin: 0;
		font-size: 0.72rem;
		font-weight: 800;
		letter-spacing: 0.16em;
		text-transform: uppercase;
		color: rgba(148, 163, 184, 0.88);
	}

	.system-value {
		margin: 0;
		color: #f8fafc;
		font-size: 1.2rem;
		font-weight: 700;
		line-height: 1.25;
	}

	.system-detail,
	.alert-copy {
		margin: 0;
		color: rgba(226, 232, 240, 0.74);
		line-height: 1.5;
	}

	.ops-control-row {
		display: flex;
		gap: 0.65rem;
		flex-wrap: wrap;
		margin-top: 0.25rem;
	}

	.alert-strip {
		position: relative;
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		gap: 1rem;
		align-items: center;
		padding: 1rem 1.1rem;
		border: 1px solid rgba(148, 163, 184, 0.18);
		background: rgba(55, 24, 11, 0.96);
		box-shadow: 0 18px 38px rgba(2, 6, 23, 0.2);
		overflow: hidden;
	}

	.console-link,
	.alert-action {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		padding: 0.72rem 0.95rem;
		border: 1px solid rgba(56, 189, 248, 0.22);
		background: rgba(15, 23, 42, 0.72);
		color: #e2e8f0;
		font-weight: 700;
		text-decoration: none;
		transition:
			border-color 150ms ease,
			background-color 150ms ease,
			color 150ms ease,
			transform 150ms ease;
	}

	.console-link:hover,
	.alert-action:hover {
		transform: translateY(-1px);
		border-color: rgba(56, 189, 248, 0.5);
		background: rgba(30, 41, 59, 0.94);
	}

	:global(.ops-screen .muted-copy) {
		color: rgba(226, 232, 240, 0.72) !important;
	}

	:global(.queue-pill.attention) {
		background: rgba(120, 53, 15, 0.84) !important;
		color: #ffedd5 !important;
	}

	:global(.queue-pill.ok) {
		background: rgba(20, 83, 45, 0.82) !important;
		color: #dcfce7 !important;
	}

	:global(.queue-pill.neutral),
	:global(.queue-link-pill),
	:global(.section-summary-chip) {
		background: rgba(30, 41, 59, 0.78) !important;
		color: rgba(226, 232, 240, 0.78) !important;
	}

	:global(.section-summary-chip.active) {
		background: rgba(8, 47, 73, 0.82) !important;
		color: #dbeafe !important;
	}

	:global(.section-action-link),
	:global(.queue-link-pill) {
		color: #7dd3fc !important;
	}

	:global(.queue-detail-shell),
	:global(.encode-telemetry-row) {
		background: rgba(15, 23, 42, 0.6) !important;
		border-color: rgba(148, 163, 184, 0.16) !important;
	}

	:global(.attention-detail-shell) {
		background: rgba(67, 20, 7, 0.62) !important;
		border-color: rgba(249, 115, 22, 0.24) !important;
	}

	:global(.queue-detail-shell summary),
	:global(.attention-title),
	:global(.encode-telemetry-summary),
	:global(.encode-telemetry-title) {
		color: #f8fafc !important;
	}

	@media (max-width: 1100px) {
		.system-strip {
			grid-template-columns: 1fr 1fr;
		}
	}

	@media (max-width: 960px) {
		.alert-strip {
			grid-template-columns: 1fr;
		}
	}

	@media (max-width: 720px) {
		.system-strip {
			grid-template-columns: 1fr;
		}
	}
</style>
