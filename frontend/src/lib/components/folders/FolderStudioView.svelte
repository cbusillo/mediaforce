<script lang="ts">
	import { browser } from '$app/environment';
	import { resolve } from '$app/paths';
	import { tick } from 'svelte';
	import type {
		EncodeJobProgressTelemetry,
		EncodeQueueJob,
		EncodeQueueSummary,
		FolderPayload,
		FolderStatusPayload,
		HostsPayload
	} from '$lib/api/types';
	import { invalidate, invalidateAll } from '$app/navigation';
	import { postJson } from '$lib/api/client';
	import Button from '$lib/components/Button.svelte';
	import HostCard from '$lib/components/HostCard.svelte';
	import Panel from '$lib/components/Panel.svelte';
	import Pill from '$lib/components/Pill.svelte';
	import SectionHead from '$lib/components/SectionHead.svelte';
	import {
		codecLabel,
		compactCopy,
		compactScheduleCopy,
		compareValues,
		comparisonValue,
		approvalReviewSignature,
		buildCalibrationThreadScrollSignature,
		describeHighImpactApprovalGate,
		encodeQueueSummaryCopy,
		encodeStatusTone,
		flattenPolicy,
		formatBitrateCopy,
		formatCodecCountsCopy,
		formatDateTimeCopy,
		formatPercentCopy,
		formatPolicyValue,
		formatResolutionCopy,
		formatStatusCountCopy,
		hostCapacityCopy,
		inferResolutionFromPath,
		normalizeReviewArtifacts,
		pathExtension,
		pathStem,
		policyRowLabel,
		queueSummaryCopy,
		resolveBenchDraftNote,
		softWrapTokens,
		summarizeAudioPlan,
		summarizeAudioTrack,
		summarizeMetricPlan,
		summarizeMetricPolicy,
		summarizeSubtitlePlan,
		summarizeSubtitleSource,
		workbenchSection,
		type ApprovedSeasonShortcut,
		type BreadcrumbItem,
		type CalibrationThreadSession,
		type ComparisonRow,
		type FolderAdviceState,
		type FolderCalibrationJob,
		type FolderCalibrationQueue,
		type FolderCalibrationState,
		type FolderItemPlan,
		type FolderMultimodalReviewPack,
		type FolderOperatorRequest,
		type FolderPolicy,
		type FolderReviewPair,
		type FolderRunVerdict,
		type FolderSampleItem,
		type PendingSampleProposal,
		type PolicyWorkbenchRow,
		type PolicyWorkbenchSection,
		type ProposalSelfCheck,
		type ProposalTrace,
		type ReviewGate,
		type SampleAudioTrack,
		type SampleHostCard,
		type SampleHostOption,
		type SampleSubtitleTrack,
		type SnapshotItem,
		type SteadyComparisonRow,
		type TuningSessionSummary,
		type WorkbenchStat
	} from '$lib/folders/studio';
	import { folderLibraryLabel } from '$lib/folder-display';
	import { formatGiB, formatTimestamp, titleCase } from '$lib/format';
	import { folderAwareQualitySearchSummary } from '$lib/hosts/runtime';
	import { toasts } from '$lib/stores/toasts';

	let autoRefreshTimer: number | null = null;
	let autoRefreshInFlight = false;
	let autoRefreshPauseUntil = 0;

	function pauseAutoRefreshFor(durationMs: number) {
		autoRefreshPauseUntil = Math.max(autoRefreshPauseUntil, Date.now() + durationMs);
	}

	function focusedElementNeedsRefreshGrace() {
		if (!browser) return false;
		const activeElement = document.activeElement;
		if (!(activeElement instanceof HTMLElement)) return false;
		return Boolean(
			activeElement.closest(
				'input, textarea, select, [contenteditable="true"], [contenteditable=""], [role="textbox"]'
			)
		);
	}

	function shouldPauseAutoRefresh() {
		return (
			actionState !== null ||
			note.trim().length > 0 ||
			focusedElementNeedsRefreshGrace() ||
			Date.now() < autoRefreshPauseUntil
		);
	}

	async function refreshFolderView() {
		if (autoRefreshInFlight) return;
		autoRefreshInFlight = true;
		try {
			await Promise.all([
				invalidate(`/api/folders/${apiPrefix}`),
				invalidate(`/api/folders/${apiPrefix}/status`),
				invalidate('/api/hosts?compact=1')
			]);
		} finally {
			autoRefreshInFlight = false;
		}
	}

	let {
		data
	}: {
		data: {
			folder: FolderPayload;
			status: FolderStatusPayload;
			hosts: HostsPayload;
		};
	} = $props();

	const folder = $derived(data.folder);
	const status = $derived(data.status);
	const hosts = $derived(data.hosts);
	const rankedHosts = $derived.by(() =>
		[...hosts.hosts].sort(
			(left, right) =>
				Number(right.priority) - Number(left.priority) || left.label.localeCompare(right.label)
		)
	);
	const hostRuntimeByKey = $derived.by(
		() => new Map(rankedHosts.map((host) => [host.key, host] as const))
	);
	const calibrationJob = $derived((status.calibration_job as FolderCalibrationJob | null) ?? null);
	const retryableSampleJob = $derived(
		(status.retryable_sample_job as FolderCalibrationJob | null) ?? null
	);
	const failedCalibrationPopupKey = $derived.by(() => {
		if (
			!calibrationJob ||
			!['failed', 'stopped'].includes(String(status.calibration_status ?? '').trim())
		) {
			return null;
		}
		const identity =
			String(calibrationJob.job_id ?? '').trim() ||
			String(calibrationJob.finished_at ?? calibrationJob.created_at ?? '').trim() ||
			String(calibrationJob.error ?? '').trim();
		if (!identity) return null;
		return `${folder.prefix}:${identity}`;
	});
	const calibrationFailureDetail = $derived.by(() => {
		const rawError = String(calibrationJob?.error ?? '').trim();
		if (!rawError) {
			return calibrationJob?.host?.label
				? `Host: ${String(calibrationJob.host.label)}`
				: 'Open the latest calibration job details to inspect the failure.';
		}
		const summaryLine =
			rawError
				.split('\n')
				.map((line) => line.trim())
				.find((line) => line.length > 0 && !line.startsWith('export ')) ?? rawError;
		const hostLabel = String(calibrationJob?.host?.label ?? '').trim();
		return hostLabel ? `Host: ${hostLabel} · ${summaryLine}` : summaryLine;
	});
	const sampleItem = $derived((folder.sample_item as FolderSampleItem | undefined) ?? {});
	const itemPlan = $derived((folder.item_plan as FolderItemPlan | undefined) ?? {});
	const policy = $derived((folder.policy as FolderPolicy | undefined) ?? {});
	const baselinePolicy = $derived((sampleItem.resolved_policy as FolderPolicy | undefined) ?? {});
	const calibrationQueue = $derived(
		(folder.calibration_queue as FolderCalibrationQueue | undefined) ?? {}
	);
	const sampleHostOptions = $derived(
		(folder.sample_host_options as SampleHostOption[] | undefined) ?? []
	);
	const calibration = $derived((folder.calibration as FolderCalibrationState | undefined) ?? {});
	const currentCalibrationDraftHash = $derived(String(calibration.draft_hash ?? '').trim());
	const folderAdvice = $derived((folder.advice as FolderAdviceState | undefined) ?? {});
	const storedPendingProposal = $derived(
		(folder.pending_proposal as PendingSampleProposal | undefined | null) ?? null
	);
	const calibrationAdvice = $derived(
		(calibration.advice as FolderAdviceState | undefined) ?? folderAdvice
	);
	const approvedSeasonShortcut = $derived(
		(folder.approved_season_shortcut as ApprovedSeasonShortcut | undefined | null) ?? null
	);
	const recentTuningSessions = $derived(
		(folder.recent_tuning_sessions as TuningSessionSummary[] | undefined) ?? []
	);
	const runVerdict = $derived(
		(calibrationAdvice.run_verdict as FolderRunVerdict | undefined) ?? {}
	);
	const operatorRequest = $derived(
		(calibrationAdvice.operator_request as FolderOperatorRequest | undefined) ?? null
	);
	let note = $state('');
	let selectedHost = $state('');
	let actionState = $state<string | null>(null);
	let highImpactApprovalArmToken = $state('');
	let highImpactReviewedDraftHash = $state('');
	let highImpactApprovalLocked = $state(false);
	let highImpactApprovalLockTimer = $state<ReturnType<typeof setTimeout> | null>(null);
	let previewDraftEcho = $state<PendingSampleProposal | null>(null);
	let threadScrollViewport = $state<HTMLDivElement | null>(null);
	let lastAutoScrolledThreadSignature = $state('');
	let previewSubmission = $state<{
		note: string;
		hostKey: string;
		hostLabel: string;
	} | null>(null);
	const pendingProposal = $derived.by(() => previewDraftEcho ?? storedPendingProposal);
	$effect(() => {
		if (!previewDraftEcho?.proposal_id) return;
		if (storedPendingProposal?.proposal_id !== previewDraftEcho.proposal_id) return;
		previewDraftEcho = null;
	});
	const pendingOperatorRequest = $derived(
		(pendingProposal?.operator_request as FolderOperatorRequest | undefined) ?? null
	);
	const hasCalibration = $derived(Boolean(folder.calibration));
	const reviewGate = $derived((folder.review_gate as ReviewGate | undefined) ?? {});
	const reviewGateStatus = $derived(String(reviewGate.status ?? 'missing_sample'));
	const folderStatusCounts = $derived(
		(folder.summary?.statuses as Record<string, number> | undefined) ?? {}
	);
	const encodedOutputCount = $derived(Number(folderStatusCounts.encoded ?? 0));
	const validatedOutputCount = $derived(Number(folderStatusCounts.validated ?? 0));
	const promotedOutputCount = $derived(Number(folderStatusCounts.promoted ?? 0));
	const stagedOutputCount = $derived(encodedOutputCount + validatedOutputCount);
	const highImpactPolicySignature = $derived.by(() =>
		approvalReviewSignature(policyComparisonRows)
	);
	const highImpactApprovalGate = $derived.by(() =>
		describeHighImpactApprovalGate({
			reviewGateStatus,
			highImpactPolicyCount: highImpactPolicyRows.length,
			armed:
				highImpactPolicySignature.length > 0 &&
				highImpactApprovalArmToken === highImpactPolicySignature
		})
	);
	const approvalButtonDisabled = $derived.by(() => {
		if (!hasCalibration) return true;
		if (highImpactApprovalLocked) return true;
		if (highImpactApprovalGate.requiresConfirmation && highImpactApprovalGate.armed) {
			return !highImpactReviewedDraftHash;
		}
		return false;
	});
	const approvalButtonLabel = $derived(highImpactApprovalGate.buttonLabel);
	const reviewGateNextActionLabel = $derived(String(reviewGate.next_action_label ?? '').trim());
	const queueGateLabel = $derived.by(() => {
		if (reviewGateStatus === 'needs_approval') return 'Needs approval';
		if (reviewGateStatus === 'missing_review_media') return 'Review clips missing';
		if (reviewGateStatus === 'needs_fresh_sample') return 'Needs fresh sample';
		return 'Needs review';
	});
	const reviewMediaStatusCopy = $derived.by(() => {
		if (calibration.browser_review_ready) {
			return 'Synced source and draft clips are ready for approval.';
		}
		if (calibration.review_media_ready) {
			return 'Review clips are retained, but this older draft needs a fresh sample to use the in-browser A/B player.';
		}
		if (calibration.preview_clips_purged || calibration.compare_clips_purged) {
			return 'This draft no longer has review clips available. Run a fresh sample before approving it.';
		}
		return 'Run a fresh sample to generate review clips for approval.';
	});
	const operatorRequestLabel = $derived.by(() => {
		if (operatorRequest?.budget_label) return String(operatorRequest.budget_label).trim();
		if (!operatorRequest?.metric || operatorRequest.target == null) return '';
		const metric = String(operatorRequest.metric).trim().toUpperCase();
		const target = Number(operatorRequest.target);
		if (!Number.isFinite(target)) return '';
		return `${metric} ${target.toLocaleString('en-US', { maximumFractionDigits: 2 })}`;
	});
	const pendingOperatorRequestLabel = $derived.by(() => {
		if (pendingOperatorRequest?.budget_label)
			return String(pendingOperatorRequest.budget_label).trim();
		if (!pendingOperatorRequest?.metric || pendingOperatorRequest.target == null) return '';
		const metric = String(pendingOperatorRequest.metric).trim().toUpperCase();
		const target = Number(pendingOperatorRequest.target);
		if (!Number.isFinite(target)) return '';
		return `${metric} ${target.toLocaleString('en-US', { maximumFractionDigits: 2 })}`;
	});
	const calibrationThreadSessions = $derived.by(() => {
		const sessions: CalibrationThreadSession[] = recentTuningSessions
			.slice()
			.reverse()
			.map((session, index) => ({
				key:
					String(session.session_id ?? '').trim() ||
					`session-${index}-${String(session.created_at ?? '').trim()}`,
				note: String(session.note ?? '').trim(),
				requestResponse: String(session.request_response ?? '').trim() || null,
				requestDisposition: String(session.request_disposition ?? '').trim() || null,
				summary: String(session.summary ?? '').trim(),
				diagnosis: String(session.diagnosis ?? '').trim() || null,
				feasibilityNote: String(session.feasibility_note ?? '').trim() || null,
				confidence: String(session.confidence ?? '').trim() || null,
				suggestedFollowUp: String(session.suggested_follow_up ?? '').trim() || null,
				runSummary: null,
				runNextStep: null,
				runOutcome: '',
				runConfidence: null,
				isCurrent: false
			}))
			.filter((session) => session.note || session.requestResponse || session.summary);

		const currentSession: CalibrationThreadSession = {
			key: 'current-thread',
			note: String(calibrationAdvice.operator_note ?? '').trim(),
			requestResponse: String(calibrationAdvice.request_response ?? '').trim() || null,
			requestDisposition: String(calibrationAdvice.request_disposition ?? '').trim() || null,
			summary: String(calibrationAdvice.summary ?? '').trim(),
			diagnosis: String(calibrationAdvice.diagnosis ?? '').trim() || null,
			feasibilityNote: String(calibrationAdvice.feasibility_note ?? '').trim() || null,
			confidence: String(calibrationAdvice.confidence ?? '').trim() || null,
			suggestedFollowUp: String(calibrationAdvice.suggested_follow_up ?? '').trim() || null,
			runSummary: String(runVerdict.summary ?? '').trim() || null,
			runNextStep: String(runVerdict.next_step ?? '').trim() || null,
			runOutcome: String(runVerdict.outcome ?? '').trim(),
			runConfidence: String(runVerdict.confidence ?? '').trim() || null,
			isCurrent: true
		};

		if (
			currentSession.note ||
			currentSession.requestResponse ||
			currentSession.summary ||
			currentSession.runSummary ||
			currentSession.suggestedFollowUp
		) {
			const previous = sessions.at(-1);
			if (
				previous &&
				previous.note === currentSession.note &&
				previous.summary === currentSession.summary
			) {
				previous.diagnosis = currentSession.diagnosis || previous.diagnosis;
				previous.requestResponse = currentSession.requestResponse || previous.requestResponse;
				previous.requestDisposition =
					currentSession.requestDisposition || previous.requestDisposition;
				previous.feasibilityNote = currentSession.feasibilityNote || previous.feasibilityNote;
				previous.confidence = currentSession.confidence || previous.confidence;
				previous.suggestedFollowUp = currentSession.suggestedFollowUp || previous.suggestedFollowUp;
				previous.runSummary = currentSession.runSummary;
				previous.runNextStep = currentSession.runNextStep;
				previous.runOutcome = currentSession.runOutcome;
				previous.runConfidence = currentSession.runConfidence;
				previous.isCurrent = true;
			} else {
				sessions.push(currentSession);
			}
		}

		const proposalSession: CalibrationThreadSession | null = previewSubmission
			? {
					key: `preview-submission:${previewSubmission.hostKey}:${previewSubmission.note}`,
					note: previewSubmission.note,
					requestResponse: 'Drafting bench reply...',
					requestDisposition: null,
					summary: 'Drafting bench reply...',
					diagnosis: previewSubmission.hostLabel
						? `The bench has your latest note for ${previewSubmission.hostLabel} and is preparing a new draft now. The proposal card below will refresh when the reply lands.`
						: 'The bench has your latest note and is preparing a new draft now. The proposal card below will refresh when the reply lands.',
					feasibilityNote: null,
					confidence: null,
					suggestedFollowUp: null,
					runSummary: null,
					runNextStep: null,
					runOutcome: '',
					runConfidence: null,
					isCurrent: true
				}
			: pendingProposal &&
				  (String(pendingProposal.operator_note ?? '').trim() ||
						String(pendingProposal.summary ?? '').trim() ||
						String(pendingProposal.diagnosis ?? '').trim())
				? {
						key: `proposal-${String(pendingProposal.proposal_id ?? 'pending')}`,
						note: String(pendingProposal.operator_note ?? '').trim(),
						requestResponse: String(pendingProposal.request_response ?? '').trim() || null,
						requestDisposition: String(pendingProposal.request_disposition ?? '').trim() || null,
						summary: String(pendingProposal.summary ?? '').trim(),
						diagnosis:
							String(pendingProposal.diagnosis ?? '').trim() ||
							String(pendingProposal.message ?? '').trim() ||
							null,
						feasibilityNote: String(pendingProposal.feasibility_note ?? '').trim() || null,
						confidence: String(pendingProposal.confidence ?? '').trim() || null,
						suggestedFollowUp: String(pendingProposal.suggested_follow_up ?? '').trim() || null,
						runSummary: null,
						runNextStep: null,
						runOutcome: '',
						runConfidence: null,
						isCurrent: true
					}
				: null;

		if (proposalSession) {
			const previous = sessions.at(-1);
			if (
				previous &&
				previous.note === proposalSession.note &&
				previous.summary === proposalSession.summary
			) {
				previous.diagnosis = proposalSession.diagnosis || previous.diagnosis;
				previous.requestResponse = proposalSession.requestResponse || previous.requestResponse;
				previous.requestDisposition =
					proposalSession.requestDisposition || previous.requestDisposition;
				previous.feasibilityNote = proposalSession.feasibilityNote || previous.feasibilityNote;
				previous.confidence = proposalSession.confidence || previous.confidence;
				previous.suggestedFollowUp =
					proposalSession.suggestedFollowUp || previous.suggestedFollowUp;
				previous.isCurrent = true;
			} else {
				sessions.push(proposalSession);
			}
		}

		return sessions;
	});
	const latestThreadNote = $derived.by(() => {
		const latestSessionWithNote = calibrationThreadSessions
			.slice()
			.reverse()
			.find((session) => String(session.note ?? '').trim());
		return String(latestSessionWithNote?.note ?? '').trim();
	});
	const hasCalibrationThread = $derived(calibrationThreadSessions.length > 0);
	const currentThreadSession = $derived.by(() => calibrationThreadSessions.at(-1) ?? null);
	const calibrationThreadCountLabel = $derived.by(() => {
		const count = calibrationThreadSessions.length;
		return `${count} ${count === 1 ? 'turn' : 'turns'}`;
	});
	$effect(() => {
		const latestSession = calibrationThreadSessions.at(-1);
		if (!latestSession) {
			lastAutoScrolledThreadSignature = '';
			return;
		}
		if (!threadScrollViewport) return;
		const latestSessionSignature = buildCalibrationThreadScrollSignature(
			latestSession,
			calibrationThreadSessions.length
		);
		if (latestSessionSignature === lastAutoScrolledThreadSignature) return;
		lastAutoScrolledThreadSignature = latestSessionSignature;
		queueMicrotask(() => {
			if (!threadScrollViewport) return;
			threadScrollViewport.scrollTop = threadScrollViewport.scrollHeight;
		});
	});
	const hasClearableTuningState = $derived(
		Boolean(
			pendingProposal ||
			hasCalibrationThread ||
			hasCalibration ||
			String(folderAdvice.summary ?? '').trim() ||
			String(folderAdvice.operator_note ?? '').trim()
		)
	);
	const sampleSetupHeading = $derived.by(() =>
		reviewGateStatus === 'accepted'
			? 'Revise the approved draft'
			: hasCalibration
				? 'Continue the calibration'
				: 'Start the sample'
	);
	const sampleSetupLede = $derived.by(() => {
		if (reviewGateStatus === 'accepted') {
			return 'The current draft is already saved for queueing. Keep tuning here only if you want to replace it with a newer draft.';
		}
		return 'Choose the host, steer the bench, and keep the latest diagnosis beside the controls instead of buried below them.';
	});
	const noteFieldLabel = $derived.by(() => (hasCalibration ? 'Note for bench' : 'Optional note'));
	const noteFieldLede = $derived.by(() =>
		hasCalibration
			? calibration.review_media_ready
				? 'Describe what you noticed in the clips and what should change next.'
				: 'Describe the artifact, scene type, or size tradeoff you want adjusted next.'
			: 'Ask for a literal experiment or name the artifact you want protected.'
	);
	const notePlaceholder = $derived.by(() =>
		hasCalibration
			? calibration.review_media_ready
				? 'Examples: this still looks clean at 200 MB, voices sound fine but action feels smeared, keep this sharpness but go smaller, or spend more bits on dark scenes.'
				: 'Examples: cleaner dark scenes, keep this sharpness but go smaller, or spend more bits on fast motion.'
			: 'Examples: try 85 VMAF, keep dark scenes cleaner, or spend more bits on motion.'
	);
	const reviewConversationCopy = $derived.by(() => {
		if (!hasCalibration || !calibration.review_media_ready) {
			return null;
		}
		return 'The bench can use the current review deck as context, so reference anything you saw or heard in the source-versus-draft clips.';
	});
	const runVerdictOutcomeCopy = $derived.by(() => {
		switch (String(runVerdict.outcome ?? '')) {
			case 'strong_match':
				return 'Strong match';
			case 'acceptable_experiment':
				return 'Acceptable experiment';
			case 'needs_review':
				return 'Needs review';
			case 'poor_fit':
				return 'Poor fit';
			default:
				return '';
		}
	});
	const runVerdictOutcomeVariant = $derived.by(() => {
		switch (String(runVerdict.outcome ?? '')) {
			case 'strong_match':
				return 'ok';
			case 'acceptable_experiment':
				return 'default';
			case 'needs_review':
				return 'neutral';
			case 'poor_fit':
				return 'ghost';
			default:
				return 'neutral';
		}
	});
	const apiPrefix = $derived(
		folder.prefix
			.split('/')
			.map((segment) => encodeURIComponent(segment))
			.join('/')
	);
	const selectedHostOption = $derived(
		sampleHostOptions.find((option) => String(option.key ?? '') === selectedHost) ?? null
	);
	const selectedHostRuntime = $derived.by(() => hostRuntimeByKey.get(selectedHost) ?? null);
	const selectedHostLabel = $derived(String(selectedHostOption?.label ?? '').trim());
	const selectedHostDetail = $derived(String(selectedHostOption?.detail ?? '').trim());
	const selectedHostScheduleCopy = $derived(compactScheduleCopy(selectedHostRuntime));
	const selectedHostCapacityCopy = $derived(hostCapacityCopy(selectedHostRuntime));
	const selectedHostSearchSummary = $derived(
		folderAwareQualitySearchSummary(selectedHostRuntime, folder.prefix)
	);
	const canRunSample = $derived(selectedHostOption?.available === true);
	const sampleRunActive = $derived(
		status.calibration_status === 'queued' || status.calibration_status === 'running'
	);
	const folderRefreshActive = $derived(
		status.folder_scan_status === 'queued' || status.folder_scan_status === 'running'
	);
	const folderRefreshSignal = $derived.by(() =>
		status.folder_scan_status === 'running' ? 'Refreshing now' : 'Refresh queued'
	);
	const calibrationSignal = $derived.by(() => {
		if (status.calibration_status === 'running') {
			return calibrationJob?.mode === 'full' ? 'Proof encode live' : 'Sample live';
		}
		return 'Calibration queued';
	});
	const sampleActionBlockedReason = $derived.by(() => {
		if (sampleRunActive) {
			return 'A calibration job is already active for this folder.';
		}
		if (!canRunSample) {
			return String(
				selectedHostOption?.detail ??
					folder.sample_host_help_text ??
					'Choose a ready sample host first.'
			);
		}
		return '';
	});
	const runReadinessHeading = $derived.by(() => {
		if (sampleRunActive) return 'Calibration in progress';
		if (canRunSample && selectedHostLabel) return `Ready on ${selectedHostLabel}`;
		return 'Choose a ready host';
	});
	const runReadinessCopy = $derived.by(() => {
		if (sampleRunActive) {
			return sampleActionBlockedReason || 'A calibration job is already active for this folder.';
		}
		if (canRunSample) {
			return (
				selectedHostCapacityCopy ||
				selectedHostDetail ||
				'This host is ready for a representative sample run.'
			);
		}
		return sampleActionBlockedReason || 'Select a host before starting a sample.';
	});
	const reviewGateHeading = $derived.by(() => {
		if (reviewGateStatus === 'accepted') return 'Draft approved and saved';
		if (reviewGateStatus === 'needs_approval') return 'Draft ready for approval';
		if (reviewGateStatus === 'missing_review_media')
			return 'Run a fresh sample to restore review clips';
		if (reviewGateStatus === 'needs_fresh_sample') return 'Run a fresh sample before approving';
		return 'Run a sample before approving';
	});
	const reviewGateDetail = $derived.by(() => {
		if (reviewGateStatus === 'accepted') {
			const acceptedAtCopy = formatDateTimeCopy(reviewGate.accepted_at);
			return acceptedAtCopy
				? `Approved ${acceptedAtCopy}. Mediaforce queues the full folder encode automatically after approval.`
				: 'This draft is approved. Mediaforce queues the full folder encode automatically after approval.';
		}
		const gateMessage = String(reviewGate.message ?? '').trim();
		if (gateMessage) return gateMessage;
		if (reviewGateStatus === 'needs_approval') {
			return 'Review the sample result, compare clips, and policy diff below before saving the draft.';
		}
		return reviewMediaStatusCopy;
	});
	const reviewGateEyebrow = $derived.by(() =>
		reviewGateStatus === 'accepted' ? '2. Approval complete' : '2. Approval gate'
	);
	const calibrationFailureNoticeId = $derived(`calibration-failure:${folder.prefix}`);
	const pendingProposalHostKey = $derived(String(pendingProposal?.host?.key ?? '').trim());
	const pendingProposalTrace = $derived(
		(pendingProposal?.trace as ProposalTrace | undefined | null) ?? null
	);
	const pendingProposalTraceContext = $derived(
		(pendingProposalTrace?.context as ProposalTrace['context'] | undefined | null) ?? null
	);
	const pendingProposalSignal = $derived(String(pendingProposal?.operator_signal ?? '').trim());
	const pendingProposalCanQueue = $derived(pendingProposal?.can_queue === true);
	const pendingProposalSelfCheck = $derived(
		(pendingProposal?.self_check as ProposalSelfCheck | undefined) ?? {}
	);
	const pendingProposalSelfCheckStatus = $derived(
		String(pendingProposalSelfCheck.status ?? '').trim()
	);
	const pendingProposalSelfCheckVariant = $derived.by(() => {
		if (pendingProposalSelfCheckStatus === 'fail') return 'ghost';
		if (pendingProposalSelfCheckStatus === 'warn') return 'neutral';
		if (pendingProposalSelfCheckStatus === 'pass') return 'ok';
		return 'default';
	});
	const pendingProposalSelfCheckLabel = $derived.by(() => {
		if (pendingProposalSelfCheckStatus === 'fail') return 'Needs note rewrite';
		if (pendingProposalSelfCheckStatus === 'warn') return 'Run with caution';
		if (pendingProposalSelfCheckStatus === 'pass') return 'Self-check passed';
		return '';
	});
	const proposalDraftHeading = $derived.by(() => {
		if (!pendingProposal) return '';
		if (pendingOperatorRequestLabel) return `Bench draft heard ${pendingOperatorRequestLabel}`;
		if (hasCalibration) return 'Bench draft for the next sample';
		return 'Bench draft for the first sample';
	});
	const pendingProposalNote = $derived.by(() => {
		const explicitProposalNote = String(pendingProposal?.operator_note ?? '').trim();
		if (explicitProposalNote) return explicitProposalNote;
		return latestThreadNote;
	});
	const proposalNoteMismatch = $derived.by(() => {
		if (!pendingProposal) return false;
		const currentNote = note.trim();
		if (!currentNote) return false;
		return currentNote !== pendingProposalNote;
	});
	const proposalHostMismatch = $derived.by(() => {
		if (!pendingProposal) return false;
		return selectedHost.trim() !== pendingProposalHostKey;
	});
	const pendingProposalNeedsRefresh = $derived(
		Boolean(pendingProposal && (proposalNoteMismatch || proposalHostMismatch))
	);
	const retryableCalibrationJob = $derived.by(() => {
		if (pendingProposal) return null;
		if (!retryableSampleJob) return null;
		const jobStatus = String(retryableSampleJob.status ?? '').trim();
		if (!['failed', 'stopped'].includes(jobStatus)) return null;
		if (String(retryableSampleJob.mode ?? 'sample').trim() !== 'sample') return null;
		const action = String(retryableSampleJob.action ?? '').trim();
		if (!['baseline', 'ai_tune'].includes(action)) return null;
		return retryableSampleJob;
	});
	const retryableCalibrationHostKey = $derived(
		String(retryableCalibrationJob?.host?.key ?? '').trim()
	);
	const retryableCalibrationNote = $derived(String(retryableCalibrationJob?.notes ?? '').trim());
	const retryableCalibrationNeedsRefresh = $derived.by(() => {
		if (!retryableCalibrationJob) return false;
		const currentNote = note.trim();
		if (currentNote && currentNote !== retryableCalibrationNote) return true;
		return selectedHost.trim() !== retryableCalibrationHostKey;
	});
	const retryableCalibrationRefreshBlockedByEmptyNote = $derived.by(() => {
		if (!retryableCalibrationNeedsRefresh) return false;
		return note.trim().length === 0;
	});
	const benchDraftBlockedByEmptyNote = $derived.by(
		() => hasCalibration && note.trim().length === 0
	);
	const canRetrySavedSampleDraft = $derived(
		Boolean(retryableCalibrationJob && !retryableCalibrationNeedsRefresh)
	);
	const previewButtonLabel = $derived.by(() => {
		if (benchDraftBlockedByEmptyNote) return 'Add note first';
		if (retryableCalibrationNeedsRefresh || pendingProposalNeedsRefresh) return 'Refresh draft';
		return 'Ask bench';
	});
	const confirmButtonLabel = $derived.by(() => {
		if (canRetrySavedSampleDraft)
			return selectedHostLabel ? `Run this sample on ${selectedHostLabel}` : 'Run this sample';
		if (retryableCalibrationRefreshBlockedByEmptyNote) return 'Add note first';
		if (retryableCalibrationNeedsRefresh) return 'Refresh draft first';
		if (!pendingProposal) return 'Run sample after bench draft';
		if (retryableCalibrationNeedsRefresh || pendingProposalNeedsRefresh)
			return 'Run sample after draft refresh';
		if (!pendingProposalCanQueue) return 'Run sample after draft fix';
		return selectedHostLabel ? `Run this sample on ${selectedHostLabel}` : 'Run this sample';
	});
	const canRunPrimarySampleAction = $derived.by(() => {
		if (!canRunSample || sampleRunActive || actionState === 'preview') return false;
		if (canRetrySavedSampleDraft) return true;
		if (retryableCalibrationRefreshBlockedByEmptyNote) return false;
		if (retryableCalibrationNeedsRefresh) return false;
		if (!pendingProposal) return false;
		if (pendingProposalNeedsRefresh) return false;
		return pendingProposalCanQueue;
	});
	const canRequestBenchDraft = $derived(
		canRunSample &&
			!sampleRunActive &&
			actionState !== 'preview' &&
			!retryableCalibrationRefreshBlockedByEmptyNote &&
			!benchDraftBlockedByEmptyNote
	);
	const canUseApprovedSeasonShortcut = $derived(
		canRunSample &&
			!sampleRunActive &&
			actionState !== 'preview' &&
			Boolean(String(approvedSeasonShortcut?.suggested_note ?? '').trim())
	);
	const sampleActionSupportCopy = $derived.by(() => {
		if (actionState === 'preview') {
			return 'The bench is reading the latest note and calibration context before it drafts the next sample.';
		}
		if (sampleRunActive) {
			return sampleActionBlockedReason || 'A calibration job is already active for this folder.';
		}
		if (!canRunSample) {
			return (
				sampleActionBlockedReason || 'Choose a ready host before asking the bench for a draft.'
			);
		}
		if (canRetrySavedSampleDraft) {
			return 'The last sample draft is still available. Run it again to retry the test encode without asking the bench again.';
		}
		if (retryableCalibrationRefreshBlockedByEmptyNote) {
			return 'Add a note in the bench chat before refreshing the stopped sample draft. The tuner needs updated guidance when the next run changes host or request.';
		}
		if (retryableCalibrationNeedsRefresh) {
			return 'You changed the note or host since the stopped sample. Refresh the bench draft in the chat below so the next run matches the latest request.';
		}
		if (pendingProposalNeedsRefresh) {
			return 'You changed the note or host. Refresh the bench draft in the chat below so the plan matches what will run.';
		}
		if (pendingProposal) {
			return pendingProposalCanQueue
				? 'The draft is ready. Review the warning and the policy changes, then run the sample when it looks right.'
				: String(pendingProposal.message ?? 'Adjust the note and ask the bench again.');
		}
		return 'Use the bench chat below to draft the next sample. Nothing queues until you approve that draft.';
	});
	const nextActionStatus = $derived.by(() => {
		if (sampleRunActive) return { label: 'In progress', variant: 'neutral' as const };
		if (!canRunSample) return { label: 'Blocked', variant: 'warn' as const };
		if (canRetrySavedSampleDraft) return { label: 'Ready to run', variant: 'ok' as const };
		if (retryableCalibrationRefreshBlockedByEmptyNote)
			return { label: 'Add note', variant: 'warn' as const };
		if (retryableCalibrationNeedsRefresh)
			return { label: 'Refresh in chat', variant: 'warn' as const };
		if (pendingProposalNeedsRefresh) return { label: 'Refresh in chat', variant: 'warn' as const };
		if (pendingProposal && pendingProposalCanQueue) {
			return { label: 'Ready to run', variant: 'ok' as const };
		}
		if (pendingProposal) return { label: 'Needs revision', variant: 'neutral' as const };
		return { label: 'Draft in chat', variant: 'default' as const };
	});
	const nextActionHeading = $derived.by(() => {
		if (sampleRunActive) return 'Wait for the current sample to finish';
		if (!canRunSample) return 'Choose a ready host';
		if (canRetrySavedSampleDraft) {
			return selectedHostLabel
				? `Run the saved draft on ${selectedHostLabel}`
				: 'Run the saved draft';
		}
		if (retryableCalibrationRefreshBlockedByEmptyNote) return 'Add a note before refreshing';
		if (retryableCalibrationNeedsRefresh) return 'Refresh the draft in bench chat';
		if (pendingProposalNeedsRefresh) return 'Refresh the draft in bench chat';
		if (pendingProposal && pendingProposalCanQueue) {
			return selectedHostLabel ? `Run the draft on ${selectedHostLabel}` : 'Run the draft';
		}
		if (pendingProposal) return 'Revise the draft in bench chat';
		return 'Draft the next sample in bench chat';
	});
	const noteSubmitHint = $derived.by(() => {
		if (actionState === 'preview') {
			return selectedHostLabel
				? `The bench is drafting the next sample for ${selectedHostLabel}.`
				: 'The bench is drafting the next sample now.';
		}
		if (benchDraftBlockedByEmptyNote) {
			return 'Add a note for the bench before asking for another draft.';
		}
		if (canRequestBenchDraft) {
			return 'Press Enter to ask the bench. Shift+Enter adds a new line.';
		}
		return sampleActionBlockedReason || 'Choose a ready host before asking the bench for a draft.';
	});
	const approvedSeasonShortcutSummary = $derived.by(() => {
		const labels = (approvedSeasonShortcut?.season_labels ?? []).filter((value) =>
			String(value).trim()
		);
		if (!labels.length) return '';
		if (labels.length === 1) return labels[0];
		if (labels.length === 2) return `${labels[0]} and ${labels[1]}`;
		return `${labels.slice(0, 2).join(', ')}, and ${labels.length - 2} more`;
	});
	const proposalWorkbenchRows = $derived.by(() => {
		if (!pendingProposal) return [] as PolicyWorkbenchRow[];
		const currentPolicy = flattenPolicy(pendingProposal.current_policy ?? policy);
		const previewPolicy = flattenPolicy(
			pendingProposal.preview_policy ?? pendingProposal.current_policy ?? policy
		);
		const paths = Array.from(
			new Set([...Object.keys(currentPolicy), ...Object.keys(previewPolicy)])
		).sort();
		return paths.map((path) => {
			const current = formatPolicyValue(path, currentPolicy[path], folder.metric_support);
			const draft = formatPolicyValue(path, previewPolicy[path], folder.metric_support);
			return {
				path,
				section: workbenchSection(path),
				label: policyRowLabel(path),
				current,
				draft,
				changed: current !== draft
			};
		});
	});
	const proposalChangedWorkbenchRows = $derived.by(() =>
		proposalWorkbenchRows.filter((row) => row.changed)
	);
	const proposalWorkbenchSections = $derived.by<PolicyWorkbenchSection[]>(() => {
		const grouped: Record<string, PolicyWorkbenchRow[]> = {};
		for (const row of proposalChangedWorkbenchRows) {
			const existing = grouped[row.section] ?? [];
			existing.push(row);
			grouped[row.section] = existing;
		}
		return Object.entries(grouped).map(([title, rows]) => ({ title, rows }));
	});
	const proposalSteadyWorkbenchRows = $derived.by(() =>
		proposalWorkbenchRows.filter((row) => !row.changed)
	);
	const workbenchContextStats = $derived.by<WorkbenchStat[]>(() => {
		if (!pendingProposalTraceContext) return [];
		const contextSample = pendingProposalTraceContext.sample_item ?? sampleItem;
		const stats: WorkbenchStat[] = [];
		const resolution = formatResolutionCopy(contextSample?.width, contextSample?.height);
		if (contextSample?.rel_path) {
			stats.push({
				label: 'Representative file',
				value: pathStem(contextSample.rel_path),
				detail: compactCopy([
					codecLabel(contextSample.video_codec),
					resolution,
					pathExtension(contextSample.rel_path)
				])
			});
		}
		const sourceSize = Number(contextSample?.source_size_bytes ?? 0);
		if (Number.isFinite(sourceSize) && sourceSize > 0) {
			stats.push({ label: 'Source size', value: formatGiB(sourceSize) });
		}
		const duration = Number(contextSample?.duration_seconds ?? 0);
		if (Number.isFinite(duration) && duration > 0) {
			stats.push({ label: 'Runtime', value: formatTimestamp(duration) });
		}
		const recentSample = pendingProposalTraceContext.runtime_toolbelt?.recent_sample_result as
			| Record<string, unknown>
			| undefined;
		const recentMetric = compactCopy([
			String(recentSample?.quality_metric ?? '').toUpperCase() || null,
			typeof recentSample?.quality_score === 'number'
				? Number(recentSample.quality_score).toFixed(1)
				: null
		]);
		if (recentMetric) {
			stats.push({
				label: 'Last measured sample',
				value: recentMetric,
				detail: formatPercentCopy(
					recentSample?.predicted_encode_percent as number | string | null | undefined
				)
			});
		}
		const folderSummary = pendingProposalTraceContext.folder_summary as
			| Record<string, unknown>
			| undefined;
		if (folderSummary?.item_count != null) {
			stats.push({
				label: 'Folder shape',
				value: `${folderSummary.item_count} item${Number(folderSummary.item_count) === 1 ? '' : 's'}`,
				detail: formatStatusCountCopy(folderSummary.statuses as Record<string, number> | undefined)
			});
		}
		return stats;
	});
	const workbenchMemoryEntries = $derived.by(() => {
		const rawEntries = pendingProposalTraceContext?.retrieved_memory ?? [];
		return rawEntries
			.map((entry) => ({
				title: String(entry.title ?? '').trim(),
				summary: String(entry.summary ?? '').trim(),
				excerpt: String(entry.excerpt ?? '').trim()
			}))
			.filter((entry) => entry.title || entry.summary || entry.excerpt)
			.slice(0, 3);
	});
	const workbenchToolbeltRows = $derived.by(() => {
		const toolbelt = (pendingProposalTraceContext?.runtime_toolbelt ?? {}) as Record<
			string,
			unknown
		>;
		return Object.entries(toolbelt).map(([key, value]) => ({
			label: titleCase(key.replace(/_/g, ' ')),
			value:
				typeof value === 'string'
					? value
					: Array.isArray(value)
						? value.join(', ')
						: JSON.stringify(value)
		}));
	});
	const pendingProposalRawResponse = $derived(
		String(pendingProposalTrace?.raw_response ?? '').trim()
	);
	const visibleReviewPack = $derived(
		(pendingProposal?.multimodal_review_pack as FolderMultimodalReviewPack | undefined | null) ??
			(calibrationAdvice.multimodal_review_pack as FolderMultimodalReviewPack | undefined | null) ??
			null
	);
	const visibleReviewArtifacts = $derived(normalizeReviewArtifacts(visibleReviewPack));
	const visibleAudioReviewArtifacts = $derived(
		visibleReviewArtifacts.filter((artifact) => artifact.category === 'audio')
	);
	const visibleVisualReviewArtifacts = $derived(
		visibleReviewArtifacts.filter((artifact) => artifact.category === 'visual')
	);
	const visibleReviewPackHeading = $derived.by(() => {
		if (!visibleReviewArtifacts.length) return '';
		if (pendingProposal) return 'What the bench saw';
		return 'Last bench review pack';
	});
	const visibleReviewPackCopy = $derived.by(() => {
		if (!visibleReviewArtifacts.length) return '';
		const artifactCount =
			typeof visibleReviewPack?.artifact_count === 'number'
				? visibleReviewPack.artifact_count
				: visibleReviewArtifacts.length;
		const noun = artifactCount === 1 ? 'artifact' : 'artifacts';
		if (pendingProposal) {
			return `These are the exact review artifacts attached to the current bench draft (${artifactCount} ${noun}).`;
		}
		return `These are the exact review artifacts attached to the latest saved bench reply (${artifactCount} ${noun}).`;
	});
	const visibleReviewAudioSummary = $derived(
		String(visibleReviewPack?.audio_plan?.summary ?? '').trim()
	);

	function openReviewAsset(path: string): void {
		const target = path.trim();
		if (!browser || !target) return;
		window.open(target, '_blank', 'noopener,noreferrer');
	}

	function reviewCompareDownloadHref(): string {
		const encodedPrefix = folder.prefix
			.split('/')
			.filter(Boolean)
			.map((segment) => encodeURIComponent(segment))
			.join('/');
		return `/api/folders/${encodedPrefix}/review-compare/download`;
	}

	function downloadReviewCompareVideo(): void {
		if (!browser) return;
		const link = document.createElement('a');
		link.href = reviewCompareDownloadHref();
		link.rel = 'noopener noreferrer';
		link.style.display = 'none';
		document.body.appendChild(link);
		link.click();
		link.remove();
	}

	$effect(() => {
		if (!browser) return;
		const intervalMs = status.polling_active ? 5_000 : 60_000;
		let cancelled = false;

		const scheduleRefresh = () => {
			if (cancelled) return;
			autoRefreshTimer = window.setTimeout(async () => {
				if (!cancelled && document.visibilityState === 'visible' && !shouldPauseAutoRefresh()) {
					await refreshFolderView();
				}
				scheduleRefresh();
			}, intervalMs);
		};

		const handleVisibilityChange = () => {
			if (document.visibilityState === 'visible' && !shouldPauseAutoRefresh()) {
				void refreshFolderView();
			}
		};

		const handlePointerDown = (event: PointerEvent) => {
			const target = event.target;
			if (!(target instanceof HTMLElement)) return;
			if (target.closest('a[href], button, summary, [role="button"], [role="link"]')) {
				pauseAutoRefreshFor(1500);
			}
		};

		const handleKeyDown = (event: KeyboardEvent) => {
			if (event.key !== 'Enter' && event.key !== ' ') return;
			const target = event.target;
			if (!(target instanceof HTMLElement)) return;
			if (target.closest('a[href], button, summary, [role="button"], [role="link"]')) {
				pauseAutoRefreshFor(1500);
			}
		};

		document.addEventListener('visibilitychange', handleVisibilityChange);
		document.addEventListener('pointerdown', handlePointerDown, true);
		document.addEventListener('keydown', handleKeyDown, true);
		scheduleRefresh();

		return () => {
			cancelled = true;
			document.removeEventListener('visibilitychange', handleVisibilityChange);
			document.removeEventListener('pointerdown', handlePointerDown, true);
			document.removeEventListener('keydown', handleKeyDown, true);
			if (autoRefreshTimer !== null) {
				window.clearTimeout(autoRefreshTimer);
				autoRefreshTimer = null;
			}
		};
	});

	const breadcrumbItems = $derived.by(() => {
		const segments = folder.prefix.split('/').filter(Boolean);
		const items: BreadcrumbItem[] = [{ label: 'Folders', href: '/' }];
		const prefixParts: string[] = [];
		segments.forEach((segment, index) => {
			prefixParts.push(segment);
			items.push({
				label: index === 0 ? folderLibraryLabel(segment) : segment,
				href:
					index === segments.length - 1
						? null
						: `/folders/${prefixParts.map((part) => encodeURIComponent(part)).join('/')}`
			});
		});
		return items;
	});

	const summaryStatusCopy = $derived.by(() => formatStatusCountCopy(folder.summary?.statuses));

	const sampleHostCards = $derived.by(() =>
		sampleHostOptions
			.map((option) => {
				const key = String(option.key ?? '');
				const runtime = hostRuntimeByKey.get(key) ?? null;
				const card: SampleHostCard = {
					key,
					label: String(option.label ?? 'Unknown host'),
					detail: String(option.detail ?? ''),
					available: option.available === true,
					runtime,
					searchSummary: folderAwareQualitySearchSummary(runtime, folder.prefix),
					preferred: key === String(folder.sample_host_key ?? '')
				};
				return card;
			})
			.sort(
				(left, right) =>
					Number(right.available) - Number(left.available) ||
					Number(right.runtime?.priority ?? -1) - Number(left.runtime?.priority ?? -1) ||
					left.label.localeCompare(right.label)
			)
	);
	const reviewPairs = $derived((calibration.review_pairs as FolderReviewPair[] | undefined) ?? []);
	const fullCompareClipCount = $derived(
		((calibration.compare_clips as { path?: string }[] | undefined) ?? []).filter((clip) =>
			Boolean(String(clip?.path ?? '').trim())
		).length
	);
	const hasFullCompareDownload = $derived(fullCompareClipCount > 0);
	let selectedReviewPairIndex = $state(0);
	const selectedReviewPair = $derived(reviewPairs[selectedReviewPairIndex] ?? null);
	let seenReviewMomentKeys = $state<string[]>([]);
	let sourceReviewVideo = $state<HTMLVideoElement | null>(null);
	let draftReviewVideo = $state<HTMLVideoElement | null>(null);
	let reviewSyncLocked = false;
	let reviewMomentChangeLocked = false;
	let reviewResumePending = $state(false);

	const reviewMomentPills = $derived.by(() => {
		const labels = ['Early review', 'Midpoint review', 'Late review'];
		const moments = reviewPairs.length
			? reviewPairs.map((pair) => ({
					timestamp_seconds: pair.timestamp_seconds,
					duration_seconds: pair.duration_seconds
				}))
			: (calibration.compare_clips ?? []);
		return moments.map((clip, index) => {
			const timestamp = Number(clip.timestamp_seconds ?? 0);
			const duration = Number(clip.duration_seconds ?? 0);
			return {
				key: `${index}-${timestamp}`,
				title: labels[index] ?? `Review clip ${index + 1}`,
				timestamp: formatTimestamp(timestamp),
				detail: duration > 0 ? `${Math.round(duration)}s clip` : 'Review clip'
			};
		});
	});
	const selectedReviewMoment = $derived(reviewMomentPills[selectedReviewPairIndex] ?? null);
	const reviewMomentCount = $derived(reviewMomentPills.length);
	const hasPreviousReviewMoment = $derived(selectedReviewPairIndex > 0);
	const hasNextReviewMoment = $derived(selectedReviewPairIndex < reviewMomentCount - 1);
	const seenReviewMomentSet = $derived.by(
		() =>
			new Set(
				seenReviewMomentKeys.filter((key) => reviewMomentPills.some((pill) => pill.key === key))
			)
	);
	const seenReviewMomentCount = $derived(seenReviewMomentSet.size);
	const remainingReviewMomentCount = $derived(
		Math.max(reviewMomentCount - seenReviewMomentCount, 0)
	);
	const reviewMomentProgressCopy = $derived.by(() => {
		const total = reviewMomentCount;
		if (total === 0) return 'No review moments yet';
		const seen = seenReviewMomentCount;
		if (seen === 0) return `${total} ${total === 1 ? 'moment' : 'moments'} to review`;
		if (seen >= total) return `Reviewed all ${total} ${total === 1 ? 'moment' : 'moments'}`;
		return `Reviewed ${seen} of ${total} ${total === 1 ? 'moment' : 'moments'}`;
	});
	const selectedReviewMomentCopy = $derived.by(() => {
		if (!selectedReviewMoment) return '';
		return `${selectedReviewMoment.title} at ${selectedReviewMoment.timestamp}`;
	});
	const reviewDecisionProgressCopy = $derived.by(() => {
		if (reviewMomentCount === 0) return '';
		if (remainingReviewMomentCount === 0) {
			return 'You have reviewed every proof moment in this browser session. Save or queue the draft when it looks right.';
		}
		return `You have checked ${seenReviewMomentCount} of ${reviewMomentCount} proof moments in this browser session. Review ${remainingReviewMomentCount} more before you commit the folder run.`;
	});
	const threadBenchSummary = $derived.by(() => ({
		headline:
			String(currentThreadSession?.requestResponse ?? '').trim() ||
			String(currentThreadSession?.summary ?? '').trim(),
		detail:
			String(currentThreadSession?.runNextStep ?? '').trim() ||
			String(currentThreadSession?.suggestedFollowUp ?? '').trim() ||
			String(currentThreadSession?.diagnosis ?? '').trim(),
		requestLabel: pendingOperatorRequestLabel || operatorRequestLabel
	}));
	const reviewBenchSummary = $derived.by(() => {
		const pendingProposalSummary = {
			headline:
				String(pendingProposal?.request_response ?? '').trim() ||
				String(pendingProposal?.summary ?? '').trim() ||
				String(pendingProposal?.message ?? '').trim(),
			detail:
				String(pendingProposal?.suggested_follow_up ?? '').trim() ||
				String(pendingProposal?.diagnosis ?? '').trim() ||
				String(pendingProposal?.feasibility_note ?? '').trim() ||
				String(pendingProposal?.message ?? '').trim(),
			requestLabel: pendingOperatorRequestLabel
		};
		if (
			pendingProposalSummary.headline ||
			pendingProposalSummary.detail ||
			pendingProposalSummary.requestLabel
		) {
			return {
				headline: pendingProposalSummary.headline || threadBenchSummary.headline,
				detail: pendingProposalSummary.detail || threadBenchSummary.detail,
				requestLabel: pendingProposalSummary.requestLabel || threadBenchSummary.requestLabel,
				available: true
			};
		}

		return {
			...threadBenchSummary,
			available: Boolean(
				threadBenchSummary.headline || threadBenchSummary.detail || threadBenchSummary.requestLabel
			)
		};
	});
	const threadHistorySummaryCopy = $derived.by(() => {
		if (!hasCalibrationThread) return '';
		const nextStep = String(currentThreadSession?.runNextStep ?? '').trim();
		if (nextStep) return nextStep;
		const followUp = String(currentThreadSession?.suggestedFollowUp ?? '').trim();
		if (followUp) return followUp;
		return 'The latest bench read is pinned beside the review decision so you can scan it without leaving the proof clips.';
	});
	const archivedThreadHeadline = $derived.by(
		() =>
			String(currentThreadSession?.requestResponse ?? '').trim() ||
			String(currentThreadSession?.summary ?? '').trim()
	);
	const archivedThreadDetail = $derived.by(() => {
		const summary = String(currentThreadSession?.summary ?? '').trim();
		if (summary && summary !== archivedThreadHeadline) {
			return summary;
		}
		return (
			String(currentThreadSession?.diagnosis ?? '').trim() ||
			String(currentThreadSession?.runSummary ?? '').trim() ||
			null
		);
	});

	const factItems = $derived.by(() =>
		folder.summary
			? [
					{ label: 'Items', value: String(folder.summary.item_count) },
					{ label: 'Total Size', value: formatGiB(folder.summary.total_size_bytes, 2) },
					{ label: 'Statuses', value: summaryStatusCopy },
					{ label: 'Video', value: formatCodecCountsCopy(folder.summary.video_codecs) },
					{ label: 'Audio', value: formatCodecCountsCopy(folder.summary.audio_codecs) }
				]
			: []
	);
	const headerFactItems = $derived.by(() =>
		factItems.filter((item) => item.label !== 'Statuses').slice(0, 2)
	);
	const sampleQueueLabel = $derived.by(() =>
		queueSummaryCopy(
			Number(calibrationQueue.sample?.running_count ?? 0),
			Number(calibrationQueue.sample?.queued_count ?? 0),
			'Sample queue'
		)
	);
	const encodeJob = $derived((folder.encode_job as EncodeQueueJob | null) ?? null);
	const encodeQueue = $derived((folder.encode_queue as EncodeQueueSummary | undefined) ?? null);
	const encodeQueuePaused = $derived(Boolean(encodeQueue?.state?.is_paused));
	const encodeQueueStopping = $derived(Boolean(encodeQueue?.state?.stop_requested));
	const encodeJobStatus = $derived(String(encodeJob?.status ?? '').trim());
	const encodeJobProgress = $derived(
		(encodeJob?.progress as EncodeJobProgressTelemetry | null | undefined) ?? null
	);
	const encodeJobActiveHosts = $derived.by(() =>
		((encodeJob?.active_hosts as Array<Record<string, unknown>> | undefined) ?? []).filter(Boolean)
	);
	const encodeJobHostLabel = $derived.by(() =>
		encodeJobActiveHosts.length > 1
			? `${encodeJobActiveHosts.length} hosts`
			: String(encodeJob?.host?.label ?? encodeJob?.host?.key ?? '').trim()
	);
	const encodeJobHostRuntime = $derived.by(() => {
		const hostKey = String(encodeJob?.host?.key ?? '').trim();
		if (!hostKey) return null;
		return hostRuntimeByKey.get(hostKey) ?? null;
	});
	const encodeJobTone = $derived.by(() => encodeStatusTone(encodeJobStatus));
	const encodeJobChipLabel = $derived.by(() => {
		if (encodeJobStatus === 'queued' && encodeQueueStopping) {
			return 'Stopping';
		}
		if (encodeJobStatus === 'queued' && encodeQueuePaused) {
			return 'Paused';
		}
		if (
			encodeJobStatus === 'queued' &&
			/waiting for a host schedule window/i.test(String(encodeJob?.scheduler_status_copy ?? ''))
		) {
			return 'Scheduled';
		}
		return titleCase(encodeJobStatus.replace(/_/g, ' '));
	});
	const recoverableItemCount = $derived(Number(encodeJob?.recoverable_item_count ?? 0));
	const encodeJobCanRecoverNow = $derived.by(
		() =>
			['running', 'queued', 'retry_backoff'].includes(encodeJobStatus) && recoverableItemCount > 0
	);
	const encodeJobHeadline = $derived.by(() => {
		if (encodeJobCanRecoverNow) {
			return 'Recovery available';
		}
		if (encodeJobStatus === 'running' && Number(encodeJob?.running_shard_count ?? 0) > 1) {
			return 'Running across multiple lanes';
		}
		if (encodeJobStatus === 'running') return 'Running now';
		if (encodeJobStatus === 'queued') {
			if (encodeQueueStopping) {
				return 'Stopping before this folder starts';
			}
			if (encodeQueuePaused) {
				return 'Queued, but the fleet is paused';
			}
			return /waiting for a host schedule window/i.test(
				String(encodeJob?.scheduler_status_copy ?? '')
			)
				? 'Queued for the next worker window'
				: 'Waiting for a lane';
		}
		if (encodeJobStatus === 'retry_backoff') return 'Retry scheduled';
		if (encodeJobStatus === 'needs_attention') return 'Needs attention';
		if (encodeJobStatus === 'completed') return 'Completed';
		if (encodeJobStatus === 'failed') return 'Failed';
		if (encodeJobStatus === 'stopped') return 'Stopped';
		return 'Idle';
	});
	const encodeJobDetail = $derived.by(() => {
		const schedulerCopy = String(encodeJob?.scheduler_status_copy ?? '').trim();
		const currentItem = String(encodeJobProgress?.current_item_rel_path ?? '').trim();
		const activeHostLabels = encodeJobActiveHosts
			.map((host) => String(host.label ?? host.key ?? '').trim())
			.filter(Boolean);
		if (encodeJobCanRecoverNow) {
			return `${recoverableItemCount} file${recoverableItemCount === 1 ? '' : 's'} can be requeued without stopping the active lanes.`;
		}
		if (encodeJobStatus === 'running' && currentItem) return currentItem;
		if (encodeJobStatus === 'queued') {
			if (encodeQueueStopping) {
				return 'The queue is stopping, so this folder will not auto-start in the next worker window.';
			}
			if (encodeQueuePaused) {
				return 'This folder stays queued until you resume the fleet from Ops.';
			}
			if (/waiting for a host schedule window/i.test(schedulerCopy)) {
				return 'Queued now. Mediaforce will start this folder when a scheduled worker window opens.';
			}
			if (schedulerCopy) return schedulerCopy;
			return 'Waiting for an open worker lane.';
		}
		if (schedulerCopy) return schedulerCopy;
		if (encodeJobStatus === 'running' && activeHostLabels.length > 1) {
			return `${activeHostLabels.join(', ')} are encoding now.`;
		}
		if (encodeJobStatus === 'running') return 'Streaming work to the selected worker now.';
		if (encodeJobStatus === 'retry_backoff') return 'Waiting for the next retry window.';
		if (encodeJobStatus === 'needs_attention')
			return 'This run stopped before it could finish cleanly.';
		if (encodeJobStatus === 'failed')
			return 'The last folder encode failed before it could finish.';
		if (encodeJobStatus === 'stopped')
			return 'The last folder encode was stopped before it completed.';
		return '';
	});
	const encodeJobNextActionCopy = $derived.by(() => {
		if (encodeJobCanRecoverNow) {
			return 'Use Recover Failed Files to requeue only the interrupted items without resetting the whole folder.';
		}
		if (
			encodeJobStatus === 'needs_attention' ||
			encodeJobStatus === 'failed' ||
			encodeJobStatus === 'stopped'
		) {
			return 'Review the failure detail, then retry the folder encode when you want Mediaforce to continue this folder.';
		}
		if (encodeJobStatus === 'retry_backoff') {
			return 'Mediaforce will retry automatically when the cooldown window expires.';
		}
		if (encodeJobStatus === 'queued') {
			if (encodeQueueStopping) {
				return 'If you still want this folder later, requeue it after the stop request finishes.';
			}
			if (encodeQueuePaused) {
				return 'Resume the fleet from Ops when you want this folder to start again.';
			}
			if (
				/waiting for a host schedule window/i.test(String(encodeJob?.scheduler_status_copy ?? ''))
			) {
				return 'Leave this queued. Mediaforce will start it automatically in the next worker window.';
			}
			return 'Leave this queued unless you need to pause or stop the fleet.';
		}
		if (encodeJobStatus === 'running') {
			return 'Let the active run finish before validating or promoting staged files.';
		}
		return '';
	});
	const encodeJobFacts = $derived.by(() => {
		const facts: Array<{ label: string; value: string }> = [];
		const runningShardCount = Number(encodeJob?.running_shard_count ?? 0);
		const totalShardCount = Number(encodeJob?.shard_count ?? 0);
		if (encodeJobHostLabel) {
			facts.push({ label: 'Worker', value: encodeJobHostLabel });
		}
		if (runningShardCount > 1) {
			facts.push({
				label: 'Active lanes',
				value: `${runningShardCount} of ${totalShardCount || runningShardCount}`
			});
		}
		if (recoverableItemCount > 0) {
			facts.push({
				label: 'Needs recovery',
				value: `${recoverableItemCount} file${recoverableItemCount === 1 ? '' : 's'}`
			});
		}
		const percentCopy =
			encodeJobProgress?.percent_complete != null
				? formatPercentCopy(encodeJobProgress.percent_complete)
				: '';
		if (percentCopy && percentCopy !== 'n/a') {
			facts.push({ label: 'Progress', value: percentCopy });
		}
		const etaCopy = String(encodeJobProgress?.eta_copy ?? '').trim();
		if (etaCopy) {
			facts.push({ label: 'Estimated finish', value: etaCopy });
		}
		const queuePosition = Number(encodeJob?.queue_position ?? 0);
		if (queuePosition > 0) {
			const queueDepth = Number(encodeJob?.queue_depth ?? queuePosition);
			facts.push({ label: 'Queue spot', value: `${queuePosition} of ${queueDepth}` });
		}
		const fleetEtaCopy = String(encodeQueue?.telemetry?.eta_copy ?? '').trim();
		if (fleetEtaCopy) {
			facts.push({ label: 'Estimated queue finish', value: fleetEtaCopy });
		}
		return facts;
	});
	const encodeJobMetaCopy = $derived.by(() => {
		const parts: string[] = [];
		const runtimeReason = String(
			encodeJobHostRuntime?.active_reason ?? encodeJobHostRuntime?.message ?? ''
		).trim();
		if (runtimeReason && runtimeReason !== encodeJobDetail) {
			parts.push(runtimeReason);
		}
		return parts.join(' · ');
	});
	const encodeQueueLabel = $derived.by(() => encodeQueueSummaryCopy(folder.encode_queue_summary));
	const deliveryBlockedByEncode = $derived.by(() =>
		['queued', 'running', 'retry_backoff'].includes(encodeJobStatus)
	);
	const deliveryPanelVisible = $derived(stagedOutputCount > 0);
	const validateButtonLabel = $derived.by(() =>
		validatedOutputCount > 0 ? 'Re-validate Staged Files' : 'Validate Encoded Files'
	);
	const promoteButtonLabel = $derived.by(() =>
		validatedOutputCount === 0
			? 'Promote Validated Files'
			: validatedOutputCount === 1
				? 'Promote 1 Validated File'
				: `Promote ${validatedOutputCount} Validated Files`
	);
	const validateButtonVariant = $derived.by(() =>
		validatedOutputCount > 0 || deliveryBlockedByEncode ? 'secondary' : 'primary'
	);
	const promoteButtonVariant = $derived.by(() =>
		validatedOutputCount > 0 && !deliveryBlockedByEncode ? 'primary' : 'ghost'
	);
	const deliverHeading = $derived.by(() => {
		if (promotedOutputCount > 0) return 'Validation and promotion complete';
		if (validatedOutputCount > 0) return 'Validated output is ready to promote';
		return 'Validate and promote staged output';
	});
	const deliverEyebrow = $derived.by(() =>
		promotedOutputCount > 0 ? '3. Validation complete' : '3. Validate'
	);
	const queueEncodeButtonLabel = $derived.by(() =>
		encodeJobCanRecoverNow
			? 'Recover Failed Files'
			: ['needs_attention', 'failed', 'stopped'].includes(encodeJobStatus)
				? 'Retry Folder Encode'
				: 'Start Saved Folder Encode'
	);
	const queueActionVisible = $derived.by(() => {
		if (encodeJobCanRecoverNow) return true;
		if (['needs_attention', 'failed', 'stopped'].includes(encodeJobStatus)) return true;
		return reviewGate.can_confirm_full && !encodeJobStatus;
	});
	const workflowStageCards = $derived.by(() => {
		const draftStage = (() => {
			if (reviewGateStatus === 'accepted') {
				return { key: 'draft', label: 'Draft', status: 'done', detail: 'Draft saved' };
			}
			if (sampleRunActive) {
				return { key: 'draft', label: 'Draft', status: 'current', detail: 'Sample running' };
			}
			if (pendingProposal && pendingProposalCanQueue && !pendingProposalNeedsRefresh) {
				return { key: 'draft', label: 'Draft', status: 'done', detail: 'Bench draft ready' };
			}
			if (pendingProposalNeedsRefresh) {
				return { key: 'draft', label: 'Draft', status: 'current', detail: 'Refresh needed' };
			}
			if (hasCalibrationThread || hasCalibration) {
				return { key: 'draft', label: 'Draft', status: 'current', detail: 'Keep tuning' };
			}
			return { key: 'draft', label: 'Draft', status: 'pending', detail: 'Ask bench first' };
		})();

		const reviewStage = (() => {
			if (reviewGateStatus === 'accepted') {
				return { key: 'review', label: 'Review', status: 'done', detail: 'Policy approved' };
			}
			if (reviewGateStatus === 'needs_approval') {
				return { key: 'review', label: 'Review', status: 'current', detail: 'Review clips' };
			}
			if (
				reviewGateStatus === 'missing_review_media' ||
				reviewGateStatus === 'needs_fresh_sample'
			) {
				return { key: 'review', label: 'Review', status: 'current', detail: 'Need fresh sample' };
			}
			return { key: 'review', label: 'Review', status: 'pending', detail: 'Await sample' };
		})();

		const queueStage = (() => {
			if (
				['running', 'queued', 'retry_backoff', 'needs_attention', 'failed', 'stopped'].includes(
					encodeJobStatus
				)
			) {
				return { key: 'queue', label: 'Queue', status: 'current', detail: encodeJobHeadline };
			}
			if (reviewGateStatus === 'accepted') {
				return {
					key: 'queue',
					label: 'Queue',
					status: 'current',
					detail: 'Auto-queued after approval'
				};
			}
			if (promotedOutputCount > 0 || encodeJobStatus === 'completed') {
				return { key: 'queue', label: 'Queue', status: 'done', detail: 'Encode finished' };
			}
			return { key: 'queue', label: 'Queue', status: 'pending', detail: 'Approval unlocks this' };
		})();

		const deliverStage = (() => {
			if (promotedOutputCount > 0) {
				return { key: 'deliver', label: 'Validate', status: 'done', detail: 'Promoted' };
			}
			if (validatedOutputCount > 0) {
				return { key: 'deliver', label: 'Validate', status: 'current', detail: 'Ready to promote' };
			}
			if (stagedOutputCount > 0) {
				return { key: 'deliver', label: 'Validate', status: 'current', detail: 'Validate outputs' };
			}
			return { key: 'deliver', label: 'Validate', status: 'pending', detail: 'Wait for encode' };
		})();

		return [draftStage, reviewStage, queueStage, deliverStage];
	});
	const folderSnapshotItems = $derived.by<SnapshotItem[]>(() => {
		const discoveredCount = Number(folder.summary?.statuses?.discovered ?? 0);
		return factItems
			.filter((item) => !['Items', 'Total Size'].includes(item.label))
			.map((item) => ({
				label: item.label,
				value: item.value,
				detail:
					item.label === 'Statuses' && discoveredCount > 0
						? `${discoveredCount} item${discoveredCount === 1 ? '' : 's'} still have scanned defaults and would inherit the saved folder policy.`
						: undefined
			}));
	});
	const representativePath = $derived(String(sampleItem.rel_path ?? '').trim());
	const representativeFilenameStem = $derived(pathStem(representativePath));
	const representativeFilenameTokens = $derived.by(() =>
		softWrapTokens(representativeFilenameStem)
	);
	const representativeExtension = $derived(pathExtension(representativePath));
	const representativeResolution = $derived.by(
		() =>
			formatResolutionCopy(sampleItem.width, sampleItem.height) ??
			inferResolutionFromPath(representativePath)
	);
	const calibrationSampleResult = $derived(calibration.sample_result ?? null);
	const predictedOutputSizeBytes = $derived(
		Number(calibrationSampleResult?.predicted_total_size_bytes ?? 0)
	);
	const predictedOutputPercentCopy = $derived(
		formatPercentCopy(calibrationSampleResult?.predicted_encode_percent)
	);
	const predictedEncodeTimeCopy = $derived.by(() => {
		const seconds = Number(calibrationSampleResult?.predicted_encode_seconds ?? 0);
		if (!Number.isFinite(seconds) || seconds <= 0) return null;
		return formatTimestamp(seconds);
	});
	const predictedQualityCopy = $derived.by(() => {
		const metric = String(calibrationSampleResult?.quality_metric ?? '')
			.trim()
			.toUpperCase();
		const score = Number(calibrationSampleResult?.quality_score ?? 0);
		if (!metric || !Number.isFinite(score) || score <= 0) return null;
		return `${metric} ${score.toFixed(1)}`;
	});
	const representativeVideoBitrate = $derived.by(() => formatBitrateCopy(sampleItem.video_bitrate));
	const representativeAudioTrack = $derived.by(() => {
		const tracks = (sampleItem.audio_summary ?? []) as SampleAudioTrack[];
		return tracks.find((track) => Boolean(track.default)) ?? tracks[0] ?? null;
	});
	const streamComparisonRows = $derived.by(() => {
		const rows: ComparisonRow[] = [];
		const currentVideo = comparisonValue(
			codecLabel(sampleItem.video_codec),
			representativeExtension ?? undefined
		);
		const draftVideo = comparisonValue(
			codecLabel(itemPlan.video?.output_codec),
			representativeExtension ?? undefined
		);
		rows.push({
			label: 'Video stream',
			current: currentVideo,
			draft: draftVideo,
			changed: compareValues(currentVideo, draftVideo)
		});

		const currentAudio = summarizeAudioTrack(representativeAudioTrack);
		const draftAudio = summarizeAudioPlan(itemPlan.audio);
		rows.push({
			label: 'Primary audio',
			current: currentAudio,
			draft: draftAudio,
			changed: compareValues(currentAudio, draftAudio)
		});

		const currentSubs = summarizeSubtitleSource(
			(sampleItem.subtitle_summary ?? []) as SampleSubtitleTrack[]
		);
		const draftSubs = summarizeSubtitlePlan(
			itemPlan.subtitles,
			Boolean(policy.subtitle?.prefer_text ?? baselinePolicy.subtitle?.prefer_text ?? true)
		);
		rows.push({
			label: 'Subtitles',
			current: currentSubs,
			draft: draftSubs,
			changed: compareValues(currentSubs, draftSubs)
		});

		return rows;
	});
	const policyComparisonRows = $derived.by(() => {
		const rows: ComparisonRow[] = [];
		const currentMetric = summarizeMetricPolicy(baselinePolicy, folder.metric_support);
		const draftMetric = summarizeMetricPlan(itemPlan.video);
		rows.push({
			label: 'Quality guardrail',
			current: currentMetric,
			draft: draftMetric,
			changed: compareValues(currentMetric, draftMetric)
		});

		const currentCap = comparisonValue(
			formatPercentCopy(baselinePolicy.video?.max_encoded_percent)
		);
		const draftCap = comparisonValue(
			formatPercentCopy(itemPlan.video?.max_encoded_percent ?? policy.video?.max_encoded_percent)
		);
		rows.push({
			label: 'Size ceiling',
			current: currentCap,
			draft: draftCap,
			changed: compareValues(currentCap, draftCap)
		});

		const currentGrain = comparisonValue(String(baselinePolicy.video?.default_grain ?? 'n/a'));
		const draftGrain = comparisonValue(
			String(itemPlan.video?.default_grain ?? policy.video?.default_grain ?? 'n/a')
		);
		rows.push({
			label: 'Film grain',
			current: currentGrain,
			draft: draftGrain,
			changed: compareValues(currentGrain, draftGrain)
		});

		const currentSurround = comparisonValue(
			formatBitrateCopy(baselinePolicy.audio?.surround_5_1_opus_bitrate) ?? 'n/a'
		);
		const draftSurround = comparisonValue(
			formatBitrateCopy(
				policy.audio?.surround_5_1_opus_bitrate ?? itemPlan.audio?.output_bitrate
			) ?? 'n/a'
		);
		rows.push({
			label: '5.1 Opus budget',
			current: currentSurround,
			draft: draftSurround,
			changed: compareValues(currentSurround, draftSurround)
		});

		const currentStereo = comparisonValue(
			formatBitrateCopy(baselinePolicy.audio?.stereo_opus_bitrate) ?? 'n/a'
		);
		const draftStereo = comparisonValue(
			formatBitrateCopy(policy.audio?.stereo_opus_bitrate) ?? 'n/a'
		);
		rows.push({
			label: '2.0 Opus budget',
			current: currentStereo,
			draft: draftStereo,
			changed: compareValues(currentStereo, draftStereo)
		});

		const currentWide = comparisonValue(
			formatBitrateCopy(baselinePolicy.audio?.surround_7_1_opus_bitrate) ?? 'n/a'
		);
		const draftWide = comparisonValue(
			formatBitrateCopy(policy.audio?.surround_7_1_opus_bitrate) ?? 'n/a'
		);
		rows.push({
			label: '7.1 Opus budget',
			current: currentWide,
			draft: draftWide,
			changed: compareValues(currentWide, draftWide)
		});

		return rows;
	});
	const changedPolicyRows = $derived.by(() => policyComparisonRows.filter((row) => row.changed));
	const highImpactPolicyRows = $derived.by(() =>
		changedPolicyRows.filter((row) =>
			['Quality guardrail', 'Size ceiling', 'Film grain'].includes(row.label)
		)
	);
	const highImpactPolicyLabels = $derived.by(() =>
		highImpactPolicyRows.map((row) => row.label.toLowerCase()).join(', ')
	);
	const steadyPolicyRows = $derived.by<SteadyComparisonRow[]>(() =>
		policyComparisonRows
			.filter((row) => !row.changed)
			.map((row) => ({
				label: row.label,
				value: compactCopy([row.current.headline, row.current.detail ?? 'unchanged'])
			}))
	);

	$effect(() => {
		return () => {
			if (highImpactApprovalLockTimer) {
				clearTimeout(highImpactApprovalLockTimer);
			}
		};
	});

	$effect(() => {
		if (!highImpactApprovalGate.requiresConfirmation) {
			highImpactApprovalArmToken = '';
			highImpactReviewedDraftHash = '';
			highImpactApprovalLocked = false;
			return;
		}
		if (
			highImpactApprovalArmToken &&
			highImpactPolicySignature &&
			highImpactApprovalArmToken !== highImpactPolicySignature
		) {
			highImpactApprovalArmToken = '';
			highImpactReviewedDraftHash = '';
			highImpactApprovalLocked = false;
		}
		if (
			highImpactApprovalGate.armed &&
			highImpactReviewedDraftHash &&
			currentCalibrationDraftHash &&
			highImpactReviewedDraftHash !== currentCalibrationDraftHash
		) {
			highImpactApprovalArmToken = '';
			highImpactReviewedDraftHash = '';
			highImpactApprovalLocked = false;
		}
	});

	$effect(() => {
		if (!browser) {
			return;
		}
		if (pendingProposalCanQueue) {
			toasts.remove(calibrationFailureNoticeId);
			return;
		}
		if (
			!calibrationJob ||
			!['failed', 'stopped'].includes(String(status.calibration_status ?? '').trim()) ||
			!failedCalibrationPopupKey
		) {
			toasts.remove(calibrationFailureNoticeId);
			return;
		}
		const storageKey = `mediaforce.dismissedCalibrationFailure:${folder.prefix}`;
		if (window.sessionStorage.getItem(storageKey) === failedCalibrationPopupKey) {
			toasts.remove(calibrationFailureNoticeId);
			return;
		}
		toasts.upsert({
			id: calibrationFailureNoticeId,
			kind: 'error',
			eyebrow:
				calibrationJob.mode === 'full'
					? 'Proof Encode Stopped'
					: status.calibration_status === 'stopped'
						? 'Calibration Stopped'
						: 'Calibration Failed',
			heading:
				calibrationJob.mode === 'full'
					? 'Representative-file proof encode stopped before review clips were ready'
					: 'Sample calibration stopped before a reviewable draft was ready',
			lede:
				status.calibration_status === 'stopped'
					? 'The last background run was stopped. Review the stop detail before retrying so the next click does not feel like a no-op.'
					: 'The last background run failed. Review the failure detail before retrying so the next click does not feel like a no-op.',
			detail: calibrationFailureDetail,
			dismissLabel: 'Dismiss',
			autoCloseMs: null,
			onDismiss: dismissCalibrationFailurePopup
		});
		return () => {
			toasts.remove(calibrationFailureNoticeId);
		};
	});

	$effect(() => {
		const preferred = pendingProposalHostKey || String(folder.sample_host_key || '').trim();
		const currentStillExists = sampleHostOptions.some(
			(option) => String(option.key ?? '') === selectedHost
		);
		if (currentStillExists) return;

		selectedHost =
			preferred ||
			String(sampleHostOptions.find((option) => option.available)?.key ?? '') ||
			String(sampleHostOptions[0]?.key ?? '');
	});

	$effect(() => {
		if (reviewPairs.length === 0) {
			selectedReviewPairIndex = 0;
			seenReviewMomentKeys = [];
			return;
		}
		if (selectedReviewPairIndex >= 0 && selectedReviewPairIndex < reviewPairs.length) return;
		selectedReviewPairIndex = 0;
	});

	function markReviewMomentSeen(index: number) {
		const key = reviewMomentPills[index]?.key;
		if (!key || seenReviewMomentSet.has(key)) return;
		seenReviewMomentKeys = [...seenReviewMomentKeys, key];
	}

	async function selectReviewMoment(index: number) {
		if (index === selectedReviewPairIndex) return;
		if (!reviewPairs[index]) return;
		markReviewMomentSeen(index);
		const shouldResumePlayback = Boolean(
			(sourceReviewVideo && !sourceReviewVideo.paused) ||
			(draftReviewVideo && !draftReviewVideo.paused)
		);
		reviewMomentChangeLocked = true;
		reviewResumePending = shouldResumePlayback;
		selectedReviewPairIndex = index;
		await tick();
		if (sourceReviewVideo) {
			sourceReviewVideo.currentTime = 0;
		}
		if (draftReviewVideo) {
			draftReviewVideo.currentTime = 0;
		}
		if (shouldResumePlayback) {
			maybeResumeReviewPlayback();
		}
		window.setTimeout(() => {
			reviewMomentChangeLocked = false;
		}, 160);
	}

	async function stepReviewMoment(direction: -1 | 1) {
		const nextIndex = selectedReviewPairIndex + direction;
		if (nextIndex < 0 || nextIndex >= reviewPairs.length) return;
		await selectReviewMoment(nextIndex);
	}

	function maybeResumeReviewPlayback() {
		if (!reviewResumePending) return;
		if (!sourceReviewVideo || !draftReviewVideo) return;
		if (sourceReviewVideo.readyState < 2 || draftReviewVideo.readyState < 2) return;
		reviewResumePending = false;
		void Promise.allSettled([sourceReviewVideo.play(), draftReviewVideo.play()]);
	}

	function dismissCalibrationFailurePopup() {
		if (browser && failedCalibrationPopupKey) {
			window.sessionStorage.setItem(
				`mediaforce.dismissedCalibrationFailure:${folder.prefix}`,
				failedCalibrationPopupKey
			);
		}
		toasts.remove(calibrationFailureNoticeId);
	}

	function syncReviewPlayers(
		origin: 'source' | 'draft',
		eventName: 'play' | 'pause' | 'seek' | 'rate'
	) {
		if (reviewSyncLocked || reviewMomentChangeLocked) return;
		const leader = origin === 'source' ? sourceReviewVideo : draftReviewVideo;
		const follower = origin === 'source' ? draftReviewVideo : sourceReviewVideo;
		if (!leader || !follower) return;
		reviewSyncLocked = true;
		try {
			if (Math.abs(follower.currentTime - leader.currentTime) > 0.08) {
				follower.currentTime = leader.currentTime;
			}
			if (follower.playbackRate !== leader.playbackRate) {
				follower.playbackRate = leader.playbackRate;
			}
			if (eventName === 'play') {
				if (follower.paused) {
					void follower.play().catch(() => undefined);
				}
			} else if (eventName === 'pause') {
				if (!follower.paused) {
					follower.pause();
				}
			}
		} finally {
			window.setTimeout(() => {
				reviewSyncLocked = false;
			}, 0);
		}
	}

	function handleNoteKeydown(event: KeyboardEvent) {
		if (event.key !== 'Enter') return;
		if (event.shiftKey || event.altKey || event.ctrlKey || event.metaKey || event.isComposing) {
			return;
		}
		event.preventDefault();
		if (!canRequestBenchDraft) {
			return;
		}
		void previewSampleDraft();
	}

	function handlePreviewSampleDraftClick() {
		void previewSampleDraft();
	}

	async function previewSampleDraft(noteOverride?: unknown) {
		const submittedNote = resolveBenchDraftNote(note, noteOverride);
		const submittedHostKey = selectedHost.trim();
		const submittedHostLabel = String(selectedHostLabel).trim();
		previewSubmission = {
			note: submittedNote,
			hostKey: submittedHostKey,
			hostLabel: submittedHostLabel
		};
		note = '';
		actionState = 'preview';
		try {
			const response = await postJson<{
				ok: boolean;
				message: string;
				proposal?: PendingSampleProposal | null;
			}>(`/api/folders/${apiPrefix}/ai-tune/preview`, {
				note: submittedNote,
				host_key: selectedHost
			});
			previewDraftEcho = response.proposal ?? null;
			previewSubmission = null;
			if (response.proposal?.can_queue === false) {
				toasts.info('Bench draft needs revision', response.message);
			} else {
				toasts.success('Bench draft ready', response.message);
			}
			await invalidateAll();
		} catch (error) {
			note = submittedNote;
			previewSubmission = null;
			toasts.error(
				'Could not draft sample',
				error instanceof Error ? error.message : 'Unexpected sample error'
			);
		} finally {
			actionState = null;
		}
	}

	async function previewApprovedSeasonDraft() {
		const suggestedNote = String(approvedSeasonShortcut?.suggested_note ?? '').trim();
		if (!suggestedNote) {
			await previewSampleDraft();
			return;
		}
		const currentNote = note.trim();
		const submittedNote = currentNote
			? currentNote.includes(suggestedNote)
				? currentNote
				: `${currentNote}\n\n${suggestedNote}`
			: suggestedNote;
		await previewSampleDraft(submittedNote);
	}

	async function runSample() {
		if (canRetrySavedSampleDraft) {
			actionState = 'sample';
			try {
				const response = await postJson<{ ok: boolean; message: string }>(
					`/api/folders/${apiPrefix}/ai-tune/confirm`,
					{ proposal_id: '' }
				);
				toasts.success('Sample queued', response.message);
				previewDraftEcho = null;
				previewSubmission = null;
				await invalidateAll();
			} catch (error) {
				toasts.error(
					'Could not queue sample',
					error instanceof Error ? error.message : 'Unexpected sample error'
				);
			} finally {
				actionState = null;
			}
			return;
		}
		if (
			!pendingProposal?.proposal_id ||
			pendingProposalNeedsRefresh ||
			retryableCalibrationNeedsRefresh
		) {
			const guidance = retryableCalibrationNeedsRefresh
				? 'Refresh the saved sample draft from the bench chat below before queueing the next run.'
				: 'Use the bench chat below to draft or refresh the next sample before queueing it.';
			toasts.info('Bench draft required', guidance);
			return;
		}
		actionState = 'sample';
		try {
			const response = await postJson<{ ok: boolean; message: string }>(
				`/api/folders/${apiPrefix}/ai-tune/confirm`,
				{
					proposal_id: pendingProposal.proposal_id
				}
			);
			toasts.success('Sample queued', response.message);
			previewDraftEcho = null;
			previewSubmission = null;
			note = '';
			await invalidateAll();
		} catch (error) {
			toasts.error(
				'Could not queue sample',
				error instanceof Error ? error.message : 'Unexpected sample error'
			);
		} finally {
			actionState = null;
		}
	}

	async function saveProfile() {
		if (highImpactApprovalGate.requiresConfirmation && !highImpactApprovalGate.armed) {
			highImpactApprovalArmToken = highImpactPolicySignature;
			highImpactReviewedDraftHash = currentCalibrationDraftHash;
			highImpactApprovalLocked = true;
			if (highImpactApprovalLockTimer) {
				clearTimeout(highImpactApprovalLockTimer);
			}
			highImpactApprovalLockTimer = setTimeout(() => {
				highImpactApprovalLocked = false;
				highImpactApprovalLockTimer = null;
			}, 750);
			toasts.info(
				'Review high-impact changes',
				`Read the diff for ${highImpactPolicyLabels || 'the highlighted policy rows'}, then press "Confirm High-Impact Approval" to save the draft and queue the folder.`
			);
			return;
		}
		actionState = 'save';
		try {
			const response = await postJson<{ message: string; auto_queue_status?: string }>(
				`/api/folders/${apiPrefix}/save-profile`,
				{
					confirm_high_impact: highImpactApprovalGate.requiresConfirmation,
					reviewed_draft_hash: highImpactReviewedDraftHash || currentCalibrationDraftHash
				}
			);
			const autoQueueStatus = String(response.auto_queue_status ?? '').trim();
			if (autoQueueStatus === 'blocked') {
				toasts.info('Draft approved', response.message);
			} else {
				toasts.success(
					autoQueueStatus === 'queued' ? 'Draft approved and queued' : 'Draft approved',
					response.message
				);
			}
			await invalidateAll();
		} catch (error) {
			toasts.error(
				'Could not approve draft',
				error instanceof Error ? error.message : 'Unexpected save error'
			);
		} finally {
			actionState = null;
		}
	}

	async function clearTuningState() {
		if (browser) {
			const confirmed = window.confirm(
				"Clear this folder's tuning thread and sampled calibration artifacts? This removes the bench reply, sample review clips, and saved sample history for this folder only."
			);
			if (!confirmed) return;
		}
		actionState = 'clear';
		try {
			const response = await postJson<{ message: string }>(
				`/api/folders/${apiPrefix}/clear-tuning`,
				{}
			);
			toasts.success('Tuning cleared', response.message);
			previewDraftEcho = null;
			previewSubmission = null;
			note = '';
			await invalidateAll();
		} catch (error) {
			toasts.error(
				'Could not clear tuning state',
				error instanceof Error ? error.message : 'Unexpected clear error'
			);
		} finally {
			actionState = null;
		}
	}

	async function queueEncode() {
		actionState = 'encode';
		try {
			const response = await postJson<{ message: string; action?: string }>(
				`/api/folders/${apiPrefix}/queue-encode`,
				{
					notes: note,
					bypass_schedule: false
				}
			);
			toasts.success(
				response.action === 'recovered' ? 'Failed files recovered' : 'Folder encode queued',
				response.message
			);
			await invalidateAll();
		} catch (error) {
			toasts.error(
				'Could not queue folder encode',
				error instanceof Error ? error.message : 'Unexpected queue error'
			);
		} finally {
			actionState = null;
		}
	}

	async function validateOutputs() {
		actionState = 'validate';
		try {
			const response = await postJson<{
				message: string;
				validated_count?: number;
				failed_count?: number;
				item_count?: number;
			}>(`/api/folders/${apiPrefix}/validate-outputs`, {});
			const failedCount = Number(response.failed_count ?? 0);
			if (failedCount > 0) {
				toasts.info('Validation finished with warnings', response.message);
			} else {
				toasts.success('Validation finished', response.message);
			}
			await invalidateAll();
		} catch (error) {
			toasts.error(
				'Could not validate outputs',
				error instanceof Error ? error.message : 'Unexpected validation error'
			);
		} finally {
			actionState = null;
		}
	}

	async function promoteOutputs() {
		if (browser) {
			const confirmed = window.confirm(
				`Promote ${validatedOutputCount} validated file${validatedOutputCount === 1 ? '' : 's'} to the library? The originals will be moved to the archive and this cannot be undone.`
			);
			if (!confirmed) return;
		}
		actionState = 'promote';
		try {
			const response = await postJson<{ message: string; promoted_count?: number }>(
				`/api/folders/${apiPrefix}/promote-outputs`,
				{}
			);
			toasts.success('Promotion complete', response.message);
			await invalidateAll();
		} catch (error) {
			toasts.error(
				'Could not promote outputs',
				error instanceof Error ? error.message : 'Unexpected promotion error'
			);
		} finally {
			actionState = null;
		}
	}
</script>

<svelte:head>
	<title>{folder.prefix} · Mediaforce</title>
</svelte:head>

<div class="page-stack">
	<nav class="breadcrumb-row">
		{#each breadcrumbItems as item, index (`${item.label}-${index}`)}
			{#if index > 0}
				<span aria-hidden="true">›</span>
			{/if}
			{#if item.href}
				<a href={resolve(item.href)}>{item.label}</a>
			{:else}
				<span>{item.label}</span>
			{/if}
		{/each}
	</nav>

	<Panel class="folder-header" padding="1.35rem 1.45rem">
		<div class="folder-header-grid">
			<SectionHead
				eyebrow="Calibration Studio"
				heading={folder.prefix}
				lede="Tune this folder with hard-scene samples before you run the real batch."
				size="section"
			/>
			<div class="folder-header-side">
				<p class="lede-copy">{folder.metric_status_copy}</p>
				<div class="pill-row">
					{#each headerFactItems as item (item.label)}
						<Pill label={`${item.label}: ${item.value}`} variant="neutral" wide />
					{/each}
				</div>
			</div>
		</div>
		{#if actionState === 'preview' && previewSubmission}
			<div class="status-strip internal-status-strip">
				<div class="section-copy-block">
					<div class="status-strip-signal" aria-live="polite">
						<span class="status-strip-beacon" aria-hidden="true"></span>
						<span>Drafting bench reply</span>
					</div>
					<p class="eyebrow-copy">Bench request</p>
					<p class="status-strip-title">The bench is preparing the next sample draft now</p>
					<p class="muted-copy">
						{previewSubmission.hostLabel
							? `Mediaforce is reading the latest note and review context for ${previewSubmission.hostLabel}. The updated draft card will appear below when the reply lands.`
							: 'Mediaforce is reading the latest note and review context. The updated draft card will appear below when the reply lands.'}
					</p>
				</div>
				<p class="status-strip-meta">
					{previewSubmission.note
						? `Latest note: ${previewSubmission.note}`
						: 'Using the current host and saved review context'}
				</p>
			</div>
		{/if}
		<div class="workflow-stage-strip" aria-label="Folder workflow stages">
			{#each workflowStageCards as stage (stage.key)}
				<div class={`workflow-stage-card ${stage.status}`.trim()}>
					<p class="workflow-stage-label">{stage.label}</p>
					<p class="workflow-stage-detail">{stage.detail}</p>
				</div>
			{/each}
		</div>
	</Panel>

	{#if status.folder_scan_job && folderRefreshActive}
		<Panel class="status-strip-panel in-progress" padding="0.95rem 1rem">
			<div class="status-strip">
				<div class="section-copy-block">
					<div class="status-strip-signal" aria-live="polite">
						<span class="status-strip-beacon" aria-hidden="true"></span>
						<span>{folderRefreshSignal}</span>
					</div>
					<p class="eyebrow-copy">Folder refresh</p>
					<p class="status-strip-title">Refreshing in the background before you start a run</p>
					<p class="muted-copy">
						The catalog snapshot is updating so this view stays aligned with the latest media state.
					</p>
				</div>
				<p class="status-strip-meta">
					Started {String(
						status.folder_scan_job.started_at ?? status.folder_scan_job.created_at ?? ''
					)}
				</p>
			</div>
		</Panel>
	{/if}

	{#if calibrationJob && (status.calibration_status === 'queued' || status.calibration_status === 'running')}
		<Panel class="status-strip-panel accent-strip in-progress" padding="0.95rem 1rem">
			<div class="status-strip">
				<div class="section-copy-block">
					<div class="status-strip-signal" aria-live="polite">
						<span class="status-strip-beacon" aria-hidden="true"></span>
						<span>{calibrationSignal}</span>
					</div>
					<p class="eyebrow-copy">
						{calibrationJob.mode === 'full' ? 'Proof encode' : 'Calibration'}
					</p>
					<p class="status-strip-title">
						{calibrationJob.mode === 'full'
							? 'Representative-file proof encode is running'
							: 'Sample calibration is running'}
					</p>
					<p class="muted-copy">
						{calibrationJob.mode === 'full'
							? 'This full-file proof creates reviewable compare clips from a finished encode.'
							: 'This sampled run predicts full size quickly, then renders hotspot clips for review.'}
					</p>
				</div>
				<p class="status-strip-meta">
					Action {String(calibrationJob.action ?? '')} · Host {String(
						calibrationJob.host?.label ?? ''
					)}
				</p>
			</div>
		</Panel>
	{/if}

	<div class="workflow-stack">
		<Panel class="studio-panel" variant="accent">
			<div class="panel-stack">
				<SectionHead
					eyebrow="1. Shape the next draft"
					heading={sampleSetupHeading}
					lede={sampleSetupLede}
					size="section"
				/>
				<div class="studio-grid">
					<aside class="studio-sidebar">
						<div class="control-deck">
							<div class="run-readiness-card compact-status-card">
								<p class="eyebrow-copy">Run environment</p>
								<h3 class="run-card-title">{runReadinessHeading}</h3>
								<p class="muted-copy">{runReadinessCopy}</p>
								<div class="pill-row run-status-pills">
									<Pill label={sampleQueueLabel} variant="neutral" wide />
									<Pill label={encodeQueueLabel} variant="ghost" wide />
								</div>
								{#if encodeJobStatus && encodeJobStatus !== 'completed'}
									<div class={`folder-encode-card ${encodeJobTone}`.trim()}>
										<div class="folder-encode-header">
											<div>
												<p class="eyebrow-copy">Folder encode</p>
												<p class="folder-encode-title">{encodeJobHeadline}</p>
											</div>
											<span class={`folder-encode-chip ${encodeJobTone}`.trim()}>
												{encodeJobChipLabel}
											</span>
										</div>
										{#if encodeJobDetail}
											<p class="muted-copy folder-encode-detail">{encodeJobDetail}</p>
										{/if}
										{#if encodeJobNextActionCopy}
											<p class="inline-gate-copy folder-encode-next-step">
												<span class="eyebrow-copy">Next step</span>
												{encodeJobNextActionCopy}
											</p>
										{/if}
										{#if encodeJobFacts.length > 0}
											<div class="folder-encode-facts" aria-label="Folder encode telemetry">
												{#each encodeJobFacts as fact (`${fact.label}:${fact.value}`)}
													<div class="folder-encode-fact">
														<p>{fact.label}</p>
														<strong>{fact.value}</strong>
													</div>
												{/each}
											</div>
										{/if}
										{#if encodeJobMetaCopy}
											<p class="muted-copy folder-encode-meta">{encodeJobMetaCopy}</p>
										{/if}
									</div>
								{/if}
							</div>

							<div class="host-picker-shell">
								<div class="section-copy-block">
									<p class="eyebrow-copy">Host picker</p>
									<p class="muted-copy host-section-copy">
										Pick the machine for this representative pass. The chosen host stays visible in
										the action bar below.
									</p>
									<p class="muted-copy host-section-copy host-schedule-note">
										Representative samples can run now. Full-folder encodes still wait for the
										worker windows you see in Ops.
									</p>
								</div>
								<div class="sample-host-grid compact-host-grid">
									{#each sampleHostCards as hostCard (hostCard.key)}
										<button
											type="button"
											class:selected={selectedHost === hostCard.key}
											class:disabled={!hostCard.available}
											class:preferred={hostCard.preferred}
											class="sample-host-card compact-host-card"
											disabled={!hostCard.available}
											onclick={() => (selectedHost = hostCard.key)}
										>
											<span class="sample-host-badges">
												<span
													class={`sample-host-state ${hostCard.available ? 'ready' : 'unavailable'}`}
													>{hostCard.available ? 'Ready' : 'Unavailable'}</span
												>
												{#if hostCard.preferred}
													<span class="sample-host-badge">Recommended</span>
												{/if}
											</span>
											<span class="sample-host-label">{hostCard.label}</span>
											{#if compactScheduleCopy(hostCard.runtime)}
												<span class="muted-copy compact-host-meta"
													>{compactScheduleCopy(hostCard.runtime)}</span
												>
											{/if}
											<span class="muted-copy compact-host-meta secondary">
												{hostCapacityCopy(hostCard.runtime) ||
													hostCard.detail ||
													'No runtime detail'}
											</span>
											{#if hostCard.searchSummary}
												<span class="muted-copy compact-host-meta tertiary">
													{hostCard.searchSummary.label}
												</span>
											{/if}
										</button>
									{/each}
								</div>
								{#if !selectedHostRuntime && folder.sample_host_help_text}
									<p class="muted-copy host-selection-note">{folder.sample_host_help_text}</p>
								{/if}
							</div>

							<div class="run-setup-card action-card">
								<p class="eyebrow-copy">Next action</p>
								<div class="action-card-head">
									<h3 class="run-card-title">{nextActionHeading}</h3>
									<Pill label={nextActionStatus.label} variant={nextActionStatus.variant} />
								</div>
								<p class="muted-copy">{sampleActionSupportCopy}</p>
								<p class="selected-host-inline">
									<span class="selected-host-inline-value"
										>{selectedHostLabel || 'Choose a host above'}</span
									>
									{#if selectedHostScheduleCopy || selectedHostCapacityCopy}
										<span class="muted-copy selected-host-inline-meta">
											{[selectedHostScheduleCopy, selectedHostCapacityCopy]
												.filter(Boolean)
												.join(' · ')}
										</span>
									{:else if selectedHostDetail}
										<span class="muted-copy selected-host-inline-meta">{selectedHostDetail}</span>
									{/if}
								</p>
								{#if selectedHostSearchSummary}
									<p class="muted-copy host-search-callout">
										<span class="host-search-callout-label">{selectedHostSearchSummary.label}</span>
										{selectedHostSearchSummary.detail}
									</p>
								{/if}
								<div
									class="action-row primary-action-row compact-action-row single-primary-action-row"
								>
									<Button
										loading={actionState === 'sample' || actionState === 'preview'}
										disabled={!canRunPrimarySampleAction}
										onclick={runSample}>{confirmButtonLabel}</Button
									>
								</div>
								<p class="inline-gate-copy sample-action-copy action-inline-note">
									<span class="eyebrow-copy">Run gate</span>
									{#if !canRunSample}
										The sample button stays locked until the host is ready.
									{:else if sampleRunActive}
										Wait for the current sample to finish before starting another one.
									{:else if canRetrySavedSampleDraft}
										The last bench draft is already saved for this host, so this button reruns that
										test encode directly.
									{:else if retryableCalibrationRefreshBlockedByEmptyNote}
										Add a note before refreshing this stopped sample draft. The tuner cannot build a
										new draft from an empty request.
									{:else if retryableCalibrationNeedsRefresh}
										The saved sample draft no longer matches the current note or host. Refresh it
										from the bench chat below before queueing the next run.
									{:else if !pendingProposal}
										Use the bench chat below to draft the next sample. This button only queues a
										ready draft.
									{:else if pendingProposalNeedsRefresh}
										The note or host changed. Refresh the draft from the bench chat below so the
										queued sample matches the latest request.
									{:else if pendingProposalCanQueue}
										The current draft is aligned with the selected host and can queue when you
										confirm it.
									{:else}
										Adjust the draft or ask the bench again before queueing the sample.
									{/if}
								</p>
								{#if hasClearableTuningState}
									<details class="danger-disclosure">
										<summary>
											<span>Advanced reset</span>
											<span class="summary-hint">Danger</span>
										</summary>
										<div class="danger-disclosure-body">
											<p class="inline-gate-copy destructive-action-copy">
												<span class="eyebrow-copy">Destructive</span>
												Removes the tuning thread, retained sample context, and sample artifacts for this
												folder only.
											</p>
											<div class="action-row utility-action-row">
												<Button
													variant="danger"
													loading={actionState === 'clear'}
													disabled={sampleRunActive}
													onclick={clearTuningState}>Clear thread + sample artifacts</Button
												>
											</div>
										</div>
									</details>
								{/if}
							</div>
						</div>
					</aside>

					<div class="studio-main">
						<div class="bench-workspace-shell">
							<section class="bench-chat-shell">
								<div class="thread-inline-head bench-chat-head">
									<div class="section-copy-block">
										<p class="eyebrow-copy">Bench chat</p>
										<p class="muted-copy thread-history-copy">
											Talk to the bench in one running conversation. Draft requests, in-progress
											replies, and saved guidance all stay here.
										</p>
									</div>
									<span class="thread-history-meta">{calibrationThreadCountLabel}</span>
								</div>
								<div
									bind:this={threadScrollViewport}
									class="calibration-thread-shell history-thread-shell thread-scroll-list bench-chat-log"
								>
									{#if calibrationThreadSessions.length}
										<div class="calibration-thread-list">
											{#each calibrationThreadSessions as session (session.key)}
												{#if session.note}
													<article class="thread-turn operator-turn">
														<p class="thread-role">You</p>
														<p class="thread-copy">{session.note}</p>
													</article>
												{/if}
												{#if session.summary || session.runSummary}
													<article class="thread-turn system-turn">
														<p class="thread-role">Bench</p>
														{#if session.requestResponse}
															<p class="thread-copy">{session.requestResponse}</p>
														{/if}
														{#if session.summary}
															<p class="thread-support">{session.summary}</p>
														{/if}
														{#if session.feasibilityNote}
															<p class="thread-support">{session.feasibilityNote}</p>
														{/if}
														{#if session.diagnosis}
															<p class="thread-support">{session.diagnosis}</p>
														{/if}
														{#if session.suggestedFollowUp}
															<p class="thread-support">
																<span class="eyebrow-copy">Suggested follow-up</span>
																{session.suggestedFollowUp}
															</p>
														{/if}
														{#if session.runSummary}
															<div class="thread-readout">
																<p class="eyebrow-copy">Last run readout</p>
																<p class="thread-copy">{session.runSummary}</p>
																{#if session.runNextStep}
																	<p class="thread-support">{session.runNextStep}</p>
																{/if}
															</div>
														{/if}
														<div class="pill-row thread-chip-row">
															{#if session.requestDisposition}
																<Pill
																	label={`Request ${titleCase(String(session.requestDisposition).replace(/_/g, ' '))}`}
																	variant="ghost"
																/>
															{/if}
															{#if session.confidence}
																<Pill
																	label={`Tuner confidence ${titleCase(String(session.confidence))}`}
																	variant="ghost"
																/>
															{/if}
															{#if session.isCurrent && operatorRequestLabel}
																<Pill
																	label={`Experiment ${operatorRequestLabel}`}
																	variant="default"
																/>
															{/if}
															{#if session.isCurrent && runVerdictOutcomeCopy}
																<Pill
																	label={runVerdictOutcomeCopy}
																	variant={runVerdictOutcomeVariant}
																/>
															{/if}
															{#if session.isCurrent && session.runConfidence}
																<Pill
																	label={`Run confidence ${titleCase(String(session.runConfidence))}`}
																	variant="ghost"
																/>
															{/if}
														</div>
													</article>
												{/if}
											{/each}
										</div>
									{:else}
										<div class="bench-chat-empty">
											<p class="thread-role">Bench</p>
											<p class="thread-copy">
												Ask for the first draft to start the bench conversation.
											</p>
											<p class="thread-support">
												Your latest request, the bench reply, and any pending draft state will all
												stay in this one thread.
											</p>
										</div>
									{/if}
								</div>
								<div class="field-block note-panel bench-chat-composer">
									<div class="section-copy-block">
										<span class="eyebrow-copy">{noteFieldLabel}</span>
										<span class="muted-copy note-lede">{noteFieldLede}</span>
									</div>
									<textarea
										bind:value={note}
										rows="3"
										onkeydown={handleNoteKeydown}
										placeholder={notePlaceholder}
									></textarea>
									{#if reviewConversationCopy || approvedSeasonShortcut}
										<div class="bench-chat-context">
											{#if reviewConversationCopy}
												<p class="inline-gate-copy note-review-copy">
													<span class="eyebrow-copy">Bench context</span>
													{reviewConversationCopy}
												</p>
											{/if}
											{#if approvedSeasonShortcut}
												<p class="inline-gate-copy note-review-copy approved-season-shortcut-copy">
													<span class="eyebrow-copy">Approved season memory</span>
													{approvedSeasonShortcut.count} approved {approvedSeasonShortcut.count ===
													1
														? 'season is'
														: 'seasons are'} already saved for {approvedSeasonShortcut.root_label}.
													Use the shortcut to ask the bench to match {approvedSeasonShortcutSummary}.
												</p>
											{/if}
										</div>
									{/if}
									<div class="note-composer-footer bench-chat-footer">
										<p class="muted-copy note-submit-hint">{noteSubmitHint}</p>
										<div class="action-row note-composer-actions">
											{#if approvedSeasonShortcut}
												<Button
													variant="ghost"
													loading={actionState === 'preview'}
													disabled={!canUseApprovedSeasonShortcut}
													onclick={previewApprovedSeasonDraft}>Match approved seasons</Button
												>
											{/if}
											<Button
												loading={actionState === 'preview'}
												disabled={!canRequestBenchDraft}
												onclick={handlePreviewSampleDraftClick}>{previewButtonLabel}</Button
											>
										</div>
									</div>
								</div>
							</section>

							{#if pendingProposal}
								<div
									class:stale={pendingProposalNeedsRefresh}
									class="proposal-shell diagnosis-shell"
								>
									<div class="proposal-head">
										<div class="section-copy-block">
											<p class="eyebrow-copy">Current bench draft</p>
											<h4 class="proposal-title">{proposalDraftHeading}</h4>
											{#if pendingProposalSignal}
												<p class="muted-copy">{pendingProposalSignal}</p>
											{/if}
										</div>
										<div class="pill-row proposal-pill-row">
											{#if pendingOperatorRequestLabel}
												<Pill label={`Heard ${pendingOperatorRequestLabel}`} variant="default" />
											{/if}
											{#if pendingProposal?.request_disposition}
												<Pill
													label={`Request ${titleCase(String(pendingProposal.request_disposition).replace(/_/g, ' '))}`}
													variant="ghost"
												/>
											{/if}
											{#if pendingProposalSelfCheckLabel}
												<Pill
													label={pendingProposalSelfCheckLabel}
													variant={pendingProposalSelfCheckVariant}
												/>
											{/if}
											{#if pendingProposal?.confidence}
												<Pill
													label={`Bench confidence ${titleCase(String(pendingProposal.confidence))}`}
													variant="ghost"
												/>
											{/if}
										</div>
									</div>

									<div class="diagnosis-grid">
										<section class="proposal-workbench-card diagnosis-copy-card">
											<p class="eyebrow-copy">Latest read</p>
											{#if pendingProposal?.request_response || pendingProposal?.summary}
												<p class="thread-copy">
													{pendingProposal.request_response || pendingProposal.summary}
												</p>
											{/if}
											{#if pendingProposal?.request_response && pendingProposal.request_response !== pendingProposal.summary && pendingProposal?.summary}
												<p class="thread-support">{pendingProposal.summary}</p>
											{/if}
											{#if pendingProposal?.feasibility_note}
												<p class="thread-support">{pendingProposal.feasibility_note}</p>
											{/if}
											{#if pendingProposal?.diagnosis}
												<p class="thread-support">{pendingProposal.diagnosis}</p>
											{/if}
											{#if pendingProposal?.suggested_follow_up}
												<p class="inline-gate-copy proposal-warning-copy">
													<span class="eyebrow-copy">Suggested follow-up</span>
													{pendingProposal.suggested_follow_up}
												</p>
											{/if}
											{#if previewSubmission}
												<p class="inline-gate-copy proposal-warning-copy">
													<span class="eyebrow-copy">Drafting now</span>
													Bench received {previewSubmission.note || 'your latest request'} and is preparing
													a fresh reply.
												</p>
											{/if}
										</section>

										{#if proposalWorkbenchSections.length}
											<section class="proposal-workbench-card">
												<p class="eyebrow-copy">Changed in this draft</p>
												{#each proposalWorkbenchSections as section (section.title)}
													<div class="proposal-change-section">
														<p class="proposal-section-title">{section.title}</p>
														<div class="proposal-change-grid workbench-change-grid">
															{#each section.rows as row (row.path)}
																<div class="proposal-change-card">
																	<p class="eyebrow-copy">{row.label}</p>
																	<p class="proposal-change-value">{row.draft}</p>
																	<p class="proposal-change-previous">From {row.current}</p>
																</div>
															{/each}
														</div>
													</div>
												{/each}
											</section>
										{/if}

										<section class="proposal-workbench-card">
											<p class="eyebrow-copy">Run posture</p>
											{#if !proposalWorkbenchSections.length}
												<p class="muted-copy">
													This draft keeps the current policy. Review the evidence below before
													deciding whether to ask again.
												</p>
											{/if}
											{#if pendingProposalSelfCheck.summary}
												<p class="inline-gate-copy proposal-warning-copy">
													<span class="eyebrow-copy">Before you run it</span>
													{pendingProposalSelfCheck.summary}
												</p>
											{/if}
											{#if pendingProposalSelfCheck.issues?.length}
												<ul class="proposal-issue-list">
													{#each pendingProposalSelfCheck.issues ?? [] as issue (issue)}
														<li>{issue}</li>
													{/each}
												</ul>
											{/if}
											{#if pendingProposalNeedsRefresh}
												<p class="inline-gate-copy proposal-warning-copy">
													<span class="eyebrow-copy">Refresh needed</span>
													This draft no longer matches the current note or selected host.
												</p>
											{/if}
										</section>
									</div>

									<details class="proposal-trace-shell diagnosis-details">
										<summary>Bench context and trace</summary>
										<div class="proposal-workbench-grid diagnosis-disclosure-grid">
											<section class="proposal-workbench-card">
												<p class="eyebrow-copy">Bench saw</p>
												{#if pendingProposalTraceContext?.goal}
													<p class="muted-copy">{pendingProposalTraceContext.goal}</p>
												{/if}
												{#if workbenchContextStats.length}
													<div class="proposal-context-grid">
														{#each workbenchContextStats as stat (stat.label)}
															<div class="proposal-context-card">
																<p class="eyebrow-copy">{stat.label}</p>
																<p class="proposal-change-value">{stat.value}</p>
																{#if stat.detail}
																	<p class="muted-copy">{stat.detail}</p>
																{/if}
															</div>
														{/each}
													</div>
												{/if}
												{#if pendingProposal?.evidence_checked?.length}
													<div class="pill-row">
														{#each pendingProposal.evidence_checked ?? [] as evidence (evidence)}
															<Pill label={evidence} variant="neutral" />
														{/each}
													</div>
												{/if}
											</section>

											{#if workbenchMemoryEntries.length}
												<section class="proposal-workbench-card">
													<p class="eyebrow-copy">Retrieved memory</p>
													<div class="proposal-memory-shell">
														{#each workbenchMemoryEntries as entry (entry.title + entry.summary)}
															<div class="proposal-memory-card">
																<p class="proposal-memory-title">
																	{entry.title || 'Prior approved tuning note'}
																</p>
																{#if entry.summary}
																	<p class="muted-copy">{entry.summary}</p>
																{/if}
																{#if entry.excerpt}
																	<p class="thread-support">{entry.excerpt}</p>
																{/if}
															</div>
														{/each}
													</div>
												</section>
											{/if}

											<section class="proposal-workbench-card proposal-trace-card-shell">
												<p class="eyebrow-copy">Advanced trace</p>
												{#if pendingProposalTrace?.prompt_version}
													<p class="muted-copy">
														<strong>Prompt version:</strong>
														{pendingProposalTrace.prompt_version}
													</p>
												{/if}
												{#if workbenchToolbeltRows.length}
													{#each workbenchToolbeltRows as row (row.label)}
														<p class="muted-copy"><strong>{row.label}:</strong> {row.value}</p>
													{/each}
												{/if}
												{#if proposalSteadyWorkbenchRows.length}
													<p class="muted-copy"><strong>Unchanged settings</strong></p>
													{#each proposalSteadyWorkbenchRows as row (row.path)}
														<p class="muted-copy"><strong>{row.label}:</strong> {row.current}</p>
													{/each}
												{/if}
												{#if pendingProposalRawResponse}
													<pre class="proposal-trace-raw">{pendingProposalRawResponse}</pre>
												{/if}
											</section>
										</div>
									</details>
								</div>
							{:else if currentThreadSession}
								<div class="proposal-shell diagnosis-shell archived-diagnosis-shell">
									<p class="eyebrow-copy">Bench history</p>
									<h4 class="proposal-title">{calibrationThreadCountLabel} saved in the thread</h4>
									{#if archivedThreadHeadline}
										<p class="thread-copy">{archivedThreadHeadline}</p>
									{/if}
									{#if archivedThreadDetail}
										<p class="thread-support">{archivedThreadDetail}</p>
									{/if}
									<p class="thread-support">
										{threadHistorySummaryCopy} Scroll the thread above when you want to reopen earlier
										notes, compare bench replies, or rewrite the next draft request.
									</p>
								</div>
							{/if}
						</div>
					</div>
				</div>
			</div>
		</Panel>

		<Panel class="review-panel">
			<div class="panel-stack">
				<SectionHead
					eyebrow="2. Review the sample"
					heading="Watch the proof clips, then decide whether this draft is ready"
					lede="Review the synced clips, check the meaningful changes, and decide whether this draft is ready to save, queue, validate, or promote without leaving the page."
					size="section"
				/>
				<div class="review-grid review-grid-balanced">
					<div class="review-main-column">
						<div class="review-block review-evidence-block">
							<p class="eyebrow-copy">Evidence deck</p>
							{#if reviewPairs.length && selectedReviewPair}
								<div class="review-player-shell-block">
									<div class="review-player-head">
										<div>
											<h3 class="review-block-title">Synced compare clips</h3>
											<p class="muted-copy">
												Play either clip and the other follows. Use these moments to decide whether
												the current draft is good enough to keep.
											</p>
										</div>
										<div class="review-head-actions">
											<div class="pill-row review-progress-pills">
												<Pill
													label={reviewMomentProgressCopy}
													variant={seenReviewMomentCount >= reviewMomentCount &&
													reviewMomentCount > 0
														? 'ok'
														: 'neutral'}
												/>
												{#if selectedReviewMomentCopy}
													<Pill label={selectedReviewMomentCopy} variant="ghost" />
												{/if}
											</div>
											<div class="action-row review-sequence-actions">
												<Button
													variant="ghost"
													disabled={!hasPreviousReviewMoment}
													onclick={() => void stepReviewMoment(-1)}>Previous moment</Button
												>
												<Button
													variant="ghost"
													disabled={!hasNextReviewMoment}
													onclick={() => void stepReviewMoment(1)}>Next moment</Button
												>
											</div>
											{#if hasFullCompareDownload}
												<Button
													variant="secondary"
													onclick={() => {
														markReviewMomentSeen(selectedReviewPairIndex);
														downloadReviewCompareVideo();
													}}
												>
													Download full side-by-side compare
												</Button>
											{/if}
										</div>
									</div>
									<div class="review-selector-row" role="tablist" aria-label="Review moments">
										{#each reviewPairs as pair, index (`review-pair-${index}`)}
											<button
												type="button"
												class:selected={selectedReviewPairIndex === index}
												class:seen={seenReviewMomentSet.has(reviewMomentPills[index]?.key ?? '')}
												class="review-selector-chip"
												onclick={() => void selectReviewMoment(index)}
											>
												<span class="eyebrow-copy"
													>{reviewMomentPills[index]?.title ?? `Review clip ${index + 1}`}</span
												>
												<strong
													>{reviewMomentPills[index]?.timestamp ??
														formatTimestamp(Number(pair.timestamp_seconds ?? 0))}</strong
												>
												<span
													>{seenReviewMomentSet.has(reviewMomentPills[index]?.key ?? '')
														? 'Reviewed'
														: (reviewMomentPills[index]?.detail ??
															`${Math.round(Number(pair.duration_seconds ?? 0))}s`)}</span
												>
											</button>
										{/each}
									</div>
									<div class="review-player-stack review-player-columns">
										<div class="review-player-panel">
											<div class="review-player-meta">
												<p class="eyebrow-copy">Source reference</p>
												<p class="muted-copy">
													Reference clip from the original representative file.
												</p>
											</div>
											<video
												bind:this={sourceReviewVideo}
												src={String(selectedReviewPair.source_clip?.path ?? '')}
												controls
												muted
												playsinline
												preload="auto"
												oncanplay={maybeResumeReviewPlayback}
												onplay={() => {
													markReviewMomentSeen(selectedReviewPairIndex);
													syncReviewPlayers('source', 'play');
												}}
												onpause={() => syncReviewPlayers('source', 'pause')}
												onseeked={() => syncReviewPlayers('source', 'seek')}
												onratechange={() => syncReviewPlayers('source', 'rate')}
											></video>
										</div>
										<div class="review-player-panel draft-review-player-panel">
											<div class="review-player-meta">
												<p class="eyebrow-copy">Draft preview</p>
												<p class="muted-copy">
													Preview clip from the AV1 draft you are deciding on now.
												</p>
											</div>
											<video
												bind:this={draftReviewVideo}
												src={String(selectedReviewPair.preview_clip?.path ?? '')}
												controls
												muted
												playsinline
												preload="auto"
												oncanplay={maybeResumeReviewPlayback}
												onplay={() => {
													markReviewMomentSeen(selectedReviewPairIndex);
													syncReviewPlayers('draft', 'play');
												}}
												onpause={() => syncReviewPlayers('draft', 'pause')}
												onseeked={() => syncReviewPlayers('draft', 'seek')}
												onratechange={() => syncReviewPlayers('draft', 'rate')}
											></video>
										</div>
									</div>
								</div>
							{:else if calibration.review_media_ready}
								<div class="legacy-review-note">
									<p class="eyebrow-copy">In-browser A/B playback unavailable</p>
									<p class="muted-copy">
										This draft still has review artifacts, but it predates the paired browser player
										media. Run a fresh sample if you want synced playback here before approving it.
									</p>
								</div>
							{/if}

							{#if visibleReviewArtifacts.length}
								<div class="review-pack-shell review-pack-inline-shell">
									<div class="section-copy-block">
										<p class="eyebrow-copy">Bench review pack</p>
										<h4 class="proposal-title">{visibleReviewPackHeading}</h4>
										<p class="muted-copy">{visibleReviewPackCopy}</p>
									</div>
									{#if visibleReviewAudioSummary}
										<p class="inline-gate-copy proposal-warning-copy">
											<span class="eyebrow-copy">Audio context</span>
											{visibleReviewAudioSummary}
										</p>
									{/if}
									{#if visibleAudioReviewArtifacts.length}
										<div class="review-pack-section">
											<div class="section-copy-block review-pack-section-copy">
												<p class="eyebrow-copy">Audio graph</p>
												<p class="muted-copy">
													The spectrogram compare from this bench draft is surfaced separately so it
													doesn't get buried after the video frames.
												</p>
											</div>
											<div class="review-pack-grid review-pack-audio-grid">
												{#each visibleAudioReviewArtifacts as artifact (artifact.imageUrl || artifact.label)}
													<article class="review-pack-card">
														<button
															type="button"
															class="review-pack-link"
															onclick={() => openReviewAsset(artifact.imageUrl)}
														>
															<img
																src={artifact.imageUrl}
																alt={artifact.label || 'Bench review artifact'}
																loading="lazy"
															/>
														</button>
														<div class="review-pack-copy">
															<p class="proposal-memory-title">{artifact.label || 'Review artifact'}</p>
															{#if artifact.detail}
																<p class="thread-support">{artifact.detail}</p>
															{/if}
														</div>
													</article>
												{/each}
											</div>
										</div>
									{/if}
									{#if visibleVisualReviewArtifacts.length}
										<div class="review-pack-section">
											<div class="section-copy-block review-pack-section-copy">
												<p class="eyebrow-copy">Image review</p>
												<p class="muted-copy">
													Compare timelines and frame sheets captured from the same retained review
													moments.
												</p>
											</div>
											<div class="review-pack-grid">
												{#each visibleVisualReviewArtifacts as artifact (artifact.imageUrl || artifact.label)}
													<article class="review-pack-card">
														<button
															type="button"
															class="review-pack-link"
															onclick={() => openReviewAsset(artifact.imageUrl)}
														>
															<img
																src={artifact.imageUrl}
																alt={artifact.label || 'Bench review artifact'}
																loading="lazy"
															/>
														</button>
														<div class="review-pack-copy">
															<p class="proposal-memory-title">{artifact.label || 'Review artifact'}</p>
															{#if artifact.detail}
																<p class="thread-support">{artifact.detail}</p>
															{/if}
														</div>
													</article>
												{/each}
											</div>
										</div>
									{/if}
								</div>
							{/if}
						</div>

						<div class="review-block review-diff-block">
							<p class="eyebrow-copy">Changed-first diff</p>
							<div class="comparison-stack">
								<div class="comparison-group">
									<div class="comparison-group-head">
										<h3 class="comparison-group-title">Source media to draft output</h3>
									</div>
									<div class="comparison-table-head" aria-hidden="true">
										<span>Current</span>
										<span>Draft</span>
									</div>
									<div class="comparison-list compact-list">
										{#each streamComparisonRows as row (row.label)}
											<article class="comparison-row compact-row changed-row">
												<div class="comparison-row-head">
													<p class="comparison-label">{row.label}</p>
													<span class="comparison-status-pill changed-pill">Changed</span>
												</div>
												<div class="comparison-values">
													<div class="comparison-value-card">
														<p class="comparison-copy">{row.current.headline}</p>
														{#if row.current.detail}
															<p class="comparison-subcopy">{row.current.detail}</p>
														{/if}
													</div>
													<div class="comparison-arrow" aria-hidden="true">→</div>
													<div class="comparison-value-card draft-value-card">
														<p class="comparison-copy">{row.draft.headline}</p>
														{#if row.draft.detail}
															<p class="comparison-subcopy">{row.draft.detail}</p>
														{/if}
													</div>
												</div>
											</article>
										{/each}
									</div>
								</div>

								<div class="comparison-group">
									<div class="comparison-group-head">
										<h3 class="comparison-group-title">Current policy to draft policy</h3>
									</div>
									<div class="comparison-table-head" aria-hidden="true">
										<span>Current</span>
										<span>Draft</span>
									</div>
									<div class="comparison-list compact-list">
										{#each changedPolicyRows as row (row.label)}
											<article class="comparison-row compact-row changed-row">
												<div class="comparison-row-head">
													<p class="comparison-label">{row.label}</p>
													<span class="comparison-status-pill changed-pill">Changed</span>
												</div>
												<div class="comparison-values">
													<div class="comparison-value-card">
														<p class="comparison-copy">{row.current.headline}</p>
														{#if row.current.detail}
															<p class="comparison-subcopy">{row.current.detail}</p>
														{/if}
													</div>
													<div class="comparison-arrow" aria-hidden="true">→</div>
													<div class="comparison-value-card draft-value-card">
														<p class="comparison-copy">{row.draft.headline}</p>
														{#if row.draft.detail}
															<p class="comparison-subcopy">{row.draft.detail}</p>
														{/if}
													</div>
												</div>
											</article>
										{/each}
									</div>
									{#if steadyPolicyRows.length}
										<details class="steady-details-shell">
											<summary>Show unchanged settings</summary>
											<div class="steady-summary-block">
												<div class="steady-summary-list">
													{#each steadyPolicyRows as row (row.label)}
														<div class="steady-summary-row">
															<p class="steady-summary-label">{row.label}</p>
															<p class="steady-summary-value">{row.value}</p>
														</div>
													{/each}
												</div>
											</div>
										</details>
									{/if}
								</div>
							</div>
						</div>
					</div>

					<aside class="review-side-column review-approval-column">
						<div class="review-block approval-block">
							<p class="eyebrow-copy">{reviewGateEyebrow}</p>
							<h3 class="review-block-title">{reviewGateHeading}</h3>
							<p class="muted-copy">{reviewGateDetail}</p>
							<p class="inline-gate-copy review-action-copy">
								<span class="eyebrow-copy">Review media</span>
								{reviewMediaStatusCopy}
							</p>
							{#if reviewDecisionProgressCopy}
								<p class="inline-gate-copy review-action-copy">
									<span class="eyebrow-copy">Review progress</span>
									{reviewDecisionProgressCopy}
								</p>
							{/if}
							{#if reviewBenchSummary.available}
								<div class="review-bench-summary-card">
									<p class="eyebrow-copy">Latest bench read</p>
									{#if reviewBenchSummary.requestLabel}
										<div class="pill-row review-bench-pill-row">
											<Pill
												label={`Experiment ${reviewBenchSummary.requestLabel}`}
												variant="ghost"
											/>
										</div>
									{/if}
									{#if reviewBenchSummary.headline}
										<p class="thread-copy review-bench-headline">{reviewBenchSummary.headline}</p>
									{/if}
									{#if reviewBenchSummary.detail}
										<p class="thread-support">{reviewBenchSummary.detail}</p>
									{/if}
								</div>
							{/if}
							{#if highImpactPolicyRows.length}
								<p class="inline-gate-copy proposal-warning-copy impact-warning-copy">
									<span class="eyebrow-copy">High-impact changes</span>
									This draft makes larger-than-usual moves to {highImpactPolicyLabels}. {#if highImpactApprovalGate.armed}Read
										the diff one last time, then press Confirm High-Impact Approval to save and
										queue the folder.{:else}Read the diff before you approve and queue the full
										folder.{/if}
								</p>
							{/if}
							{#if reviewGateStatus === 'accepted'}
								<p class="inline-gate-copy review-action-copy accepted-next-step-copy">
									<span class="eyebrow-copy">Next move</span>
									The saved draft is locked in. Mediaforce queues approved folders automatically, so only
									retry the folder encode here if a prior run stopped early.
								</p>
							{/if}
							<div class="action-row review-action-row stacked-review-actions">
								{#if reviewGateStatus === 'accepted'}
									<Pill label="Policy saved" variant="ok" />
								{:else}
									<Button
										variant="secondary"
										loading={actionState === 'save'}
										disabled={approvalButtonDisabled}
										onclick={saveProfile}>{approvalButtonLabel}</Button
									>
								{/if}
								{#if queueActionVisible || !reviewGate.can_confirm_full}
									<div class="queue-action-block">
										{#if queueActionVisible}
											<Button
												variant={reviewGate.can_confirm_full ? 'primary' : 'ghost'}
												loading={actionState === 'encode'}
												disabled={!reviewGate.can_confirm_full}
												onclick={queueEncode}>{queueEncodeButtonLabel}</Button
											>
										{/if}
										{#if !reviewGate.can_confirm_full}
											<p class="inline-gate-copy">
												<span class="eyebrow-copy"
													>{reviewGateNextActionLabel || queueGateLabel}</span
												>
												{String(reviewGate.message ?? 'Run or review a calibration to continue.')}
											</p>
										{/if}
									</div>
								{/if}
							</div>
						</div>

						{#if deliveryPanelVisible}
							<div class="review-block approval-block deliver-block">
								<p class="eyebrow-copy">{deliverEyebrow}</p>
								<h3 class="review-block-title">{deliverHeading}</h3>
								<p class="muted-copy">
									Check the encoded files before anything moves, then promote the validated set into
									the library.
								</p>
								<div class="pill-row deliver-pill-row">
									{#if encodedOutputCount > 0}
										<Pill label={`${encodedOutputCount} encoded`} variant="default" />
									{/if}
									{#if validatedOutputCount > 0}
										<Pill label={`${validatedOutputCount} validated`} variant="ok" />
									{/if}
									{#if promotedOutputCount > 0}
										<Pill label={`${promotedOutputCount} promoted`} variant="neutral" />
									{/if}
								</div>
								<div class="action-row review-action-row stacked-review-actions">
									<Button
										variant={validateButtonVariant}
										loading={actionState === 'validate'}
										disabled={stagedOutputCount === 0 || deliveryBlockedByEncode}
										onclick={validateOutputs}>{validateButtonLabel}</Button
									>
									<Button
										variant={promoteButtonVariant}
										loading={actionState === 'promote'}
										disabled={validatedOutputCount === 0 || deliveryBlockedByEncode}
										onclick={promoteOutputs}>{promoteButtonLabel}</Button
									>
								</div>
								{#if deliveryBlockedByEncode}
									<p class="inline-gate-copy">
										<span class="eyebrow-copy">Waiting for encode</span>
										Finish the active folder encode before validating or promoting staged files.
									</p>
								{:else if validatedOutputCount === 0}
									<p class="inline-gate-copy">
										<span class="eyebrow-copy">Validation required</span>
										Validate the staged files before anything moves into the library.
									</p>
								{:else}
									<p class="inline-gate-copy">
										<span class="eyebrow-copy">Promotion archive</span>
										Moves validated files into the library and archives the original sources.
									</p>
								{/if}
								{#if encodedOutputCount > 0 && validatedOutputCount > 0}
									<p class="inline-gate-copy">
										<span class="eyebrow-copy">Partial results</span>
										{encodedOutputCount} file{encodedOutputCount === 1 ? '' : 's'} still need validation.
										You can promote the validated set now or validate everything first.
									</p>
								{/if}
							</div>
						{/if}

						{#if predictedOutputSizeBytes > 0}
							<div class="review-block estimate-block compact-estimate-block">
								<p class="eyebrow-copy">Current draft estimate</p>
								<h3 class="review-block-title">
									{formatGiB(predictedOutputSizeBytes, 2)} predicted output
								</h3>
								<p class="muted-copy">
									{reviewGateStatus === 'accepted'
										? 'Representative-file estimate for the saved draft that will drive the next folder encode.'
										: 'Representative-file estimate for the current draft before you approve this folder.'}
								</p>
								<div class="pill-row">
									<Pill
										label={`Source ${formatGiB(Number(sampleItem.source_size_bytes ?? 0), 2)}`}
										variant="neutral"
									/>
									<Pill label={`Output ${formatGiB(predictedOutputSizeBytes, 2)}`} variant="ok" />
									{#if predictedOutputPercentCopy !== 'n/a'}
										<Pill label={`Relative size ${predictedOutputPercentCopy}`} variant="default" />
									{/if}
									{#if predictedEncodeTimeCopy}
										<Pill label={`Est encode ${predictedEncodeTimeCopy}`} variant="neutral" />
									{/if}
									{#if predictedQualityCopy}
										<Pill label={`Predicted ${predictedQualityCopy}`} variant="neutral" />
									{/if}
									{#if calibrationSampleResult?.chosen_crf != null}
										<Pill
											label={`CRF ${Number(calibrationSampleResult.chosen_crf).toFixed(1)}`}
											variant="ghost"
										/>
									{/if}
								</div>
							</div>
						{/if}

						<details class="reference-disclosure" open={false}>
							<summary
								><span>Representative file and folder context</span><span class="summary-hint"
									>Inspect</span
								></summary
							>
							<div class="reference-disclosure-grid">
								<div class="review-block representative-block compact-reference-block">
									<p class="eyebrow-copy">Representative file</p>
									<div
										class="representative-path-shell"
										title={representativePath || 'No representative file yet'}
									>
										<h3 class="review-block-title representative-file-name">
											{#each representativeFilenameTokens as token, index (`${index}-${token}`)}
												<span>{token}</span><wbr />
											{/each}
										</h3>
										<div class="representative-meta-row">
											{#if representativeExtension}
												<Pill label={representativeExtension} variant="neutral" />
											{/if}
											{#if representativeResolution}
												<Pill label={representativeResolution} variant="neutral" />
											{/if}
											<Pill
												label={`Size ${formatGiB(Number(sampleItem.source_size_bytes ?? 0), 2)}`}
												variant="neutral"
											/>
											{#if representativeVideoBitrate}
												<Pill label={`Video ${representativeVideoBitrate}`} variant="neutral" />
											{/if}
											<Pill
												label={`Length ${formatTimestamp(Number(sampleItem.duration_seconds ?? 0))}`}
												variant="neutral"
											/>
										</div>
									</div>
								</div>
								<div class="review-block folder-snapshot-block compact-reference-block">
									<p class="eyebrow-copy">Folder state</p>
									<div class="snapshot-grid compact-snapshot-grid">
										{#each folderSnapshotItems as item (item.label)}
											<div class="fact-card compact-fact-card">
												<p class="eyebrow-copy">{item.label}</p>
												<p class="fact-value">{item.value}</p>
												{#if item.detail}
													<p class="muted-copy snapshot-detail-copy">{item.detail}</p>
												{/if}
											</div>
										{/each}
									</div>
									{#if reviewMomentPills.length}
										<div class="pill-row hotspot-pill-row">
											{#each reviewMomentPills as pill (pill.key)}
												<Pill label={`${pill.title} · ${pill.detail}`} variant="neutral" />
											{/each}
										</div>
									{/if}
								</div>
							</div>
						</details>

						<details class="reference-disclosure host-detail-disclosure" open={false}>
							<summary><span>All host lanes</span><span class="summary-hint">Inspect</span></summary
							>
							<div class="host-grid">
								{#each rankedHosts as host (host.key)}
									<HostCard {host} folderPrefix={folder.prefix} />
								{/each}
							</div>
						</details>
					</aside>
				</div>
			</div>
		</Panel>
	</div>
</div>

<style>
	.page-stack,
	.panel-stack {
		display: grid;
		gap: var(--space-3);
	}

	.folder-header-grid {
		display: grid;
		grid-template-columns: minmax(0, 1.05fr) minmax(280px, 0.95fr);
		gap: var(--space-4);
		align-items: start;
	}

	.workflow-stage-strip {
		display: grid;
		grid-template-columns: repeat(4, minmax(0, 1fr));
		gap: 0.75rem;
		margin-top: 1rem;
	}

	.workflow-stage-card {
		display: grid;
		gap: 0.2rem;
		padding: 0.85rem 0.95rem;
		border-radius: var(--radius-md);
		background: rgba(255, 255, 255, 0.68);
		border: 1px solid rgba(23, 35, 31, 0.08);
		box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.5);
	}

	.workflow-stage-card.current {
		background: linear-gradient(180deg, rgba(15, 118, 110, 0.14), rgba(255, 255, 255, 0.78));
		border-color: rgba(15, 118, 110, 0.22);
		box-shadow:
			inset 0 1px 0 rgba(255, 255, 255, 0.56),
			0 14px 24px rgba(15, 118, 110, 0.06);
	}

	.workflow-stage-card.done {
		background: linear-gradient(180deg, rgba(47, 107, 62, 0.14), rgba(255, 255, 255, 0.78));
		border-color: rgba(47, 107, 62, 0.22);
	}

	.workflow-stage-card.pending {
		border-style: dashed;
		background: rgba(255, 255, 255, 0.54);
	}

	.workflow-stage-label,
	.workflow-stage-detail {
		margin: 0;
	}

	.workflow-stage-label {
		font-size: 0.74rem;
		font-weight: 800;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--accent-deep);
	}

	.workflow-stage-detail {
		font-size: 0.94rem;
		font-weight: 700;
		line-height: 1.3;
		color: var(--ink);
	}

	.folder-header-side {
		display: grid;
		gap: var(--space-3);
	}

	.breadcrumb-row {
		display: flex;
		gap: 0.55rem;
		align-items: center;
		font-size: 0.92rem;
		color: var(--ink-soft);
		flex-wrap: wrap;
	}

	.breadcrumb-row a {
		color: var(--accent-deep);
		font-weight: 700;
	}

	.workflow-stack {
		display: grid;
		gap: var(--space-4);
	}

	.status-strip-panel {
		background: rgba(255, 255, 255, 0.82);
	}

	.status-strip-panel.in-progress {
		position: relative;
		overflow: hidden;
	}

	.status-strip-panel.in-progress::after {
		content: '';
		position: absolute;
		inset: 0;
		pointer-events: none;
		background: linear-gradient(
			110deg,
			rgba(255, 255, 255, 0) 0%,
			rgba(255, 255, 255, 0.45) 18%,
			rgba(255, 255, 255, 0) 36%
		);
		transform: translateX(-150%);
		animation: status-strip-sheen 2.8s ease-in-out infinite;
	}

	.accent-strip {
		background: rgba(241, 252, 248, 0.88);
	}

	.status-strip {
		display: flex;
		justify-content: space-between;
		gap: 1rem;
		align-items: end;
		flex-wrap: wrap;
	}

	.status-strip-title {
		margin: 0;
		font-size: 1rem;
		font-weight: 700;
		line-height: 1.35;
	}

	.status-strip-signal {
		display: inline-flex;
		align-items: center;
		gap: 0.45rem;
		margin-bottom: 0.35rem;
		padding: 0.28rem 0.62rem;
		border-radius: 999px;
		background: rgba(17, 24, 39, 0.08);
		color: var(--ink);
		font-size: 0.72rem;
		font-weight: 700;
		letter-spacing: 0.08em;
		text-transform: uppercase;
	}

	.status-strip-beacon {
		position: relative;
		width: 0.62rem;
		height: 0.62rem;
		border-radius: 999px;
		background: #e4572e;
		box-shadow: 0 0 0 0 rgba(228, 87, 46, 0.4);
		animation: status-strip-pulse 1.35s ease-out infinite;
	}

	.status-strip-meta {
		margin: 0;
		font-size: 0.82rem;
		line-height: 1.45;
		color: var(--ink-soft);
		white-space: nowrap;
	}

	@keyframes status-strip-pulse {
		0% {
			box-shadow: 0 0 0 0 rgba(228, 87, 46, 0.38);
		}

		70% {
			box-shadow: 0 0 0 0.68rem rgba(228, 87, 46, 0);
		}

		100% {
			box-shadow: 0 0 0 0 rgba(228, 87, 46, 0);
		}
	}

	@keyframes status-strip-sheen {
		0% {
			transform: translateX(-150%);
		}

		100% {
			transform: translateX(150%);
		}
	}

	.studio-grid {
		display: grid;
		grid-template-columns: minmax(280px, 360px) minmax(0, 1fr);
		gap: var(--space-3);
		align-items: start;
	}

	.studio-sidebar,
	.studio-main,
	.review-main-column {
		display: grid;
		gap: var(--space-3);
		align-content: start;
		min-width: 0;
	}

	.studio-sidebar {
		position: sticky;
		top: 1rem;
		align-self: start;
	}

	.studio-panel {
		background:
			linear-gradient(180deg, rgba(241, 247, 241, 0.92), rgba(236, 244, 239, 0.88)),
			radial-gradient(circle at top left, rgba(15, 118, 110, 0.1), transparent 40%),
			radial-gradient(circle at bottom right, rgba(210, 180, 140, 0.12), transparent 38%);
	}

	.fact-card {
		display: grid;
		gap: var(--space-1);
		padding: 0.95rem 1rem;
		border-radius: var(--radius-md);
		background: var(--surface-2);
		border: 1px solid rgba(23, 35, 31, 0.08);
	}

	.fact-value {
		font-size: 1.05rem;
		font-weight: 700;
		line-height: 1.35;
	}

	.pill-row {
		display: flex;
		gap: var(--space-2);
		flex-wrap: wrap;
	}

	.host-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
		gap: var(--space-3);
	}

	.run-layout {
		display: grid;
		gap: var(--space-3);
	}

	.run-host-section {
		display: grid;
		gap: var(--space-3);
		align-content: start;
	}

	.section-copy-block {
		display: grid;
		gap: 0.3rem;
		min-width: 0;
	}

	.host-section-copy {
		max-width: 62ch;
	}

	.host-schedule-note {
		margin-top: -0.12rem;
	}

	.run-readiness-card,
	.run-setup-card {
		display: grid;
		gap: var(--space-2);
		padding: 1.1rem 1.15rem;
		border-radius: var(--radius-lg);
		border: 1px solid rgba(15, 118, 110, 0.12);
		background:
			linear-gradient(180deg, rgba(255, 255, 255, 0.94), rgba(255, 255, 255, 0.78)),
			rgba(15, 118, 110, 0.04);
		box-shadow: 0 16px 36px rgba(23, 35, 31, 0.05);
	}

	.control-deck {
		display: grid;
		gap: 0;
		padding: 0.35rem;
		border-radius: calc(var(--radius-lg) + 0.2rem);
		background:
			linear-gradient(180deg, rgba(255, 252, 246, 0.96), rgba(248, 244, 236, 0.92)),
			radial-gradient(circle at top left, rgba(225, 241, 236, 0.18), transparent 34%),
			radial-gradient(circle at bottom right, rgba(222, 207, 176, 0.16), transparent 36%);
		box-shadow: 0 24px 46px rgba(62, 43, 24, 0.09);
		border: 1px solid rgba(104, 87, 61, 0.08);
		color: inherit;
	}

	.control-deck :global(.eyebrow-copy) {
		color: var(--accent-deep);
	}

	.control-deck .muted-copy,
	.control-deck .selected-host-inline-meta,
	.control-deck .compact-host-meta,
	.control-deck .inline-gate-copy {
		color: var(--ink-soft);
	}

	.control-deck .run-readiness-card,
	.control-deck .host-picker-shell,
	.control-deck .action-card {
		background: rgba(255, 255, 255, 0.6);
		box-shadow: none;
		border: 1px solid rgba(23, 35, 31, 0.07);
		border-radius: calc(var(--radius-lg) - 0.1rem);
	}

	.control-deck .host-picker-shell,
	.control-deck .action-card {
		margin-top: 0.35rem;
	}

	.control-deck .sample-host-card {
		background: rgba(250, 248, 242, 0.88);
		border-color: rgba(23, 35, 31, 0.08);
		color: var(--ink);
	}

	.control-deck .sample-host-card.selected {
		background: rgba(222, 246, 239, 0.72);
		border-color: rgba(15, 118, 110, 0.28);
		box-shadow: 0 10px 24px rgba(15, 118, 110, 0.1);
	}

	.control-deck .sample-host-state.ready {
		background: rgba(47, 107, 62, 0.12);
		color: var(--ok);
	}

	.control-deck .sample-host-badge {
		background: rgba(15, 118, 110, 0.1);
		color: var(--accent-deep);
	}

	.control-deck .selected-host-inline-value,
	.control-deck .run-card-title,
	.control-deck .sample-host-label {
		color: var(--ink);
	}

	.host-picker-shell,
	.bench-chat-shell,
	.note-composer-card,
	.archived-diagnosis-shell {
		display: grid;
		gap: var(--space-2);
		padding: 1rem 1.05rem;
		border-radius: var(--radius-lg);
		border: 1px solid rgba(23, 35, 31, 0.08);
		background: rgba(255, 255, 255, 0.7);
		box-shadow: 0 10px 28px rgba(23, 35, 31, 0.04);
	}

	.compact-status-card,
	.action-card {
		gap: 0.75rem;
	}

	.bench-workspace-shell {
		display: grid;
		gap: 0.9rem;
		padding: 0.35rem;
		min-width: 0;
		border-radius: calc(var(--radius-lg) + 0.2rem);
		background:
			linear-gradient(180deg, rgba(255, 252, 246, 0.96), rgba(252, 249, 242, 0.92)),
			radial-gradient(circle at top left, rgba(242, 220, 182, 0.2), transparent 34%);
		box-shadow: 0 24px 46px rgba(62, 43, 24, 0.09);
	}

	.bench-workspace-shell .note-composer-card,
	.bench-workspace-shell .bench-chat-shell,
	.bench-workspace-shell .diagnosis-shell,
	.bench-workspace-shell .archived-diagnosis-shell {
		background: rgba(255, 255, 255, 0.72);
		border-color: rgba(104, 87, 61, 0.08);
		box-shadow: none;
	}

	.bench-chat-shell {
		gap: 0.9rem;
		padding: 1rem 1.05rem;
		background:
			linear-gradient(180deg, rgba(255, 255, 255, 0.8), rgba(255, 251, 245, 0.72)),
			radial-gradient(circle at top left, rgba(221, 199, 160, 0.16), transparent 36%);
	}

	.bench-workspace-shell .bench-chat-shell {
		border-color: rgba(164, 115, 46, 0.12);
		background:
			linear-gradient(180deg, rgba(255, 255, 255, 0.8), rgba(255, 251, 245, 0.72)),
			radial-gradient(circle at top left, rgba(221, 199, 160, 0.16), transparent 36%);
	}

	.bench-chat-head {
		align-items: center;
	}

	.bench-chat-log {
		max-height: 34rem;
		padding: 0.95rem;
		background:
			linear-gradient(180deg, rgba(255, 255, 255, 0.94), rgba(251, 247, 240, 0.9)),
			rgba(221, 199, 160, 0.08);
		border-color: rgba(164, 115, 46, 0.12);
	}

	.bench-chat-empty {
		display: grid;
		gap: 0.35rem;
		align-content: center;
		min-height: 12rem;
		padding: 0.35rem 0.2rem;
	}

	.bench-chat-composer {
		gap: 0.75rem;
		padding: 0.95rem 1rem;
		border-radius: var(--radius-md);
		border: 1px solid rgba(15, 118, 110, 0.12);
		background:
			linear-gradient(180deg, rgba(244, 252, 249, 0.92), rgba(255, 255, 255, 0.84)),
			radial-gradient(circle at bottom right, rgba(15, 118, 110, 0.08), transparent 34%);
	}

	.bench-chat-context {
		display: grid;
		gap: 0.55rem;
	}

	.bench-chat-footer {
		align-items: center;
	}

	.bench-workspace-shell .diagnosis-shell {
		background:
			linear-gradient(180deg, rgba(255, 250, 240, 0.94), rgba(255, 255, 255, 0.84)),
			radial-gradient(circle at top right, rgba(221, 199, 160, 0.18), transparent 38%);
	}

	.run-card-title {
		margin: 0;
		font-size: 1.35rem;
		line-height: 1.15;
	}

	.run-status-pills {
		padding-top: 0.15rem;
	}

	.folder-encode-card {
		display: grid;
		gap: 0.75rem;
		padding: 0.9rem 0.95rem;
		border-radius: var(--radius-md);
		border: 1px solid rgba(23, 35, 31, 0.08);
		background: rgba(255, 255, 255, 0.74);
	}

	.folder-encode-card.live {
		background:
			linear-gradient(180deg, rgba(236, 250, 244, 0.92), rgba(255, 255, 255, 0.84)),
			rgba(47, 107, 62, 0.06);
		border-color: rgba(47, 107, 62, 0.16);
	}

	.folder-encode-card.queued {
		background:
			linear-gradient(180deg, rgba(238, 248, 250, 0.92), rgba(255, 255, 255, 0.84)),
			rgba(15, 118, 110, 0.05);
		border-color: rgba(15, 118, 110, 0.16);
	}

	.folder-encode-card.warning {
		background:
			linear-gradient(180deg, rgba(255, 245, 238, 0.94), rgba(255, 255, 255, 0.84)),
			rgba(194, 65, 12, 0.05);
		border-color: rgba(194, 65, 12, 0.16);
	}

	.folder-encode-header {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		gap: 0.75rem;
		align-items: start;
	}

	.folder-encode-title {
		margin: 0.1rem 0 0;
		font-size: 1rem;
		font-weight: 700;
		line-height: 1.2;
	}

	.folder-encode-chip {
		display: inline-flex;
		align-items: center;
		padding: 0.28rem 0.55rem;
		border-radius: 999px;
		font-size: 0.72rem;
		font-weight: 700;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		background: rgba(148, 163, 184, 0.16);
		color: var(--ink-soft);
	}

	.folder-encode-chip.live {
		background: rgba(47, 107, 62, 0.12);
		color: var(--ok);
	}

	.folder-encode-chip.queued {
		background: rgba(15, 118, 110, 0.12);
		color: var(--accent-deep);
	}

	.folder-encode-chip.warning {
		background: rgba(194, 65, 12, 0.12);
		color: #b45309;
	}

	.folder-encode-detail,
	.folder-encode-next-step,
	.folder-encode-meta {
		margin: 0;
	}

	.folder-encode-facts {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
		gap: 0.65rem;
	}

	.folder-encode-fact {
		display: grid;
		gap: 0.16rem;
		padding: 0.72rem 0.78rem;
		border-radius: var(--radius-md);
		background: rgba(250, 248, 242, 0.9);
		border: 1px solid rgba(23, 35, 31, 0.06);
	}

	.folder-encode-fact p,
	.folder-encode-fact strong {
		margin: 0;
	}

	.folder-encode-fact p {
		font-size: 0.74rem;
		font-weight: 700;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--ink-soft);
	}

	.folder-encode-fact strong {
		font-size: 0.92rem;
		line-height: 1.3;
		color: var(--ink);
	}

	.sample-host-grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
		gap: var(--space-3);
		justify-content: start;
	}

	.compact-host-grid {
		grid-template-columns: 1fr;
		gap: 0.7rem;
	}

	.sample-host-card {
		display: grid;
		gap: var(--space-2);
		padding: 1rem;
		border-radius: var(--radius-lg);
		border: 1px solid rgba(23, 35, 31, 0.1);
		background: rgba(255, 255, 255, 0.76);
		text-align: left;
		transition:
			transform 160ms ease,
			border-color 160ms ease,
			box-shadow 160ms ease,
			background 160ms ease;
	}

	.compact-host-card {
		gap: 0.45rem;
		padding: 0.85rem 0.9rem;
		border-radius: var(--radius-md);
	}

	.sample-host-card:hover:not(.disabled) {
		transform: translateY(-2px);
		border-color: rgba(15, 118, 110, 0.22);
		box-shadow: 0 14px 28px rgba(23, 35, 31, 0.08);
	}

	.sample-host-card.preferred {
		border-color: rgba(15, 118, 110, 0.22);
	}

	.sample-host-card.selected {
		border-color: rgba(15, 118, 110, 0.34);
		background: rgba(15, 118, 110, 0.1);
		box-shadow: 0 10px 30px rgba(15, 118, 110, 0.12);
	}

	.sample-host-card.disabled {
		opacity: 0.7;
		cursor: not-allowed;
	}

	.sample-host-badges {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		flex-wrap: wrap;
	}

	.sample-host-state {
		display: inline-flex;
		align-items: center;
		padding: 0.25rem 0.55rem;
		border-radius: 999px;
		font-size: 0.72rem;
		font-weight: 700;
		letter-spacing: 0.08em;
		text-transform: uppercase;
	}

	.sample-host-state.ready {
		background: rgba(47, 107, 62, 0.12);
		color: var(--ok);
	}

	.sample-host-state.unavailable {
		background: rgba(148, 163, 184, 0.16);
		color: var(--ink-soft);
	}

	.sample-host-label {
		font-size: 1.05rem;
		font-weight: 700;
		line-height: 1.2;
	}

	.compact-host-meta {
		font-size: 0.84rem;
		line-height: 1.45;
	}

	.compact-host-meta.secondary {
		font-size: 0.79rem;
		color: var(--library-soft-text);
	}

	.compact-host-meta.tertiary {
		font-size: 0.77rem;
		font-weight: 700;
		color: var(--accent-deep);
	}

	.sample-host-badge {
		display: inline-flex;
		align-items: center;
		padding: 0.25rem 0.5rem;
		border-radius: 999px;
		background: rgba(15, 118, 110, 0.12);
		color: var(--accent-deep);
		font-size: 0.72rem;
		font-weight: 700;
		letter-spacing: 0.08em;
		text-transform: uppercase;
	}

	.sample-host-facts {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
		gap: 0.65rem;
	}

	.sample-host-fact {
		display: grid;
		gap: 0.18rem;
		padding: 0.72rem 0.78rem;
		border-radius: var(--radius-md);
		background: rgba(247, 246, 241, 0.92);
		border: 1px solid rgba(23, 35, 31, 0.06);
	}

	.sample-host-priority {
		margin: 0;
		font-size: 0.76rem;
		font-weight: 700;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--ink-soft);
	}

	.host-selection-note {
		margin-top: -0.1rem;
	}

	.selected-host-inline {
		display: grid;
		margin: 0;
		gap: 0.2rem;
	}

	.calibration-thread-shell {
		display: grid;
		gap: 0.85rem;
		padding: 0.95rem 1rem;
		min-width: 0;
		border-radius: var(--radius-md);
		border: 1px solid rgba(15, 118, 110, 0.12);
		background:
			linear-gradient(180deg, rgba(255, 255, 255, 0.9), rgba(255, 255, 255, 0.76)),
			rgba(15, 118, 110, 0.04);
	}

	.history-thread-shell {
		padding: 0.85rem;
	}

	.calibration-thread-list {
		display: grid;
		gap: 0.65rem;
	}

	.thread-scroll-list {
		overflow: auto;
		padding-right: 0.35rem;
	}

	.thread-turn {
		display: grid;
		gap: 0.32rem;
		max-width: min(100%, 43rem);
		min-width: 0;
		padding: 0.85rem 0.95rem;
		border-radius: 1rem;
		border: 1px solid rgba(23, 35, 31, 0.08);
		box-shadow: 0 12px 24px rgba(23, 35, 31, 0.04);
	}

	.operator-turn {
		justify-self: end;
		background:
			linear-gradient(180deg, rgba(216, 243, 236, 0.96), rgba(200, 236, 227, 0.9)),
			radial-gradient(circle at top right, rgba(255, 255, 255, 0.24), transparent 36%);
		border-color: rgba(15, 118, 110, 0.22);
	}

	.system-turn {
		justify-self: start;
		background:
			linear-gradient(180deg, rgba(255, 251, 244, 0.96), rgba(250, 246, 239, 0.9)),
			radial-gradient(circle at top left, rgba(225, 200, 155, 0.16), transparent 38%);
		border-color: rgba(164, 115, 46, 0.14);
		border-left: 4px solid rgba(180, 83, 9, 0.18);
	}

	.operator-turn .thread-copy {
		color: #103e39;
	}

	.system-turn .thread-copy {
		color: #32251a;
	}

	.system-turn .thread-support {
		color: rgba(23, 35, 31, 0.72);
	}

	.thread-role {
		margin: 0;
		font-size: 0.73rem;
		font-weight: 700;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--accent-deep);
	}

	.thread-copy {
		margin: 0;
		font-size: 0.98rem;
		line-height: 1.45;
		overflow-wrap: anywhere;
	}

	.thread-support {
		margin: 0;
		font-size: 0.88rem;
		line-height: 1.45;
		color: var(--ink-soft);
		overflow-wrap: anywhere;
	}

	.thread-readout {
		display: grid;
		gap: 0.22rem;
		padding-top: 0.55rem;
		border-top: 1px solid rgba(23, 35, 31, 0.08);
	}

	.thread-chip-row {
		padding-top: 0.1rem;
	}

	.proposal-shell {
		display: grid;
		gap: 0.8rem;
		padding: 0.95rem 1rem;
		border-radius: var(--radius-md);
		border: 1px solid rgba(15, 118, 110, 0.16);
		background:
			linear-gradient(180deg, rgba(255, 255, 255, 0.94), rgba(255, 255, 255, 0.8)),
			rgba(15, 118, 110, 0.06);
	}

	.diagnosis-shell {
		gap: 1rem;
	}

	.diagnosis-grid {
		display: grid;
		gap: 0.8rem;
		grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
	}

	.diagnosis-copy-card {
		grid-column: span 2;
	}

	.diagnosis-details {
		padding-top: 0.2rem;
	}

	.diagnosis-disclosure-grid {
		padding-top: 0.7rem;
	}

	.proposal-shell.stale {
		border-color: rgba(180, 83, 9, 0.24);
		background:
			linear-gradient(180deg, rgba(255, 251, 235, 0.94), rgba(255, 251, 235, 0.82)),
			rgba(180, 83, 9, 0.04);
	}

	.proposal-head {
		display: flex;
		gap: var(--space-3);
		justify-content: space-between;
		align-items: start;
		flex-wrap: wrap;
		min-width: 0;
	}

	.proposal-title {
		margin: 0;
		font-size: 1.08rem;
		line-height: 1.3;
	}

	.proposal-pill-row {
		justify-content: flex-end;
	}

	.proposal-change-grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
		gap: 0.7rem;
	}

	.proposal-change-card {
		display: grid;
		gap: 0.18rem;
		padding: 0.82rem 0.85rem;
		border-radius: var(--radius-md);
		background: rgba(247, 246, 241, 0.92);
		border: 1px solid rgba(23, 35, 31, 0.07);
	}

	.proposal-change-value {
		margin: 0;
		font-size: 0.98rem;
		font-weight: 700;
		line-height: 1.3;
		overflow-wrap: anywhere;
	}

	.proposal-change-previous {
		margin: 0;
		font-size: 0.83rem;
		line-height: 1.4;
		color: var(--ink-soft);
		overflow-wrap: anywhere;
	}

	.proposal-warning-copy {
		padding: 0.75rem 0.8rem;
		border-radius: calc(var(--radius-md) - 0.18rem);
		background: rgba(15, 118, 110, 0.06);
		border: 1px solid rgba(15, 118, 110, 0.1);
	}

	.impact-warning-copy {
		background: rgba(180, 83, 9, 0.08);
		border-color: rgba(180, 83, 9, 0.18);
	}

	.proposal-workbench-grid {
		display: grid;
		gap: 0.8rem;
		grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
		min-width: 0;
	}

	.proposal-workbench-card {
		display: grid;
		gap: 0.65rem;
		min-width: 0;
		padding: 0.9rem;
		border-radius: var(--radius-md);
		background: rgba(255, 255, 255, 0.78);
		border: 1px solid rgba(23, 35, 31, 0.08);
		align-content: start;
	}

	.proposal-workbench-card .muted-copy {
		min-width: 0;
		max-width: 100%;
		overflow-wrap: anywhere;
		word-break: break-word;
	}

	.proposal-trace-card-shell {
		grid-column: 1 / -1;
	}

	.proposal-context-grid {
		display: grid;
		gap: 0.65rem;
	}

	.proposal-context-card,
	.proposal-trace-card {
		display: grid;
		gap: 0.22rem;
		min-width: 0;
		padding: 0.75rem 0.8rem;
		border-radius: calc(var(--radius-md) - 0.15rem);
		background: rgba(247, 246, 241, 0.88);
		border: 1px solid rgba(23, 35, 31, 0.06);
	}

	.workbench-change-grid {
		grid-template-columns: 1fr;
	}

	.proposal-change-section {
		display: grid;
		gap: 0.5rem;
	}

	.proposal-section-title,
	.proposal-memory-title {
		margin: 0;
		font-size: 0.9rem;
		font-weight: 700;
		line-height: 1.35;
		overflow-wrap: anywhere;
	}

	.proposal-memory-shell {
		display: grid;
		gap: 0.55rem;
	}

	.proposal-memory-card {
		display: grid;
		gap: 0.22rem;
		padding: 0.75rem 0.8rem;
		border-radius: calc(var(--radius-md) - 0.15rem);
		background: rgba(247, 246, 241, 0.88);
		border: 1px solid rgba(23, 35, 31, 0.06);
	}

	.review-pack-shell {
		display: grid;
		gap: 0.8rem;
		padding: 0.95rem 1rem;
		border-radius: var(--radius-md);
		border: 1px solid rgba(15, 118, 110, 0.16);
		background:
			linear-gradient(180deg, rgba(255, 255, 255, 0.94), rgba(255, 255, 255, 0.84)),
			rgba(15, 118, 110, 0.04);
	}

	.review-pack-inline-shell {
		margin-top: 0.25rem;
	}

	.review-pack-section {
		display: grid;
		gap: 0.7rem;
	}

	.review-pack-section-copy {
		gap: 0.18rem;
	}

	.review-pack-grid {
		display: grid;
		gap: 0.8rem;
		grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
	}

	.review-pack-audio-grid {
		grid-template-columns: minmax(0, 1fr);
	}

	.review-pack-card {
		display: grid;
		gap: 0.6rem;
		padding: 0.8rem;
		border-radius: var(--radius-md);
		background: rgba(255, 255, 255, 0.82);
		border: 1px solid rgba(23, 35, 31, 0.08);
	}

	.review-pack-link {
		display: block;
		padding: 0;
		border-radius: calc(var(--radius-md) - 0.2rem);
		overflow: hidden;
		border: 1px solid rgba(23, 35, 31, 0.08);
		background: rgba(247, 246, 241, 0.88);
		cursor: pointer;
	}

	.review-pack-link img {
		display: block;
		width: 100%;
		height: auto;
		aspect-ratio: 16 / 10;
		object-fit: cover;
	}

	.review-pack-audio-grid .review-pack-link img {
		aspect-ratio: 16 / 6;
		object-fit: contain;
		background: rgba(247, 246, 241, 0.94);
	}

	.review-pack-copy {
		display: grid;
		gap: 0.22rem;
	}

	.proposal-trace-shell {
		display: grid;
		gap: 0.7rem;
		padding-top: 0.1rem;
		min-width: 0;
	}

	.proposal-trace-shell summary {
		cursor: pointer;
		display: flex;
		justify-content: space-between;
		align-items: center;
		padding: 0.8rem 0.95rem;
		border-radius: calc(var(--radius-md) - 0.12rem);
		background: rgba(255, 255, 255, 0.72);
		border: 1px solid rgba(23, 35, 31, 0.08);
		font-size: 0.84rem;
		font-weight: 700;
		letter-spacing: 0.05em;
		text-transform: uppercase;
		color: var(--accent-deep);
	}

	.proposal-trace-shell summary::after {
		content: '+';
		font-size: 1rem;
		font-weight: 700;
		line-height: 1;
		color: var(--ink-soft);
	}

	.proposal-trace-shell[open] summary {
		background: rgba(247, 244, 237, 0.92);
	}

	.proposal-trace-shell[open] summary::after {
		content: '-';
	}

	.proposal-trace-grid {
		display: grid;
		gap: 0.65rem;
		padding: 0.2rem 0.1rem 0;
		min-width: 0;
	}

	.proposal-trace-raw {
		display: block;
		width: 100%;
		margin: 0;
		padding: 0.85rem;
		min-width: 0;
		max-width: 100%;
		border-radius: calc(var(--radius-md) - 0.15rem);
		background: rgba(23, 35, 31, 0.06);
		border: 1px solid rgba(23, 35, 31, 0.08);
		font-size: 0.82rem;
		line-height: 1.45;
		white-space: pre-wrap;
		word-break: break-word;
		overflow-wrap: anywhere;
		overflow-x: auto;
		box-sizing: border-box;
	}

	.proposal-issue-list {
		margin: 0;
		padding-left: 1.15rem;
		display: grid;
		gap: 0.32rem;
		color: var(--ink-soft);
		font-size: 0.88rem;
		line-height: 1.45;
	}

	.selected-host-inline-value {
		font-size: 1rem;
		font-weight: 700;
		line-height: 1.35;
	}

	.selected-host-inline-meta {
		max-width: 44ch;
	}

	.host-search-callout {
		margin: -0.18rem 0 0;
		display: grid;
		gap: 0.12rem;
		font-size: 0.82rem;
		line-height: 1.42;
	}

	.host-search-callout-label {
		font-weight: 700;
		color: var(--ink);
	}

	.action-inline-note {
		padding: 0.15rem 0 0;
		border-radius: 0;
		background: transparent;
		border: 0;
		font-size: 0.86rem;
		line-height: 1.4;
	}

	.utility-action-row {
		padding-top: 0.15rem;
	}

	.destructive-action-copy {
		padding-top: 0;
	}

	.note-panel {
		padding: 1rem 1.05rem;
	}

	.note-lede {
		max-width: 42ch;
		overflow-wrap: anywhere;
	}

	.note-composer-footer {
		display: flex;
		justify-content: space-between;
		gap: var(--space-2);
		align-items: end;
		flex-wrap: wrap;
	}

	.note-submit-hint {
		max-width: 40ch;
		font-size: 0.88rem;
		line-height: 1.45;
	}

	.field-block {
		display: grid;
		gap: var(--space-2);
	}

	textarea {
		width: 100%;
		padding: 0.9rem 1rem;
		border-radius: var(--radius-md);
		border: 1px solid rgba(23, 35, 31, 0.12);
		background: var(--surface-2);
		color: var(--ink);
		min-height: 7rem;
		resize: vertical;
	}

	.action-row {
		display: flex;
		gap: var(--space-2);
		flex-wrap: wrap;
		align-items: start;
	}

	.primary-action-row :global(button) {
		min-width: min(100%, 18rem);
	}

	.compact-action-row :global(button) {
		flex: 1 1 12rem;
		min-width: 0;
	}

	.single-primary-action-row :global(button) {
		flex-basis: 100%;
	}

	.action-card-head {
		display: flex;
		justify-content: space-between;
		gap: 0.8rem;
		align-items: start;
		flex-wrap: wrap;
	}

	.queue-action-block {
		display: grid;
		gap: 0.45rem;
		max-width: 24rem;
	}

	.inline-gate-copy {
		display: grid;
		gap: 0.15rem;
		font-size: 0.9rem;
		line-height: 1.45;
		color: var(--ink-soft);
		overflow-wrap: anywhere;
	}

	.run-unlock-grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
		gap: 0.75rem;
	}

	.run-unlock-card {
		display: grid;
		gap: 0.18rem;
		padding: 0.85rem 0.9rem;
		border-radius: var(--radius-md);
		background: rgba(247, 246, 241, 0.92);
		border: 1px solid rgba(23, 35, 31, 0.07);
	}

	.run-unlock-title {
		margin: 0;
		font-size: 0.98rem;
		font-weight: 700;
		line-height: 1.3;
	}

	.review-action-row {
		align-items: flex-start;
	}

	.review-action-copy {
		padding-top: 0.15rem;
	}

	.review-grid {
		display: grid;
		grid-template-columns: minmax(260px, 320px) minmax(0, 1fr);
		gap: var(--space-3);
		align-items: start;
	}

	.review-grid-balanced {
		grid-template-columns: minmax(0, 1.25fr) minmax(280px, 360px);
	}

	.review-side-column {
		display: grid;
		gap: var(--space-3);
		align-content: start;
	}

	.review-approval-column {
		position: sticky;
		top: 1rem;
		align-self: start;
	}

	.review-block {
		display: grid;
		gap: var(--space-2);
		padding: 1rem 1.05rem;
		border-radius: var(--radius-md);
		background: rgba(255, 255, 255, 0.64);
		border: 1px solid rgba(23, 35, 31, 0.08);
	}

	.review-panel {
		background:
			linear-gradient(180deg, rgba(255, 253, 247, 0.96), rgba(255, 251, 244, 0.9)),
			radial-gradient(circle at top left, rgba(225, 241, 236, 0.22), transparent 34%);
	}

	.review-block-title {
		font-size: 1.14rem;
		font-weight: 700;
		line-height: 1.25;
	}

	.representative-block {
		align-content: start;
	}

	.representative-path-shell {
		display: grid;
		gap: 0.75rem;
		padding: 0.95rem 1rem;
		border-radius: calc(var(--radius-md) - 0.1rem);
		background:
			linear-gradient(180deg, rgba(255, 255, 255, 0.78), rgba(255, 255, 255, 0.54)),
			radial-gradient(circle at top right, rgba(15, 118, 110, 0.09), transparent 52%);
		border: 1px solid rgba(15, 118, 110, 0.12);
	}

	.representative-file-name {
		font-size: 1.05rem;
		line-height: 1.32;
		overflow-wrap: anywhere;
	}

	.representative-meta-row {
		display: flex;
		gap: 0.5rem;
		flex-wrap: wrap;
	}

	.representative-support-copy {
		font-size: 0.88rem;
		line-height: 1.45;
	}

	.review-player-shell-block,
	.legacy-review-note {
		display: grid;
		gap: var(--space-3);
		padding: 1rem;
		border-radius: calc(var(--radius-md) - 0.08rem);
		background:
			linear-gradient(180deg, rgba(255, 255, 255, 0.82), rgba(255, 255, 255, 0.62)),
			radial-gradient(circle at top left, rgba(15, 118, 110, 0.1), transparent 56%);
		border: 1px solid rgba(15, 118, 110, 0.12);
	}

	.review-player-head {
		display: flex;
		justify-content: space-between;
		gap: var(--space-3);
		align-items: start;
		flex-wrap: wrap;
	}

	.review-head-actions {
		display: grid;
		justify-items: end;
		gap: 0.7rem;
		min-width: min(100%, 22rem);
	}

	.review-progress-pills {
		justify-content: flex-end;
	}

	.review-selector-row {
		display: flex;
		gap: 0.65rem;
		flex-wrap: wrap;
	}

	.review-selector-chip {
		display: grid;
		gap: 0.15rem;
		padding: 0.75rem 0.85rem;
		border-radius: 1rem;
		border: 1px solid rgba(23, 35, 31, 0.12);
		background: rgba(255, 255, 255, 0.84);
		min-width: 7rem;
		text-align: left;
	}

	.review-selector-chip.selected {
		border-color: rgba(15, 118, 110, 0.32);
		background: rgba(15, 118, 110, 0.12);
		box-shadow: 0 10px 24px rgba(15, 118, 110, 0.1);
	}

	.review-selector-chip.seen {
		border-color: rgba(47, 107, 62, 0.22);
		background: linear-gradient(180deg, rgba(47, 107, 62, 0.08), rgba(255, 255, 255, 0.84));
	}

	.review-selector-chip.seen span:last-child {
		color: #2f6b3e;
		font-weight: 700;
	}

	.review-selector-chip strong {
		font-size: 0.98rem;
		line-height: 1.2;
	}

	.review-selector-chip span:last-child {
		font-size: 0.82rem;
		color: var(--ink-soft);
	}

	.review-player-stack {
		display: grid;
		gap: var(--space-3);
	}

	.review-player-columns {
		grid-template-columns: repeat(2, minmax(0, 1fr));
	}

	.review-player-panel {
		display: grid;
		gap: 0.75rem;
		padding: 0.85rem;
		border-radius: calc(var(--radius-md) - 0.18rem);
		background: rgba(247, 245, 238, 0.88);
		border: 1px solid rgba(23, 35, 31, 0.08);
	}

	.draft-review-player-panel {
		background: rgba(15, 118, 110, 0.08);
		border-color: rgba(15, 118, 110, 0.14);
	}

	.review-player-meta {
		display: grid;
		gap: 0.2rem;
	}

	.review-player-panel video {
		width: 100%;
		border-radius: calc(var(--radius-md) - 0.24rem);
		background: #020617;
		border: 1px solid rgba(23, 35, 31, 0.1);
		aspect-ratio: 16 / 9;
	}

	.review-bench-summary-card {
		display: grid;
		gap: 0.55rem;
		padding: 0.85rem 0.9rem;
		border-radius: calc(var(--radius-md) - 0.14rem);
		background: rgba(255, 255, 255, 0.7);
		border: 1px solid rgba(23, 35, 31, 0.08);
	}

	.review-bench-pill-row {
		margin-top: -0.1rem;
	}

	.review-bench-headline {
		font-weight: 700;
	}

	.review-compare-link {
		padding: 0;
		border: 0;
		background: transparent;
		font-size: 0.9rem;
		font-weight: 700;
		color: var(--accent-deep);
		cursor: pointer;
	}

	.comparison-stack {
		display: grid;
		gap: var(--space-3);
	}

	.comparison-group {
		display: grid;
		gap: 0.7rem;
	}

	.comparison-group-head {
		display: grid;
	}

	.comparison-group-title {
		font-size: 0.98rem;
		font-weight: 700;
		line-height: 1.35;
	}

	.comparison-list {
		display: grid;
		gap: 0.6rem;
	}

	.comparison-table-head {
		display: grid;
		grid-template-columns: minmax(180px, 220px) minmax(0, 1fr) auto minmax(0, 1fr);
		gap: 0.75rem;
		padding: 0 0.2rem;
		font-size: 0.72rem;
		font-weight: 700;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--ink-soft);
	}

	.comparison-table-head span:first-child {
		grid-column: 2;
	}

	.comparison-table-head span:last-child {
		grid-column: 4;
	}

	.comparison-row {
		display: grid;
		gap: 0.5rem;
		padding: 0.8rem 0.9rem;
		border-radius: calc(var(--radius-md) - 0.12rem);
		background: rgba(255, 255, 255, 0.62);
		border: 1px solid rgba(23, 35, 31, 0.08);
	}

	.compact-row {
		grid-template-columns: minmax(180px, 220px) minmax(0, 1fr) auto minmax(0, 1fr);
		align-items: center;
	}

	.changed-row {
		background: linear-gradient(180deg, rgba(255, 255, 255, 0.86), rgba(255, 255, 255, 0.72));
	}

	.comparison-row-head {
		display: grid;
		gap: 0.4rem;
		align-items: start;
	}

	.comparison-label {
		font-size: 0.95rem;
		font-weight: 700;
		line-height: 1.3;
	}

	.comparison-status-pill {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		justify-self: start;
		padding: 0.22rem 0.58rem;
		border-radius: 999px;
		font-size: 0.72rem;
		font-weight: 700;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		white-space: nowrap;
	}

	.changed-pill {
		background: rgba(15, 118, 110, 0.12);
		color: var(--accent-deep);
	}

	.comparison-values {
		display: grid;
		grid-column: 2 / 5;
		grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
		gap: 0.75rem;
		align-items: stretch;
	}

	.comparison-value-card {
		display: grid;
		gap: 0.35rem;
		padding: 0.75rem 0.8rem;
		border-radius: calc(var(--radius-md) - 0.2rem);
		background: rgba(247, 245, 238, 0.88);
		border: 1px solid rgba(23, 35, 31, 0.08);
	}

	.draft-value-card {
		background: rgba(15, 118, 110, 0.1);
		border-color: rgba(15, 118, 110, 0.14);
	}

	.comparison-copy {
		font-size: 0.98rem;
		font-weight: 600;
		line-height: 1.35;
		word-break: break-word;
	}

	.comparison-subcopy {
		font-size: 0.82rem;
		line-height: 1.45;
		color: var(--ink-soft);
	}

	.comparison-arrow {
		display: grid;
		place-items: center;
		font-size: 1.15rem;
		font-weight: 800;
		color: var(--ink-soft);
	}

	.steady-summary-block {
		display: grid;
		gap: 0.55rem;
		padding-top: 0.2rem;
	}

	.steady-summary-list {
		display: grid;
		gap: 0.45rem;
	}

	.steady-summary-row {
		display: flex;
		justify-content: space-between;
		gap: var(--space-3);
		padding: 0.65rem 0.8rem;
		border-radius: calc(var(--radius-md) - 0.18rem);
		background: rgba(255, 255, 255, 0.48);
		border: 1px solid rgba(23, 35, 31, 0.06);
	}

	.steady-summary-label {
		font-size: 0.88rem;
		font-weight: 600;
		line-height: 1.35;
	}

	.steady-summary-value {
		font-size: 0.84rem;
		line-height: 1.4;
		color: var(--ink-soft);
		text-align: right;
	}

	.hotspot-block,
	.folder-snapshot-block {
		margin-top: 0;
	}

	.snapshot-grid {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: var(--space-3);
	}

	.folder-snapshot-block .snapshot-grid {
		gap: var(--space-2);
	}

	.compact-fact-card {
		background: rgba(255, 255, 255, 0.58);
	}

	.snapshot-detail-copy {
		font-size: 0.82rem;
		line-height: 1.45;
	}

	.thread-history-shell,
	.reference-disclosure,
	.steady-details-shell {
		display: grid;
		gap: 0.75rem;
		padding: 0.95rem 1rem;
		border-radius: var(--radius-md);
		background: rgba(255, 255, 255, 0.6);
		border: 1px solid rgba(23, 35, 31, 0.08);
	}

	.inline-thread-shell {
		padding: 0.95rem 1rem 1rem;
		background: rgba(255, 255, 255, 0.66);
	}

	.thread-inline-head {
		display: flex;
		justify-content: space-between;
		gap: 1rem;
		align-items: start;
		min-width: 0;
	}

	.reference-disclosure summary,
	.steady-details-shell summary {
		cursor: pointer;
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto auto;
		gap: 0.8rem;
		align-items: center;
		padding: 0.8rem 0.95rem;
		margin: -0.05rem -0.05rem 0;
		border-radius: calc(var(--radius-md) - 0.12rem);
		background: rgba(255, 255, 255, 0.72);
		border: 1px solid rgba(23, 35, 31, 0.08);
		font-size: 0.9rem;
		font-weight: 700;
		color: var(--ink);
	}

	.summary-hint {
		font-size: 0.82rem;
		font-weight: 700;
		color: var(--ink-soft);
	}

	.reference-disclosure summary::after,
	.steady-details-shell summary::after {
		content: '+';
		font-size: 1rem;
		font-weight: 700;
		line-height: 1;
		color: var(--ink-soft);
	}

	.reference-disclosure[open] summary,
	.steady-details-shell[open] summary {
		background: rgba(247, 244, 237, 0.92);
	}

	.reference-disclosure[open] summary::after,
	.steady-details-shell[open] summary::after {
		content: '-';
	}

	.thread-history-meta {
		font-size: 0.8rem;
		color: var(--ink-soft);
		overflow-wrap: anywhere;
	}

	.thread-history-copy {
		margin-top: -0.15rem;
		overflow-wrap: anywhere;
	}

	.reference-disclosure-grid {
		display: grid;
		gap: var(--space-3);
		padding-top: 0.35rem;
	}

	.danger-disclosure {
		display: grid;
		gap: 0.75rem;
		padding: 0.9rem 0.95rem;
		border-radius: var(--radius-md);
		background: rgba(255, 255, 255, 0.54);
		border: 1px solid rgba(138, 68, 19, 0.14);
	}

	.danger-disclosure summary {
		cursor: pointer;
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto auto;
		gap: 0.8rem;
		align-items: center;
		font-size: 0.9rem;
		font-weight: 700;
		color: var(--ink);
	}

	.danger-disclosure summary::after {
		content: '+';
		font-size: 1rem;
		font-weight: 700;
		line-height: 1;
		color: var(--ink-soft);
	}

	.danger-disclosure[open] summary::after {
		content: '-';
	}

	.danger-disclosure-body {
		display: grid;
		gap: 0.7rem;
		padding-top: 0.1rem;
	}

	.compact-reference-block {
		background: rgba(255, 255, 255, 0.74);
	}

	.compact-snapshot-grid {
		grid-template-columns: 1fr;
	}

	.hotspot-pill-row {
		padding-top: 0.2rem;
	}

	@media (max-width: 900px) {
		.folder-header-grid,
		.workflow-stage-strip,
		.studio-grid,
		.run-layout,
		.review-grid,
		.review-grid-balanced,
		.review-player-columns,
		.snapshot-grid {
			grid-template-columns: 1fr;
		}

		.studio-sidebar,
		.review-approval-column {
			position: static;
		}

		.diagnosis-copy-card {
			grid-column: auto;
		}

		.compact-row,
		.comparison-table-head {
			grid-template-columns: 1fr;
		}

		.comparison-values {
			grid-column: auto;
		}

		.comparison-table-head {
			display: none;
		}

		.comparison-values {
			grid-template-columns: 1fr;
		}

		.comparison-arrow {
			display: none;
		}

		.review-head-actions {
			justify-items: stretch;
			min-width: 0;
		}

		.review-progress-pills {
			justify-content: flex-start;
		}

		.steady-summary-row {
			grid-template-columns: 1fr;
			display: grid;
		}

		.steady-summary-value {
			text-align: left;
		}
	}

	@media (max-width: 720px) {
		.host-grid,
		.sample-host-grid {
			grid-template-columns: 1fr;
		}

		.note-composer-footer,
		.thread-inline-head {
			flex-direction: column;
			align-items: start;
		}

		.note-composer-footer :global(button) {
			width: 100%;
		}

		.status-strip {
			align-items: start;
		}

		.status-strip-meta {
			white-space: normal;
		}

		.reference-disclosure summary,
		.steady-details-shell summary,
		.danger-disclosure summary {
			flex-direction: column;
			align-items: start;
		}
	}
</style>
