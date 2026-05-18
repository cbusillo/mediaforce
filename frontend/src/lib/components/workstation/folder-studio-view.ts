import {
	codecLabel,
	flattenPolicy,
	formatPolicyValue,
	formatResolutionCopy,
	normalizeReviewArtifacts,
	pathFilename,
	policyRowLabel,
	workbenchSection,
	type FolderCalibrationJob,
	type FolderCalibrationState,
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

export function record<T extends Record<string, unknown>>(value: unknown): T | null {
	return value && typeof value === 'object' ? (value as T) : null;
}

function compactText(value: unknown): string {
	return typeof value === 'string' ? value.trim() : '';
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
		blocker = 'Describe the Bench request before sending.';
	} else if (!selectedHostKey) {
		blocker = 'Choose an available host before sending.';
	} else if (!selectedHost) {
		blocker = 'Selected host is no longer available for this folder.';
	} else if (!selectedHost.available) {
		blocker = 'Selected host is not available right now.';
	} else if (activeCalibrationJob) {
		blocker = 'A sample job is already active for this folder.';
	}
	return {
		disabled: pending || Boolean(blocker),
		blocker: pending ? 'Sending request to Bench.' : blocker,
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
	if (action === 'open-ops') return { disabled: false, title: '' };
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
			return { disabled: true, title: 'Ask Bench for a draft before starting the sample.' };
		}
		if (pendingProposal.can_queue === false) {
			return {
				disabled: true,
				title: pendingProposal.message || 'The current bench draft is not ready to queue.'
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
			body: 'Use the composer to ask Bench for a representative sample, a revision, or a validation pass.',
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
		label: 'Bench',
		title:
			pendingProposal?.request_disposition ??
			calibration?.advice?.request_disposition ??
			'Response',
		body:
			responseBody ||
			'Waiting for a request. Bench responses will appear here with the resulting proposal context.',
		meta: pendingProposal?.confidence ?? calibration?.advice?.confidence ?? undefined,
		tone: responseBody ? 'active' : 'neutral'
	});

	const diagnosis =
		compactText(pendingProposal?.diagnosis) || compactText(calibration?.advice?.diagnosis);
	pushMessage(messages, {
		id: 'bench-diagnosis',
		role: 'bench',
		label: 'Bench',
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
		label: 'Bench',
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
	return `${(value / 1024 ** 3).toLocaleString('en-US', {
		maximumFractionDigits: value >= 1024 ** 3 ? 1 : 2
	})} GiB`;
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
	const sampleResult = record<Record<string, unknown>>(folder.calibration)?.sample_result;
	const predictedSize = Number(
		record<Record<string, unknown>>(sampleResult)?.predicted_total_size_bytes
	);
	const sourceSize = Number(folder.summary?.total_size_bytes);
	if (!Number.isFinite(sourceSize) || sourceSize <= 0) return null;
	if (!Number.isFinite(predictedSize) || predictedSize <= 0) return null;
	return Math.max(sourceSize - predictedSize, 0);
}

export function buildSampleFacts(
	sampleItem: FolderSampleItem | null,
	summary: FolderPayload['summary']
): Array<{ label: string; value: string }> {
	return [
		{ label: 'File', value: sampleItem ? pathFilename(sampleItem.rel_path) : '—' },
		{
			label: 'Resolution',
			value: formatResolutionCopy(sampleItem?.width, sampleItem?.height) ?? '—'
		},
		{ label: 'Codec', value: codecLabel(sampleItem?.video_codec) },
		{
			label: 'Size',
			value: formatBytes(sampleItem?.source_size_bytes ?? summary?.total_size_bytes)
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
			title: 'Encode needs recovery',
			copy:
				encodeJob?.error ??
				encodeJob?.attempt_summary ??
				'The approved encode needs review before it runs again. Retry from Ops when the folder is still safe to process.',
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
			title: 'Approved encode is in the queue',
			copy:
				encodeJob?.telemetry_summary ??
				folder.encode_queue_summary ??
				'Folder policy is approved. Monitor progress here or open Ops for deeper fleet state.',
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
					'The latest representative sample did not complete. Retry when the host state is valid.'
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
			copy: 'The operator is waiting for review evidence. Keep host, elapsed time, and queue state visible until the pack is ready.',
			primary: 'Open Ops',
			primaryAction: 'open-ops',
			secondary: 'Stop sample',
			secondaryAction: 'stop-sample'
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
		return {
			tone: 'ready',
			label: 'Draft ready',
			title: 'Bench draft is ready to sample',
			copy:
				pendingProposal.message ??
				'Review the Bench draft, then queue the representative sample when it looks right.',
			primary: 'Start sample',
			primaryAction: 'start-sample',
			secondary: 'Revise',
			secondaryAction: 'revise-proposal'
		};
	}
	if (pendingProposal || calibration?.browser_review_ready || calibration?.review_media_ready) {
		return {
			tone: 'ready',
			label: 'Review ready',
			title: 'Review the sample pack, then approve or revise',
			copy:
				pendingProposal?.message ??
				'Evidence is ready. Download the review pack, inspect the sample clips, then narrow the decision to approve or revise.',
			primary: 'Download review pack',
			primaryAction: 'download-review-pack',
			secondary: 'Revise',
			secondaryAction: 'revise-proposal'
		};
	}
	if (!folder.sample_item) {
		return {
			tone: 'idle',
			label: 'Not sampled',
			title: 'No representative sample yet',
			copy: 'Ask Bench for a sample draft before approving folder-wide settings. Host readiness and policy context stay visible while the sample is queued.',
			primary: 'Ask Bench for draft',
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
		primary: 'Ask Bench to refresh',
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
			detail: approveCurrent ? workflow.title : 'Accept or revise policy',
			tone: approveCurrent ? workflow.tone : 'idle',
			current: approveCurrent
		},
		{
			label: 'Encode',
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
			label: 'Encode queue',
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
			label: 'Hosts',
			value: `${readyHosts} ready / ${totalHosts}`,
			detail: totalHosts ? 'capacity check complete' : 'host status unavailable',
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
			label: 'Hosts',
			value: `${hosts.hosts.filter((host) => host.available).length}/${hosts.hosts.length}`
		},
		{ label: 'API', value: status.polling_active ? 'polling' : 'idle' }
	];
}
