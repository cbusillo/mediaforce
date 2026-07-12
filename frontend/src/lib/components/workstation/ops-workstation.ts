import type {
	DashboardSummaryPayload,
	EncodeQueueJob,
	HostRuntime,
	HostsPayload
} from '$lib/api/types';
import type { FooterSignal, ShellTone, StatusTile } from './shell-types';

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

function activeJobStatus(status: unknown): boolean {
	return ['running', 'processing', 'active', 'queued'].includes(String(status ?? '').toLowerCase());
}

function hostEncodeReady(host: HostRuntime): boolean {
	const storageRecovery = host.storage_recovery_available === true;
	return (
		(host.available || storageRecovery) &&
		host.schedule_open !== false &&
		(storageRecovery || host.queue_active !== false) &&
		numberValue(host.active_encode_count) < numberValue(host.max_parallel_encodes)
	);
}

function hostCapacityCounts(hosts: HostsPayload | null | undefined) {
	const rows = hosts?.hosts ?? [];
	const available = rows.filter(
		(host) => host.available || host.storage_recovery_available === true
	).length;
	return {
		available,
		activeEncodes: rows.reduce((total, host) => total + numberValue(host.active_encode_count), 0),
		encodeReady: rows.filter(hostEncodeReady).length,
		busy: rows.filter(
			(host) =>
				(host.available || host.storage_recovery_available === true) &&
				numberValue(host.active_encode_count) > 0
		).length,
		scheduledOff: rows.filter(
			(host) =>
				(host.available || host.storage_recovery_available === true) && host.schedule_open === false
		).length,
		unavailable: Math.max(rows.length - available, 0),
		total: rows.length
	};
}

function hostCopy(value: unknown): string {
	const host = record(value);
	if (!host) return '';
	return compactText(host.label) || compactText(host.key) || compactText(host.host);
}

function calibrationHostCopy(job: CalibrationJob): string {
	const host = record(job.host);
	return hostCopy(host) || compactText(job.host_key) || 'unassigned';
}

function encodeHostCopy(job: EncodeQueueJob): string {
	const activeHosts = job.active_hosts?.map(hostCopy).filter(Boolean).join(', ');
	if (activeHosts) return activeHosts;
	const progressHosts = job.progress?.active_host_labels
		?.map(compactText)
		.filter(Boolean)
		.join(', ');
	if (progressHosts) return progressHosts;
	const assignedHost = hostCopy(job.host);
	if (assignedHost) return assignedHost;
	const status = String(job.status ?? '').toLowerCase();
	return ['queued', 'retry_backoff'].includes(status) ? 'Selecting computer' : 'Unassigned';
}

function operatorErrorCopy(value: unknown): string {
	const detail = compactText(value);
	if (!detail) return '';
	const permissionPath = detail.match(/permission denied:\s*['"]([^'"]+)['"]/i)?.[1];
	if (permissionPath) {
		return `Mediaforce cannot access ${permissionPath} on this computer. Mount the storage, then retry.`;
	}
	const missingPath = detail.match(/no such file or directory:\s*['"]([^'"]+)['"]/i)?.[1];
	if (missingPath) {
		return `Mediaforce cannot find ${missingPath} on this computer. Mount the storage, then retry.`;
	}
	return detail;
}

function operatorErrorSummary(value: unknown): string {
	const detail = compactText(value);
	const permissionPath = detail.match(/permission denied:\s*['"]([^'"]+)['"]/i)?.[1];
	if (permissionPath) return 'Storage unavailable';
	const missingPath = detail.match(/no such file or directory:\s*['"]([^'"]+)['"]/i)?.[1];
	if (missingPath) return 'Storage not found';
	return operatorErrorCopy(detail);
}

function compactTelemetryCopy(value: string): string {
	return value.replace(/^\d+(?:\.\d+)?%\s*·\s*/, '').replace(/Est\. ETA\s*/i, 'ETA ');
}

function controllerStorageWaitingJobs(
	dashboard: DashboardSummaryPayload | null | undefined
): EncodeQueueJob[] {
	return (dashboard?.encode_queue?.queued ?? []).filter((job) => {
		const schedulerCopy = compactText(job.scheduler_status_copy);
		return (
			schedulerCopy.startsWith('Mediaforce cannot access ') &&
			schedulerCopy.includes('Mount the storage')
		);
	});
}

function calibrationPrefix(job: CalibrationJob): string {
	return compactText(job.prefix) || compactText(job.folder_prefix) || 'system scope';
}

function calibrationDetail(job: CalibrationJob): string {
	const status = String(job.status ?? '').toLowerCase();
	const raw =
		(activeJobStatus(status) ? '' : compactText(job.error)) ||
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

export function workerCapabilityLabel(capability: string): string {
	const normalized = capability.trim().toLowerCase();
	if (normalized === 'encode_queue') return 'Process folders';
	if (normalized === 'sample_calibration') return 'Run samples';
	if (normalized === 'proof_encode') return 'Run review evidence';
	return capability.trim().replaceAll('_', ' ') || 'Mediaforce work';
}

export function workerCapabilitiesSummary(capabilities: string[]): string {
	const labels = capabilities.map(workerCapabilityLabel).filter(Boolean);
	return labels.length ? labels.join(' · ') : 'No work assigned';
}

export function hostWorkReason(
	host: HostRuntime,
	hosts: HostsPayload | null | undefined,
	dashboard: DashboardSummaryPayload | null | undefined
): string {
	if (host.available && host.schedule_open === false) {
		return host.schedule_detail || 'Outside its schedule; this is a normal wait.';
	}
	const activeCount = numberValue(host.active_encode_count);
	const maxParallel = Math.max(numberValue(host.max_parallel_encodes), 1);
	if (host.available && activeCount > 0) {
		return activeCount >= maxParallel
			? 'Working at capacity.'
			: `Processing ${activeCount} episode part${activeCount === 1 ? '' : 's'}; capacity remains.`;
	}
	if (host.available && host.queue_active !== false) {
		const queue = dashboard?.encode_queue;
		const allCurrentWorkAssigned =
			(queue?.running_count ?? 0) > 0 && (queue?.queued_count ?? 0) === 0;
		if (allCurrentWorkAssigned) {
			const readyHosts = (hosts?.hosts ?? [])
				.filter(hostEncodeReady)
				.sort(
					(left, right) => right.priority - left.priority || left.label.localeCompare(right.label)
				);
			const readyIndex = readyHosts.findIndex((candidate) => candidate.key === host.key);
			return readyIndex === 0
				? 'Next in line; all current episode parts are already assigned.'
				: 'Ready; all current episode parts are already assigned.';
		}
		return 'Ready for the next episode part.';
	}
	if (host.available && host.queue_active === false) {
		return host.active_reason || host.message || 'Reachable but not accepting Mediaforce work.';
	}
	if (host.setup_supported === false) {
		return host.message || 'Unavailable and cannot be prepared from this screen.';
	}
	return host.message || 'Unavailable; start or prepare this computer when it should be working.';
}

export function opsWorkLabel(prefix: string): string {
	const segments = prefix
		.split('/')
		.map((segment) => segment.trim())
		.filter(Boolean);
	const workSegments = segments.length > 1 ? segments.slice(1) : segments;
	return workSegments.join(' · ') || 'Media work';
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

function encodeJobRawDetail(job: EncodeQueueJob): string {
	return (
		(activeJobStatus(job.status) ? '' : job.error) ||
		job.telemetry_summary ||
		job.progress?.current_item_rel_path ||
		job.attempt_summary ||
		job.progress?.failure_analysis?.summary ||
		'waiting for queue telemetry'
	);
}

export function encodeJobDetail(job: EncodeQueueJob): string {
	return operatorErrorSummary(compactTelemetryCopy(encodeJobRawDetail(job)));
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
		const activePartCount =
			numberValue(job.running_shard_count) || job.progress?.active_host_labels?.length || 0;
		return {
			key: `encode:${job.job_id}`,
			kind: 'encode',
			tone: encodeJobTone(job),
			status: statusCopy(job.status || 'unknown'),
			prefix: job.prefix || 'system scope',
			host: encodeHostCopy(job),
			phase: activePartCount
				? `${activePartCount} active episode ${activePartCount === 1 ? 'part' : 'parts'}`
				: 'processing queue',
			progress: encodeJobProgress(job),
			scheduler:
				job.scheduler_status_copy || (job.schedule_waiting ? 'waiting for window' : 'ready'),
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
				: 'review evidence history'
			: laneName === 'sample'
				? 'sample check'
				: 'review evidence',
		progress: compactText(job.progress) || compactText(job.stage) || '—',
		scheduler:
			compactText(job.scheduler_status_copy) || compactText(job.created_at) || 'queued order',
		detail: calibrationDetail(job)
	}));
}

export function rowRecoveryLabel(row: OpsQueueRow): string {
	if (!row.action) {
		if (row.tone === 'active') return 'Automatic';
		if (row.tone === 'wait') return 'Waiting';
		return 'No action';
	}
	if (row.action === 'retry-encode-prefix') return 'Retry folder';
	if (row.action === 'retry-failed-encode') return 'Retry all';
	if (row.action === 'stop-calibration') return 'Stop samples';
	return row.actionScope === 'global' ? 'Global action' : 'Run action';
}

export function rowRecoveryTitle(row: OpsQueueRow): string {
	if (!row.action) return 'No action is available for this row.';
	if (row.action === 'retry-encode-prefix') {
		return 'Retry processing for this folder only.';
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
	const attentionJob = (queue?.recent ?? []).find((job) =>
		['failed', 'needs_attention', 'stopped'].includes(String(job.status ?? '').toLowerCase())
	);
	const storageWaitingJobs = controllerStorageWaitingJobs(dashboard);
	const capacity = hostCapacityCounts(hosts);
	const runningCount = queue?.running_count ?? 0;
	const queuedWork = (queue?.queued_count ?? 0) + (queue?.running_count ?? 0);
	const scheduleWaiting = queue?.queued_waiting_count ?? 0;
	if (loadError) {
		blockers.push({
			key: 'runtime-load',
			tone: 'fail',
			title: 'Activity is unavailable',
			detail: loadError
		});
	}
	if (queue?.state.stop_requested) {
		blockers.push({
			key: 'stop-requested',
			tone: 'fail',
			title: 'Season work is stopping',
			detail: 'Computers are finishing their current episodes before more work can start.',
			action: 'resume-encode'
		});
	}
	if (queue?.state.is_paused) {
		blockers.push({
			key: 'paused',
			tone: 'wait',
			title: 'Season work is paused',
			detail: queue.state.scheduler_summary ?? 'No new season work will start until you resume it.',
			action: 'resume-encode'
		});
	}
	if (attentionCount > 0) {
		blockers.push({
			key: 'needs-attention',
			tone: 'wait',
			title:
				attentionCount === 1 && attentionJob?.prefix
					? `${opsWorkLabel(attentionJob.prefix)} needs attention`
					: `${attentionCount} ${attentionCount === 1 ? 'season needs' : 'seasons need'} attention`,
			detail: attentionJob
				? operatorErrorCopy(encodeJobRawDetail(attentionJob))
				: 'Review what happened, then retry the unfinished work.',
			action: 'retry-failed-encode'
		});
	}
	if (storageWaitingJobs.length > 0) {
		blockers.push({
			key: 'controller-storage',
			tone: 'wait',
			title: 'Media storage is not available',
			detail:
				storageWaitingJobs[0].scheduler_status_copy ??
				'Mount the media storage on this computer to continue.'
		});
	} else if (
		capacity.total > 0 &&
		capacity.encodeReady === 0 &&
		queuedWork > 0 &&
		runningCount === 0
	) {
		const allAvailableHostsScheduledOff =
			capacity.available > 0 && capacity.scheduledOff === capacity.available;
		const workersReachable = capacity.available > 0;
		blockers.push({
			key: 'no-hosts-ready',
			tone: workersReachable ? 'wait' : 'fail',
			title: allAvailableHostsScheduledOff
				? 'Computers are outside their schedules'
				: workersReachable
					? 'Computers are busy or waiting'
					: 'No computer can work right now',
			detail: allAvailableHostsScheduledOff
				? 'Waiting seasons will start when the next allowed time begins.'
				: workersReachable
					? 'The computers are reachable but cannot start another season right now.'
					: 'Work is waiting, but every configured computer is unavailable or outside its schedule.'
		});
	} else if (scheduleWaiting > 0 && capacity.encodeReady === 0) {
		blockers.push({
			key: 'schedule-waiting',
			tone: 'wait',
			title: `${scheduleWaiting} ${scheduleWaiting === 1 ? 'season is' : 'seasons are'} waiting for their scheduled time`,
			detail: queue?.state.scheduler_summary ?? 'Work will start when its allowed time begins.'
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
	const capacity = hostCapacityCounts(hosts);
	const runningCount = queue?.running_count ?? 0;
	const queuedCount = queue?.queued_count ?? 0;
	const queuedWaiting = queue?.queued_waiting_count ?? 0;
	const needsAttention = queue?.needs_attention_count ?? 0;
	const storageWaitingJobs = controllerStorageWaitingJobs(dashboard);
	const activeChecks = calibration?.active_count ?? 0;
	const queuedWork = runningCount + queuedCount;

	if (loadError) {
		return {
			tone: 'fail',
			title: 'Activity is unavailable',
			detail: loadError,
			metricLabel: 'Data',
			metricValue: 'offline'
		};
	}
	if (queue?.state.stop_requested) {
		return {
			tone: 'fail',
			title: 'Season work is stopping',
			detail: 'Computers are finishing their current episodes before Mediaforce starts more work.',
			metricLabel: 'Active work',
			metricValue: String(runningCount)
		};
	}
	if (queue?.state.is_paused) {
		return {
			tone: 'wait',
			title: 'Season work is paused',
			detail: queue.state.scheduler_summary ?? 'Resume when season work should continue.',
			metricLabel: 'Queued',
			metricValue: String(queuedCount)
		};
	}
	if (needsAttention > 0) {
		return {
			tone: 'wait',
			title: 'A season needs attention',
			detail: `${needsAttention} ${needsAttention === 1 ? 'season needs' : 'seasons need'} a quick review before retrying.`,
			metricLabel: 'Needs you',
			metricValue: String(needsAttention)
		};
	}
	if (storageWaitingJobs.length > 0 && runningCount === 0) {
		return {
			tone: 'wait',
			title: 'Waiting for media storage',
			detail:
				storageWaitingJobs[0].scheduler_status_copy ??
				'Mount the media storage on this computer to continue.',
			metricLabel: 'Waiting',
			metricValue: String(storageWaitingJobs.length)
		};
	}
	if (capacity.total > 0 && capacity.encodeReady === 0 && queuedWork > 0 && runningCount === 0) {
		const allAvailableHostsScheduledOff =
			capacity.available > 0 && capacity.scheduledOff === capacity.available;
		const workersReachable = capacity.available > 0;
		return {
			tone: workersReachable ? 'wait' : 'fail',
			title: allAvailableHostsScheduledOff
				? 'Waiting for scheduled time'
				: workersReachable
					? 'Computers are busy or waiting'
					: 'No computer can work right now',
			detail: allAvailableHostsScheduledOff
				? 'Computers are reachable but outside their schedules.'
				: workersReachable
					? 'Computers are reachable but cannot start another season right now.'
					: 'Work is waiting, but every configured computer is unavailable or outside its schedule.',
			metricLabel: 'Available',
			metricValue: '0'
		};
	}
	if (runningCount > 0 || activeChecks > 0) {
		const activeParts = capacity.activeEncodes;
		const activeTotal = activeParts + activeChecks;
		const detailParts: string[] = [];
		if (activeParts > 0) {
			detailParts.push(
				`${activeParts} episode ${activeParts === 1 ? 'part' : 'parts'} across ${capacity.busy} ${capacity.busy === 1 ? 'computer' : 'computers'}`
			);
		}
		if (activeChecks > 0) {
			detailParts.push(`${activeChecks} ${activeChecks === 1 ? 'test' : 'tests'} active`);
		}
		if (queue?.telemetry?.eta_copy) {
			detailParts.push(`Estimated finish in ${queue.telemetry.eta_copy}`);
		}
		return {
			tone: 'active',
			title: 'Mediaforce is working',
			detail: detailParts.join(' · ') || 'Mediaforce is working.',
			metricLabel: 'Running',
			metricValue: String(activeTotal || runningCount)
		};
	}
	if (queuedWaiting > 0 && capacity.encodeReady === 0) {
		return {
			tone: 'wait',
			title: 'Waiting for scheduled time',
			detail:
				queue?.state.scheduler_summary ?? 'Waiting work will start when its allowed time begins.',
			metricLabel: 'Waiting',
			metricValue: String(queuedWaiting)
		};
	}
	if (capacity.encodeReady > 0) {
		return {
			tone: 'ready',
			title: queuedCount > 0 ? 'Ready to start' : 'Ready for work',
			detail:
				queuedCount > 0
					? 'An available computer can start the waiting season now.'
					: 'Computers are available for season work.',
			metricLabel: 'Available',
			metricValue: String(capacity.encodeReady)
		};
	}
	return {
		tone: 'idle',
		title: 'Standing by',
		detail:
			capacity.total > 0 ? 'Nothing is waiting right now.' : 'Computer status is unavailable.',
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
	const capacity = hostCapacityCounts(hosts);
	return [
		{
			label: 'Work schedule',
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
			label: 'Processing',
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
			value:
				capacity.encodeReady === 0 && capacity.busy > 0
					? `${capacity.busy} busy / ${capacity.total}`
					: `${capacity.encodeReady} can encode / ${capacity.total}`,
			detail: capacity.total
				? `${capacity.available} reachable · ${capacity.busy} busy · ${capacity.unavailable} unavailable`
				: 'worker status unavailable',
			tone:
				capacity.encodeReady > 0
					? 'ready'
					: capacity.available > 0
						? 'wait'
						: capacity.total > 0
							? 'fail'
							: 'idle'
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
			label: 'Processing',
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
			value: `${hosts?.hosts.filter((host) => host.available || host.storage_recovery_available === true).length ?? 0}/${hosts?.hosts.length ?? 0}`
		}
	];
}

export function hostTone(host: HostRuntime, fleetHasReadyCapacity = false): ShellTone {
	if (host.storage_recovery_available === true) return 'wait';
	if (!host.available) return fleetHasReadyCapacity ? 'wait' : 'fail';
	if (host.active_encode_count > 0) return 'active';
	if (host.schedule_open === false) return 'wait';
	if (host.queue_active === false) return 'idle';
	return 'ready';
}

export function hostStateCopy(host: HostRuntime): string {
	if (host.storage_recovery_available === true) return 'Reconnects storage';
	if (!host.available) return 'Unavailable';
	if (host.active_encode_count > 0) return 'Busy';
	if (host.schedule_open === false) return 'Window closed';
	if (host.queue_active === false) return 'Not accepting';
	return 'Ready';
}
