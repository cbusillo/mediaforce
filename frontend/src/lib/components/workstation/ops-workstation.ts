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

export type OpsReadinessSummary = {
	tone: ShellTone;
	title: string;
	detail: string;
	metricLabel: string;
	metricValue: string;
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
	return compactText(job.prefix) || compactText(job.folder_prefix) || 'system scope';
}

function calibrationDetail(job: CalibrationJob): string {
	const raw =
		compactText(job.error) ||
		compactText(job.notes) ||
		compactText(job.operator_note) ||
		compactText(job.created_at) ||
		'waiting for worker update';
	const detail =
		raw
			.split('\n')
			.map((line) => line.trim())
			.filter(Boolean)
			.at(-1)
			?.slice(0, 180) ?? 'waiting for worker update';
	const normalized = detail.toLowerCase();
	if (normalized.includes('failed to find a suitable crf')) {
		return 'Sample check did not find a usable quality setting.';
	}
	if (normalized.includes('interrupted by a web process restart')) {
		return 'Sample check was interrupted before it finished.';
	}
	if (normalized.includes('queue job was stopped')) {
		return 'Sample check was stopped and cleaned up.';
	}
	return detail.replace(/^error:\s*/i, '');
}

function statusTone(status: string): ShellTone {
	if (['failed', 'needs_attention', 'stopped', 'error'].includes(status)) return 'fail';
	if (['running', 'processing', 'active'].includes(status)) return 'active';
	if (['queued', 'pending_review', 'retry_backoff', 'waiting'].includes(status)) return 'wait';
	if (['completed', 'ready'].includes(status)) return 'ready';
	return 'idle';
}

function statusCopy(status: string): string {
	const normalized = status.toLowerCase();
	if (normalized === 'needs_attention' || normalized === 'failed' || normalized === 'stopped') {
		return 'Retry available';
	}
	if (normalized === 'retry_backoff') return 'Retry waiting';
	if (normalized === 'pending_review') return 'Needs review';
	return normalized.replaceAll('_', ' ');
}

export function encodeJobTone(job: EncodeQueueJob): ShellTone {
	const status = String(job.status ?? '').toLowerCase();
	if (['failed', 'needs_attention', 'stopped', 'retry_backoff'].includes(status)) return 'wait';
	return statusTone(status);
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

function encodeJobTimestamp(job: EncodeQueueJob): number {
	const raw = job.updated_at || job.finished_at || job.created_at || '';
	const timestamp = Date.parse(raw);
	return Number.isFinite(timestamp) ? timestamp : 0;
}

export function retryableEncodeJobIds(jobs: EncodeQueueJob[]): Set<string> {
	const prefixRetryStatuses = new Set(['failed', 'needs_attention', 'stopped']);
	const latestJobByPrefix = new Map<string, EncodeQueueJob>();
	for (const job of jobs) {
		const prefix = String(job.prefix ?? '').trim();
		if (!prefix) continue;
		const current = latestJobByPrefix.get(prefix);
		if (!current || encodeJobTimestamp(job) >= encodeJobTimestamp(current)) {
			latestJobByPrefix.set(prefix, job);
		}
	}

	const retryableJobIds = new Set<string>();
	for (const job of latestJobByPrefix.values()) {
		if (prefixRetryStatuses.has(String(job.status ?? '').toLowerCase())) {
			retryableJobIds.add(job.job_id);
		}
	}
	return retryableJobIds;
}

export function buildEncodeRows(
	dashboard: DashboardSummaryPayload | null | undefined
): OpsQueueRow[] {
	const queue = dashboard?.encode_queue;
	if (!queue) return [];
	const prefixRetryStatuses = new Set(['failed', 'needs_attention', 'stopped']);
	const recentAttention = (queue.recent ?? []).filter((job) =>
		prefixRetryStatuses.has(String(job.status ?? '').toLowerCase())
	);
	const displayJobs = [...queue.running, ...queue.queued, ...recentAttention];
	const retryableJobIds = retryableEncodeJobIds(displayJobs);
	return displayJobs.map((job) => {
		const status = String(job.status ?? '').toLowerCase();
		const canRetryPrefix = prefixRetryStatuses.has(status) && retryableJobIds.has(job.job_id);
		return {
			key: `encode:${job.job_id}`,
			kind: 'encode',
			tone: encodeJobTone(job),
			status: statusCopy(job.status || 'unknown'),
			prefix: job.prefix || 'system scope',
			host: job.active_hosts?.map(hostCopy).filter(Boolean).join(', ') || hostCopy(job.host),
			phase: job.running_shard_count ? `${job.running_shard_count} active shards` : 'encode queue',
			progress: encodeJobProgress(job),
			scheduler: job.scheduler_status_copy || (job.schedule_waiting ? 'schedule waiting' : 'ready'),
			detail: encodeJobDetail(job),
			action: canRetryPrefix ? 'retry-encode-prefix' : undefined,
			actionScope: canRetryPrefix ? 'row' : undefined
		};
	});
}

function buildCalibrationLaneRows(
	laneName: OpsQueueKind,
	jobs: CalibrationJob[] | undefined,
	status: string,
	options: { historical?: boolean } = {}
): OpsQueueRow[] {
	return (jobs ?? []).map((job, index) => ({
		key: `${laneName}:${compactText(job.job_id) || compactText(job.prefix) || `${status}:${index}`}`,
		kind: laneName,
		tone: options.historical ? 'idle' : statusTone(status),
		status: options.historical ? 'History' : statusCopy(status),
		prefix: calibrationPrefix(job),
		host: calibrationHostCopy(job),
		phase: options.historical
			? laneName === 'sample'
				? 'sample check history'
				: 'proof encode history'
			: laneName === 'sample'
				? 'sample check'
				: 'proof encode',
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
	if (!row.action) return 'No action is available for this row.';
	if (row.action === 'retry-encode-prefix') {
		return 'Retry the encode for this folder only.';
	}
	if (row.action === 'retry-failed-encode') {
		return 'Retry every approved folder that has retry available.';
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
	if (host.setup_supported === false) return 'Prepare is unavailable for this worker.';
	if (host.setup_requires_password) return 'Enter the prepare password for this worker.';
	return 'Prepare this worker for Mediaforce work';
}

export function buildCalibrationRows(
	dashboard: DashboardSummaryPayload | null | undefined,
	{ includeHistory = true }: { includeHistory?: boolean } = {}
): OpsQueueRow[] {
	const queue = dashboard?.calibration_queue;
	if (!queue) return [];
	const currentRows = [
		...buildCalibrationLaneRows('sample', queue.sample.running, 'running'),
		...buildCalibrationLaneRows('sample', queue.sample.queued, 'queued'),
		...buildCalibrationLaneRows('sample', queue.sample.pending_review, 'pending_review'),
		...buildCalibrationLaneRows('proof', queue.full.running, 'running'),
		...buildCalibrationLaneRows('proof', queue.full.queued, 'queued'),
		...buildCalibrationLaneRows('proof', queue.full.pending_review, 'pending_review')
	];
	if (!includeHistory) return currentRows;
	return [...currentRows, ...buildOpsHistoryRows(dashboard)];
}

export function buildOpsHistoryRows(
	dashboard: DashboardSummaryPayload | null | undefined
): OpsQueueRow[] {
	const queue = dashboard?.calibration_queue;
	if (!queue) return [];
	return [
		...buildCalibrationLaneRows('sample', queue.sample.recent_failed, 'failed', {
			historical: true
		}),
		...buildCalibrationLaneRows('proof', queue.full.recent_failed, 'failed', {
			historical: true
		})
	];
}

export function buildOpsQueueRows(
	dashboard: DashboardSummaryPayload | null | undefined
): OpsQueueRow[] {
	return [
		...buildEncodeRows(dashboard),
		...buildCalibrationRows(dashboard, { includeHistory: false })
	];
}

export function buildOpsBlockers(
	dashboard: DashboardSummaryPayload | null | undefined,
	hosts: HostsPayload | null | undefined,
	loadError: string | null
): OpsBlocker[] {
	const blockers: OpsBlocker[] = [];
	const queue = dashboard?.encode_queue;
	const attentionCount = queue?.needs_attention_count ?? 0;
	const readyHosts = hosts?.hosts.filter((host) => host.available).length ?? 0;
	const totalHosts = hosts?.hosts.length ?? 0;
	const queuedWork = (queue?.queued_count ?? 0) + (queue?.running_count ?? 0);
	const scheduleWaiting = queue?.queued_waiting_count ?? 0;
	if (loadError) {
		blockers.push({
			key: 'runtime-load',
			tone: 'fail',
			title: 'Mediaforce data unavailable',
			detail: loadError
		});
	}
	if (queue?.state.stop_requested) {
		blockers.push({
			key: 'stop-requested',
			tone: 'fail',
			title: 'Encoding is stopping',
			detail: 'Workers are finishing current items before the queue can start more work.',
			action: 'resume-encode'
		});
	}
	if (queue?.state.is_paused) {
		blockers.push({
			key: 'paused',
			tone: 'wait',
			title: 'Encoding is paused',
			detail: queue.state.scheduler_summary ?? 'No new encode jobs will start until resumed.',
			action: 'resume-encode'
		});
	}
	if (attentionCount > 0) {
		blockers.push({
			key: 'needs-attention',
			tone: 'wait',
			title: `${attentionCount} encode ${attentionCount === 1 ? 'job has' : 'jobs have'} retry available`,
			detail: 'Review the row, then retry approved folders from this page.',
			action: 'retry-failed-encode'
		});
	}
	if (totalHosts > 0 && readyHosts === 0 && queuedWork > 0) {
		blockers.push({
			key: 'no-hosts-ready',
			tone: 'fail',
			title: 'No workers can encode right now',
			detail:
				'Queued work exists, but every configured worker is unavailable or outside its work window.'
		});
	} else if (scheduleWaiting > 0) {
		blockers.push({
			key: 'schedule-waiting',
			tone: 'wait',
			title: `${scheduleWaiting} queued ${scheduleWaiting === 1 ? 'folder is' : 'folders are'} waiting for schedule`,
			detail:
				queue?.state.scheduler_summary ?? 'Work will start when an allowed encode window opens.'
		});
	}
	return blockers;
}

export function buildOpsReadinessSummary(
	dashboard: DashboardSummaryPayload | null | undefined,
	hosts: HostsPayload | null | undefined,
	loadError: string | null
): OpsReadinessSummary {
	const queue = dashboard?.encode_queue;
	const calibration = dashboard?.calibration_queue;
	const readyHosts = hosts?.hosts.filter((host) => host.available).length ?? 0;
	const totalHosts = hosts?.hosts.length ?? 0;
	const runningCount = queue?.running_count ?? 0;
	const queuedCount = queue?.queued_count ?? 0;
	const queuedWaiting = queue?.queued_waiting_count ?? 0;
	const needsAttention = queue?.needs_attention_count ?? 0;
	const activeChecks = calibration?.active_count ?? 0;
	const queuedWork = runningCount + queuedCount;

	if (loadError) {
		return {
			tone: 'fail',
			title: 'Ops data is unavailable',
			detail: loadError,
			metricLabel: 'Data',
			metricValue: 'offline'
		};
	}
	if (queue?.state.stop_requested) {
		return {
			tone: 'fail',
			title: 'Encoding is stopping',
			detail: 'Workers are finishing current items before Mediaforce can start more work.',
			metricLabel: 'Active work',
			metricValue: String(runningCount)
		};
	}
	if (queue?.state.is_paused) {
		return {
			tone: 'wait',
			title: 'Encoding is paused',
			detail: queue.state.scheduler_summary ?? 'Resume the queue when encoding should continue.',
			metricLabel: 'Queued',
			metricValue: String(queuedCount)
		};
	}
	if (needsAttention > 0) {
		return {
			tone: 'wait',
			title: 'Retry is available',
			detail: `${needsAttention} approved encode ${needsAttention === 1 ? 'job needs' : 'jobs need'} operator review before retry.`,
			metricLabel: 'Retry',
			metricValue: String(needsAttention)
		};
	}
	if (totalHosts > 0 && readyHosts === 0 && queuedWork > 0) {
		return {
			tone: 'fail',
			title: 'No worker can work right now',
			detail:
				'Queued work exists, but every configured worker is unavailable or outside its work window.',
			metricLabel: 'Workers ready',
			metricValue: '0'
		};
	}
	if (runningCount > 0 || activeChecks > 0) {
		return {
			tone: 'active',
			title: 'Mediaforce is working',
			detail:
				queue?.telemetry?.eta_copy ??
				`${runningCount} encode ${runningCount === 1 ? 'job' : 'jobs'} and ${activeChecks} sample/proof ${activeChecks === 1 ? 'job' : 'jobs'} active.`,
			metricLabel: 'Running',
			metricValue: String(runningCount + activeChecks)
		};
	}
	if (queuedWaiting > 0) {
		return {
			tone: 'wait',
			title: 'Waiting for the encode window',
			detail:
				queue?.state.scheduler_summary ?? 'Queued work will start when an allowed window opens.',
			metricLabel: 'Waiting',
			metricValue: String(queuedWaiting)
		};
	}
	if (readyHosts > 0) {
		return {
			tone: 'ready',
			title: 'Ready for work',
			detail: 'Workers are available and Mediaforce can start eligible encode work.',
			metricLabel: 'Workers ready',
			metricValue: String(readyHosts)
		};
	}
	return {
		tone: 'idle',
		title: 'Standing by',
		detail: totalHosts > 0 ? 'No current work is waiting on Ops.' : 'Worker status is unavailable.',
		metricLabel: 'Queued',
		metricValue: String(queuedCount)
	};
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
					: 'ready',
			detail:
				encode?.state.scheduler_summary ?? (loadError ? 'data unavailable' : 'work window state'),
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
			detail:
				encode?.telemetry?.eta_copy ?? `${encode?.needs_attention_count ?? 0} retry available`,
			tone:
				(encode?.needs_attention_count ?? 0) > 0
					? 'wait'
					: (encode?.running_count ?? 0) > 0
						? 'active'
						: (encode?.queued_count ?? 0) > 0
							? 'wait'
							: 'idle'
		},
		{
			label: 'Sample checks',
			value: `${calibration?.sample.running_count ?? 0} running · ${calibration?.sample.queued_count ?? 0} queued`,
			detail: `${calibration?.sample.pending_review_count ?? 0} waiting for review`,
			tone: (calibration?.active_count ?? 0) > 0 ? 'active' : 'idle'
		},
		{
			label: 'Workers',
			value: `${readyHosts} ready / ${totalHosts}`,
			detail: totalHosts
				? readyHosts > 0
					? 'capacity available'
					: 'no worker can start work'
				: 'worker status unavailable',
			tone: readyHosts > 0 ? 'ready' : totalHosts > 0 ? 'fail' : 'idle'
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
			tone: (encode?.needs_attention_count ?? 0) > 0 ? 'wait' : 'idle'
		},
		{
			label: 'Checks',
			value: String(calibration?.active_count ?? 0),
			tone: (calibration?.active_count ?? 0) > 0 ? 'active' : 'idle'
		},
		{
			label: 'Workers',
			value: `${hosts?.hosts.filter((host) => host.available).length ?? 0}/${hosts?.hosts.length ?? 0}`
		}
	];
}

export function hostTone(host: HostRuntime, fleetHasReadyCapacity = false): ShellTone {
	if (!host.available) return fleetHasReadyCapacity ? 'wait' : 'fail';
	if (host.schedule_open === false || host.queue_active === false) return 'idle';
	if (host.active_encode_count > 0) return 'active';
	return 'ready';
}

export function hostStateCopy(host: HostRuntime): string {
	if (!host.available) return 'Unavailable';
	if (host.schedule_open === false) return 'Scheduled off';
	if (host.queue_active === false) return 'Not accepting';
	if (host.active_encode_count > 0) return 'Encoding';
	return 'Ready';
}
