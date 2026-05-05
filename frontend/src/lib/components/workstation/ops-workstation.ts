import type {
	DashboardSummaryPayload,
	EncodeQueueJob,
	HostRuntime,
	HostsPayload
} from '$lib/api/types';
import type { FooterSignal, ShellTone, StatusTile } from './OperatorShell.svelte';

export type OpsQueueKind = 'encode' | 'sample' | 'proof';
export type OpsActionId =
	| 'pause-encode'
	| 'resume-encode'
	| 'retry-failed-encode'
	| 'retry-encode-prefix'
	| 'stop-encode'
	| 'stop-calibration'
	| 'start-host'
	| 'prepare-host'
	| 'reset-host-trust';

export type OpsQueueRow = {
	key: string;
	kind: OpsQueueKind;
	tone: ShellTone;
	status: string;
	prefix: string;
	host: string;
	phase: string;
	progress: string;
	scheduler: string;
	detail: string;
	action?: OpsActionId;
	actionScope?: 'global' | 'row';
};

export type OpsBlocker = {
	key: string;
	tone: ShellTone;
	title: string;
	detail: string;
	action?: OpsActionId;
};

type CalibrationJob = Record<string, unknown>;

function compactText(value: unknown): string {
	return typeof value === 'string' ? value.trim() : '';
}

function numberValue(value: unknown): number {
	return typeof value === 'number' && Number.isFinite(value) ? value : 0;
}

function percentCopy(value: unknown): string {
	if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
	return `${Math.round(value)}%`;
}

function record(value: unknown): Record<string, unknown> | null {
	return value && typeof value === 'object' ? (value as Record<string, unknown>) : null;
}

function hostCopy(value: unknown): string {
	const host = record(value);
	if (!host) return 'unassigned';
	return compactText(host.label) || compactText(host.key) || compactText(host.host) || 'host';
}

function calibrationHostCopy(job: CalibrationJob): string {
	const host = record(job.host);
	return hostCopy(host) || compactText(job.host_key) || 'unassigned';
}

function calibrationPrefix(job: CalibrationJob): string {
	return compactText(job.prefix) || compactText(job.folder_prefix) || 'runtime scope';
}

function calibrationDetail(job: CalibrationJob): string {
	const raw =
		compactText(job.error) ||
		compactText(job.notes) ||
		compactText(job.operator_note) ||
		compactText(job.created_at) ||
		'waiting for worker update';
	return (
		raw
			.split('\n')
			.map((line) => line.trim())
			.filter(Boolean)
			.at(-1)
			?.slice(0, 180) ?? 'waiting for worker update'
	);
}

function statusTone(status: string): ShellTone {
	if (['failed', 'needs_attention', 'stopped', 'error'].includes(status)) return 'fail';
	if (['running', 'processing', 'active'].includes(status)) return 'active';
	if (['queued', 'pending_review', 'retry_backoff', 'waiting'].includes(status)) return 'wait';
	if (['completed', 'ready'].includes(status)) return 'ready';
	return 'idle';
}

function statusCopy(status: string): string {
	return status.replaceAll('_', ' ');
}

export function encodeJobTone(job: EncodeQueueJob): ShellTone {
	if (job.status === 'retry_backoff') return 'wait';
	return statusTone(String(job.status ?? '').toLowerCase());
}

export function encodeJobProgress(job: EncodeQueueJob): string {
	const progress = job.progress;
	if (!progress) {
		const completed = numberValue(job.completed_shard_count);
		const total = numberValue(job.shard_count);
		return total > 0 ? `${completed}/${total} shards` : '—';
	}
	const percent = percentCopy(progress.percent_complete);
	const item =
		progress.current_item_number && progress.total_item_count
			? `${progress.current_item_number}/${progress.total_item_count}`
			: '';
	return [percent, item].filter((part) => part && part !== '—').join(' · ') || percent;
}

export function encodeJobDetail(job: EncodeQueueJob): string {
	return (
		job.error ||
		job.attempt_summary ||
		job.telemetry_summary ||
		job.progress?.current_item_rel_path ||
		job.progress?.failure_analysis?.summary ||
		'waiting for queue telemetry'
	);
}

export function buildEncodeRows(
	dashboard: DashboardSummaryPayload | null | undefined
): OpsQueueRow[] {
	const queue = dashboard?.encode_queue;
	if (!queue) return [];
	const recentAttention = (queue.recent ?? []).filter((job) =>
		['failed', 'needs_attention', 'stopped'].includes(String(job.status ?? '').toLowerCase())
	);
	return [...queue.running, ...queue.queued, ...recentAttention].map((job) => ({
		key: `encode:${job.job_id}`,
		kind: 'encode',
		tone: encodeJobTone(job),
		status: statusCopy(job.status || 'unknown'),
		prefix: job.prefix || 'runtime scope',
		host: job.active_hosts?.map(hostCopy).filter(Boolean).join(', ') || hostCopy(job.host),
		phase: job.running_shard_count ? `${job.running_shard_count} active shards` : 'encode queue',
		progress: encodeJobProgress(job),
		scheduler: job.scheduler_status_copy || (job.schedule_waiting ? 'schedule waiting' : 'ready'),
		detail: encodeJobDetail(job),
		action: ['failed', 'needs_attention', 'stopped', 'retry_backoff'].includes(job.status)
			? 'retry-encode-prefix'
			: undefined,
		actionScope: ['failed', 'needs_attention', 'stopped', 'retry_backoff'].includes(job.status)
			? 'row'
			: undefined
	}));
}

function buildCalibrationLaneRows(
	laneName: OpsQueueKind,
	jobs: CalibrationJob[] | undefined,
	status: string
): OpsQueueRow[] {
	return (jobs ?? []).map((job, index) => ({
		key: `${laneName}:${compactText(job.job_id) || compactText(job.prefix) || `${status}:${index}`}`,
		kind: laneName,
		tone: statusTone(status),
		status: statusCopy(status),
		prefix: calibrationPrefix(job),
		host: calibrationHostCopy(job),
		phase: laneName === 'sample' ? 'sample calibration' : 'proof encode',
		progress: compactText(job.progress) || compactText(job.stage) || '—',
		scheduler:
			compactText(job.scheduler_status_copy) || compactText(job.created_at) || 'queued order',
		detail: calibrationDetail(job)
	}));
}

export function rowRecoveryLabel(row: OpsQueueRow): string {
	if (!row.action) return 'No action';
	if (row.action === 'retry-encode-prefix') return 'Retry folder';
	if (row.action === 'retry-failed-encode') return 'Retry all';
	if (row.action === 'stop-calibration') return 'Stop samples';
	return row.actionScope === 'global' ? 'Global action' : 'Run action';
}

export function rowRecoveryTitle(row: OpsQueueRow): string {
	if (!row.action) return 'No runtime action is available for this row.';
	if (row.action === 'retry-encode-prefix') {
		return 'Retry the failed encode for this folder prefix only.';
	}
	if (row.action === 'retry-failed-encode') {
		return 'Runs the global retry for all approved failed folder encodes.';
	}
	return row.actionScope === 'global'
		? 'Runs a global queue action; this is not scoped to one row.'
		: 'Runs the action for this row.';
}

export function hostPrepareDisabled(host: HostRuntime, password: string): boolean {
	return (
		host.setup_supported === false || (Boolean(host.setup_requires_password) && !password.trim())
	);
}

export function hostPrepareTitle(host: HostRuntime): string {
	if (host.setup_supported === false) return 'Prepare is unavailable for this host.';
	if (host.setup_requires_password) return 'Enter the prepare password for this host.';
	return 'Prepare this host for Mediaforce work';
}

export function buildCalibrationRows(
	dashboard: DashboardSummaryPayload | null | undefined
): OpsQueueRow[] {
	const queue = dashboard?.calibration_queue;
	if (!queue) return [];
	return [
		...buildCalibrationLaneRows('sample', queue.sample.running, 'running'),
		...buildCalibrationLaneRows('sample', queue.sample.queued, 'queued'),
		...buildCalibrationLaneRows('sample', queue.sample.pending_review, 'pending_review'),
		...buildCalibrationLaneRows('sample', queue.sample.recent_failed, 'failed'),
		...buildCalibrationLaneRows('proof', queue.full.running, 'running'),
		...buildCalibrationLaneRows('proof', queue.full.queued, 'queued'),
		...buildCalibrationLaneRows('proof', queue.full.pending_review, 'pending_review'),
		...buildCalibrationLaneRows('proof', queue.full.recent_failed, 'failed')
	];
}

export function buildOpsQueueRows(
	dashboard: DashboardSummaryPayload | null | undefined
): OpsQueueRow[] {
	return [...buildEncodeRows(dashboard), ...buildCalibrationRows(dashboard)];
}

export function buildOpsBlockers(
	dashboard: DashboardSummaryPayload | null | undefined,
	hosts: HostsPayload | null | undefined,
	loadError: string | null
): OpsBlocker[] {
	const blockers: OpsBlocker[] = [];
	const queue = dashboard?.encode_queue;
	const attentionCount = queue?.needs_attention_count ?? 0;
	if (loadError) {
		blockers.push({
			key: 'runtime-load',
			tone: 'fail',
			title: 'Runtime data partial',
			detail: loadError
		});
	}
	if (queue?.state.stop_requested) {
		blockers.push({
			key: 'stop-requested',
			tone: 'fail',
			title: 'Encode stop requested',
			detail: 'Workers are draining current encode work before the queue can resume.',
			action: 'resume-encode'
		});
	}
	if (queue?.state.is_paused) {
		blockers.push({
			key: 'paused',
			tone: 'wait',
			title: 'Encode scheduler paused',
			detail: queue.state.scheduler_summary ?? 'No new encode jobs will start.',
			action: 'resume-encode'
		});
	}
	if (attentionCount > 0) {
		blockers.push({
			key: 'needs-attention',
			tone: 'fail',
			title: `${attentionCount} encode ${attentionCount === 1 ? 'job needs' : 'jobs need'} attention`,
			detail: 'Retry after reviewing the failed rows in the queue table.',
			action: 'retry-failed-encode'
		});
	}
	for (const host of hosts?.hosts ?? []) {
		if (!host.available || host.schedule_open === false || host.queue_active === false) {
			blockers.push({
				key: `host:${host.key}`,
				tone: host.available ? 'wait' : 'fail',
				title: `${host.label} not ready`,
				detail:
					host.schedule_open === false ? host.schedule_detail : host.message || host.active_reason,
				action: !host.available && host.setup_supported !== false ? 'start-host' : undefined
			});
		}
	}
	return blockers;
}

export function buildOpsStatusTiles(
	dashboard: DashboardSummaryPayload | null | undefined,
	hosts: HostsPayload | null | undefined,
	loadError: string | null
): StatusTile[] {
	const encode = dashboard?.encode_queue;
	const calibration = dashboard?.calibration_queue;
	const readyHosts = hosts?.hosts.filter((host) => host.available).length ?? 0;
	const totalHosts = hosts?.hosts.length ?? 0;
	return [
		{
			label: 'Scheduler',
			value: encode?.state.is_paused
				? 'paused'
				: encode?.state.stop_requested
					? 'stopping'
					: 'open',
			detail:
				encode?.state.scheduler_summary ?? (loadError ? 'runtime partial' : 'scheduler state'),
			tone: loadError
				? 'fail'
				: encode?.state.stop_requested
					? 'fail'
					: encode?.state.is_paused
						? 'wait'
						: 'ready'
		},
		{
			label: 'Encode jobs',
			value: `${encode?.running_count ?? 0} running · ${encode?.queued_count ?? 0} queued`,
			detail: encode?.telemetry?.eta_copy ?? `${encode?.needs_attention_count ?? 0} need attention`,
			tone:
				(encode?.needs_attention_count ?? 0) > 0
					? 'fail'
					: (encode?.running_count ?? 0) > 0
						? 'active'
						: (encode?.queued_count ?? 0) > 0
							? 'wait'
							: 'idle'
		},
		{
			label: 'Samples',
			value: `${calibration?.sample.running_count ?? 0} running · ${calibration?.sample.queued_count ?? 0} queued`,
			detail: `${calibration?.sample.pending_review_count ?? 0} waiting for review`,
			tone: (calibration?.active_count ?? 0) > 0 ? 'active' : 'idle'
		},
		{
			label: 'Hosts',
			value: `${readyHosts} ready / ${totalHosts}`,
			detail: totalHosts ? 'compact runtime probe' : 'no host payload',
			tone: readyHosts > 0 ? 'ready' : totalHosts > 0 ? 'wait' : 'idle'
		}
	];
}

export function buildOpsFooterSignals(
	dashboard: DashboardSummaryPayload | null | undefined,
	hosts: HostsPayload | null | undefined
): FooterSignal[] {
	const encode = dashboard?.encode_queue;
	const calibration = dashboard?.calibration_queue;
	return [
		{
			label: 'Encode',
			value: `${encode?.running_count ?? 0}/${encode?.queued_count ?? 0}`,
			tone:
				(encode?.running_count ?? 0) > 0
					? 'active'
					: (encode?.queued_count ?? 0) > 0
						? 'wait'
						: 'idle'
		},
		{
			label: 'Attention',
			value: String(encode?.needs_attention_count ?? 0),
			tone: (encode?.needs_attention_count ?? 0) > 0 ? 'fail' : 'idle'
		},
		{
			label: 'Samples',
			value: String(calibration?.active_count ?? 0),
			tone: (calibration?.active_count ?? 0) > 0 ? 'active' : 'idle'
		},
		{
			label: 'Hosts',
			value: `${hosts?.hosts.filter((host) => host.available).length ?? 0}/${hosts?.hosts.length ?? 0}`
		}
	];
}

export function hostTone(host: HostRuntime): ShellTone {
	if (!host.available) return 'fail';
	if (host.schedule_open === false || host.queue_active === false) return 'wait';
	if (host.active_encode_count > 0) return 'active';
	return 'ready';
}

export function hostStateCopy(host: HostRuntime): string {
	if (!host.available) return 'Unavailable';
	if (host.schedule_open === false) return 'Off window';
	if (host.queue_active === false) return 'Idle';
	if (host.active_encode_count > 0) return 'Encoding';
	return 'Ready';
}
