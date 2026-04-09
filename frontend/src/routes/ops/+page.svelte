<script lang="ts">
	import { browser } from '$app/environment';
	import { resolve } from '$app/paths';
	import type { DashboardSummaryPayload, HostsPayload } from '$lib/api/types';
	import { goto, invalidateAll } from '$app/navigation';
	import DashboardHostGrid from '$lib/components/dashboard/DashboardHostGrid.svelte';
	import DashboardQueues from '$lib/components/dashboard/DashboardQueues.svelte';
	import Panel from '$lib/components/Panel.svelte';
	import Pill from '$lib/components/Pill.svelte';
	import SectionHead from '$lib/components/SectionHead.svelte';
	import { hostSettingsAnchor } from '$lib/format';
	import { allQueueWorkersScheduledOffWindow, nextQueueWindowCopy } from '$lib/hosts/schedule';
	import { toasts } from '$lib/stores/toasts';

	let {
		data
	}: {
		data: {
			dashboard: DashboardSummaryPayload;
			hosts: HostsPayload;
		};
	} = $props();

	const dashboard = $derived(data.dashboard);
	const hosts = $derived(data.hosts);
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
		{ label: fleetSnapshotLabel, variant: dashboard.encode_queue.queued_count > 0 ? 'warn' as const : 'ghost' as const },
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

	async function runQueueAction(action: 'pause' | 'resume' | 'stop') {
		if (
			action === 'stop' &&
			browser &&
			!window.confirm('Stop active encodes and clear the queue? This cannot be undone.')
		) {
			return;
		}

		queueAction = action;
		try {
			const response = await fetch(`/api/encode-queue/${action}`, { method: 'POST' });
			const payload = (await response.json()) as { message?: string };
			if (!response.ok) {
				toasts.error('Queue update failed', payload.message ?? `Could not ${action} queue.`);
				return;
			}
			toasts.success('Queue updated', payload.message ?? 'Queue updated.');
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

	async function stopCalibrationQueue() {
		if (
			browser &&
			!window.confirm('Stop active calibrations and clear queued calibration jobs? This cannot be undone.')
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
			await invalidateAll();
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
</script>

<svelte:head>
	<title>Ops · Mediaforce</title>
</svelte:head>

<div class="page-stack">
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
				<a class="ops-return-link" href={resolve('/')}>
					Back to folders
				</a>
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
		gap: var(--space-4);
	}

	.ops-hero {
		display: flex;
		justify-content: space-between;
		align-items: end;
		gap: var(--space-3);
		flex-wrap: wrap;
	}

	.ops-hero-side {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: 0.75rem;
		justify-items: start;
	}

	.ops-pill-row {
		display: flex;
		flex-wrap: wrap;
		gap: 0.7rem;
	}

	:global(.ops-hero-panel) {
		background:
			radial-gradient(circle at top left, rgba(15, 118, 110, 0.14), transparent 36%),
			linear-gradient(145deg, rgba(255, 253, 247, 0.9), rgba(244, 237, 224, 0.84));
	}

	.ops-return-link {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		padding: 0.72rem 0.95rem;
		border-radius: var(--radius-pill);
		border: 1px solid rgba(15, 118, 110, 0.18);
		background: rgba(15, 118, 110, 0.1);
		color: var(--accent-deep);
		font-weight: 700;
		text-decoration: none;
	}
</style>
