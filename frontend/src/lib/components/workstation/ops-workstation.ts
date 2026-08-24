import type {
	DashboardSummaryPayload,
	EncodeQueueJob,
	HostRuntime,
	HostsPayload,
	ReviewReadySample
} from '$lib/api/types';
import { folderRoutePath } from '$lib/folder-display';
import { hostSchedulePresentation, jobSchedulePresentation } from '$lib/hosts/schedule';
import { safeOperatorErrorCopy } from '$lib/operator-copy';
import type { FooterSignal, ShellTone, StatusTile } from './shell-types';

export type OpsQueueKind = 'encode' | 'sample' | 'proof';
export type RefreshKind = 'quiet' | 'manual';
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
	scopeLabel?: string;
	tone: ShellTone;
	status: string;
	prefix: string;
	host: string;
	phase: string;
	progress: string;
	scheduler: string;
	schedulerDetail: string;
	schedulerTone: ShellTone;
	scheduleState?: string;
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
	href?: '/settings' | `/folders/${string}`;
	linkLabel?: string;
};

export type OpsReadinessSummary = {
	tone: ShellTone;
	title: string;
	detail: string;
	metricLabel: string;
	metricValue: string;
};

export class RefreshCoordinator {
	#nextId = 0;
	#latestId = 0;
	#quietInFlight = 0;
	#manualInFlight = 0;

	start(kind: RefreshKind): number | null {
		if (kind === 'manual' && this.#manualInFlight > 0) return null;
		if (kind === 'quiet' && (this.#quietInFlight > 0 || this.#manualInFlight > 0)) return null;

		const id = ++this.#nextId;
		this.#latestId = id;
		if (kind === 'quiet') this.#quietInFlight += 1;
		else this.#manualInFlight += 1;
		return id;
	}

	finish(kind: RefreshKind, id: number): boolean {
		if (kind === 'quiet') this.#quietInFlight = Math.max(0, this.#quietInFlight - 1);
		else this.#manualInFlight = Math.max(0, this.#manualInFlight - 1);
		return id === this.#latestId;
	}
}

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

type EncodeMediaKind = 'movie' | 'episode' | 'season' | 'show' | 'file' | 'media item';

function encodeJobMediaKind(job: EncodeQueueJob): EncodeMediaKind {
	const scope = job.media_scope;
	if (!scope) return 'media item';
	if (scope.domain === 'movie') return 'movie';
	if (scope.domain === 'other') return 'file';
	if (scope.match === 'exact_item') return 'episode';
	if (scope.kind === 'tv_series') return 'show';
	if (scope.kind === 'tv_season') return 'season';
	return 'media item';
}

function encodeJobLabel(job: EncodeQueueJob): string {
	const scope = job.media_scope;
	if (scope?.domain === 'tv' && scope.match === 'exact_item') {
		const segments = String(job.prefix ?? '')
			.split('/')
			.filter(Boolean);
		const episodeMatch = String(job.prefix ?? '').match(/S\d{1,3}E(\d{1,3})/i);
		const episode = episodeMatch
			? `Episode ${Number.parseInt(episodeMatch[1], 10)}`
			: scope.scope_label;
		return [segments[1], scope.parent?.title ?? segments[2], episode].filter(Boolean).join(' · ');
	}
	return compactText(job.media_scope?.title) || opsWorkLabel(job.prefix ?? '');
}

function reviewReadySampleLabel(sample: ReviewReadySample): string {
	const scope = sample.media_scope;
	if (scope?.domain === 'tv' && scope.match === 'exact_item') {
		const segments = sample.prefix.split('/').filter(Boolean);
		const series = String(segments[1] ?? '').replace(/\s*\(\d{4}\)\s*$/, '');
		const episodeMatch = sample.prefix.match(/S\d{1,3}E(\d{1,3})/i);
		const episode = episodeMatch ? `Episode ${Number.parseInt(episodeMatch[1], 10)}` : '';
		const parts = [series, scope.parent?.title ?? segments[2], episode]
			.map(compactText)
			.filter(Boolean);
		return parts
			.filter(
				(part, index) =>
					parts.findIndex(
						(candidate) =>
							candidate.replace(/\s*\(\d{4}\)\s*$/, '').toLowerCase() ===
							part.replace(/\s*\(\d{4}\)\s*$/, '').toLowerCase()
					) === index
			)
			.join(' · ');
	}
	return compactText(scope?.title) || opsWorkLabel(sample.prefix);
}

function reviewReadySummaryCopy(count: number): string {
	return `${count} ${count === 1 ? 'sample' : 'samples'} ready for review`;
}

function encodeAttentionJobs(
	queue: DashboardSummaryPayload['encode_queue'] | null | undefined
): EncodeQueueJob[] {
	const recentTerminalJobs = (queue?.recent ?? []).filter((job) =>
		['failed', 'needs_attention', 'stopped'].includes(String(job.status ?? '').toLowerCase())
	);
	const seenJobs = new Set<string>();
	return [...(queue?.needs_attention ?? []), ...recentTerminalJobs].filter((job) => {
		const key = compactText(job.job_id) || `${compactText(job.prefix)}:${String(job.status ?? '')}`;
		if (seenJobs.has(key)) return false;
		seenJobs.add(key);
		return true;
	});
}

function encodeWorkLabel(jobs: EncodeQueueJob[], totalCount = jobs.length): string {
	if (jobs.length < totalCount) return 'Media work';
	const kinds = new Set(jobs.map(encodeJobMediaKind));
	if (kinds.size !== 1) return 'Media work';
	const [kind] = kinds;
	if (!kind || kind === 'media item') return 'Media work';
	if (kind === 'file') return 'File processing';
	return `${kind[0].toUpperCase()}${kind.slice(1)} work`;
}

function encodeCountLabel(jobs: EncodeQueueJob[], count: number): string {
	const kinds = new Set(jobs.map(encodeJobMediaKind));
	const [kind] = kinds;
	const noun = jobs.length >= count && kinds.size === 1 && kind ? kind : 'media item';
	if (count === 1) return `${['episode'].includes(noun) ? 'An' : 'A'} ${noun}`;
	return `${count} ${noun}s`;
}

function encodeRequiresChangedInputs(job: EncodeQueueJob): boolean {
	return job.progress?.failure_analysis?.kind === 'final_size_target_miss';
}

function hostEncodeReady(
	host: HostRuntime,
	queue?: DashboardSummaryPayload['encode_queue'] | null
): boolean {
	const storageRecovery = host.storage_recovery_available === true;
	const schedule = hostSchedulePresentation(host, queue);
	return (
		(host.available || storageRecovery) &&
		host.schedule_open !== false &&
		schedule?.state !== 'host_draining' &&
		(storageRecovery || host.queue_active !== false) &&
		numberValue(host.active_encode_count) < numberValue(host.max_parallel_encodes)
	);
}

function hostCapacityCounts(
	hosts: HostsPayload | null | undefined,
	queue?: DashboardSummaryPayload['encode_queue'] | null
) {
	const rows = hosts?.hosts ?? [];
	const available = rows.filter(
		(host) => host.available || host.storage_recovery_available === true
	).length;
	return {
		available,
		activeEncodes: rows.reduce((total, host) => total + numberValue(host.active_encode_count), 0),
		encodeReady: rows.filter((host) => hostEncodeReady(host, queue)).length,
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
	if (/final output size missed the approved target band/i.test(detail)) {
		return 'The finished file was outside the approved size range. Your original media is safe; review the item before retrying.';
	}
	if (
		/target_band_violates_quality_floor|target size conflicts with the configured quality floor/i.test(
			detail
		)
	) {
		return 'The saved size goal is too small to preserve the required quality. Choose a new size goal before retrying.';
	}
	return safeOperatorErrorCopy(detail, 'Mediaforce could not finish this work.');
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

function calibrationPrefix(job: { prefix?: unknown; folder_prefix?: unknown }): string {
	return compactText(job.prefix) || compactText(job.folder_prefix) || 'system scope';
}

function normalizedQueuePath(value: unknown): string {
	return compactText(value).replace(/^\/+|\/+$/g, '');
}

function encodeJobShadowsSampleReview(
	job: EncodeQueueJob,
	pendingReviewPrefixes: Set<string>
): boolean {
	const currentItem = normalizedQueuePath(job.progress?.current_item_rel_path);
	if (currentItem && pendingReviewPrefixes.has(currentItem)) return true;

	if (job.media_scope?.match !== 'exact_item') return false;
	const exactItemPrefix = normalizedQueuePath(job.media_scope.prefix || job.prefix);
	return Boolean(exactItemPrefix && pendingReviewPrefixes.has(exactItemPrefix));
}

function sampleReviewShadowEncodeJobKeys(
	dashboard: DashboardSummaryPayload | null | undefined
): Set<string> {
	const calibrationQueue = dashboard?.calibration_queue;
	const pendingReviewPrefixes = new Set(
		[...(calibrationQueue?.sample.pending_review ?? []), ...(calibrationQueue?.review_ready ?? [])]
			.map((job) => normalizedQueuePath(calibrationPrefix(job)))
			.filter(Boolean)
	);
	return new Set(
		[...(dashboard?.encode_queue?.running ?? []), ...(dashboard?.encode_queue?.queued ?? [])]
			.filter((job) => encodeJobShadowsSampleReview(job, pendingReviewPrefixes))
			.map((job) => `encode:${job.job_id}`)
	);
}

function visibleEncodeJobs(jobs: EncodeQueueJob[], shadowJobKeys: Set<string>): EncodeQueueJob[] {
	return jobs.filter((job) => !shadowJobKeys.has(`encode:${job.job_id}`));
}

export function visibleEncodeQueueCounts(dashboard: DashboardSummaryPayload | null | undefined): {
	running: number;
	queued: number;
} {
	const queue = dashboard?.encode_queue;
	const shadowJobKeys = sampleReviewShadowEncodeJobKeys(dashboard);
	const visibleRunningJobs = visibleEncodeJobs(queue?.running ?? [], shadowJobKeys);
	const visibleQueuedJobs = visibleEncodeJobs(queue?.queued ?? [], shadowJobKeys);
	return {
		running: Math.max(
			(queue?.running_count ?? 0) - ((queue?.running.length ?? 0) - visibleRunningJobs.length),
			0
		),
		queued: Math.max(
			(queue?.queued_count ?? 0) - ((queue?.queued.length ?? 0) - visibleQueuedJobs.length),
			0
		)
	};
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
	const schedule = hostSchedulePresentation(host, dashboard?.encode_queue);
	if (host.available && host.schedule_open === false) {
		return (
			schedule?.detail || host.schedule_detail || 'Outside its schedule; this is a normal wait.'
		);
	}
	const activeCount = numberValue(host.active_encode_count);
	const maxParallel = Math.max(numberValue(host.max_parallel_encodes), 1);
	if (host.available && activeCount > 0) {
		if (schedule) return schedule.detail;
		return activeCount >= maxParallel
			? 'Working at capacity.'
			: `${activeCount} ${activeCount === 1 ? 'item is' : 'items are'} processing; capacity remains.`;
	}
	if (host.available && host.queue_active !== false) {
		if (schedule?.state === 'host_draining') return schedule.detail;
		const queue = dashboard?.encode_queue;
		const allCurrentWorkAssigned =
			(queue?.running_count ?? 0) > 0 && (queue?.queued_count ?? 0) === 0;
		if (allCurrentWorkAssigned) {
			const readyHosts = (hosts?.hosts ?? [])
				.filter((candidate) => hostEncodeReady(candidate, dashboard?.encode_queue))
				.sort(
					(left, right) => right.priority - left.priority || left.label.localeCompare(right.label)
				);
			const readyIndex = readyHosts.findIndex((candidate) => candidate.key === host.key);
			return readyIndex === 0
				? 'Next in line; all current media work is already assigned.'
				: 'Ready; all current media work is already assigned.';
		}
		return 'Ready for the next media item.';
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
	const detail = compactTelemetryCopy(encodeJobRawDetail(job));
	return activeJobStatus(job.status) ? detail : operatorErrorSummary(detail);
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
	dashboard: DashboardSummaryPayload | null | undefined,
	hosts?: HostsPayload | null,
	now = new Date()
): OpsQueueRow[] {
	const queue = dashboard?.encode_queue;
	if (!queue) return [];
	const prefixRetryStatuses = new Set(['failed', 'needs_attention', 'stopped']);
	const displayJobs = [...queue.running, ...queue.queued];
	const retryableJobIds = retryableEncodeJobIds(displayJobs);
	return displayJobs.map((job) => {
		const status = String(job.status ?? '').toLowerCase();
		const schedule = jobSchedulePresentation(job, hosts?.hosts ?? [], now);
		const jobTone = encodeJobTone(job);
		const needsChangedInputs = encodeRequiresChangedInputs(job);
		const canRetryPrefix =
			prefixRetryStatuses.has(status) && retryableJobIds.has(job.job_id) && !needsChangedInputs;
		const activePartCount =
			numberValue(job.running_shard_count) || job.progress?.active_host_labels?.length || 0;
		return {
			key: `encode:${job.job_id}`,
			kind: 'encode',
			scopeLabel: encodeJobMediaKind(job).replace(/^./, (character) => character.toUpperCase()),
			tone: jobTone === 'fail' || schedule.tone !== 'fail' ? jobTone : 'fail',
			status: statusCopy(job.status || 'unknown'),
			prefix: job.prefix || 'system scope',
			host: encodeHostCopy(job),
			phase: activePartCount
				? `${activePartCount} active processing ${activePartCount === 1 ? 'task' : 'tasks'}`
				: 'processing queue',
			progress: encodeJobProgress(job),
			scheduler: schedule.label,
			schedulerDetail: schedule.detail,
			schedulerTone: schedule.tone,
			scheduleState: schedule.state,
			detail: needsChangedInputs
				? job.progress?.failure_analysis?.summary ||
					'Open this item and choose a fresh size or compression goal before retrying.'
				: encodeJobDetail(job),
			action: canRetryPrefix ? 'retry-encode-prefix' : undefined,
			actionScope: canRetryPrefix ? 'row' : undefined
		};
	});
}

function buildCalibrationLaneRows(
	laneName: OpsQueueKind,
	jobs: CalibrationJob[] | undefined,
	status: string,
	options: { historical?: boolean; reviewAvailable?: boolean } = {}
): OpsQueueRow[] {
	return (jobs ?? []).map((job, index) => {
		const waitingForReview = !options.historical && status === 'pending_review';
		const reviewUnavailable = waitingForReview && options.reviewAvailable === false;
		return {
			key: `${laneName}:${compactText(job.job_id) || compactText(job.prefix) || `${status}:${index}`}`,
			kind: laneName,
			tone: options.historical ? 'idle' : reviewUnavailable ? 'wait' : statusTone(status),
			status: options.historical ? 'History' : reviewUnavailable ? 'Waiting' : statusCopy(status),
			prefix: calibrationPrefix(job),
			host: calibrationHostCopy(job),
			phase: options.historical
				? laneName === 'sample'
					? 'sample check history'
					: 'review evidence history'
				: laneName === 'sample'
					? 'sample check'
					: 'review evidence',
			progress: waitingForReview
				? 'Complete'
				: compactText(job.progress) || compactText(job.stage) || '—',
			scheduler: waitingForReview
				? reviewUnavailable
					? 'Review unavailable'
					: 'Finished'
				: compactText(job.scheduler_status_copy) || 'Waiting in queue',
			schedulerDetail: '',
			schedulerTone: 'idle',
			detail: waitingForReview
				? reviewUnavailable
					? 'Review media is unavailable. Mediaforce kept the completed sample visible for diagnosis.'
					: 'Open the item to compare the sample.'
				: calibrationDetail(job)
		};
	});
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
	const reviewReadyJobIds = new Set(
		(queue.review_ready ?? []).map((sample) => compactText(sample.job_id)).filter(Boolean)
	);
	const unavailablePendingReviews = (queue.sample.pending_review ?? []).filter(
		(job) => !reviewReadyJobIds.has(compactText(job.job_id))
	);
	const currentRows = [
		...buildCalibrationLaneRows('sample', queue.sample.running, 'running'),
		...buildCalibrationLaneRows('sample', queue.sample.queued, 'queued'),
		...buildCalibrationLaneRows('sample', unavailablePendingReviews, 'pending_review', {
			reviewAvailable: false
		}),
		...buildCalibrationLaneRows('proof', queue.full.running, 'running'),
		...buildCalibrationLaneRows('proof', queue.full.queued, 'queued')
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
	dashboard: DashboardSummaryPayload | null | undefined,
	hosts?: HostsPayload | null,
	now = new Date()
): OpsQueueRow[] {
	const shadowEncodeJobIds = sampleReviewShadowEncodeJobKeys(dashboard);
	const encodeRows = buildEncodeRows(dashboard, hosts, now).filter(
		(row) => !shadowEncodeJobIds.has(row.key)
	);
	return [...encodeRows, ...buildCalibrationRows(dashboard, { includeHistory: false })];
}

export function buildOpsBlockers(
	dashboard: DashboardSummaryPayload | null | undefined,
	hosts: HostsPayload | null | undefined,
	loadError: string | null
): OpsBlocker[] {
	const blockers: OpsBlocker[] = [];
	const queue = dashboard?.encode_queue;
	const attentionJobs = encodeAttentionJobs(queue);
	const attentionCount = Math.max(queue?.needs_attention_count ?? 0, attentionJobs.length);
	const impossibleWindowJobs = (queue?.queued ?? []).filter(
		(job) => job.schedule_state === 'draining_impossible'
	);
	const storageWaitingJobs = controllerStorageWaitingJobs(dashboard);
	const activeJobs = [...(queue?.running ?? []), ...(queue?.queued ?? [])];
	const capacity = hostCapacityCounts(hosts, queue);
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
	for (const sample of dashboard?.calibration_queue.review_ready ?? []) {
		blockers.push({
			key: `review-ready:${sample.job_id}`,
			tone: 'ready',
			title: `${reviewReadySampleLabel(sample)} is ready for your review`,
			detail: 'Compare the sample and decide whether to approve it.',
			href: folderRoutePath(sample.prefix),
			linkLabel: 'Review sample'
		});
	}
	if (queue?.state.stop_requested) {
		blockers.push({
			key: 'stop-requested',
			tone: 'fail',
			title: `${encodeWorkLabel(activeJobs, queuedWork)} is stopping`,
			detail: 'Computers are finishing their current tasks before more work can start.',
			action: 'resume-encode'
		});
	}
	if (queue?.state.is_paused) {
		blockers.push({
			key: 'paused',
			tone: 'wait',
			title: `${encodeWorkLabel(activeJobs, queuedWork)} is paused`,
			detail: queue.state.scheduler_summary ?? 'No new media work will start until you resume it.',
			action: 'resume-encode'
		});
	}
	if (attentionCount > 0) {
		if (attentionJobs.length > 0) {
			for (const job of attentionJobs) {
				blockers.push({
					key: `needs-attention:${job.job_id}`,
					tone: 'wait',
					title: `${encodeJobLabel(job)} needs review`,
					detail: operatorErrorCopy(encodeJobRawDetail(job)),
					href: job.prefix ? folderRoutePath(job.prefix) : undefined,
					linkLabel: job.prefix ? 'Review item' : undefined
				});
			}
		} else {
			blockers.push({
				key: 'needs-attention',
				tone: 'wait',
				title: `${encodeCountLabel(attentionJobs, attentionCount)} need review`,
				detail: 'Nothing was replaced. Open the affected folders to see what each one needs.'
			});
		}
	}
	if (impossibleWindowJobs.length > 0) {
		const firstJob = impossibleWindowJobs[0];
		blockers.push({
			key: 'schedule-window-impossible',
			tone: 'fail',
			title:
				impossibleWindowJobs.length === 1
					? `${opsWorkLabel(firstJob.prefix)} needs a longer work window`
					: `${encodeCountLabel(impossibleWindowJobs, impossibleWindowJobs.length)} need longer work windows`,
			detail:
				firstJob.waiting_reason ??
				'The estimated task is longer than every compatible worker window.',
			href: '/settings',
			linkLabel: 'Edit work windows'
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
		impossibleWindowJobs.length === 0 &&
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
				? 'Waiting work will start when the next allowed time begins.'
				: workersReachable
					? 'The computers are reachable but cannot start another task right now.'
					: 'Work is waiting, but every configured computer is unavailable or outside its schedule.'
		});
	} else if (
		impossibleWindowJobs.length === 0 &&
		scheduleWaiting > 0 &&
		capacity.encodeReady === 0
	) {
		blockers.push({
			key: 'schedule-waiting',
			tone: 'wait',
			title: `${encodeCountLabel(queue?.queued ?? [], scheduleWaiting)} ${scheduleWaiting === 1 ? 'is' : 'are'} waiting for the scheduled time`,
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
	const capacity = hostCapacityCounts(hosts, queue);
	const shadowEncodeJobKeys = sampleReviewShadowEncodeJobKeys(dashboard);
	const visibleRunningJobs = visibleEncodeJobs(queue?.running ?? [], shadowEncodeJobKeys);
	const visibleQueuedJobs = visibleEncodeJobs(queue?.queued ?? [], shadowEncodeJobKeys);
	const { running: runningCount, queued: queuedCount } = visibleEncodeQueueCounts(dashboard);
	const queuedWaiting = queue?.queued_waiting_count ?? 0;
	const storageWaitingJobs = controllerStorageWaitingJobs(dashboard);
	const activeJobs = [...visibleRunningJobs, ...visibleQueuedJobs];
	const attentionJobs = encodeAttentionJobs(queue);
	const needsAttention = Math.max(queue?.needs_attention_count ?? 0, attentionJobs.length);
	const reviewReadySamples = calibration?.review_ready ?? [];
	const reviewReadyCount = reviewReadySamples.length;
	const activeChecks = calibration?.active_count ?? 0;
	const queuedWork = runningCount + queuedCount;
	const impossibleWindowJobs = (queue?.queued ?? []).filter(
		(job) => job.schedule_state === 'draining_impossible'
	);
	const drainingJobs = (queue?.queued ?? []).filter(
		(job) => job.schedule_state === 'draining_no_fit'
	);

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
			title: `${encodeWorkLabel(activeJobs, queuedWork)} is stopping`,
			detail: 'Computers are finishing their current tasks before Mediaforce starts more work.',
			metricLabel: 'Active work',
			metricValue: String(runningCount)
		};
	}
	if (queue?.state.is_paused) {
		return {
			tone: 'wait',
			title: `${encodeWorkLabel(activeJobs, queuedWork)} is paused`,
			detail: queue.state.scheduler_summary ?? 'Resume when media work should continue.',
			metricLabel: 'Queued',
			metricValue: String(queuedCount)
		};
	}
	if (impossibleWindowJobs.length > 0) {
		return {
			tone: 'fail',
			title: `${encodeCountLabel(impossibleWindowJobs, impossibleWindowJobs.length)} ${impossibleWindowJobs.length === 1 ? 'needs' : 'need'} a longer work window`,
			detail:
				impossibleWindowJobs[0].waiting_reason ??
				'Widen a compatible worker window or intentionally bypass the schedule.',
			metricLabel: 'Blocked',
			metricValue: String(impossibleWindowJobs.length)
		};
	}
	if (runningCount > 0) {
		const activeProcessingCount = Math.max(runningCount, capacity.activeEncodes);
		const activeTotal = activeProcessingCount + activeChecks;
		const detailParts: string[] = [];
		if (activeProcessingCount > 0) {
			detailParts.push(
				`${activeProcessingCount} ${activeProcessingCount === 1 ? 'item' : 'items'} processing across ${capacity.busy} ${capacity.busy === 1 ? 'computer' : 'computers'}`
			);
		}
		if (activeChecks > 0) {
			detailParts.push(`${activeChecks} ${activeChecks === 1 ? 'test' : 'tests'} active`);
		}
		if (queuedCount === 1 && visibleQueuedJobs[0]?.prefix) {
			detailParts.push(`${opsWorkLabel(visibleQueuedJobs[0].prefix)} is queued`);
		} else if (queuedCount > 0) {
			detailParts.push(`${encodeCountLabel(visibleQueuedJobs, queuedCount)} queued`);
		}
		if (needsAttention > 0) {
			detailParts.push(
				`${encodeCountLabel(attentionJobs, needsAttention)} ${needsAttention === 1 ? 'needs' : 'need'} attention`
			);
		}
		if (reviewReadyCount > 0) {
			detailParts.push(reviewReadySummaryCopy(reviewReadyCount));
		}
		if (queue?.telemetry?.eta_copy) {
			detailParts.push(`Estimated finish in ${queue.telemetry.eta_copy}`);
		}
		return {
			tone: 'active',
			title:
				runningCount === 1 && visibleRunningJobs[0]?.prefix
					? `${opsWorkLabel(visibleRunningJobs[0].prefix)} is working`
					: 'Mediaforce is working',
			detail: detailParts.join(' · ') || 'Mediaforce is working.',
			metricLabel: 'Running',
			metricValue: String(activeTotal || runningCount)
		};
	}
	if (needsAttention > 0) {
		const reviewDetail =
			reviewReadyCount > 0 ? ` · ${reviewReadySummaryCopy(reviewReadyCount)}` : '';
		return {
			tone: 'wait',
			title: `${encodeCountLabel(attentionJobs, needsAttention)} ${needsAttention === 1 ? 'needs' : 'need'} attention`,
			detail: `${encodeCountLabel(attentionJobs, needsAttention)} ${needsAttention === 1 ? 'needs' : 'need'} a quick review before retrying.${reviewDetail}`,
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
	if (drainingJobs.length > 0 && runningCount === 0) {
		return {
			tone: 'wait',
			title: 'Workers are draining',
			detail:
				'No queued task safely fits the time left. Work resumes automatically in the next compatible full window.',
			metricLabel: 'Waiting',
			metricValue: String(drainingJobs.length)
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
					? 'Computers are reachable but cannot start another task right now.'
					: 'Work is waiting, but every configured computer is unavailable or outside its schedule.',
			metricLabel: 'Available',
			metricValue: '0'
		};
	}
	if (activeChecks > 0) {
		const reviewDetail =
			reviewReadyCount > 0 ? ` · ${reviewReadySummaryCopy(reviewReadyCount)}` : '';
		return {
			tone: 'active',
			title: 'Mediaforce is working',
			detail: `${activeChecks} ${activeChecks === 1 ? 'test' : 'tests'} active${reviewDetail}`,
			metricLabel: 'Running',
			metricValue: String(activeChecks)
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
	if (reviewReadyCount > 0) {
		return {
			tone: 'ready',
			title:
				reviewReadyCount === 1
					? `${reviewReadySampleLabel(reviewReadySamples[0])} is ready for review`
					: `${reviewReadyCount} samples are ready for review`,
			detail:
				reviewReadyCount === 1
					? 'Compare the sample and decide whether to approve it.'
					: 'Compare each sample and decide whether to approve it.',
			metricLabel: 'Needs you',
			metricValue: String(reviewReadyCount)
		};
	}
	if (capacity.encodeReady > 0) {
		return {
			tone: 'ready',
			title: queuedCount > 0 ? 'Ready to start' : 'Ready for work',
			detail:
				queuedCount > 0
					? 'An available computer can start the waiting work now.'
					: 'Computers are available for media work.',
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
	const capacity = hostCapacityCounts(hosts, encode);
	const { running: visibleRunningCount, queued: visibleQueuedCount } =
		visibleEncodeQueueCounts(dashboard);
	const reviewReadyCount = calibration?.review_ready?.length ?? 0;
	const samplePendingReviewCount = Math.max(
		reviewReadyCount,
		calibration?.sample.pending_review_count ?? 0
	);
	const unavailableReviewCount = samplePendingReviewCount - reviewReadyCount;
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
			value: `${visibleRunningCount} running · ${visibleQueuedCount} queued`,
			detail:
				encode?.telemetry?.eta_copy ?? `${encode?.needs_attention_count ?? 0} retry available`,
			tone:
				(encode?.needs_attention_count ?? 0) > 0
					? 'wait'
					: visibleRunningCount > 0
						? 'active'
						: visibleQueuedCount > 0
							? 'wait'
							: 'idle'
		},
		{
			label: 'Sample checks',
			value: `${calibration?.sample.running_count ?? 0} running · ${calibration?.sample.queued_count ?? 0} queued`,
			detail:
				unavailableReviewCount > 0
					? `${reviewReadyCount} ready · ${unavailableReviewCount} unavailable`
					: `${reviewReadyCount} waiting for review`,
			tone:
				unavailableReviewCount > 0
					? 'wait'
					: reviewReadyCount > 0
						? 'ready'
						: (calibration?.active_count ?? 0) > 0
							? 'active'
							: 'idle'
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
	const reviewReadyCount = calibration?.review_ready?.length ?? 0;
	const samplePendingReviewCount = Math.max(
		reviewReadyCount,
		calibration?.sample.pending_review_count ?? 0
	);
	const unavailableReviewCount = samplePendingReviewCount - reviewReadyCount;
	const encodeAttentionCount = encodeAttentionJobs(encode).length;
	const attentionCount = encodeAttentionCount + samplePendingReviewCount;
	const visibleCounts = visibleEncodeQueueCounts(dashboard);
	return [
		{
			label: 'Processing',
			value: `${visibleCounts.running}/${visibleCounts.queued}`,
			tone: visibleCounts.running > 0 ? 'active' : visibleCounts.queued > 0 ? 'wait' : 'idle'
		},
		{
			label: 'Attention',
			value: String(attentionCount),
			tone:
				encodeAttentionCount > 0 || unavailableReviewCount > 0
					? 'wait'
					: reviewReadyCount > 0
						? 'ready'
						: 'idle'
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

export function hostTone(
	host: HostRuntime,
	fleetHasReadyCapacity = false,
	dashboard?: DashboardSummaryPayload | null
): ShellTone {
	if (host.storage_recovery_available === true) return 'wait';
	if (!host.available) return fleetHasReadyCapacity ? 'wait' : 'fail';
	const schedule = hostSchedulePresentation(host, dashboard?.encode_queue);
	if (schedule) return schedule.tone;
	if (host.active_encode_count > 0) return 'active';
	if (host.queue_active === false) return 'idle';
	return 'ready';
}

export function hostStateCopy(
	host: HostRuntime,
	dashboard?: DashboardSummaryPayload | null
): string {
	if (host.storage_recovery_available === true) return 'Reconnects storage';
	if (!host.available) return 'Unavailable';
	const schedule = hostSchedulePresentation(host, dashboard?.encode_queue);
	if (schedule) return schedule.label;
	if (host.active_encode_count > 0) return 'Busy';
	if (host.queue_active === false) return 'Not accepting';
	return 'Ready';
}
