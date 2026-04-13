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
	import Panel from '$lib/components/Panel.svelte';
	import Pill from '$lib/components/Pill.svelte';
	import SectionHead from '$lib/components/SectionHead.svelte';
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
	const opsHeroPills = $derived.by(() => [
		{
			label: fleetSnapshotLabel,
			variant: dashboard.encode_queue.queued_count > 0 ? ('warn' as const) : ('ghost' as const)
		},
		...(readyHosts > 0
			? [{ label: `${readyHosts} hosts ready`, variant: 'ok' as const }]
			: queueWorkersScheduledOffWindow
				? [
						{
							label: nextWorkerWindow
								? `Next worker window ${nextWorkerWindow}`
								: 'All queue workers are scheduled off-window',
							variant: 'neutral' as const
						}
					]
				: [{ label: '0 hosts ready', variant: 'ghost' as const }]),
		...(Number(dashboard.archive_cleanup?.file_count ?? 0) > 0
			? [
					{
						label: `${Number(dashboard.archive_cleanup?.file_count ?? 0)} archived backups`,
						variant: 'warn' as const
					}
				]
			: []),
		...(pendingReviewCount > 0
			? [{ label: `${pendingReviewCount} pending review`, variant: 'warn' as const }]
			: [])
	]);
	const queueCards = $derived.by(() => [
		{
			eyebrow: 'Calibration',
			heading: `${dashboard.calibration_queue.sample.running_count + dashboard.calibration_queue.full.running_count} running · ${dashboard.calibration_queue.sample.queued_count + dashboard.calibration_queue.full.queued_count} queued`,
			lede: 'Sample work stays separate from the full queue so you can tune before committing a folder run.'
		},
		{
			eyebrow: 'Encode Queue',
			heading: `${dashboard.encode_queue.running_count} running · ${dashboard.encode_queue.queued_count} queued`,
			lede: 'Fleet state and controls for whole-folder runs.'
		}
	]);
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

<div class="page-stack">
	{#if !dashboardPayload}
		<Panel padding="1.1rem 1.2rem">
			<div class="panel-stack">
				<SectionHead
					eyebrow="Operations"
					heading={isLoadingOps ? 'Loading fleet status' : 'Ops unavailable'}
					lede={opsLoadError ??
						'The page shell is ready now. Queue state and host readiness will populate as soon as the runtime calls finish.'}
					size="compact"
				/>
				{#if opsLoadError}
					<div class="section-actions-row">
						<a class="ops-return-link" href={resolve('/')}>Back to folders</a>
						<button
							type="button"
							class="refresh-button"
							onclick={() => refreshOpsData({ force: true })}
						>
							Retry ops load
						</button>
					</div>
				{/if}
			</div>
		</Panel>
	{/if}

	<Panel class="ops-hero-panel" padding="1.1rem 1.2rem">
		<div class="ops-hero">
			<SectionHead
				eyebrow="Operations"
				heading="Fleet status, queue controls, and worker readiness"
				lede="Use this view when you need queue detail and worker readiness, then jump back to folder selection when you are ready to choose work."
				size="compact"
			/>
			<div class="ops-hero-side">
				<div class="ops-pill-row">
					{#each opsHeroPills as pill (pill.label)}
						<Pill label={pill.label} variant={pill.variant} />
					{/each}
				</div>
				{#if Number(dashboard.archive_cleanup?.file_count ?? 0) > 0}
					<a class="ops-return-link" href={resolve('/completed')}> Open completed </a>
				{/if}
				<a class="ops-return-link" href={resolve('/')}> Back to folders </a>
			</div>
		</div>
	</Panel>

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
</div>

<style>
	.page-stack {
		display: grid;
		gap: 1rem;
	}

	.ops-hero {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		align-items: start;
		gap: 1rem;
		flex-wrap: wrap;
	}

	.ops-hero-side {
		display: grid;
		align-content: start;
		gap: 0.75rem;
		justify-items: end;
	}

	.ops-pill-row {
		display: flex;
		flex-wrap: wrap;
		gap: 0.7rem;
		justify-content: flex-end;
	}

	:global(.ops-hero-panel) {
		border: 1px solid rgba(148, 163, 184, 0.18) !important;
		background: rgba(15, 20, 27, 0.94) !important;
		box-shadow: 0 18px 38px rgba(2, 6, 23, 0.2) !important;
	}

	:global(.ops-hero-panel)::before,
	:global(.ops-hero-panel)::after,
	:global(.ops-queue-panel)::before,
	:global(.ops-queue-panel)::after,
	:global(.folder-section)::before,
	:global(.folder-section)::after,
	:global(.host-card)::after {
		display: none !important;
	}

	:global(.ops-queue-panel),
	:global(.folder-section),
	:global(.host-card) {
		border: 1px solid rgba(148, 163, 184, 0.18) !important;
		background: rgba(15, 20, 27, 0.94) !important;
		box-shadow: 0 18px 38px rgba(2, 6, 23, 0.2) !important;
	}

	:global(.primary-ops-panel) {
		border-color: rgba(56, 189, 248, 0.22) !important;
	}

	:global(.ops-queue-panel .eyebrow-copy),
	:global(.folder-section .eyebrow-copy),
	:global(.ops-hero-panel .eyebrow-copy) {
		color: rgba(125, 211, 252, 0.84) !important;
	}

	:global(.ops-queue-panel h2),
	:global(.folder-section h2),
	:global(.folder-section h3),
	:global(.ops-hero-panel h2),
	:global(.host-card h3) {
		color: #f8fafc !important;
	}

	:global(.ops-queue-panel .muted-copy),
	:global(.folder-section .muted-copy),
	:global(.host-card .muted-copy),
	:global(.ops-hero-panel .lede-copy) {
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
	:global(.encode-telemetry-row),
	:global(.host-card .status-tray),
	:global(.running-job-shell),
	:global(.running-job-row) {
		background: rgba(15, 23, 42, 0.6) !important;
		border-color: rgba(148, 163, 184, 0.16) !important;
	}

	:global(.attention-detail-shell),
	:global(.host-card .issues-box) {
		background: rgba(67, 20, 7, 0.62) !important;
		border-color: rgba(249, 115, 22, 0.24) !important;
	}

	:global(.host-card .state-chip),
	:global(.host-card .meta-pill) {
		background: rgba(30, 41, 59, 0.82) !important;
		color: rgba(226, 232, 240, 0.78) !important;
	}

	:global(.host-card .state-chip.ok) {
		background: rgba(20, 83, 45, 0.82) !important;
		color: #dcfce7 !important;
	}

	:global(.host-card .state-chip.hold) {
		background: rgba(120, 53, 15, 0.82) !important;
		color: #ffedd5 !important;
	}

	:global(.host-card .search-mode-copy),
	:global(.host-card .running-job-summary),
	:global(.host-card .tray-headline),
	:global(.host-card .attention-kicker),
	:global(.queue-detail-shell summary),
	:global(.attention-title),
	:global(.encode-telemetry-summary),
	:global(.encode-telemetry-title) {
		color: #f8fafc !important;
	}

	.ops-return-link {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		padding: 0.72rem 0.95rem;
		border: 1px solid rgba(56, 189, 248, 0.22);
		background: rgba(15, 23, 42, 0.72);
		color: #e2e8f0;
		font-weight: 700;
		text-decoration: none;
	}

	.ops-return-link:hover {
		border-color: rgba(56, 189, 248, 0.5);
		background: rgba(30, 41, 59, 0.94);
	}

	@media (max-width: 960px) {
		.ops-hero {
			grid-template-columns: 1fr;
		}

		.ops-hero-side {
			justify-items: start;
		}

		.ops-pill-row {
			justify-content: flex-start;
		}
	}
</style>
