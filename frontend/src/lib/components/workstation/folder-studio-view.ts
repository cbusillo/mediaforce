import {
	codecLabel,
	flattenPolicy,
	formatBitrateCopy,
	formatLanguageCopy,
	formatPolicyValue,
	formatPercentCopy,
	formatResolutionCopy,
	normalizeReviewArtifacts,
	pathFilename,
	policyRowLabel,
	summarizeAudioPlan,
	summarizeAudioTrack,
	summarizeSubtitlePlan,
	summarizeSubtitleSource,
	workbenchSection,
	type FolderCalibrationJob,
	type FolderCalibrationState,
	type FolderItemPlan,
	type FolderPolicy,
	type FolderSampleItem,
	type FolderOperatorRequest,
	type PendingSampleProposal,
	type ReviewGate,
	type VisibleReviewArtifact
} from '$lib/folders/studio';
import type {
	EncodeQueueJob,
	FolderPayload,
	FolderStatusPayload,
	FolderWorkflowState,
	HostsPayload
} from '$lib/api/types';
import type { FooterSignal, ShellTone, StatusTile } from './OperatorShell.svelte';

export type WorkflowState = {
	tone: ShellTone;
	label: string;
	title: string;
	copy: string;
	primary: string;
	primaryAction: WorkflowAction;
	secondary: string;
	secondaryAction: WorkflowAction;
	revisionPrompt?: string;
	isOutputWorkflow?: boolean;
};

export type SampleVerdict = {
	tone: ShellTone;
	label: string;
	title: string;
	recommendation: string;
	predictedPerItem: string;
	predictedFolderTotal: string;
	predictedBitrate: string;
	reclaim: string;
	quality: string;
	target: string;
	targetBitrate: string;
	targetDelta: string;
	missRatio: number | null;
	missesTarget: boolean;
	stalePolicy: boolean;
};

function reviseSmallerPrompt(verdict: SampleVerdict): string {
	const measured = [verdict.predictedPerItem, verdict.targetDelta].filter(Boolean).join(' · ');
	return `Revise this sample smaller toward ${verdict.target}. The last sample was ${measured}; keep the review quality as high as possible, but make the next sample materially smaller.`;
}

export type WorkflowAction =
	| 'approve-size-tradeoff'
	| 'download-review-pack'
	| 'focus-bench'
	| 'monitor-processing'
	| 'monitor-review'
	| 'monitor-sample'
	| 'open-folders'
	| 'open-ops'
	| 'open-series'
	| 'promote-outputs'
	| 'queue-encode'
	| 'revise-smaller'
	| 'retry-encode'
	| 'retry-sample'
	| 'revise-proposal'
	| 'start-sample'
	| 'stop-sample'
	| 'validate-outputs'
	| 'resample';

export type ProposalRow = {
	section: string;
	label: string;
	current: string;
	draft: string;
	changed: boolean;
};

export type OutputReviewRow = {
	label: string;
	source: string;
	output: string;
	detail: string;
	tone?: ShellTone;
	current?: boolean;
};

export type ReviewWorkspaceView = {
	badge: string;
	badgeTone?: ShellTone;
	title: string;
	rows: OutputReviewRow[];
	layout?: 'evidence' | 'pipeline';
};

export type BudgetEnforcementView = {
	active: boolean;
	cap: string;
	capBytes: string | null;
	reason: string;
};

export type DecisionFact = {
	label: string;
	value: string;
	detail: string;
};

export type BenchMessage = {
	id: string;
	role: 'operator' | 'bench' | 'system';
	label: string;
	title: string;
	body: string;
	meta?: string;
	tone?: ShellTone | 'neutral';
};

export type BenchHostOption = {
	key: string;
	label: string;
	detail: string;
	available: boolean;
	scheduleOpen: boolean | null;
	state: string;
};

export type BenchRequestState = {
	disabled: boolean;
	blocker: string;
	selectedHost: BenchHostOption | null;
	activeCalibrationJob: boolean;
};

export type WorkflowActionState = {
	disabled: boolean;
	title: string;
};

export type QueueSubmissionMode = 'approve-profile' | 'queue-approved';

export type WorkflowStep = {
	label: string;
	detail: string;
	tone: ShellTone;
	current: boolean;
};

function numberValue(value: unknown): number | null {
	const parsed = Number(value);
	return Number.isFinite(parsed) ? parsed : null;
}

function sampleResult(calibration: FolderCalibrationState | null): Record<string, unknown> | null {
	return record<Record<string, unknown>>(calibration?.sample_result);
}

function calibrationAdvice(
	calibration: FolderCalibrationState | null
): Record<string, unknown> | null {
	return record<Record<string, unknown>>(calibration?.advice);
}

function operatorRequest(
	calibration: FolderCalibrationState | null
): Record<string, unknown> | null {
	return record<Record<string, unknown>>(calibrationAdvice(calibration)?.operator_request);
}

function runVerdict(calibration: FolderCalibrationState | null): Record<string, unknown> | null {
	return record<Record<string, unknown>>(calibrationAdvice(calibration)?.run_verdict);
}

function policyVideo(value: unknown): Record<string, unknown> | null {
	return record<Record<string, unknown>>(record<Record<string, unknown>>(value)?.video);
}

function policyAudio(value: unknown): Record<string, unknown> | null {
	return record<Record<string, unknown>>(record<Record<string, unknown>>(value)?.audio);
}

function policySubtitle(value: unknown): Record<string, unknown> | null {
	return record<Record<string, unknown>>(record<Record<string, unknown>>(value)?.subtitle);
}

function activePolicy(
	folder: FolderPayload,
	pendingProposal: PendingSampleProposal | null
): FolderPolicy | null {
	return (pendingProposal?.preview_policy ??
		pendingProposal?.applied_policy ??
		folder.policy ??
		folder.summary?.resolved_policy ??
		null) as FolderPolicy | null;
}

function activeVideoPolicy(
	folder: FolderPayload,
	pendingProposal: PendingSampleProposal | null
): Record<string, unknown> | null {
	return policyVideo(activePolicy(folder, pendingProposal));
}

export function record<T extends Record<string, unknown>>(value: unknown): T | null {
	return value && typeof value === 'object' ? (value as T) : null;
}

function compactText(value: unknown): string {
	return typeof value === 'string' ? value.trim() : '';
}

function compactParts(parts: Array<string | null | undefined>): string {
	return parts.filter((part) => part && part.trim()).join(' · ');
}

function activeCalibrationStatus(value: unknown): boolean {
	return ['queued', 'running', 'pending_review'].includes(String(value ?? '').toLowerCase());
}

export function buildBenchHostOptions(
	folderOptions: Array<Record<string, unknown>> | undefined
): BenchHostOption[] {
	return (folderOptions ?? [])
		.map((host) => {
			const key = compactText(host.key);
			const available = host.available !== false;
			const scheduleOpen = typeof host.schedule_open === 'boolean' ? host.schedule_open : null;
			return {
				key,
				label: compactText(host.label) || key || 'Host',
				detail: compactText(host.detail) || compactText(host.message),
				available,
				scheduleOpen,
				state: !available
					? 'Unavailable'
					: scheduleOpen === false
						? 'Sample ok, encode later'
						: 'Ready for samples'
			};
		})
		.filter((host) => host.key);
}

export function resolveBenchRequestState(
	note: string,
	selectedHostKey: string,
	hostOptions: BenchHostOption[],
	calibrationJob: FolderCalibrationJob | null,
	pending: boolean
): BenchRequestState {
	const selectedHost = hostOptions.find((host) => host.key === selectedHostKey) ?? null;
	const activeCalibrationJob = activeCalibrationStatus(calibrationJob?.status);
	let blocker = '';
	if (!note.trim()) {
		blocker = 'Describe the review request before sending.';
	} else if (!selectedHostKey) {
		blocker = 'Choose an available worker before sending.';
	} else if (!selectedHost) {
		blocker = 'Selected worker is no longer available for this folder.';
	} else if (!selectedHost.available) {
		blocker = 'Selected worker is not available right now.';
	} else if (activeCalibrationJob) {
		blocker = 'A sample job is already active for this folder.';
	}
	return {
		disabled: pending || Boolean(blocker),
		blocker: pending ? 'Sending request to the review assistant.' : blocker,
		selectedHost,
		activeCalibrationJob
	};
}

export function resolveWorkflowActionState(
	action: WorkflowAction,
	{
		reviewPackReady,
		approvalReviewReady,
		approvedProfileReady,
		pendingProposal,
		calibrationJob,
		pendingAction
	}: {
		reviewPackReady: boolean;
		approvalReviewReady?: boolean;
		approvedProfileReady?: boolean;
		pendingProposal: PendingSampleProposal | null;
		calibrationJob: FolderCalibrationJob | null;
		pendingAction?: WorkflowAction | null;
	}
): WorkflowActionState {
	if (pendingAction) {
		return {
			disabled: true,
			title: pendingAction === action ? 'Action is running.' : 'Another folder action is running.'
		};
	}
	if (action === 'focus-bench') return { disabled: false, title: '' };
	if (action === 'approve-size-tradeoff') {
		return approvalReviewReady
			? { disabled: false, title: '' }
			: { disabled: true, title: 'Review media is not ready yet.' };
	}
	if (action === 'revise-proposal') return { disabled: false, title: '' };
	if (['monitor-processing', 'monitor-review', 'monitor-sample'].includes(action)) {
		return { disabled: false, title: '' };
	}
	if (action === 'open-folders' || action === 'open-series') return { disabled: false, title: '' };
	if (action === 'open-ops') return { disabled: false, title: '' };
	if (action === 'validate-outputs' || action === 'promote-outputs')
		return { disabled: false, title: '' };
	if (action === 'retry-encode') return { disabled: false, title: '' };
	if (action === 'stop-sample') {
		if (activeCalibrationStatus(calibrationJob?.status)) return { disabled: false, title: '' };
		return { disabled: true, title: 'No sample job is running.' };
	}
	if (action === 'queue-encode') {
		if (approvedProfileReady) return { disabled: false, title: '' };
		if (pendingProposal?.proposal_id && pendingProposal.can_queue === false) {
			return {
				disabled: true,
				title: pendingProposal.message || 'The current draft is not ready to queue.'
			};
		}
		return { disabled: false, title: '' };
	}
	if (action === 'download-review-pack') {
		return reviewPackReady
			? { disabled: false, title: '' }
			: { disabled: true, title: 'Review pack is not ready yet.' };
	}
	if (action === 'start-sample' || action === 'retry-sample') {
		if (activeCalibrationStatus(calibrationJob?.status)) {
			return { disabled: true, title: 'A sample job is already active for this folder.' };
		}
		if (action === 'retry-sample') return { disabled: false, title: '' };
		if (!pendingProposal?.proposal_id) {
			return {
				disabled: true,
				title: 'Ask the review assistant for a draft before starting the sample.'
			};
		}
		if (pendingProposal.can_queue === false) {
			return {
				disabled: true,
				title: pendingProposal.message || 'The current draft is not ready to queue.'
			};
		}
		return { disabled: false, title: '' };
	}
	return { disabled: true, title: 'Wiring handoff pending for this workflow action.' };
}

export function resolveQueueSubmissionMode(
	action: WorkflowAction,
	reviewGate: ReviewGate | null
): QueueSubmissionMode | null {
	if (action === 'approve-size-tradeoff') return 'approve-profile';
	if (action !== 'queue-encode') return null;
	return reviewGate?.status === 'accepted' ? 'queue-approved' : 'approve-profile';
}

function requestSummary(request: FolderOperatorRequest | null | undefined): string {
	if (!request) return '';
	const parts = [
		compactText(request.request_text),
		compactText(request.metric),
		request.target == null ? '' : `target ${request.target}`,
		compactText(request.budget_label),
		compactText(request.scale_label),
		compactText(request.feasibility)
	].filter(Boolean);
	return parts.join(' · ');
}

function pushMessage(messages: BenchMessage[], message: BenchMessage): void {
	if (!message.body.trim()) return;
	if (messages.some((item) => item.role === message.role && item.body === message.body)) return;
	messages.push(message);
}

export function buildBenchMessages(
	calibration: FolderCalibrationState | null,
	pendingProposal: PendingSampleProposal | null,
	retryableSampleJob: FolderCalibrationJob | null
): BenchMessage[] {
	const messages: BenchMessage[] = [];
	const operatorBody =
		compactText(pendingProposal?.operator_note) ||
		compactText(calibration?.advice?.operator_note) ||
		requestSummary(pendingProposal?.operator_request ?? calibration?.advice?.operator_request);

	if (operatorBody) {
		pushMessage(messages, {
			id: 'operator-request',
			role: 'operator',
			label: 'Operator',
			title: 'Request',
			body: operatorBody,
			meta: pendingProposal?.created_at ? `submitted ${pendingProposal.created_at}` : undefined,
			tone: 'ready'
		});
	} else {
		pushMessage(messages, {
			id: 'operator-empty',
			role: 'operator',
			label: 'Operator',
			title: 'No request sent',
			body: 'Use the composer to request a representative sample, a revision, or a validation pass.',
			tone: 'neutral'
		});
	}

	const responseBody =
		compactText(pendingProposal?.request_response) ||
		compactText(calibration?.advice?.request_response) ||
		compactText(pendingProposal?.summary) ||
		compactText(calibration?.advice?.summary);
	pushMessage(messages, {
		id: 'bench-response',
		role: 'bench',
		label: 'Review assistant',
		title:
			pendingProposal?.request_disposition ??
			calibration?.advice?.request_disposition ??
			'Response',
		body:
			responseBody ||
			'Waiting for a request. Review guidance will appear here with the resulting proposal context.',
		meta: pendingProposal?.confidence ?? calibration?.advice?.confidence ?? undefined,
		tone: responseBody ? 'active' : 'neutral'
	});

	const diagnosis =
		compactText(pendingProposal?.diagnosis) || compactText(calibration?.advice?.diagnosis);
	pushMessage(messages, {
		id: 'bench-diagnosis',
		role: 'bench',
		label: 'Review assistant',
		title: 'Diagnosis',
		body: diagnosis,
		tone: 'active'
	});

	const followUp =
		compactText(pendingProposal?.suggested_follow_up) ||
		compactText(calibration?.advice?.suggested_follow_up);
	pushMessage(messages, {
		id: 'bench-follow-up',
		role: 'bench',
		label: 'Review assistant',
		title: 'Suggested next step',
		body: followUp,
		tone: 'active'
	});

	const selfCheck = pendingProposal?.self_check ?? null;
	if (selfCheck && (selfCheck.summary || (selfCheck.issues?.length ?? 0) > 0)) {
		pushMessage(messages, {
			id: 'system-self-check',
			role: 'system',
			label: 'System',
			title: `Self-check ${selfCheck.status ?? 'returned'}`,
			body: [compactText(selfCheck.summary), ...(selfCheck.issues ?? [])]
				.filter(Boolean)
				.join(' · '),
			tone: selfCheck.status === 'passed' ? 'ready' : 'wait'
		});
	}

	if (retryableSampleJob) {
		pushMessage(messages, {
			id: 'system-retryable-sample',
			role: 'system',
			label: 'System',
			title: 'Retryable sample',
			body:
				compactText(retryableSampleJob.error) ||
				compactText(retryableSampleJob.notes) ||
				'Retry is available for the last sample run.',
			meta: retryableSampleJob.host?.label,
			tone: 'fail'
		});
	}

	return messages;
}

export function resolveFolderTitle(prefix: string): string {
	const parts = prefix.split('/').filter(Boolean);
	if (parts.length === 0) return 'Current folder';
	const last = parts.at(-1) ?? prefix;
	if (/^season\s+\d+$/i.test(last) && parts.length > 1) {
		return `${parts.at(-2)} · ${last}`;
	}
	return last.replaceAll('_', ' ');
}

export function formatBytes(value: number | null | undefined): string {
	if (value == null) return '—';
	if (!Number.isFinite(value)) return '—';
	const absValue = Math.abs(value);
	if (absValue >= 1024 ** 3) {
		return `${(value / 1024 ** 3).toLocaleString('en-US', {
			maximumFractionDigits: 1
		})} GiB`;
	}
	if (absValue >= 1024 ** 2) {
		return `${(value / 1024 ** 2).toLocaleString('en-US', {
			maximumFractionDigits: absValue >= 100 * 1024 ** 2 ? 0 : 1
		})} MiB`;
	}
	if (absValue >= 1024) {
		return `${(value / 1024).toLocaleString('en-US', { maximumFractionDigits: 0 })} KiB`;
	}
	return `${value.toLocaleString('en-US', { maximumFractionDigits: 0 })} B`;
}

function formatDuration(value: number | null | undefined): string {
	if (value == null || !Number.isFinite(value) || value <= 0) return '—';
	const totalSeconds = Math.round(value);
	const hours = Math.floor(totalSeconds / 3600);
	const minutes = Math.floor((totalSeconds % 3600) / 60);
	const seconds = totalSeconds % 60;
	if (hours > 0) return `${hours}h ${minutes}m`;
	return `${minutes}m ${seconds.toString().padStart(2, '0')}s`;
}

function formatAverageBitrate(bytes: number | null, durationSeconds: number | null): string {
	if (bytes === null || durationSeconds === null || bytes <= 0 || durationSeconds <= 0) return '—';
	const kbps = (bytes * 8) / durationSeconds / 1000;
	if (!Number.isFinite(kbps) || kbps <= 0) return '—';
	if (kbps >= 1000) {
		return `${(kbps / 1000).toLocaleString('en-US', {
			maximumFractionDigits: kbps >= 10_000 ? 0 : 1
		})} Mbps`;
	}
	return `${kbps.toLocaleString('en-US', { maximumFractionDigits: 0 })} kbps`;
}

function formatKbps(value: number | null | undefined): string {
	if (value == null || !Number.isFinite(value) || value <= 0) return '—';
	if (value >= 1000) {
		return `${(value / 1000).toLocaleString('en-US', { maximumFractionDigits: value >= 10_000 ? 0 : 1 })} Mbps`;
	}
	return `${value.toLocaleString('en-US', { maximumFractionDigits: 0 })} kbps`;
}

function formatRatio(value: number | null): string {
	if (value === null || !Number.isFinite(value) || value <= 0) return '';
	return `${value.toLocaleString('en-US', {
		maximumFractionDigits: value >= 10 ? 0 : 1,
		minimumFractionDigits: value >= 10 ? 0 : 1
	})}x target`;
}

export function summarizeStatuses(statuses: Record<string, number>): string {
	const entries = Object.entries(statuses).filter(([, count]) => count > 0);
	if (entries.length === 0) return 'None';
	return entries
		.slice(0, 2)
		.map(([state, count]) => `${count} ${state.replaceAll('_', ' ')}`)
		.join(' · ');
}

export function resolvedMetricCopy(folder: FolderPayload): string {
	const supported = [];
	if (folder.metric_support.vmaf) supported.push('VMAF');
	if (folder.metric_support.xpsnr) supported.push('XPSNR');
	if (folder.metric_support.ssim) supported.push('SSIM');
	return supported.length ? supported.join(' · ') : folder.metric_status_copy || 'No metrics';
}

export function projectedReclaimBytes(folder: FolderPayload): number | null {
	const calibration = record<FolderCalibrationState>(folder.calibration);
	const result = sampleResult(calibration);
	const predictedSize = Number(result?.predicted_total_size_bytes);
	const sourceSize = Number(folder.summary?.total_size_bytes);
	const itemCount = Number(folder.summary?.item_count);
	if (!Number.isFinite(sourceSize) || sourceSize <= 0) return null;
	if (!Number.isFinite(predictedSize) || predictedSize <= 0) return null;
	const predictedFolderSize =
		Number.isFinite(itemCount) && itemCount > 0 ? predictedSize * itemCount : predictedSize;
	return Math.max(sourceSize - predictedFolderSize, 0);
}

export function predictedFolderSizeBytes(folder: FolderPayload): number | null {
	const calibration = record<FolderCalibrationState>(folder.calibration);
	const predictedSize = Number(sampleResult(calibration)?.predicted_total_size_bytes);
	const itemCount = Number(folder.summary?.item_count);
	if (!Number.isFinite(predictedSize) || predictedSize <= 0) return null;
	return Number.isFinite(itemCount) && itemCount > 0 ? predictedSize * itemCount : predictedSize;
}

export function buildSampleVerdict(
	folder: FolderPayload,
	calibration: FolderCalibrationState | null,
	pendingProposal: PendingSampleProposal | null = null
): SampleVerdict | null {
	const result = sampleResult(calibration);
	if (!result) return null;
	const predictedSize = numberValue(result.predicted_total_size_bytes);
	if (predictedSize === null || predictedSize <= 0) return null;
	const request = operatorRequest(calibration);
	const verdict = runVerdict(calibration);
	const itemCount = numberValue(folder.summary?.item_count);
	const sourceSize = numberValue(folder.summary?.total_size_bytes);
	const folderTotal =
		itemCount !== null && itemCount > 0 ? predictedSize * itemCount : predictedSize;
	const reclaim =
		sourceSize !== null && sourceSize > 0 ? Math.max(sourceSize - folderTotal, 0) : null;
	const budget = numberValue(request?.budget_bytes);
	const durationSeconds = numberValue(folder.sample_item?.duration_seconds);
	const target =
		compactText(request?.budget_label) || (budget ? formatBytes(budget) : 'No size target');
	const missRatio = budget && budget > 0 ? predictedSize / budget : null;
	const outcome = compactText(verdict?.outcome).toLowerCase();
	const missesTarget =
		outcome === 'poor_fit' || outcome === 'over_target' || (missRatio !== null && missRatio > 1.15);
	const qualityMetric =
		compactText(result.quality_metric) || compactText(request?.metric).toUpperCase() || 'Quality';
	const qualityScore = numberValue(result.quality_score);
	const resultTarget = numberValue(result.quality_target);
	const activeVideo = activeVideoPolicy(folder, pendingProposal);
	const activeMetric = compactText(activeVideo?.quality_metric).toUpperCase();
	const activeTarget = numberValue(
		activeMetric === 'XPSNR' ? activeVideo?.target_xpsnr : activeVideo?.target_vmaf
	);
	const stalePolicy =
		activeTarget !== null && resultTarget !== null && Math.abs(activeTarget - resultTarget) >= 0.1;
	const quality = qualityScore === null ? '—' : `${qualityMetric} ${qualityScore.toFixed(1)}`;
	const predictedPerItem = formatBytes(predictedSize);
	const recommendation =
		compactText(verdict?.next_step) ||
		(missesTarget
			? 'Run another sample with a smaller-size target.'
			: 'Review the sample clips, then approve or revise.');
	return {
		tone: missesTarget ? 'wait' : 'ready',
		label: missesTarget ? 'Target missed' : 'Sample result',
		title: stalePolicy
			? `${predictedPerItem} per episode came from older settings.`
			: missesTarget && target !== 'No size target'
				? `${predictedPerItem} per episode misses ${target}.`
				: `${predictedPerItem} per episode sample is ready.`,
		recommendation,
		predictedPerItem,
		predictedFolderTotal: formatBytes(folderTotal),
		predictedBitrate: formatAverageBitrate(predictedSize, durationSeconds),
		reclaim: reclaim === null ? '—' : formatBytes(reclaim),
		quality,
		target,
		targetBitrate: formatAverageBitrate(budget, durationSeconds),
		targetDelta: formatRatio(missRatio),
		missRatio,
		missesTarget,
		stalePolicy
	};
}

export function buildBudgetEnforcementView(
	pendingProposal: PendingSampleProposal | null,
	sampleItem: FolderSampleItem | null = null
): BudgetEnforcementView | null {
	if (!pendingProposal) return null;
	const enforcement = record<Record<string, unknown>>(pendingProposal.budget_enforcement);
	const cap = numberValue(policyVideo(enforcement?.applied_policy)?.max_encoded_percent);
	if (cap === null || cap <= 0) return null;
	const sourceSize = numberValue(sampleItem?.source_size_bytes);
	const capBytes = sourceSize !== null && sourceSize > 0 ? (sourceSize * cap) / 100 : null;
	const analysis = record<Record<string, unknown>>(enforcement?.size_target_analysis);
	const ratio = numberValue(analysis?.predicted_to_budget_ratio);
	const target =
		compactText(pendingProposal.operator_request?.budget_label) || 'requested size target';
	const ratioCopy = ratio && ratio > 0 ? `${formatRatio(ratio)} miss` : 'measured miss';
	return {
		active: enforcement?.status === 'enforced_after_miss',
		cap: `${cap.toLocaleString('en-US', { maximumFractionDigits: 1 })}%`,
		capBytes: capBytes === null ? null : formatBytes(capBytes),
		reason: `Applied after ${ratioCopy} against ${target}.`
	};
}

export function buildDecisionFacts(
	folder: FolderPayload,
	calibration: FolderCalibrationState | null,
	pendingProposal: PendingSampleProposal | null,
	workflow?: WorkflowState
): DecisionFact[] {
	const verdict = buildSampleVerdict(folder, calibration, pendingProposal);
	const sampleItem = record<FolderSampleItem>(folder.sample_item);
	const enforcement = buildBudgetEnforcementView(pendingProposal, sampleItem);
	const draftPolicy = activePolicy(folder, pendingProposal);
	const video = videoPolicySummary(draftPolicy, pendingProposal);
	const targetVideoRate = formatKbps(pendingProposal?.operator_request?.target_video_bitrate_kbps);
	if (verdict?.stalePolicy && !pendingProposal?.proposal_id) {
		return [
			{
				label: 'Old sample',
				value: compactParts([
					verdict.predictedPerItem,
					verdict.predictedBitrate !== '—' ? verdict.predictedBitrate : null
				]),
				detail: compactParts([
					verdict.quality,
					verdict.targetDelta || null,
					verdict.target ? `old target ${verdict.target}` : null
				])
			},
			{
				label: 'Current target',
				value: video.output,
				detail: video.detail || 'Uses the current folder video policy.'
			},
			{
				label: 'Next action',
				value: 'Run fresh sample',
				detail: 'The old evidence does not match the current defaults.'
			}
		];
	}
	if (enforcement?.active) {
		return [
			{
				label: 'Last sample',
				value: verdict
					? compactParts([
							verdict.predictedPerItem,
							verdict.predictedBitrate !== '—' ? verdict.predictedBitrate : null
						])
					: 'Measured miss',
				detail: compactParts([
					verdict?.targetDelta || null,
					verdict?.target ? `target ${verdict.target}` : null,
					verdict && verdict.targetBitrate !== '—' ? `target ${verdict.targetBitrate}` : null
				])
			},
			{
				label: 'Next size ceiling',
				value: enforcement.capBytes ? `${enforcement.capBytes} max` : `${enforcement.cap} cap`,
				detail: compactParts([
					enforcement.capBytes ? `${enforcement.cap} of selected source` : null,
					pendingProposal?.operator_request?.budget_label ?? null
				])
			},
			{
				label: 'Next video plan',
				value: video.output,
				detail: compactParts([
					video.detail || 'Uses the current folder video policy.',
					targetVideoRate !== '—' ? `target video ${targetVideoRate}` : null
				])
			}
		];
	}
	if (verdict) {
		return [
			{
				label: 'Per episode',
				value: verdict.predictedPerItem,
				detail: compactParts([
					verdict.predictedBitrate !== '—' ? verdict.predictedBitrate : null,
					verdict.targetDelta || `Target ${verdict.target}`,
					verdict.targetBitrate !== '—' ? `target ${verdict.targetBitrate}` : null
				])
			},
			{ label: 'Folder output', value: verdict.predictedFolderTotal, detail: 'Projected total' },
			{ label: 'Reclaim', value: verdict.reclaim, detail: verdict.quality }
		];
	}
	const outputFact = buildOutputDecisionFact(folder.workflow_state ?? undefined, workflow);
	if (outputFact) {
		return [
			outputFact,
			buildOutputScopeFact(folder),
			{
				label: 'Next action',
				value: workflow?.primary ?? 'Use the decision buttons',
				detail: workflowActionDetail(workflow)
			}
		];
	}
	return [
		{
			label: 'Review pack',
			value: resolveReviewArtifacts(calibration, pendingProposal).length
				? `${resolveReviewArtifacts(calibration, pendingProposal).length} artifacts`
				: reviewReadyCopy(calibration),
			detail: 'Evidence state'
		},
		{
			label: 'Sample',
			value: sampleItem ? pathFilename(sampleItem.rel_path) : 'No sample selected',
			detail: 'Representative file'
		},
		{
			label: 'Next action',
			value: workflow?.primary ?? 'Use the decision buttons',
			detail: workflowActionDetail(workflow)
		}
	];
}

function buildOutputDecisionFact(
	workflowPayload: FolderWorkflowState | undefined,
	workflow: WorkflowState | undefined
): { label: string; value: string; detail: string } | null {
	const action = workflow?.primaryAction;
	if (action !== 'validate-outputs' && action !== 'promote-outputs') return null;
	const counts = workflowPayload?.counts;
	const count =
		action === 'validate-outputs'
			? (counts?.ready_to_validate ?? 0)
			: (counts?.ready_to_promote ?? 0);
	if (count <= 0) return null;
	return {
		label: 'Outputs',
		value: `${count} ready`,
		detail: action === 'validate-outputs' ? 'Ready to validate' : 'Ready to promote'
	};
}

function buildOutputScopeFact(folder: FolderPayload): {
	label: string;
	value: string;
	detail: string;
} {
	const count = numberValue(folder.summary?.item_count);
	const seasonCount = Object.keys(folder.summary?.seasons ?? {}).length;
	const scope = folder.series_context || seasonCount > 1 ? 'Whole show' : 'Folder';
	return {
		label: 'Scope',
		value: scope,
		detail:
			count && count > 0
				? `${count.toLocaleString('en-US')} item${count === 1 ? '' : 's'}`
				: folder.prefix
	};
}

function workflowActionDetail(workflow: WorkflowState | undefined): string {
	if (!workflow) return 'Draft, sample, review, or approve from here';
	if (workflow.secondaryAction !== 'open-ops' && workflow.secondary !== workflow.primary) {
		return `${workflow.secondary} also available`;
	}
	return workflow.copy;
}

export function buildSampleFacts(
	sampleItem: FolderSampleItem | null,
	summary: FolderPayload['summary']
): Array<{ label: string; value: string }> {
	return [
		{ label: 'File', value: sampleItem ? pathFilename(sampleItem.rel_path) : '—' },
		{ label: 'Runtime', value: formatDuration(sampleItem?.duration_seconds) },
		{
			label: 'Resolution',
			value: formatResolutionCopy(sampleItem?.width, sampleItem?.height) ?? '—'
		},
		{
			label: 'Source rate',
			value: formatAverageBitrate(
				numberValue(sampleItem?.source_size_bytes),
				numberValue(sampleItem?.duration_seconds)
			)
		},
		{ label: 'Codec', value: codecLabel(sampleItem?.video_codec) },
		{
			label: 'Size',
			value: formatBytes(sampleItem?.source_size_bytes ?? summary?.total_size_bytes)
		}
	];
}

function itemPlan(folder: FolderPayload): FolderItemPlan | null {
	const plan = record<Record<string, unknown>>(folder.item_plan);
	return plan ? (plan as FolderItemPlan) : null;
}

function reviewPackAudioSummary(
	calibration: FolderCalibrationState | null,
	pendingProposal: PendingSampleProposal | null
): string {
	const reviewPack =
		pendingProposal?.multimodal_review_pack ?? calibration?.advice?.multimodal_review_pack;
	return compactText(reviewPack?.audio_plan?.summary);
}

function draftDownscaleReason(
	pendingProposal: PendingSampleProposal | null,
	maxHeight: number | null
): string | null {
	if (maxHeight === null || maxHeight <= 0) return null;
	const requestText = compactParts([
		pendingProposal?.operator_request?.request_text ?? null,
		pendingProposal?.operator_note ?? null
	]).toLowerCase();
	if (buildBudgetEnforcementView(pendingProposal)?.active) {
		return 'downscale enforced after the measured miss';
	}
	if (requestText.includes('downscal')) {
		return 'downscale allowed by the size request';
	}
	return 'draft changes output resolution';
}

function videoPolicySummary(
	policy: FolderPolicy | null | undefined,
	pendingProposal: PendingSampleProposal | null
): {
	output: string;
	detail: string;
} {
	const video = policyVideo(policy);
	if (!video) return { output: 'No draft video policy', detail: '' };
	const encoder = compactText(video.encoder);
	const maxHeight = numberValue(video.max_height);
	const cap = numberValue(video.max_encoded_percent);
	const enforcedCap = buildBudgetEnforcementView(pendingProposal)?.active === true;
	const metric = compactText(video.quality_metric).toUpperCase();
	const target = numberValue(metric === 'XPSNR' ? video.target_xpsnr : video.target_vmaf);
	const floor = numberValue(metric === 'XPSNR' ? video.min_target_xpsnr : video.min_target_vmaf);
	const metricCopy =
		metric === 'VMAF' && target !== null && target <= 88 ? 'VMAF low-bitrate' : metric;
	const output = compactParts([
		encoder.includes('av1') ? 'AV1' : encoder || null,
		maxHeight !== null && maxHeight > 0 ? `max ${maxHeight}p` : 'source resolution',
		enforcedCap && cap !== null && cap > 0 ? `${formatPercentCopy(cap)} cap` : null
	]);
	const detail = compactParts([
		metricCopy ? `${metricCopy}${target !== null ? ` target ${target}` : ''}` : null,
		floor !== null ? `floor ${floor}` : null,
		draftDownscaleReason(pendingProposal, maxHeight),
		numberValue(video.default_grain) === 0 ? 'grain off' : null,
		compactText(video.crop) ? `crop ${compactText(video.crop)}` : null
	]);
	return { output: output || 'Draft video policy', detail };
}

function audioPolicySummary(
	policy: FolderPolicy | null | undefined,
	sampleItem: FolderSampleItem | null
): string {
	const audio = policyAudio(policy);
	const primary = sampleItem?.audio_summary?.[0] ?? null;
	if (!audio) return 'No draft audio policy';
	const codec = String(primary?.codec_name ?? '').toLowerCase();
	const channels = Number(primary?.channels ?? 0);
	const convertCodecs = Array.isArray(audio.convert_to_opus_codecs)
		? audio.convert_to_opus_codecs.map((value) => String(value).toLowerCase())
		: [];
	const copyCodecs = Array.isArray(audio.copy_codecs)
		? audio.copy_codecs.map((value) => String(value).toLowerCase())
		: [];
	const bitrate =
		channels >= 8
			? compactText(audio.surround_7_1_opus_bitrate)
			: channels >= 6
				? compactText(audio.surround_5_1_opus_bitrate)
				: compactText(audio.stereo_opus_bitrate);
	if (convertCodecs.includes(codec)) return compactParts(['Opus', bitrate]);
	if (copyCodecs.includes(codec)) return `Copy ${codecLabel(codec)}`;
	return bitrate ? `Opus ${formatBitrateCopy(bitrate) ?? bitrate}` : 'Keep selected audio';
}

function subtitlePolicySummary(policy: FolderPolicy | null | undefined): string {
	const subtitle = policySubtitle(policy);
	if (!subtitle) return 'No subtitle policy';
	const languages = Array.isArray(subtitle.keep_languages)
		? subtitle.keep_languages.map((value) => formatLanguageCopy(String(value))).filter(Boolean)
		: [];
	return compactParts([
		languages.length ? `Keep ${languages.join(', ')}` : 'Keep selected subtitles',
		subtitle.prefer_text ? 'prefer text' : null,
		subtitle.keep_forced ? 'forced kept' : null,
		compactText(subtitle.default_mode).replaceAll('_', ' ')
	]);
}

export function buildOutputReviewRows(
	folder: FolderPayload,
	calibration: FolderCalibrationState | null,
	pendingProposal: PendingSampleProposal | null
): OutputReviewRow[] {
	const sampleItem = record<FolderSampleItem>(folder.sample_item);
	const plan = itemPlan(folder);
	const verdict = buildSampleVerdict(folder, calibration, pendingProposal);
	const draftPolicy = activePolicy(folder, pendingProposal);
	const video = videoPolicySummary(draftPolicy, pendingProposal);
	const audioSource = summarizeAudioTrack(sampleItem?.audio_summary?.[0] ?? null);
	const audioPlan = summarizeAudioPlan(plan?.audio);
	const subtitleSource = summarizeSubtitleSource(sampleItem?.subtitle_summary ?? []);
	const subtitlePlan = summarizeSubtitlePlan(
		plan?.subtitles,
		Boolean(draftPolicy?.subtitle?.prefer_text)
	);
	const reviewCount = resolveReviewArtifacts(calibration, pendingProposal).length;
	return [
		{
			label: 'Measured sample',
			source: sampleItem
				? `source ${formatBytes(sampleItem.source_size_bytes)}`
				: 'No sample selected',
			output: verdict?.predictedPerItem ?? 'No measured output yet',
			detail: verdict
				? compactParts([
						verdict.stalePolicy ? 'older settings' : null,
						`target ${verdict.target}`,
						verdict.targetDelta || null,
						`folder ${verdict.predictedFolderTotal}`
					])
				: 'Run a representative sample before approving the folder.',
			tone: verdict?.missesTarget ? 'wait' : verdict ? 'ready' : 'idle'
		},
		{
			label: pendingProposal?.proposal_id ? 'Next sample draft' : 'Video output',
			source: compactParts([
				codecLabel(sampleItem?.video_codec),
				formatResolutionCopy(sampleItem?.width, sampleItem?.height)
			]),
			output: video.output,
			detail: video.detail || 'Uses the current folder video policy.',
			tone: pendingProposal?.proposal_id ? 'active' : 'idle'
		},
		{
			label: 'Audio',
			source: compactParts([audioSource.headline, audioSource.detail]),
			output:
				reviewPackAudioSummary(calibration, pendingProposal) ||
				audioPlan.headline ||
				audioPolicySummary(draftPolicy, sampleItem),
			detail: audioPlan.detail || audioPolicySummary(draftPolicy, sampleItem),
			tone: 'idle'
		},
		{
			label: 'Subtitles',
			source: compactParts([subtitleSource.headline, subtitleSource.detail]),
			output: subtitlePlan.headline || subtitlePolicySummary(draftPolicy),
			detail: subtitlePlan.detail || subtitlePolicySummary(draftPolicy),
			tone: 'idle'
		},
		{
			label: 'Review media',
			source: reviewCount ? `${reviewCount} artifacts ready` : 'No review media yet',
			output: reviewCount ? 'Visible below' : 'Run sample',
			detail: reviewCount
				? 'Use the source/draft contact sheets and compare timelines before approving.'
				: 'A sample run creates visual and audio review evidence.',
			tone: reviewCount ? 'ready' : 'idle'
		}
	];
}

export function buildReviewWorkspaceView(
	folder: FolderPayload,
	calibration: FolderCalibrationState | null,
	pendingProposal: PendingSampleProposal | null,
	workflow: WorkflowState,
	reviewPackReady: boolean
): ReviewWorkspaceView {
	if (workflow.isOutputWorkflow || isOutputWorkflowAction(workflow.primaryAction)) {
		const counts = folder.workflow_state?.counts;
		const readyToValidate = counts?.ready_to_validate ?? 0;
		const readyToPromote = counts?.ready_to_promote ?? 0;
		const encodeCandidates = counts?.encode_candidates ?? 0;
		const processing = counts?.processing ?? 0;
		const complete = counts?.complete ?? 0;
		const itemCount = numberValue(folder.summary?.item_count);
		const outputSteps = buildWorkflowSteps(workflow);
		const stepByLabel = new Map(outputSteps.map((step) => [step.label, step]));
		const encodeStep = stepByLabel.get('Encode');
		const validateStep = stepByLabel.get('Validate');
		const promoteStep = stepByLabel.get('Promote');
		const completeStep = stepByLabel.get('Complete');
		const outputComplete = workflow.label.toLowerCase() === 'complete';
		const validateOutput =
			workflow.primaryAction === 'validate-outputs'
				? workflow.primary
				: readyToValidate
					? `${readyToValidate} ready to validate`
					: outputComplete
						? 'Validation complete'
						: 'Waiting for encoded outputs';
		const promoteOutput =
			workflow.primaryAction === 'promote-outputs'
				? workflow.primary
				: readyToPromote
					? `${readyToPromote} ready to promote`
					: outputComplete
						? 'Promotion complete'
						: 'Waiting for validated outputs';
		return {
			badge:
				workflow.primaryAction === 'promote-outputs' ? 'Promotion review' : 'Validation review',
			badgeTone: workflow.tone,
			title:
				workflow.primaryAction === 'promote-outputs' ? 'Output promotion' : 'Output validation',
			layout: 'pipeline',
			rows: [
				{
					label: 'Encode',
					source: compactParts([
						encodeCandidates ? `${encodeCandidates} not encoded` : null,
						processing ? `${processing} processing` : null,
						itemCount
							? `${itemCount.toLocaleString('en-US')} item${itemCount === 1 ? '' : 's'}`
							: null
					]),
					output:
						workflow.primaryAction === 'queue-encode'
							? workflow.primary
							: workflow.secondaryAction === 'queue-encode'
								? workflow.secondary
								: encodeCandidates
									? `${encodeCandidates} can be queued`
									: 'No encode backlog',
					detail:
						encodeCandidates || processing
							? 'Approved source items that still need an encoded output.'
							: 'Everything in this scope has left the encode stage.',
					tone: encodeStep?.tone ?? 'idle',
					current: encodeStep?.current ?? false
				},
				{
					label: 'Validate',
					source: readyToValidate
						? `${readyToValidate} ready output${readyToValidate === 1 ? '' : 's'}`
						: 'No outputs waiting',
					output: validateOutput,
					detail: 'Inspect encoded outputs and mark the ones that are good enough to publish.',
					tone: validateStep?.tone ?? 'idle',
					current: validateStep?.current ?? false
				},
				{
					label: 'Promote',
					source: readyToPromote
						? `${readyToPromote} validated output${readyToPromote === 1 ? '' : 's'}`
						: 'No outputs waiting',
					output: promoteOutput,
					detail: 'Move validated outputs into the library after review passes.',
					tone: promoteStep?.tone ?? 'idle',
					current: promoteStep?.current ?? false
				},
				{
					label: 'Complete',
					source: complete
						? `${complete} complete item${complete === 1 ? '' : 's'}`
						: 'Not complete yet',
					output: itemCount ? `${complete} of ${itemCount.toLocaleString('en-US')}` : 'Waiting',
					detail: 'Items land here after encode, validation, and promotion are all done.',
					tone: completeStep?.tone ?? 'idle',
					current: completeStep?.current ?? false
				}
			]
		};
	}
	return {
		badge: reviewPackReady ? 'Review media' : 'No review media',
		title: 'Previous sample evidence',
		layout: 'evidence',
		rows: buildOutputReviewRows(folder, calibration, pendingProposal)
	};
}

function isOutputWorkflowAction(action: WorkflowAction): boolean {
	return action === 'validate-outputs' || action === 'promote-outputs';
}

export function resolveReviewArtifacts(
	calibration: FolderCalibrationState | null,
	pendingProposal: PendingSampleProposal | null
): VisibleReviewArtifact[] {
	return normalizeReviewArtifacts(
		pendingProposal?.multimodal_review_pack ?? calibration?.advice?.multimodal_review_pack
	);
}

export function reviewReadyCopy(calibration: FolderCalibrationState | null): string {
	if (calibration?.browser_review_ready || calibration?.review_media_ready) return 'Ready';
	if (calibration?.compare_clips_purged || calibration?.preview_clips_purged) return 'Purged';
	return '—';
}

function workflowToneToShellTone(tone: FolderWorkflowState['tone']): ShellTone {
	if (tone === 'active') return 'active';
	if (tone === 'ready' || tone === 'success') return 'ready';
	if (tone === 'attention') return 'fail';
	return 'idle';
}

function actionFromBackendWorkflow(
	kind: FolderWorkflowState['next_action']['kind']
): WorkflowAction {
	if (kind === 'validate_outputs') return 'validate-outputs';
	if (kind === 'promote_outputs') return 'promote-outputs';
	if (kind === 'monitor_encode') return 'monitor-processing';
	if (kind === 'open_ops') return 'open-ops';
	if (kind === 'review_scope') return 'open-folders';
	if (kind === 'queue_encode') return 'queue-encode';
	return 'open-folders';
}

function resolveBackendWorkflow(
	folder: FolderPayload,
	workflow: FolderWorkflowState
): WorkflowState | null {
	const action = actionFromBackendWorkflow(workflow.next_action.kind);
	if (
		![
			'processing',
			'needs_attention',
			'blocked',
			'mixed',
			'ready_to_validate',
			'ready_to_promote',
			'complete'
		].includes(workflow.state)
	) {
		return null;
	}
	if (workflow.state === 'complete') {
		return {
			tone: 'ready',
			label: workflow.label,
			title: 'This scope is complete',
			copy: workflow.detail,
			primary: folder.series_context ? 'Open whole show' : 'Open Folders',
			primaryAction: folder.series_context ? 'open-series' : 'open-folders',
			secondary: 'Open Ops',
			secondaryAction: 'open-ops',
			isOutputWorkflow: true
		};
	}
	if (workflow.state === 'mixed') {
		const toValidate = workflow.counts?.ready_to_validate ?? 0;
		const toPromote = workflow.counts?.ready_to_promote ?? 0;
		const toEncode = workflow.counts?.encode_candidates ?? 0;

		let primaryAct: WorkflowAction = 'open-folders';
		let primaryLabel = 'Review scope';
		let secondaryAct: WorkflowAction = folder.series_context ? 'open-series' : 'open-ops';
		let secondaryLabel = folder.series_context ? 'Open whole show' : 'Open Ops';

		if (toValidate > 0) {
			primaryAct = 'validate-outputs';
			primaryLabel = `Validate ${toValidate} output${toValidate === 1 ? '' : 's'}`;
			if (toEncode > 0) {
				secondaryAct = 'queue-encode';
				secondaryLabel = `Queue ${toEncode} encode${toEncode === 1 ? '' : 's'}`;
			} else if (toPromote > 0) {
				secondaryAct = 'promote-outputs';
				secondaryLabel = `Promote ${toPromote} output${toPromote === 1 ? '' : 's'}`;
			}
		} else if (toPromote > 0) {
			primaryAct = 'promote-outputs';
			primaryLabel = `Promote ${toPromote} output${toPromote === 1 ? '' : 's'}`;
			if (toEncode > 0) {
				secondaryAct = 'queue-encode';
				secondaryLabel = `Queue ${toEncode} encode${toEncode === 1 ? '' : 's'}`;
			}
		} else if (toEncode > 0) {
			primaryAct = 'queue-encode';
			primaryLabel = `Queue ${toEncode} encode${toEncode === 1 ? '' : 's'}`;
		}

		return {
			tone: 'ready',
			label: 'Mixed work',
			title: 'Multiple tasks pending',
			copy: workflow.detail,
			primary: primaryLabel,
			primaryAction: primaryAct,
			secondary: secondaryLabel,
			secondaryAction: secondaryAct,
			isOutputWorkflow: true
		};
	}
	return {
		tone: workflowToneToShellTone(workflow.tone),
		label: workflow.label,
		title: workflow.next_action.label,
		copy: workflow.detail,
		primary: workflow.next_action.label,
		primaryAction: action,
		secondary: folder.series_context ? 'Open whole show' : 'Open Ops',
		secondaryAction: folder.series_context ? 'open-series' : 'open-ops',
		isOutputWorkflow: true
	};
}

export function resolveWorkflow(
	folder: FolderPayload,
	status: FolderStatusPayload,
	calibration: FolderCalibrationState | null,
	pendingProposal: PendingSampleProposal | null,
	reviewGate: ReviewGate | null,
	calibrationJob: FolderCalibrationJob | null,
	encodeJob: EncodeQueueJob | null,
	reviewPackReady = false,
	approvalReviewReady = Boolean(calibration?.review_media_ready)
): WorkflowState {
	const backendWorkflow = folder.workflow_state ?? status.workflow_state ?? null;
	if (backendWorkflow) {
		const resolvedBackendWorkflow = resolveBackendWorkflow(folder, backendWorkflow);
		if (resolvedBackendWorkflow) return resolvedBackendWorkflow;
	}
	const encodeStatus = String(encodeJob?.status ?? '').toLowerCase();
	if (['failed', 'needs_attention', 'stopped'].includes(encodeStatus)) {
		const label = encodeStatus === 'failed' ? 'Processing failed' : 'Processing stopped';
		const title =
			encodeStatus === 'failed' ? 'Retry the failed folder job' : 'Retry the stopped folder job';
		return {
			tone: 'wait',
			label,
			title,
			copy:
				encodeJob?.error ??
				encodeJob?.attempt_summary ??
				'The folder already has approved settings. Retry processing when it is safe to run the approved work again.',
			primary: 'Retry processing',
			primaryAction: 'retry-encode',
			secondary: 'Open Ops',
			secondaryAction: 'open-ops'
		};
	}
	if (['running', 'queued', 'retry_backoff'].includes(encodeStatus)) {
		return {
			tone: 'active',
			label: 'Processing',
			title: 'Approved folder is processing',
			copy:
				encodeJob?.telemetry_summary ??
				folder.encode_queue_summary ??
				'Folder settings are approved. Monitor progress here or open Ops for deeper worker state.',
			primary: 'Monitor processing',
			primaryAction: 'monitor-processing',
			secondary: 'Download pack',
			secondaryAction: 'download-review-pack'
		};
	}
	if (reviewGate?.status === 'accepted') {
		const candidateCount = numberValue(folder.encode_candidate_count);
		if (candidateCount === 0) {
			return {
				tone: 'ready',
				label: 'Approved',
				title: 'Approved folder has no queueable items',
				copy: 'Approved settings are saved, but every item in this folder is already past the pending encode states. Open the whole show to queue broader work.',
				primary: folder.series_context ? 'Open whole show' : 'Open Folders',
				primaryAction: folder.series_context ? 'open-series' : 'open-folders',
				secondary: 'Download pack',
				secondaryAction: 'download-review-pack'
			};
		}
		return {
			tone: 'ready',
			label: 'Approved',
			title: 'Queue the approved folder',
			copy:
				reviewGate.message ??
				'The sample was accepted. Queue full-folder processing when you are ready.',
			primary: 'Queue encode',
			primaryAction: 'queue-encode',
			secondary: 'Download pack',
			secondaryAction: 'download-review-pack'
		};
	}
	if (
		status.retryable_sample_job ||
		['failed', 'stopped'].includes(String(calibrationJob?.status ?? ''))
	) {
		return {
			tone: 'fail',
			label: 'Retryable',
			title: 'Sample run needs recovery',
			copy: String(
				calibrationJob?.error ??
					calibrationJob?.notes ??
					'The latest representative sample did not complete. Retry when worker state is valid.'
			),
			primary: 'Retry sample',
			primaryAction: 'retry-sample',
			secondary: 'Open Ops',
			secondaryAction: 'open-ops'
		};
	}
	if (['running', 'queued'].includes(String(status.calibration_status ?? '').toLowerCase())) {
		return {
			tone: 'active',
			label: 'Sampling',
			title: 'Representative sample is running',
			copy: 'The sample job is running. Review media is the missing prerequisite; approval appears once the comparison preview is ready.',
			primary: 'Monitor sample',
			primaryAction: 'monitor-sample',
			secondary: 'Stop sample',
			secondaryAction: 'stop-sample'
		};
	}
	const verdict = buildSampleVerdict(folder, calibration, pendingProposal);
	if (verdict?.stalePolicy && !pendingProposal?.proposal_id) {
		return {
			tone: 'wait',
			label: 'New sample needed',
			title: 'Previous sample used older settings',
			copy: `${verdict.predictedPerItem} per episode was measured against older quality targets. Run a fresh sample using the current source-resolution, low-bitrate defaults before approving this folder.`,
			primary: 'Ask for sample',
			primaryAction: 'focus-bench',
			secondary: 'Download old pack',
			secondaryAction: 'download-review-pack'
		};
	}
	if (pendingProposal?.self_check?.status && pendingProposal.self_check.status !== 'passed') {
		return {
			tone: 'wait',
			label: 'Check draft',
			title: 'Proposal needs review before approval',
			copy:
				pendingProposal.self_check.summary ??
				'The proposal self-check returned a warning. Inspect the draft and revise before approving.',
			primary: 'Download review pack',
			primaryAction: 'download-review-pack',
			secondary: 'Revise',
			secondaryAction: 'revise-proposal'
		};
	}
	if (pendingProposal?.proposal_id && pendingProposal.can_queue !== false) {
		const budgetEnforcement = buildBudgetEnforcementView(pendingProposal);
		if (budgetEnforcement?.active || !calibration?.browser_review_ready) {
			return {
				tone: 'ready',
				label: budgetEnforcement?.active ? 'Capped draft ready' : 'Draft ready',
				title: budgetEnforcement?.active
					? `Run a sample with a ${budgetEnforcement.cap} size ceiling`
					: 'Review draft is ready to sample',
				copy:
					budgetEnforcement?.reason ??
					pendingProposal.message ??
					'Review the draft, then queue the representative sample when it looks right.',
				primary: 'Start sample',
				primaryAction: 'start-sample',
				secondary: 'Revise',
				secondaryAction: 'revise-proposal'
			};
		}
	}
	if (
		pendingProposal?.proposal_id &&
		pendingProposal.can_queue === false &&
		(folder.sample_item || verdict)
	) {
		return {
			tone: 'wait',
			label: 'Draft blocked',
			title: 'The draft does not match your request yet',
			copy:
				pendingProposal.message ??
				'The bench draft changed something outside your request. Revise it before starting another sample.',
			primary: 'Revise draft',
			primaryAction: 'revise-proposal',
			secondary: 'Download pack',
			secondaryAction: 'download-review-pack'
		};
	}
	const canReviseTowardSizeTarget = verdict?.missesTarget && verdict.target !== 'No size target';
	if (canReviseTowardSizeTarget && approvalReviewReady) {
		return {
			tone: 'ready',
			label: 'Target missed',
			title: 'Approve this size or revise smaller',
			copy: `${verdict.predictedPerItem} per episode against ${verdict.target}. If the comparison preview looks good, approve this larger result; otherwise revise and sample again.`,
			primary: 'Approve anyway and queue',
			primaryAction: 'approve-size-tradeoff',
			secondary: 'Revise smaller',
			secondaryAction: 'revise-smaller',
			revisionPrompt: reviseSmallerPrompt(verdict)
		};
	}
	if (canReviseTowardSizeTarget) {
		return {
			tone: 'wait',
			label: 'Review pending',
			title: 'Target missed, waiting for review media',
			copy: `${verdict.predictedPerItem} per episode against ${verdict.target}. Wait for the comparison preview before approving this larger result, or revise smaller now and sample again.`,
			primary: 'Wait for review media',
			primaryAction: 'monitor-review',
			secondary: 'Revise smaller',
			secondaryAction: 'revise-smaller',
			revisionPrompt: reviseSmallerPrompt(verdict)
		};
	}
	if (approvalReviewReady) {
		return {
			tone: 'ready',
			label: 'Review ready',
			title: 'Approve this sample or revise it',
			copy:
				verdict === null
					? (pendingProposal?.message ??
						'Evidence is ready. Review the sample clips, then approve the draft or revise it.')
					: `${verdict.predictedPerItem} per episode, ${verdict.predictedFolderTotal} for the folder. Review the clips, then approve and queue if this is acceptable.`,
			primary: 'Approve and queue',
			primaryAction: 'queue-encode',
			secondary: 'Download pack',
			secondaryAction: 'download-review-pack'
		};
	}
	if (calibration?.browser_review_ready || reviewPackReady) {
		return {
			tone: 'wait',
			label: 'Review pending',
			title: 'Review media is stale or incomplete',
			copy: 'The old review pack can still be opened for reference, but approval needs a fresh sample with review media ready.',
			primary: 'Ask for sample',
			primaryAction: 'focus-bench',
			secondary: 'Download old pack',
			secondaryAction: 'download-review-pack'
		};
	}
	if (!folder.sample_item) {
		return {
			tone: 'idle',
			label: 'Not sampled',
			title: 'No representative sample yet',
			copy: 'Ask the review assistant for a sample proposal before approving folder-wide settings. Worker readiness and settings context stay visible while the sample is queued.',
			primary: 'Ask for draft',
			primaryAction: 'focus-bench',
			secondary: 'Open Ops',
			secondaryAction: 'open-ops'
		};
	}
	return {
		tone: 'wait',
		label: 'Waiting',
		title: 'Folder is waiting for review evidence',
		copy: 'A representative item exists, but the current review state is incomplete. Refresh status or rerun the sample if the evidence is stale.',
		primary: 'Refresh draft',
		primaryAction: 'focus-bench',
		secondary: 'Open Ops',
		secondaryAction: 'open-ops'
	};
}

export function buildWorkflowSteps(workflow: WorkflowState): WorkflowStep[] {
	const activeAction = workflow.primaryAction;
	const activeLabel = workflow.label.toLowerCase();
	const outputWorkflow =
		workflow.isOutputWorkflow ||
		isOutputWorkflowAction(activeAction) ||
		['mixed work', 'ready to validate', 'ready to promote', 'complete'].includes(activeLabel);
	if (outputWorkflow) {
		const encodeCurrent =
			['monitor-processing', 'retry-encode'].includes(activeAction) ||
			['processing', 'processing failed', 'processing stopped'].includes(activeLabel);
		const validateCurrent =
			activeAction === 'validate-outputs' || activeLabel === 'ready to validate';
		const promoteCurrent = activeAction === 'promote-outputs' || activeLabel === 'ready to promote';
		const completeCurrent = activeLabel === 'complete';
		return [
			{
				label: 'Encode',
				detail: encodeCurrent ? workflow.title : 'Process approved folder items',
				tone: encodeCurrent
					? workflow.tone
					: validateCurrent || promoteCurrent || completeCurrent
						? 'ready'
						: 'idle',
				current: encodeCurrent
			},
			{
				label: 'Validate',
				detail: validateCurrent ? workflow.title : 'Review and validate output quality',
				tone: validateCurrent
					? workflow.tone
					: promoteCurrent || completeCurrent
						? 'ready'
						: 'idle',
				current: validateCurrent
			},
			{
				label: 'Promote',
				detail: promoteCurrent ? workflow.title : 'Publish validated files',
				tone: promoteCurrent ? workflow.tone : completeCurrent ? 'ready' : 'idle',
				current: promoteCurrent
			},
			{
				label: 'Complete',
				detail: completeCurrent ? workflow.title : 'All items fully processed',
				tone: completeCurrent ? workflow.tone : 'idle',
				current: completeCurrent
			}
		];
	}
	const sampleCurrent =
		['focus-bench', 'monitor-sample', 'start-sample', 'retry-sample', 'stop-sample'].includes(
			activeAction
		) || ['not sampled', 'sampling', 'retryable', 'draft ready'].includes(activeLabel);
	const reviewCurrent =
		['download-review-pack', 'monitor-review', 'revise-proposal'].includes(activeAction) ||
		['review ready', 'check draft'].includes(activeLabel);
	const approveCurrent =
		['queue-encode', 'approve-size-tradeoff'].includes(activeAction) || activeLabel === 'approved';
	const encodeCurrent =
		[
			'monitor-processing',
			'open-ops',
			'retry-encode',
			'validate-outputs',
			'promote-outputs'
		].includes(activeAction) ||
		['processing', 'processing failed', 'processing stopped'].includes(activeLabel);
	return [
		{
			label: 'Sample',
			detail: sampleCurrent ? workflow.title : 'Choose representative evidence',
			tone: sampleCurrent ? workflow.tone : 'idle',
			current: sampleCurrent
		},
		{
			label: 'Review',
			detail: reviewCurrent ? workflow.title : 'Compare the sample pack',
			tone: reviewCurrent ? workflow.tone : 'idle',
			current: reviewCurrent
		},
		{
			label: 'Approve',
			detail: approveCurrent ? workflow.title : 'Accept or revise settings',
			tone: approveCurrent ? workflow.tone : 'idle',
			current: approveCurrent
		},
		{
			label: 'Process',
			detail: encodeCurrent ? workflow.title : 'Run approved folder work',
			tone: encodeCurrent ? workflow.tone : 'idle',
			current: encodeCurrent
		}
	];
}

export function buildProposalRows(
	folder: FolderPayload,
	pendingProposal: PendingSampleProposal | null
): ProposalRow[] {
	const current = flattenPolicy(
		(pendingProposal?.current_policy ??
			folder.policy ??
			folder.summary?.resolved_policy) as FolderPolicy
	);
	const draft = flattenPolicy(
		(pendingProposal?.preview_policy ??
			pendingProposal?.applied_policy ??
			pendingProposal?.current_policy) as FolderPolicy | null | undefined
	);
	const paths = Array.from(new Set([...Object.keys(current), ...Object.keys(draft)]));
	return paths
		.filter((path) => current[path] !== draft[path])
		.slice(0, 12)
		.map((path) => ({
			section: workbenchSection(path),
			label: policyRowLabel(path),
			current: formatPolicyValue(path, current[path], folder.metric_support),
			draft: formatPolicyValue(path, draft[path], folder.metric_support),
			changed: current[path] !== draft[path]
		}));
}

export function buildStatusTiles(
	folder: FolderPayload,
	status: FolderStatusPayload,
	hosts: HostsPayload,
	workflow: WorkflowState
): StatusTile[] {
	const readyHosts = hosts.hosts.filter((host) => host.available).length;
	const totalHosts = hosts.hosts.length;
	const encodeQueue = folder.encode_queue;
	return [
		{
			label: 'Folder state',
			value: workflow.label,
			detail: folder.prefix,
			tone: workflow.tone
		},
		{
			label: 'Sample',
			value: status.calibration_status || 'Unknown',
			detail: status.polling_active ? 'polling active' : 'polling idle',
			tone:
				status.calibration_status === 'failed' ? 'fail' : status.polling_active ? 'active' : 'idle'
		},
		{
			label: 'Processing',
			value: encodeQueue
				? `${encodeQueue.running_count} running · ${encodeQueue.queued_count} queued`
				: '—',
			detail: encodeQueue?.telemetry?.eta_copy ?? folder.encode_queue_summary ?? 'No queue summary',
			tone: encodeQueue?.state.stop_requested
				? 'fail'
				: encodeQueue?.state.is_paused
					? 'wait'
					: encodeQueue && encodeQueue.running_count > 0
						? 'active'
						: 'idle'
		},
		{
			label: 'Workers',
			value: `${readyHosts} ready / ${totalHosts}`,
			detail: totalHosts ? 'capacity check complete' : 'worker status unavailable',
			tone: readyHosts > 0 ? 'ready' : totalHosts > 0 ? 'wait' : 'idle'
		}
	];
}

export function buildFooterSignals(
	folder: FolderPayload,
	status: FolderStatusPayload,
	hosts: HostsPayload
): FooterSignal[] {
	return [
		{ label: 'Review', value: status.calibration_status || 'unknown', tone: 'active' },
		{ label: 'Metric', value: resolvedMetricCopy(folder), tone: 'ready' },
		{
			label: 'Workers',
			value: `${hosts.hosts.filter((host) => host.available).length}/${hosts.hosts.length}`
		},
		{ label: 'API', value: status.polling_active ? 'polling' : 'idle' }
	];
}
