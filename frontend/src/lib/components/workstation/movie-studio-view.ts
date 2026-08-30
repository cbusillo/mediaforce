import type {
	CalibrationJobPayload,
	EncodeQueueJob,
	EncodeQueueSummary,
	MovieTitle,
	QualityRiskPayload,
	RepresentativeSampleItemPayload,
	ResolvedOperatorIntentPayload,
	ResolvedSizeGoalPayload,
	StreamBudgetLedgerPayload
} from '$lib/api/types';
import { formatFileSize } from '$lib/format';
import { movieTitleOwnsActiveWork, movieWorkflowLabel } from '$lib/movies/library';

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
	outputDetail: string;
	estimateQuality: string;
	estimateBasis: 'sampled' | 'target' | 'unavailable';
}

export interface MovieGoalContractRow {
	label: string;
	value: string;
	detail: string;
	provenance: string;
	tone: 'normal' | 'attention';
}

export interface MovieGoalContractFinding {
	kind: 'Measured' | 'Advisory' | 'Out of date';
	label: string;
	detail?: string;
}

export interface MovieGoalContractView {
	status: 'ready' | 'resolving';
	rows: MovieGoalContractRow[];
	findings: MovieGoalContractFinding[];
}

export interface MovieSizeCapBlockView {
	blocked: boolean;
	headline: string;
	remedy: string;
}

interface MovieSizeCapWorkflow {
	primary_lane?: string | null;
	detail?: string | null;
}

interface MovieSizeCapLedger {
	size_goal?: { target_size_bytes?: number | null } | null;
	source_relative_cap?: {
		configured_total_percent?: number | null;
		total_cap_bytes?: number | null;
		status?: string | null;
	} | null;
	feasibility?: { reasons?: string[] | null } | null;
}

export function canRetrySampleJob(jobId: unknown, hasPendingProposal: boolean): boolean {
	return typeof jobId === 'string' && jobId.trim().length > 0 && !hasPendingProposal;
}

export function movieSampleSetupResult(
	needsAttention: boolean,
	readyMessage: string
): { message: string; attention: boolean; attentionTitle?: string } {
	return needsAttention
		? {
				message: 'The sample setup needs another request. Nothing was queued.',
				attention: true,
				attentionTitle: 'Sample setup needs attention.'
			}
		: { message: readyMessage, attention: false };
}

export function movieRetryResponseCopy(
	queuedCount: number,
	message: string | null | undefined,
	scopeNoun: string
): { message: string; attention: boolean } {
	if (queuedCount > 0) {
		return {
			message: `This ${scopeNoun} is waiting to try compression again.`,
			attention: false
		};
	}
	return {
		message: message?.toLowerCase().includes('fresh size')
			? 'Choose a fresh size or compression goal before trying again.'
			: 'No failed movie compression was ready to retry.',
		attention: true
	};
}

export function sampleStopResponseCopy(message: string | null | undefined): string {
	return message?.toLowerCase().includes('already idle')
		? 'Sample work was already stopped.'
		: 'Stopped waiting and running sample work.';
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

export interface ParentTitleWorkView {
	actionLabel: string;
	statusLabel: string;
	detail: string;
	reviewSample: boolean;
}

export function parentTitleWorkView(
	exactPrefix: string,
	parentTitle: MovieTitle | null | undefined,
	inheritedParentSample: boolean
): ParentTitleWorkView | null {
	if (
		!parentTitle ||
		parentTitle.prefix === exactPrefix ||
		!movieTitleOwnsActiveWork(parentTitle) ||
		(parentTitle.members.length > 1 && !inheritedParentSample)
	) {
		return null;
	}
	const badgeLabel = parentTitle.review_badge?.label?.trim() ?? '';
	const reviewSample =
		inheritedParentSample ||
		['ready to review', 'review pending'].includes(badgeLabel.toLowerCase());
	if (reviewSample) {
		return {
			actionLabel: 'Review title sample',
			statusLabel: 'Review at title level',
			detail:
				'This file belongs to a title-level sample that is ready for review. Continue in the title workspace.',
			reviewSample: true
		};
	}
	const workflowLabel = movieWorkflowLabel({
		workflow_state: parentTitle.workflow_state,
		promotion_conflicts: parentTitle.promotion_conflicts,
		details_loading: parentTitle.details_loading,
		availability: parentTitle.availability
	});
	const workflowOwnsStatus = [
		'processing',
		'validate',
		'promote',
		'mixed',
		'attention',
		'blocked'
	].includes(parentTitle.workflow_state?.primary_lane ?? '');
	return {
		actionLabel: 'Open title workspace',
		statusLabel: workflowOwnsStatus ? workflowLabel : badgeLabel || workflowLabel,
		detail:
			'This file belongs to active title-level work. Continue in the title workspace to take the next step.',
		reviewSample: false
	};
}

export function movieReviewStatusLabel(status: unknown, inheritedParentSample = false): string {
	const normalized = typeof status === 'string' ? status.trim() : '';
	if (inheritedParentSample) return 'Review at title level';
	return (
		{
			accepted: 'Sample approved',
			pending_review: 'Ready to review',
			needs_approval: 'Ready to review',
			rejected: 'Needs another sample',
			blocked: 'Needs review',
			missing_sample: 'Needs sample'
		}[normalized] ?? (normalized ? 'Status unavailable' : 'Not reviewed')
	);
}

export function movieSizeCapBlockView(
	workflow: MovieSizeCapWorkflow | null | undefined,
	ledger: MovieSizeCapLedger | null | undefined
): MovieSizeCapBlockView {
	const detail = textValue(workflow?.detail);
	const reasons: string[] = [];
	for (const reason of ledger?.feasibility?.reasons ?? []) {
		if (typeof reason === 'string' && reason.trim()) reasons.push(reason);
	}
	const blockerText = [detail, ...reasons].join(' ');
	const sourceCapInfeasible = ledger?.source_relative_cap?.status === 'arithmetically_infeasible';
	const mentionsSourceCap = /source(?:-size)? cap/i.test(blockerText);
	const preservedContentReason = reasons.includes(
		'source_relative_cap_consumed_by_non_video_budget'
	);
	const targetExceedsReason = reasons.includes('target_lower_bound_exceeds_source_relative_cap');
	const lacksSourceCapReason =
		!mentionsSourceCap && !preservedContentReason && !targetExceedsReason;
	if (workflow?.primary_lane !== 'blocked' || lacksSourceCapReason) {
		return { blocked: false, headline: '', remedy: '' };
	}

	const configuredPercent = finitePositive(ledger?.source_relative_cap?.configured_total_percent);
	const detailPercent = Number(blockerText.match(/(\d+(?:\.\d+)?)%\s+source(?:-size)? cap/i)?.[1]);
	const capPercent = configuredPercent ?? (Number.isFinite(detailPercent) ? detailPercent : null);
	const capCopy = capPercent == null ? null : `${formatPercent(capPercent)}%`;
	const targetSize = finitePositive(ledger?.size_goal?.target_size_bytes);
	const totalCap = finitePositive(ledger?.source_relative_cap?.total_cap_bytes);
	const preservedContentConsumesCap =
		preservedContentReason || /preserved streams consume .*source(?:-size)? cap/i.test(blockerText);
	const targetExceedsCap =
		targetExceedsReason ||
		/target size exceeds .*source(?:-size)? cap/i.test(blockerText) ||
		Boolean(!sourceCapInfeasible && targetSize && totalCap && targetSize > totalCap);

	if (preservedContentConsumesCap) {
		return {
			blocked: true,
			headline:
				'The sound, subtitles, and other content being kept already use the allowed movie size.',
			remedy: 'Review the movie settings and either keep less content or allow a larger output.'
		};
	}
	if (targetExceedsCap) {
		return {
			blocked: true,
			headline: capCopy
				? `The requested size is larger than the allowed ${capCopy} of the original file.`
				: 'The requested size is larger than the configured limit for this movie.',
			remedy:
				'Choose a smaller target in library settings. Then Mediaforce can prepare a valid movie plan.'
		};
	}
	return {
		blocked: true,
		headline: 'The current movie plan cannot fit within the configured size limit.',
		remedy: 'Review the movie settings and choose a size and content plan that can fit.'
	};
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
			label: 'Compressing now',
			tone: 'active',
			headline: currentItem ? `Compressing ${fileName(currentItem)}` : 'Compressing this movie now',
			detail:
				normalizeMovieQueueCopy(textValue(progress?.phase_label)) ||
				'Mediaforce is compressing the current movie.',
			blockers: [],
			percentComplete,
			queuePosition,
			worker: assignedWorker || 'Not reported',
			availableWorkers: availableWorkersLabel(availableWorkerCount),
			eta: eta || 'Not available yet',
			elapsed: formatElapsed(job.started_at, nowMs),
			speed: formatSpeed(progress?.speed, progress?.fps),
			nextCondition: 'Mediaforce is compressing this movie now.',
			currentItem
		};
	}

	const blockers: string[] = [];
	const startConditions: string[] = [];
	if (queueState?.stop_requested) {
		blockers.push('The compression queue is stopping and will not start new work.');
		startConditions.push('the global compression queue is resumed');
	} else if (queueState?.is_paused) {
		blockers.push('The compression queue is paused.');
		startConditions.push('the global compression queue is resumed');
	}
	if (availableWorkerCount === 0) {
		blockers.push('No computer is ready for compression.');
		startConditions.push('a computer becomes available');
	}
	if (
		job.schedule_waiting ||
		['active_hard_stop', 'off_schedule'].includes(job.schedule_state ?? '')
	) {
		blockers.push('The work schedule is currently closed.');
		startConditions.push('the compression schedule opens');
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
			: 'Waiting to compress';
	const detail =
		normalizeMovieQueueCopy(waitingCopy) ||
		'Mediaforce will start this movie when a computer accepts it.';

	return {
		label: queueState?.stop_requested
			? 'Queue stopping'
			: job.status === 'retry_backoff'
				? 'Waiting to retry'
				: 'Queued',
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
			: 'This movie starts automatically when a computer accepts it.',
		currentItem
	};
}

function normalizeMovieQueueCopy(value: string): string {
	return value
		.replace(/\bencode queue\b/gi, (match) => matchCase(match, 'compression queue'))
		.replace(/\bencode jobs?\b/gi, (match) =>
			matchCase(match, match.toLowerCase().endsWith('s') ? 'compression jobs' : 'compression job')
		)
		.replace(/\bencode hosts?\b/gi, (match) =>
			matchCase(match, match.toLowerCase().endsWith('s') ? 'computers' : 'computer')
		)
		.replace(/\bprocessing queue\b/gi, (match) => matchCase(match, 'compression queue'))
		.replace(/\bencoding\b/gi, (match) => matchCase(match, 'compressing'))
		.replace(/\bprocessing\b/gi, (match) => matchCase(match, 'compressing'))
		.replace(/\bencode\b/gi, (match) => matchCase(match, 'compress'))
		.replace(/\bworkers?\b/gi, (match) =>
			match.toLowerCase().endsWith('s') ? 'computers' : 'computer'
		);
}

function matchCase(source: string, replacement: string): string {
	return /^[A-Z]/.test(source)
		? replacement.charAt(0).toUpperCase() + replacement.slice(1)
		: replacement;
}

export function movieGoalFactsView(
	durationSeconds: number | null | undefined,
	sourceSizeBytes: number | null | undefined,
	sizeGoal: ResolvedSizeGoalPayload | null | undefined,
	estimate?: Pick<
		MovieTitle,
		'estimated_output_bytes' | 'estimate_provenance' | 'estimate_coverage'
	> | null
): MovieGoalFactsView {
	const targetSizeBytes = finitePositive(sizeGoal?.target_size_bytes);
	const sourceSize = finitePositive(sourceSizeBytes);
	const lowerBound = finitePositive(
		sizeGoal?.final_lower_bound_bytes ?? sizeGoal?.sample_lower_bound_bytes
	);
	const upperBound = finitePositive(
		sizeGoal?.final_upper_bound_bytes ?? sizeGoal?.sample_upper_bound_bytes
	);
	const sampledOutputBytes =
		estimate?.estimate_provenance === 'sampled_calibration' && estimate.estimate_coverage?.complete
			? finitePositive(estimate.estimated_output_bytes)
			: null;
	const expectedOutputBytes = sampledOutputBytes ?? targetSizeBytes;
	const savingsBytes = sourceSize && expectedOutputBytes ? sourceSize - expectedOutputBytes : null;
	const savingsPercent =
		sourceSize && savingsBytes != null && savingsBytes > 0
			? Math.round((savingsBytes / sourceSize) * 100)
			: null;
	const expectedSavings =
		sourceSize && expectedOutputBytes && savingsBytes != null
			? savingsBytes > 0 && savingsPercent != null
				? `${formatMovieBytes(savingsBytes)} · ${savingsPercent}%`
				: savingsBytes < 0
					? `Grows by ${formatMovieBytes(Math.abs(savingsBytes))}`
					: sampledOutputBytes
						? 'No size reduction estimated'
						: 'No size reduction planned'
			: 'Not available';
	const targetRange =
		lowerBound && upperBound
			? `${formatMovieBytes(lowerBound)}–${formatMovieBytes(upperBound)}`
			: 'Not available';
	const outputDetail = sampledOutputBytes
		? lowerBound && upperBound
			? sampledOutputBytes > upperBound
				? `Current sample estimate · ${formatMovieBytes(sampledOutputBytes - upperBound)} above target range`
				: sampledOutputBytes < lowerBound
					? `Current sample estimate · ${formatMovieBytes(lowerBound - sampledOutputBytes)} below target range`
					: 'Current sample estimate · inside target range'
			: 'Current completed-sample estimate'
		: expectedOutputBytes
			? targetRange === 'Not available'
				? 'Configured target, not a measured result'
				: `Target range ${targetRange}`
			: 'No current output estimate';

	return {
		duration: formatDuration(durationSeconds),
		sourceSize: formatMovieBytes(sourceSize),
		expectedOutput: formatMovieBytes(expectedOutputBytes),
		expectedSavings,
		targetRange,
		outputDetail,
		estimateQuality: sampledOutputBytes
			? 'Current completed-sample estimate, not a guarantee'
			: targetSizeBytes && lowerBound && upperBound
				? 'Planning range, not a guarantee'
				: targetSizeBytes
					? 'Target estimate, not a guarantee'
					: 'Target not resolved',
		estimateBasis: sampledOutputBytes ? 'sampled' : targetSizeBytes ? 'target' : 'unavailable'
	};
}

export function movieGoalContractView(
	intent: ResolvedOperatorIntentPayload | null | undefined,
	ledger: StreamBudgetLedgerPayload | null | undefined,
	sampleItem: RepresentativeSampleItemPayload | null | undefined,
	qualityRisk: QualityRiskPayload | null | undefined,
	resolvedMetric?: string | null
): MovieGoalContractView {
	if (
		!intent?.size_goal ||
		!intent.resolution ||
		!intent.quality ||
		!intent.streams ||
		!ledger?.stream_plan
	) {
		return { status: 'resolving', rows: [], findings: [] };
	}

	const sizeGoal = intent.size_goal;
	const sizeProvenance = provenanceLabel(sizeGoal.source, sizeGoal.requires_confirmation);
	const sizeValue =
		sizeGoal.mode === 'normalized'
			? 'Runtime-scaled target'
			: sizeGoal.mode === 'absolute'
				? 'Fixed target'
				: 'Needs your choice';
	const sizeDetail =
		sizeGoal.mode === 'normalized'
			? normalizedSizeDetail(sizeGoal)
			: sizeGoal.mode === 'absolute'
				? 'Use one fixed size target for this movie.'
				: 'Choose how the output size should be calculated before preparing.';

	const resolution = intent.resolution;
	const sourceWidth = finitePositive(sampleItem?.width);
	const sourceHeight = finitePositive(sampleItem?.height);
	const sourceResolution =
		sourceWidth && sourceHeight ? `${Math.round(sourceWidth)}×${Math.round(sourceHeight)}` : null;
	const resolutionProvenance = provenanceLabel(
		resolution.source,
		Boolean(resolution.requires_confirmation)
	);
	const resolutionCopy = resolutionContractCopy(
		resolution.mode,
		finitePositive(resolution.max_height),
		sourceResolution,
		sourceHeight
	);

	const quality = qualityContractCopy(intent, resolvedMetric);
	const compression = compressionContractCopy(intent);
	const streamSource = intent.streams.source ?? '';
	const plannedStreams = ledger?.stream_plan.streams ?? [];
	const audioStreams: StreamBudgetLedgerPayload['stream_plan']['streams'] = [];
	const subtitleStreams: StreamBudgetLedgerPayload['stream_plan']['streams'] = [];
	for (const stream of plannedStreams) {
		if (stream.kind === 'audio') audioStreams.push(stream);
		if (stream.kind === 'subtitle') subtitleStreams.push(stream);
	}
	const audio = streamContractCopy('Audio', audioStreams, streamSource);
	const subtitles = streamContractCopy('Subtitles', subtitleStreams, streamSource);
	const findings = goalContractFindings(sampleItem, qualityRisk);
	const reviewFocus = reviewFocusContractCopy(findings);

	return {
		status: 'ready',
		rows: [
			{
				label: 'Size rule',
				value: sizeValue,
				detail: sizeDetail,
				provenance: sizeProvenance,
				tone: sizeProvenance === 'Needs your choice' ? 'attention' : 'normal'
			},
			{
				label: 'Resolution',
				...resolutionCopy,
				provenance: resolutionProvenance,
				tone: resolutionProvenance === 'Needs your choice' ? 'attention' : 'normal'
			},
			quality,
			compression,
			audio,
			subtitles,
			reviewFocus
		],
		findings
	};
}

function normalizedSizeDetail(sizeGoal: ResolvedSizeGoalPayload): string {
	const referenceSize = finitePositive(sizeGoal.reference_size_mb);
	const referenceMinutes = finitePositive(sizeGoal.reference_runtime_minutes);
	if (referenceSize && referenceMinutes) {
		return `${formatCompactNumber(referenceSize)} MB per ${formatCompactNumber(referenceMinutes)} minutes, scaled to this movie.`;
	}
	return 'Scale the target to this movie’s runtime.';
}

function resolutionContractCopy(
	mode: string,
	maxHeight: number | null,
	sourceResolution: string | null,
	sourceHeight: number | null
): Pick<MovieGoalContractRow, 'value' | 'detail'> {
	if (mode === 'source') {
		return {
			value: sourceResolution ? `Preserve ${sourceResolution}` : 'Preserve source',
			detail: 'Keep the source resolution; no downscale is planned.'
		};
	}
	if (mode === 'max_height' && maxHeight) {
		if (sourceHeight && sourceHeight <= maxHeight) {
			return {
				value: sourceResolution ? `Preserve ${sourceResolution}` : `Up to ${maxHeight}p`,
				detail: `The source is already within the ${Math.round(maxHeight)}p cap.`
			};
		}
		return {
			value: `Cap at ${Math.round(maxHeight)}p`,
			detail: sourceResolution
				? `Downscale the ${sourceResolution} source to fit the height cap.`
				: 'Downscale only when the source is taller than this cap.'
		};
	}
	return {
		value: 'Needs your choice',
		detail: 'Choose whether to preserve the source resolution or apply a height cap.'
	};
}

function qualityContractCopy(
	intent: ResolvedOperatorIntentPayload,
	resolvedMetric: string | null | undefined
): MovieGoalContractRow {
	const quality = intent.quality;
	const configuredMetric = textValue(quality?.metric).toUpperCase();
	const metric =
		configuredMetric === 'AUTO' ? textValue(resolvedMetric).toUpperCase() : configuredMetric;
	if (configuredMetric === 'AUTO' && !metric) {
		return {
			label: 'Quality floor',
			value: 'Automatic metric guardrail',
			detail:
				'Use VMAF when available, otherwise XPSNR; Mediaforce resolves the active floor before sampling.',
			provenance: provenanceLabel(quality?.source, false),
			tone: 'normal'
		};
	}
	const usesXpsnr = metric === 'XPSNR';
	const target = finitePositive(usesXpsnr ? quality?.target_xpsnr : quality?.target_vmaf);
	const floor = finitePositive(usesXpsnr ? quality?.min_target_xpsnr : quality?.min_target_vmaf);
	if (!metric || !floor) {
		return {
			label: 'Quality floor',
			value: 'Still resolving',
			detail: 'Mediaforce is resolving the acceptance guardrail for this movie.',
			provenance: 'Still resolving',
			tone: 'normal'
		};
	}
	return {
		label: 'Quality floor',
		value: `${metric} floor ${formatCompactNumber(floor)}`,
		detail: target
			? `Aim for ${formatCompactNumber(target)}; the floor is an acceptance guardrail, not a guarantee.`
			: 'This floor is an acceptance guardrail, not a guarantee.',
		provenance: provenanceLabel(quality?.source, false),
		tone: 'normal'
	};
}

function compressionContractCopy(intent: ResolvedOperatorIntentPayload): MovieGoalContractRow {
	const compression = intent.compression_intent;
	const requiresChoice =
		!compression || compression.requires_confirmation || !compression.confirmed;
	return {
		label: 'Compression',
		value: requiresChoice ? 'Needs your choice' : compression.title,
		detail: requiresChoice
			? 'Choose the intended balance between file size and visible detail before preparing.'
			: `${compression.detail} ${compression.accepts_under_target_result ? 'A smaller result is acceptable when it still meets the quality floor.' : 'Stay near the size target when the quality floor allows it.'}`,
		provenance: provenanceLabel(compression?.source, requiresChoice),
		tone: requiresChoice ? 'attention' : 'normal'
	};
}

function streamContractCopy(
	label: 'Audio' | 'Subtitles',
	streams: StreamBudgetLedgerPayload['stream_plan']['streams'],
	source: string
): MovieGoalContractRow {
	if (!streams.length) {
		return {
			label,
			value: label === 'Audio' ? 'No audio detected' : 'No subtitles detected',
			detail: `No ${label.toLowerCase()} streams are present in the resolved stream plan.`,
			provenance: provenanceLabel(source, false),
			tone: 'normal'
		};
	}
	let copied = 0;
	let converted = 0;
	let dropped = 0;
	for (const stream of streams) {
		if (stream.action === 'copy') copied += 1;
		if (stream.action === 'transcode') converted += 1;
		if (stream.action === 'drop') dropped += 1;
	}
	const kept = copied + converted;
	const noun = label === 'Audio' ? 'track' : 'subtitle';
	const parts = [
		copied ? `${copied} copied` : '',
		converted ? `${converted} converted` : '',
		dropped ? `${dropped} dropped` : ''
	].filter(Boolean);
	return {
		label,
		value: kept ? `Keep ${kept} ${noun}${kept === 1 ? '' : 's'}` : `Drop all ${noun}s`,
		detail: `${parts.join(', ')}. ${label === 'Audio' ? 'Copied tracks stay unchanged; converted tracks use the planned output codec.' : 'Only the listed subtitle streams are kept.'}`,
		provenance: provenanceLabel(source, false),
		tone: dropped === streams.length ? 'attention' : 'normal'
	};
}

function goalContractFindings(
	sampleItem: RepresentativeSampleItemPayload | null | undefined,
	qualityRisk: QualityRiskPayload | null | undefined
): MovieGoalContractFinding[] {
	const findings: MovieGoalContractFinding[] = [];
	const fingerprint = sampleItem?.media_fingerprint_decision;
	const fingerprintStatus = textValue(fingerprint?.status).toLowerCase();
	for (const finding of fingerprint?.findings ?? []) {
		const label = qualityFindingLabel(finding.id);
		if (!label || hasFindingLabel(findings, label)) continue;
		findings.push({
			kind: finding.advisory
				? 'Advisory'
				: fingerprintStatus === 'measured'
					? 'Measured'
					: 'Out of date',
			label,
			detail: textValue(finding.rationale) || undefined
		});
	}
	for (const risk of qualityRisk?.typed_risks ?? []) {
		const label = textValue(risk.label) || qualityFindingLabel(risk.tag);
		if (!label || hasFindingLabel(findings, label)) continue;
		const measured = Boolean(risk.evidence_ids?.length || risk.moment_indexes?.length);
		findings.push({
			kind: measured ? 'Measured' : 'Advisory',
			label,
			detail: textValue(risk.rationale) || undefined
		});
	}
	for (const label of qualityRisk?.pre_test_instruction?.focus_labels ?? []) {
		const normalized = textValue(label);
		if (!normalized || hasFindingLabel(findings, normalized)) continue;
		findings.push({ kind: 'Advisory', label: normalized });
	}
	if (
		hasStaleEvidenceReason(qualityRisk?.blocking_reasons) &&
		!hasFindingKind(findings, 'Out of date')
	) {
		findings.push({
			kind: 'Out of date',
			label: 'Earlier evidence no longer matches this source'
		});
	}
	return findings;
}

function reviewFocusContractCopy(findings: MovieGoalContractFinding[]): MovieGoalContractRow {
	let measured = 0;
	let advisory = 0;
	let outOfDate = 0;
	for (const finding of findings) {
		if (finding.kind === 'Measured') measured += 1;
		if (finding.kind === 'Advisory') advisory += 1;
		if (finding.kind === 'Out of date') outOfDate += 1;
	}
	const value = measured
		? `${measured} measured ${measured === 1 ? 'finding' : 'findings'}`
		: advisory
			? `${advisory} review ${advisory === 1 ? 'focus' : 'focuses'}`
			: 'Standard visual review';
	const detail = outOfDate
		? `${outOfDate} earlier ${outOfDate === 1 ? 'finding is' : 'findings are'} out of date; use the current measured and advisory focus.`
		: measured && advisory
			? 'Measured findings are confirmed by evidence; advisories are lower-confidence places to inspect.'
			: measured
				? 'These findings are confirmed by current evidence.'
				: advisory
					? 'These are lower-confidence places to inspect during review.'
					: 'Check motion, dark gradients, texture, and sound before approval.';
	return {
		label: 'Review focus',
		value,
		detail,
		provenance: measured
			? 'Measured evidence'
			: advisory
				? 'Mediaforce advisory'
				: 'Mediaforce default',
		tone: outOfDate ? 'attention' : 'normal'
	};
}

function hasFindingLabel(findings: MovieGoalContractFinding[], label: string): boolean {
	for (const finding of findings) {
		if (finding.label === label) return true;
	}
	return false;
}

function hasFindingKind(
	findings: MovieGoalContractFinding[],
	kind: MovieGoalContractFinding['kind']
): boolean {
	for (const finding of findings) {
		if (finding.kind === kind) return true;
	}
	return false;
}

function hasStaleEvidenceReason(reasons: string[] | null | undefined): boolean {
	for (const reason of reasons ?? []) {
		if (/stale|different source/i.test(reason)) return true;
	}
	return false;
}

function qualityFindingLabel(value: unknown): string {
	const normalized = textValue(value).toLowerCase().replaceAll('-', '_').replaceAll(' ', '_');
	return (
		{
			dark_luma: 'Banding / dark-scene damage',
			softness_detail_loss: 'Softness / detail loss',
			high_texture: 'Softness / detail loss',
			animation_cues: 'Softness / detail loss',
			motion_breakup: 'Motion breakup',
			high_motion: 'Motion breakup',
			banding_dark_scene_damage: 'Banding / dark-scene damage',
			dark_gradient_banding_risk: 'Banding / dark-scene damage',
			grain_noise_treatment: 'Grain / noise treatment',
			likely_film_grain: 'Grain / noise treatment',
			likely_analog_noise: 'Grain / noise treatment',
			compression_noise_advisory: 'Grain / noise treatment',
			uncertain_noise_mix: 'Grain / noise treatment',
			cadence_interlace_artifacts: 'Cadence / interlace artifacts',
			duplicate_cadence: 'Cadence / interlace artifacts',
			audio_quality_layout: 'Audio quality / layout',
			audio_complexity: 'Audio quality / layout',
			other: 'Other review concern'
		}[normalized] ?? textValue(value)
	);
}

function provenanceLabel(source: unknown, requiresChoice: boolean): string {
	if (requiresChoice) return 'Needs your choice';
	const normalized = textValue(source).toLowerCase();
	if (!normalized) return 'Still resolving';
	if (/operator|request|note|user/.test(normalized)) return 'You set this';
	if (/folder|profile|library|policy/.test(normalized)) return 'Library setting';
	if (/legacy|snapshot|persisted/.test(normalized)) return 'Carried over';
	return 'Mediaforce default';
}

function formatCompactNumber(value: number): string {
	return value.toFixed(Number.isInteger(value) ? 0 : 1);
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

function formatPercent(value: number): string {
	return value.toFixed(Number.isInteger(value) ? 0 : 1);
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
	return formatFileSize(value, 'Unknown');
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
