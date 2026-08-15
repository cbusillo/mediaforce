import type {
	CalibrationJobPayload,
	EncodeQueueJob,
	EncodeQueueSummary,
	ResolvedSizeGoalPayload
} from '$lib/api/types';

export interface MovieCurrentWorkView {
	label: string;
	tone: 'active' | 'wait';
	headline: string;
	detail: string;
	blockers: string[];
	percentComplete: number;
	queuePosition: string;
	worker: string;
	preferredWorker?: string;
	availableWorkers: string;
	eta?: string;
	elapsed?: string;
	speed?: string;
	nextCondition: string;
	currentItem: string | null;
}

export interface MovieGoalFactsView {
	duration: string;
	sourceSize: string;
	expectedOutput: string;
	expectedSavings: string;
	targetRange: string;
	estimateQuality: string;
}

export function canRetrySampleJob(jobId: unknown, hasPendingProposal: boolean): boolean {
	return typeof jobId === 'string' && jobId.trim().length > 0 && !hasPendingProposal;
}

export function parentSampleAppliesToExactItem(
	exactPrefix: string,
	exactJob: CalibrationJobPayload | null | undefined,
	overlappingJob: CalibrationJobPayload | null | undefined
): boolean {
	if (exactJob?.job_id || !overlappingJob?.job_id || overlappingJob.prefix === exactPrefix) {
		return false;
	}
	const sampleItem = overlappingJob.sample_item;
	return sampleItem?.rel_path === exactPrefix;
}

export function movieReviewStatusLabel(status: unknown, inheritedParentSample = false): string {
	const normalized = typeof status === 'string' ? status.trim() : '';
	if (inheritedParentSample) return 'Review at title level';
	return (
		{
			accepted: 'Approved',
			pending_review: 'Ready to review',
			needs_approval: 'Ready to review',
			rejected: 'Needs another sample',
			blocked: 'Needs review',
			missing_sample: 'Not prepared'
		}[normalized] ?? (normalized ? 'Status unavailable' : 'Not reviewed')
	);
}

export function movieCurrentWorkView(
	job: EncodeQueueJob | null | undefined,
	queueState: EncodeQueueSummary['state'] | null | undefined,
	availableWorkerCount: number,
	nowMs = Date.now()
): MovieCurrentWorkView | null {
	if (!job || !['queued', 'running', 'retry_backoff'].includes(job.status)) return null;
	const progress = job.progress;
	const reportedPercent = Number(progress?.percent_complete ?? 0);
	const percentComplete = Number.isFinite(reportedPercent)
		? Math.max(0, Math.min(100, reportedPercent))
		: 0;
	const queuePosition =
		job.queue_position && job.queue_depth
			? `${job.queue_position} of ${job.queue_depth}`
			: job.status === 'running'
				? 'Running'
				: 'Not available';
	const assignedWorker = assignedWorkerLabel(job);
	const preferredWorker = preferredWorkerLabel(job);
	const currentItem = textValue(progress?.current_item_rel_path) || null;
	const eta = textValue(progress?.eta_copy);

	if (job.status === 'running') {
		return {
			label: 'Processing now',
			tone: 'active',
			headline: currentItem ? `Processing ${fileName(currentItem)}` : 'Processing this movie now',
			detail: textValue(progress?.phase_label) || 'Mediaforce is compressing the current movie.',
			blockers: [],
			percentComplete,
			queuePosition,
			worker: assignedWorker || 'Not reported',
			availableWorkers: availableWorkersLabel(availableWorkerCount),
			eta: eta || 'Not available yet',
			elapsed: formatElapsed(job.started_at, nowMs),
			speed: formatSpeed(progress?.speed, progress?.fps),
			nextCondition: 'Mediaforce is processing this movie now.',
			currentItem
		};
	}

	const blockers: string[] = [];
	const startConditions: string[] = [];
	if (queueState?.is_paused) blockers.push('The processing queue is paused.');
	if (queueState?.is_paused) startConditions.push('the global processing queue is resumed');
	if (availableWorkerCount === 0) {
		blockers.push('No processing worker is ready.');
		startConditions.push('a processing worker becomes available');
	}
	if (
		job.schedule_waiting ||
		['active_hard_stop', 'off_schedule'].includes(job.schedule_state ?? '')
	) {
		blockers.push('The work schedule is currently closed.');
		startConditions.push('the processing schedule opens');
	}
	const waitingCopy = textValue(job.waiting_reason) || textValue(job.scheduler_status_copy);
	if (
		['draining_impossible', 'draining_no_fit', 'schedule_interrupted'].includes(
			job.schedule_state ?? ''
		) &&
		waitingCopy
	) {
		blockers.push(waitingCopy);
		startConditions.push('the scheduler condition clears');
	}
	if (job.status === 'retry_backoff') {
		blockers.push('Mediaforce is waiting before retrying.');
		startConditions.push('the retry delay ends');
	}
	const headline = blockers.length
		? 'Queued, but not able to start'
		: job.queue_position && job.queue_depth
			? `Queued ${job.queue_position} of ${job.queue_depth}`
			: 'Queued for processing';
	const detail = waitingCopy || 'Mediaforce will start this movie when a worker accepts it.';

	return {
		label: job.status === 'retry_backoff' ? 'Waiting to retry' : 'Queued',
		tone: 'wait',
		headline,
		detail: blockers.length
			? 'Clear the conditions below and Mediaforce starts this movie automatically.'
			: detail,
		blockers,
		percentComplete,
		queuePosition,
		worker: 'Not assigned',
		preferredWorker: preferredWorker || undefined,
		availableWorkers: availableWorkersLabel(availableWorkerCount),
		eta: !blockers.length && eta ? eta : undefined,
		nextCondition: startConditions.length
			? `This movie starts automatically after ${joinConditions(startConditions)}.`
			: 'This movie starts automatically when a processing worker accepts it.',
		currentItem
	};
}

export function movieGoalFactsView(
	durationSeconds: number | null | undefined,
	sourceSizeBytes: number | null | undefined,
	sizeGoal: ResolvedSizeGoalPayload | null | undefined
): MovieGoalFactsView {
	const targetSizeBytes = finitePositive(sizeGoal?.target_size_bytes);
	const sourceSize = finitePositive(sourceSizeBytes);
	const lowerBound = finitePositive(
		sizeGoal?.final_lower_bound_bytes ?? sizeGoal?.sample_lower_bound_bytes
	);
	const upperBound = finitePositive(
		sizeGoal?.final_upper_bound_bytes ?? sizeGoal?.sample_upper_bound_bytes
	);
	const savingsBytes =
		sourceSize && targetSizeBytes ? Math.max(0, sourceSize - targetSizeBytes) : null;
	const savingsPercent =
		sourceSize && targetSizeBytes && targetSizeBytes < sourceSize
			? Math.round((savingsBytes! / sourceSize) * 100)
			: null;
	const expectedSavings =
		sourceSize && targetSizeBytes
			? targetSizeBytes < sourceSize && savingsBytes != null && savingsPercent != null
				? `${formatMovieBytes(savingsBytes)} · ${savingsPercent}%`
				: 'No size reduction planned'
			: 'Not available';

	return {
		duration: formatDuration(durationSeconds),
		sourceSize: formatMovieBytes(sourceSize),
		expectedOutput: formatMovieBytes(targetSizeBytes),
		expectedSavings,
		targetRange:
			lowerBound && upperBound
				? `${formatMovieBytes(lowerBound)}–${formatMovieBytes(upperBound)}`
				: 'Not available',
		estimateQuality:
			targetSizeBytes && lowerBound && upperBound
				? 'Planning range, not a guarantee'
				: targetSizeBytes
					? 'Target estimate, not a guarantee'
					: 'Target not resolved'
	};
}

function assignedWorkerLabel(job: EncodeQueueJob): string {
	for (const label of job.progress?.active_host_labels ?? []) {
		const worker = textValue(label);
		if (worker) return worker;
	}
	for (const host of job.active_hosts ?? []) {
		const label = textValue(host.label) || textValue(host.key) || textValue(host.host);
		if (label) return label;
	}
	return '';
}

function preferredWorkerLabel(job: EncodeQueueJob): string {
	const host = job.host;
	return textValue(host?.label) || textValue(host?.key) || textValue(host?.host);
}

function textValue(value: unknown): string {
	return typeof value === 'string' ? value.trim() : '';
}

function fileName(value: string): string {
	return value.split('/').at(-1) || value;
}

function formatElapsed(value: string | null | undefined, nowMs: number): string {
	const startedAt = parseServerTimestamp(value);
	if (startedAt == null) return 'Not available yet';
	if (startedAt > nowMs) return startedAt - nowMs <= 300_000 ? '0s' : 'Not available yet';
	return formatDuration((nowMs - startedAt) / 1000);
}

export function parseServerTimestamp(value: unknown): number | null {
	const timestamp = textValue(value);
	if (!timestamp) return null;
	const hasTimezone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(timestamp);
	const normalized = hasTimezone ? timestamp : `${timestamp.replace(' ', 'T')}Z`;
	const parsed = Date.parse(normalized);
	return Number.isFinite(parsed) ? parsed : null;
}

function formatSpeed(speed: number | null | undefined, fps: number | null | undefined): string {
	if (Number.isFinite(speed) && Number(speed) > 0) {
		return `${Number(speed).toFixed(Number(speed) >= 10 ? 0 : 2)}× realtime`;
	}
	if (Number.isFinite(fps) && Number(fps) > 0) return `${Number(fps).toFixed(1)} fps`;
	return 'Not available yet';
}

function formatDuration(value: number | null | undefined): string {
	const seconds = finitePositive(value);
	if (!seconds) return 'Unknown';
	const roundedSeconds = Math.round(seconds);
	const hours = Math.floor(roundedSeconds / 3600);
	const minutes = Math.floor((roundedSeconds % 3600) / 60);
	const remainingSeconds = roundedSeconds % 60;
	if (hours) return `${hours}h ${minutes}m ${remainingSeconds}s`;
	if (minutes) return `${minutes}m ${remainingSeconds}s`;
	return `${remainingSeconds}s`;
}

export function formatMovieBytes(value: unknown): string {
	const bytes = finitePositive(value);
	if (!bytes) return 'Unknown';
	const units = ['B', 'KB', 'MB', 'GB', 'TB'];
	let unitIndex = 0;
	let scaled = bytes;
	while (scaled >= 1000 && unitIndex < units.length - 1) {
		scaled /= 1000;
		unitIndex += 1;
	}
	return `${scaled >= 100 ? scaled.toFixed(0) : scaled >= 10 ? scaled.toFixed(1) : scaled.toFixed(2)} ${units[unitIndex]}`;
}

function finitePositive(value: unknown): number | null {
	const number = Number(value);
	return Number.isFinite(number) && number > 0 ? number : null;
}

function joinConditions(conditions: string[]): string {
	if (conditions.length === 1) return conditions[0];
	return `${conditions.slice(0, -1).join(', ')} and ${conditions.at(-1)}`;
}

function availableWorkersLabel(count: number): string {
	if (count <= 0) return 'None ready';
	return `${count} ready`;
}
