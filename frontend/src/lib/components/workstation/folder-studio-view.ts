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

export type WorkflowAction =
	| 'download-review-pack'
	| 'focus-bench'
	| 'open-ops'
	| 'queue-encode'
	| 'retry-encode'
	| 'retry-sample'
	| 'revise-proposal'
	| 'start-sample'
	| 'stop-sample'
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
		folder.summary?.resolved_policy ??
		folder.policy ??
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
			return {
				key,
				label: compactText(host.label) || key || 'Host',
				detail: compactText(host.detail) || compactText(host.message),
				available: host.available !== false
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
		pendingProposal,
		calibrationJob,
		pendingAction
	}: {
		reviewPackReady: boolean;
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
	if (action === 'revise-proposal') return { disabled: false, title: '' };
	if (action === 'open-ops') return { disabled: false, title: '' };
	if (action === 'stop-sample') {
		if (activeCalibrationStatus(calibrationJob?.status)) return { disabled: false, title: '' };
		return { disabled: true, title: 'No sample job is running.' };
	}
	if (action === 'queue-encode') {
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
	pendingProposal: PendingSampleProposal | null
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
			value: 'Use the decision buttons',
			detail: 'Draft, sample, review, or approve from here'
		}
	];
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

export function resolveWorkflow(
	folder: FolderPayload,
	status: FolderStatusPayload,
	calibration: FolderCalibrationState | null,
	pendingProposal: PendingSampleProposal | null,
	reviewGate: ReviewGate | null,
	calibrationJob: FolderCalibrationJob | null,
	encodeJob: EncodeQueueJob | null
): WorkflowState {
	const encodeStatus = String(encodeJob?.status ?? '').toLowerCase();
	if (['failed', 'needs_attention', 'stopped'].includes(encodeStatus)) {
		return {
			tone: 'wait',
			label: 'Retry available',
			title: 'Processing needs recovery',
			copy:
				encodeJob?.error ??
				encodeJob?.attempt_summary ??
				'The approved folder needs review before it runs again. Retry from Ops when the folder is still safe to process.',
			primary: 'Open Ops',
			primaryAction: 'open-ops',
			secondary: 'Retry',
			secondaryAction: 'retry-encode'
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
			primary: 'Download review pack',
			primaryAction: 'download-review-pack',
			secondary: 'Open Ops',
			secondaryAction: 'open-ops'
		};
	}
	if (reviewGate?.status === 'accepted') {
		return {
			tone: 'ready',
			label: 'Approved',
			title: 'Folder proposal is approved',
			copy:
				reviewGate.message ??
				'The proposal has been accepted. Queue or monitor full-folder work from this context.',
			primary: 'Download review pack',
			primaryAction: 'download-review-pack',
			secondary: 'Queue encode',
			secondaryAction: 'queue-encode'
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
			copy: 'The operator is waiting for review evidence. Keep worker, elapsed time, and queue state visible until the pack is ready.',
			primary: 'Open Ops',
			primaryAction: 'open-ops',
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
	if (verdict?.missesTarget) {
		const budgetEnforcement = buildBudgetEnforcementView(pendingProposal);
		if (
			pendingProposal?.proposal_id &&
			pendingProposal.can_queue !== false &&
			budgetEnforcement?.active
		) {
			return {
				tone: 'wait',
				label: 'Target missed',
				title: `${verdict.predictedPerItem} sample missed; run capped sample next`,
				copy: `${verdict.predictedPerItem} per episode against ${verdict.target}. Next sample will use a ${budgetEnforcement.cap} size ceiling. ${budgetEnforcement.reason}`,
				primary: 'Start sample',
				primaryAction: 'start-sample',
				secondary: 'Revise',
				secondaryAction: 'revise-proposal'
			};
		}
		return {
			tone: 'wait',
			label: 'Target missed',
			title: 'Sample is too large for the requested target',
			copy: `${verdict.predictedPerItem} per episode against ${verdict.target}. ${verdict.recommendation}`,
			primary: 'Revise sample',
			primaryAction: 'focus-bench',
			secondary: 'Download review pack',
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
	if (calibration?.browser_review_ready || calibration?.review_media_ready) {
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
	if (pendingProposal?.proposal_id && pendingProposal.can_queue !== false) {
		const budgetEnforcement = buildBudgetEnforcementView(pendingProposal);
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
	const sampleCurrent =
		['focus-bench', 'start-sample', 'retry-sample', 'stop-sample'].includes(activeAction) ||
		['not sampled', 'sampling', 'retryable', 'draft ready'].includes(activeLabel);
	const reviewCurrent =
		['download-review-pack', 'revise-proposal'].includes(activeAction) ||
		['review ready', 'check draft'].includes(activeLabel);
	const approveCurrent = ['queue-encode'].includes(activeAction) || activeLabel === 'approved';
	const encodeCurrent =
		['open-ops', 'retry-encode'].includes(activeAction) ||
		['processing', 'retry available'].includes(activeLabel);
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
