<script lang="ts">
	import { goto } from '$app/navigation';
	import { resolve } from '$app/paths';
	import { onMount, tick } from 'svelte';

	import { ApiError, apiDownloadHref, postJson } from '$lib/api/client';
	import ComparisonWorkspace from '$lib/components/review/ComparisonWorkspace.svelte';
	import SeasonIntegrityPanel from '$lib/components/season/SeasonIntegrityPanel.svelte';
	import type {
		CompressionIntentLevel,
		FolderPayload,
		FolderStatusPayload,
		HostsPayload,
		OperatorIntentRequestPayload,
		QualityRiskTag
	} from '$lib/api/types';
	import { folderRoutePath } from '$lib/folder-display';
	import { reviewAvailability } from '$lib/review/availability';
	import { clampMomentIndex, reviewPairHasSound } from '$lib/review/comparison';
	import { reviewSampleSizes, reviewSourceHasAudio } from '$lib/review/pairs';
	import {
		REVIEW_CONCERNS,
		approvalGuardFromMessage,
		calibrationActivityStatusLabel,
		calibrationEtaSummary,
		calibrationAcceptsUnderTargetResult,
		calibrationFreshnessLabel,
		calibrationJobTargetContract,
		calibrationLivenessLabel,
		calibrationResolutionLabel,
		calibrationStageLabel,
		calibrationTargetModeLabel,
		calibrationWorkLabel,
		calibrationWorkProgress,
		compareRiskSummary,
		compressionIntentContract,
		currentOperatorIntent,
		detailSeasonState,
		episodeLabel,
		exactItemFilename,
		exactReviewSizeFacts,
		expectedSizeChange,
		folderSizeTargetAnalysis,
		formatDecimalFileSize,
		formatDuration,
		goalRequest,
		isSizeGoalSelectionConfirmed,
		isSeriesPrefix,
		measuredFollowupRequest,
		overlappingCalibrationActivity,
		plainFailureMessage,
		predictedEpisodeSize,
		qualityMemoryView,
		resolvedTargetSummary,
		reviewFeedbackIntent,
		reviewFeedbackRequest,
		reviewAdjustmentIntent,
		reviewSizeAdjustment,
		scopedEncodeProgress,
		seasonIdentity,
		seasonEpisodeNavigationUnavailable,
		seasonPromotionIntegrity,
		seasonEpisodeOptions,
		stagedEpisodeLinks,
		shouldPrioritizeScopeActivity,
		sizeGoals,
		targetConstraintSummary,
		targetProvenanceSummary,
		technicalVideoPolicy,
		testRequestWithInstructions,
		withCompressionIntent,
		type ReviewSizeAdjustment,
		type ReviewSizeAdjustmentDirection,
		type SizeGoal
	} from '$lib/season/experience';

	type ActionPhase =
		| 'idle'
		| 'planning'
		| 'starting'
		| 'approving'
		| 'queueing'
		| 'checking'
		| 'finishing'
		| 'recovering';

	type RevisionMode = 'same_target' | 'roomier';

	type ActionResponse = {
		ok?: boolean;
		message?: string;
		proposal?: Record<string, unknown> | null;
	};

	type HostOption = {
		key?: string;
		label?: string;
		detail?: string;
		available?: boolean;
		schedule_open?: boolean;
		schedule_detail?: string;
	};

	type SafetyDialog = {
		kind:
			| 'approval'
			| 'recovery'
			| 'lifecycle_override'
			| 'older_seasons_override'
			| 'review_adjustment';
		title: string;
		detail: string;
		primaryLabel: string;
		confirmHighImpact?: boolean;
		confirmSizeTradeoff?: boolean;
		changes?: string[];
		adjustment?: ReviewSizeAdjustment;
		adjustmentKind?: 'smaller' | 'roomier';
	};

	let {
		folder,
		status,
		hosts,
		folderPending = false,
		loadError,
		onMutate
	}: {
		folder: FolderPayload;
		status: FolderStatusPayload;
		hosts: HostsPayload;
		folderPending?: boolean;
		loadError?: string;
		onMutate: () => Promise<void>;
	} = $props();

	let selectedGoalKey = $state<SizeGoal['key']>('recommended');
	let selectedGoalPrefix = $state('');
	let selectedCompressionIntentLevel = $state<CompressionIntentLevel | null>(null);
	let selectedCompressionIntentPrefix = $state('');
	let selectedHostKey = $state('');
	let retryMode = $state(false);
	let selectedMoment = $state(0);
	let audioChoice = $state<'original' | 'new'>('new');
	let actionPhase = $state<ActionPhase>('idle');
	let actionError = $state('');
	let actionMessage = $state('');
	let actionMessageTone = $state<'success' | 'neutral'>('success');
	let actionMessageStateKey = $state('');
	let blockerAction = $state<{ route: '/ops'; label: string } | null>(null);
	let actionStartedAt = $state(0);
	let clock = $state(Date.now());
	let showInstructions = $state(false);
	let operatorInstructions = $state('');
	let selectedConcerns = $state<QualityRiskTag[]>([]);
	let reviewFeedback = $state('');
	let revisionPaneOpen = $state(false);
	let revisionMode = $state<RevisionMode>('same_target');
	let revisionPanePrefix = $state('');
	let selectedEpisodeHref = $state('');
	let episodeNavigationPending = $state(false);
	let goalButtons = $state<HTMLButtonElement[]>([]);
	let compressionIntentButtons = $state<HTMLButtonElement[]>([]);
	let safetyDialog = $state<SafetyDialog | null>(null);
	let safetyDialogReturnFocus = $state<HTMLElement | null>(null);

	const identity = $derived(seasonIdentity(folder.prefix));
	const isSeriesScope = $derived(isSeriesPrefix(folder.prefix));
	const isExactItemScope = $derived(folder.media_scope?.match === 'exact_item');
	const exactEpisodeName = $derived(episodeLabel(folder.prefix));
	const exactFilename = $derived(exactItemFilename(folder));
	const seriesSeasonCount = $derived(Object.keys(folder.summary?.seasons ?? {}).length);
	const seriesSeasonLabel = $derived(seriesSeasonCount === 1 ? 'season' : 'seasons');
	const lifecycle = $derived(folder.lifecycle ?? null);
	const currentSeasonLifecycle = $derived(
		lifecycle?.seasons.find((season) => season.prefix === folder.prefix) ??
			lifecycle?.seasons[0] ??
			null
	);
	const eligibleEpisodeCount = $derived(
		lifecycle?.eligible_candidate_count ?? folder.encode_candidate_count ?? 0
	);
	const heldEpisodeCount = $derived(lifecycle?.held_candidate_count ?? 0);
	const canOverrideLifecycleHolds = $derived(Boolean(lifecycle?.can_override_holds));
	const olderSeasonOverride = $derived(
		isSeriesScope ? (folder.older_season_override ?? null) : null
	);
	const canQueueOlderSeasons = $derived(
		Boolean(olderSeasonOverride?.available && olderSeasonOverride.candidate_count > 0)
	);
	const cadenceBlockedEpisodeCount = $derived(
		olderSeasonOverride?.cadence_blocked_candidate_count ?? 0
	);
	const cadenceEvidenceRequiredEpisodeCount = $derived(
		olderSeasonOverride?.cadence_evidence_required_candidate_count ?? 0
	);
	const cadenceExcludedEpisodeCount = $derived(
		cadenceBlockedEpisodeCount + cadenceEvidenceRequiredEpisodeCount
	);
	const lifecycleHoldReasons = $derived(currentSeasonLifecycle?.hold_reasons ?? []);
	const lifecycleHold = $derived(lifecycleHoldReasons[0] ?? null);
	const scopeTitle = $derived(
		isSeriesScope
			? identity.show
			: isExactItemScope
				? `${identity.show} · ${identity.season} · ${exactEpisodeName}`
				: `${identity.show} · ${identity.season}`
	);
	const scopeName = $derived(
		isSeriesScope ? identity.show : isExactItemScope ? exactEpisodeName : identity.season
	);
	const scopeNoun = $derived(isSeriesScope ? 'show' : isExactItemScope ? 'episode' : 'season');
	const makeActionLabel = $derived(
		isSeriesScope
			? `Compress ${eligibleEpisodeCount} eligible ${eligibleEpisodeCount === 1 ? 'episode' : 'episodes'}`
			: isExactItemScope
				? 'Compress this episode'
				: heldEpisodeCount > 0
					? canOverrideLifecycleHolds
						? 'Override hold and compress the season'
						: 'Season remains protected'
					: 'Compress the season'
	);
	const humanState = $derived(detailSeasonState(folder, status));
	const promotionIntegrity = $derived(seasonPromotionIntegrity(status));
	const episodeOptions = $derived(seasonEpisodeOptions(status));
	const selectedEpisode = $derived(
		episodeOptions.find((option) => option.href === selectedEpisodeHref) ?? null
	);
	const encodedEpisodeLinks = $derived(stagedEpisodeLinks(status));
	const episodeNavigationUnavailable = $derived(seasonEpisodeNavigationUnavailable(status));
	const parentScope = $derived(folder.media_scope?.parent ?? null);
	const backHref = $derived(
		isExactItemScope && parentScope?.prefix
			? resolve(folderRoutePath(parentScope.prefix))
			: resolve('/')
	);
	const backLabel = $derived(
		isExactItemScope && parentScope?.title ? parentScope.title : 'Library'
	);
	const stagedAccessBlocked = $derived(
		(status.staged_integrity?.counts.remote_only_or_unreachable ?? 0) +
			(status.staged_integrity?.counts.missing ?? 0) >
			0
	);
	const storageRecoveryHost = $derived(
		hosts.hosts.find((host) => host.storage_recovery_available === true) ?? null
	);
	const goals = $derived(sizeGoals(folder));
	const selectedGoal = $derived(goals.find((goal) => goal.key === selectedGoalKey) ?? goals[0]);
	const compressionIntentOptions = $derived(folder.compression_intent_options ?? []);
	const activeCompressionIntentLevel = $derived(
		selectedCompressionIntentPrefix === folder.prefix
			? selectedCompressionIntentLevel
			: (compressionIntentOptions.find((option) => option.selected)?.key ?? null)
	);
	const selectedCompressionIntent = $derived(
		compressionIntentOptions.find((option) => option.key === activeCompressionIntentLevel) ?? null
	);
	const selectedCompressionIntentContract = $derived(
		selectedCompressionIntent ? compressionIntentContract(selectedCompressionIntent) : null
	);
	const compressionIntentConfirmed = $derived(Boolean(selectedCompressionIntent));
	const selectedOperatorIntent = $derived(
		selectedGoal && selectedCompressionIntent
			? withCompressionIntent(
					selectedGoal.operatorIntent,
					selectedCompressionIntent.compression_intent
				)
			: null
	);
	const requiresExplicitGoalSelection = $derived(
		goals.some((goal) => goal.requiresExplicitSelection)
	);
	const goalSelectionConfirmed = $derived(
		isSizeGoalSelectionConfirmed(goals, selectedGoalKey, selectedGoalPrefix, folder.prefix)
	);
	const hostOptions = $derived(
		((folder.sample_host_options ?? []) as HostOption[]).filter((host) => host.key)
	);
	const sampleItem = $derived(asRecord(folder.sample_item));
	const sampleHasAudio = $derived(reviewSourceHasAudio(folder));
	const sampleEpisode = $derived(episodeLabel(asText(sampleItem.rel_path)));
	const review = $derived(reviewAvailability(folder));
	const reviewPairs = $derived(review.pairs);
	const displayedMoment = $derived(clampMomentIndex(selectedMoment, reviewPairs.length));
	const currentPair = $derived(reviewPairs[displayedMoment]);
	const reviewHasSound = $derived(reviewPairHasSound(currentPair));
	const reviewSubject = $derived(reviewHasSound ? 'picture and sound' : 'picture');
	const calibration = $derived(asRecord(folder.calibration));
	const hasOwnCalibration = $derived(Object.keys(calibration).length > 0);
	const sampleResult = $derived(asRecord(calibration.sample_result));
	const episodeCount = $derived(folder.summary?.item_count ?? 0);
	const productionEpisodeCount = $derived(isSeriesScope ? eligibleEpisodeCount : episodeCount);
	const originalSeasonSize = $derived(folder.summary?.total_size_bytes ?? 0);
	const expectedEpisodeBytes = $derived(predictedEpisodeSize(folder));
	const olderSeasonProjectedSavingsBytes = $derived(
		olderSeasonOverride && expectedEpisodeBytes > 0
			? Math.max(
					0,
					olderSeasonOverride.current_size_bytes -
						expectedEpisodeBytes * olderSeasonOverride.candidate_count
				)
			: null
	);
	const actualSampleSizes = $derived(reviewSampleSizes(folder));
	const sizeTarget = $derived(folderSizeTargetAnalysis(folder));
	const targetSummary = $derived(resolvedTargetSummary(folder));
	const targetConstraint = $derived(targetConstraintSummary(folder, status));
	const targetProvenance = $derived(targetProvenanceSummary(folder.target_size_provenance));
	const technicalVideo = $derived(technicalVideoPolicy(folder));
	const expectedSeasonBytes = $derived(expectedEpisodeBytes * productionEpisodeCount);
	const estimatedSeasonSavingsBytes = $derived(
		!isSeriesScope && expectedSeasonBytes > 0
			? Math.max(0, originalSeasonSize - expectedSeasonBytes)
			: null
	);
	const exactExpectedSizeChange = $derived(
		expectedSizeChange(originalSeasonSize, expectedEpisodeBytes)
	);
	const exactReviewFacts = $derived(exactReviewSizeFacts(originalSeasonSize, expectedEpisodeBytes));
	const exactApprovedRangeLabel = $derived(
		targetSummary
			? `${formatDecimalFileSize(targetSummary.finalLowerBoundBytes)}–${formatDecimalFileSize(targetSummary.finalUpperBoundBytes)}`
			: sizeTarget.lowerBoundBytes > 0 && sizeTarget.upperBoundBytes > 0
				? `${formatDecimalFileSize(sizeTarget.lowerBoundBytes)}–${formatDecimalFileSize(sizeTarget.upperBoundBytes)}`
				: ''
	);
	const exactExpectedInsideApprovedRange = $derived(
		expectedEpisodeBytes > 0 &&
			((targetSummary !== null &&
				expectedEpisodeBytes >= targetSummary.finalLowerBoundBytes &&
				expectedEpisodeBytes <= targetSummary.finalUpperBoundBytes) ||
				(targetSummary === null &&
					sizeTarget.lowerBoundBytes > 0 &&
					expectedEpisodeBytes >= sizeTarget.lowerBoundBytes &&
					expectedEpisodeBytes <= sizeTarget.upperBoundBytes))
	);
	const exactCalibrationJob = $derived(
		status.exact_calibration_job !== undefined
			? status.exact_calibration_job
			: (status.calibration_job ?? folder.calibration_job ?? null)
	);
	const exactTargetContract = $derived(calibrationJobTargetContract(exactCalibrationJob));
	const scopeActivity = $derived(overlappingCalibrationActivity(status, folder.prefix));
	const scopeActivityJob = $derived(scopeActivity?.job ?? null);
	const scopeTargetContract = $derived(calibrationJobTargetContract(scopeActivityJob));
	const activeJobTargetBytes = $derived(exactTargetContract?.target_size_bytes ?? 0);
	const activeJobTargetIsCurrent = $derived(
		['queued', 'running'].includes(asText(asRecord(exactCalibrationJob).status).toLowerCase())
	);
	const currentTargetBytes = $derived(
		targetSummary?.targetBytes ||
			(activeJobTargetIsCurrent ? activeJobTargetBytes : 0) ||
			sizeTarget.budgetBytes ||
			selectedGoal?.targetSizeBytes ||
			activeJobTargetBytes ||
			0
	);
	const sizeTargetLabel = $derived(
		currentTargetBytes > 0 ? formatDecimalFileSize(currentTargetBytes) : 'the requested size'
	);
	const underTargetIsAcceptable = $derived(
		sizeTarget.status === 'under_target' && calibrationAcceptsUnderTargetResult(folder) === true
	);
	const sizeTargetMissed = $derived(
		['over_target', 'missing_prediction'].includes(sizeTarget.status) ||
			(sizeTarget.status === 'under_target' && !underTargetIsAcceptable)
	);
	const riskSummary = $derived(compareRiskSummary(folder));
	const qualityMemory = $derived(qualityMemoryView(folder));
	const approvalBlocked = $derived(Boolean(targetConstraint || riskSummary?.blocked));
	const activeOperatorIntent = $derived(currentOperatorIntent(folder));
	const smallerReviewAdjustment = $derived(
		activeOperatorIntent
			? reviewSizeAdjustment(
					goals,
					compressionIntentOptions,
					'smaller',
					currentTargetBytes,
					expectedEpisodeBytes
				)
			: null
	);
	const higherQualityReviewAdjustment = $derived(
		activeOperatorIntent
			? reviewSizeAdjustment(goals, compressionIntentOptions, 'higher_quality', currentTargetBytes)
			: null
	);
	const revisionPaneForced = $derived(
		Boolean(targetConstraint || riskSummary?.blocked || sizeTargetMissed)
	);
	const revisionPaneVisible = $derived(
		!riskSummary?.requiresCadenceResolution && (revisionPaneForced || revisionPaneOpen)
	);
	const hasReviewFeedback = $derived(
		selectedConcerns.length > 0 || reviewFeedback.trim().length > 0
	);
	const selectedGoalSampleLower = $derived(
		selectedGoal
			? Math.round(
					selectedGoal.targetSizeBytes *
						(1 - selectedGoal.operatorIntent.size_goal.sample_projection_tolerance_percent / 100)
				)
			: 0
	);
	const selectedGoalSampleUpper = $derived(
		selectedGoal
			? Math.round(
					selectedGoal.targetSizeBytes *
						(1 + selectedGoal.operatorIntent.size_goal.sample_projection_tolerance_percent / 100)
				)
			: 0
	);
	const selectedGoalFinalLower = $derived(
		selectedGoal
			? Math.round(
					selectedGoal.targetSizeBytes *
						(1 - selectedGoal.operatorIntent.size_goal.final_output_tolerance_percent / 100)
				)
			: 0
	);
	const selectedGoalFinalUpper = $derived(
		selectedGoal
			? Math.round(
					selectedGoal.targetSizeBytes *
						(1 + selectedGoal.operatorIntent.size_goal.final_output_tolerance_percent / 100)
				)
			: 0
	);
	const crfLimitReached = $derived(
		asNumber(sampleResult.chosen_crf) > 0 &&
			asNumber(technicalVideo.max_crf) > 0 &&
			asNumber(sampleResult.chosen_crf) >= asNumber(technicalVideo.max_crf)
	);
	const selectedHost = $derived(
		hostOptions.find((host) => host.key === selectedHostKey) ?? hostOptions[0]
	);
	const noAvailableHosts = $derived(
		hostOptions.length > 0 && !hostOptions.some((host) => host.available !== false)
	);
	const activeSampleJob = $derived(asRecord(exactCalibrationJob));
	const activeSampleProgress = $derived(asRecord(activeSampleJob.progress));
	const activeSampleStatusLabel = $derived(calibrationActivityStatusLabel(exactCalibrationJob));
	const activeSampleWorker = $derived(
		asText(asRecord(activeSampleJob.host).label) || selectedHost?.label || 'Choosing one'
	);
	const activeSampleWorkCompleted = $derived(
		asNumber(asRecord(activeSampleProgress.work).completed)
	);
	const activeSampleWorkTotal = $derived(asNumber(asRecord(activeSampleProgress.work).total));
	const activeSampleWorkProgress = $derived(calibrationWorkProgress(exactCalibrationJob));
	const activeSampleWorkPercent = $derived(
		activeSampleWorkTotal > 0
			? Math.min(100, Math.max(0, (activeSampleWorkCompleted / activeSampleWorkTotal) * 100))
			: 0
	);
	const activeSampleStageKey = $derived(asText(activeSampleProgress.stage));
	const activeSampleBuildingComparison = $derived(
		['selecting_review_moments', 'building_review', 'saving_results', 'completed'].includes(
			activeSampleStageKey
		)
	);
	const activeSampleEpisodeTargetLabel = $derived(
		targetSummary?.targetBytes ? formatDecimalFileSize(targetSummary.targetBytes) : sizeTargetLabel
	);
	const activeSampleRuntimeLabel = $derived(
		targetSummary?.itemRuntimeSeconds ? formatDuration(targetSummary.itemRuntimeSeconds) : ''
	);
	const finishedEpisodeCount = $derived(
		asNumber(folder.summary?.statuses.encoded) +
			asNumber(folder.summary?.statuses.validated) +
			asNumber(folder.summary?.statuses.promoted)
	);
	const encodeProgress = $derived(
		scopedEncodeProgress(folder.encode_job, finishedEpisodeCount, episodeCount)
	);
	const seasonProgressCompleted = $derived(encodeProgress.completed);
	const seasonProgressPercent = $derived(encodeProgress.percent);
	const activeOlderSeasonCount = $derived(
		isSeriesScope &&
			olderSeasonOverride &&
			encodeProgress.total > 0 &&
			encodeProgress.total === olderSeasonOverride.candidate_count
			? olderSeasonOverride.season_count
			: 0
	);
	const recoveryNeedsFreshGoal = $derived(
		asText(asRecord(folder.encode_job?.progress?.failure_analysis).retry_strategy) ===
			'fresh_goal_required' ||
			asText(folder.encode_job?.error)
				.toLowerCase()
				.includes('final output size missed the approved target band')
	);
	const recoveryNeedsAdjustment = $derived(
		humanState.recoveryKind === 'season' &&
			Boolean(folder.encode_job?.progress?.failure_analysis) &&
			!recoveryNeedsFreshGoal
	);
	const pageIsCinematic = $derived(
		['sample_waiting', 'making_test', 'ready_to_compare', 'making_season'].includes(
			humanState.key
		) && !retryMode
	);
	const actionElapsed = $derived(elapsedCopy(actionStartedAt ? clock - actionStartedAt : 0));
	const backendElapsed = $derived(
		asNumber(activeSampleProgress.elapsed_seconds) > 0
			? elapsedCopy(asNumber(activeSampleProgress.elapsed_seconds) * 1000)
			: elapsedCopy(
					clock - parseTimestamp(activeSampleJob.started_at ?? activeSampleJob.created_at)
				)
	);
	const activeSampleStageElapsed = $derived(
		asNumber(activeSampleProgress.stage_elapsed_seconds) > 0
			? elapsedCopy(asNumber(activeSampleProgress.stage_elapsed_seconds) * 1000)
			: ''
	);
	const activeSampleUpdated = $derived(
		calibrationFreshnessLabel(activeSampleProgress.heartbeat_age_seconds)
	);
	const scopeActivityStatus = $derived(scopeActivityJob?.status ?? 'idle');
	const scopeActivityFailure = $derived(['failed', 'stopped'].includes(scopeActivityStatus));
	const scopeActivityReady = $derived(
		['completed', 'pending_review'].includes(scopeActivityStatus)
	);
	const showScopeActivity = $derived(
		shouldPrioritizeScopeActivity(
			scopeActivity,
			hasOwnCalibration,
			[
				'making_season',
				'ready_to_check',
				'ready_to_finish',
				'finish_blocked',
				'finished',
				'needs_help'
			].includes(humanState.key)
		)
	);
	const scopeActivityScope = $derived(scopeActivityJob?.activity_scope ?? null);
	const scopeActivityOwnerPrefix = $derived(scopeActivityJob?.prefix ?? '');
	const scopeActivityOwnerTitle = $derived(
		scopeActivityScope?.title || seasonIdentity(scopeActivityOwnerPrefix).show || 'Related work'
	);
	const scopeActivityOwnerKind = $derived(
		scopeActivityScope?.kind === 'tv_series'
			? 'show'
			: scopeActivityScope?.kind === 'tv_season'
				? 'season'
				: 'scope'
	);
	const scopeActivityTargetLabel = $derived(
		scopeTargetContract
			? formatDecimalFileSize(scopeTargetContract.target_size_bytes)
			: 'Saved target unavailable'
	);
	const scopeActivityWorker = $derived(
		asText(asRecord(scopeActivityJob?.host).label) || 'Choosing a computer'
	);
	const scopeActivityElapsed = $derived(
		elapsedCopy(
			clock - parseTimestamp(scopeActivityJob?.started_at ?? scopeActivityJob?.created_at ?? null)
		)
	);
	const scopeActivityHref = $derived(
		scopeActivityOwnerPrefix ? resolve(folderRoutePath(scopeActivityOwnerPrefix)) : resolve('/ops')
	);
	const scopeActivityStage = $derived(calibrationStageLabel(scopeActivityJob));
	const scopeActivityWork = $derived(calibrationWorkLabel(scopeActivityJob));
	const scopeActivityLiveness = $derived(calibrationLivenessLabel(scopeActivityJob));
	const scopeActivityEta = $derived(calibrationEtaSummary(scopeActivityJob));
	const activeSampleStage = $derived(calibrationStageLabel(exactCalibrationJob));
	const activeSampleWork = $derived(calibrationWorkLabel(exactCalibrationJob));
	const activeSampleLiveness = $derived(calibrationLivenessLabel(exactCalibrationJob));
	const activeSampleEta = $derived(calibrationEtaSummary(exactCalibrationJob));
	const recoveryScopeTitle = $derived(exactCalibrationJob?.activity_scope?.title || scopeTitle);
	const recoveryWorker = $derived(
		asText(asRecord(exactCalibrationJob?.host).label) || 'Saved computer preference'
	);
	const showGoalScreen = $derived(humanState.key === 'needs_test' || retryMode);
	const actionPending = $derived(actionPhase !== 'idle');

	$effect(() => {
		if (selectedMoment !== displayedMoment) selectedMoment = displayedMoment;
	});

	$effect(() => {
		const nextStateKey = `${folder.prefix}:${humanState.key}`;
		if (!actionMessageStateKey) {
			actionMessageStateKey = nextStateKey;
			return;
		}
		if (actionMessageStateKey === nextStateKey) return;
		actionMessageStateKey = nextStateKey;
		actionMessage = '';
		actionMessageTone = 'success';
	});

	$effect(() => {
		if (revisionPanePrefix === folder.prefix) return;
		revisionPanePrefix = folder.prefix;
		revisionPaneOpen = false;
		revisionMode = 'same_target';
		selectedConcerns = [];
		reviewFeedback = '';
		operatorInstructions = '';
	});

	$effect(() => {
		if (revisionMode === 'roomier' && !higherQualityReviewAdjustment) {
			revisionMode = 'same_target';
		}
	});

	$effect(() => {
		const availableHosts = hostOptions;
		if (!availableHosts.length) {
			selectedHostKey = '';
			return;
		}
		if (!availableHosts.some((host) => host.key === selectedHostKey && host.available !== false)) {
			const configured = availableHosts.find(
				(host) => host.key === folder.sample_host_key && host.available !== false
			);
			selectedHostKey =
				configured?.key ?? availableHosts.find((host) => host.available !== false)?.key ?? '';
		}
	});

	onMount(() => {
		const timer = window.setInterval(() => (clock = Date.now()), 1000);
		return () => window.clearInterval(timer);
	});

	function asRecord(value: unknown): Record<string, unknown> {
		return value && typeof value === 'object' && !Array.isArray(value)
			? (value as Record<string, unknown>)
			: {};
	}

	function asText(value: unknown): string {
		return typeof value === 'string' ? value.trim() : '';
	}

	function asNumber(value: unknown): number {
		const parsed = Number(value ?? 0);
		return Number.isFinite(parsed) ? parsed : 0;
	}

	function parseTimestamp(value: unknown): number {
		const timestamp = Date.parse(asText(value));
		return Number.isFinite(timestamp) ? timestamp : clock;
	}

	function elapsedCopy(milliseconds: number): string {
		if (!Number.isFinite(milliseconds) || milliseconds < 1000) return 'just started';
		const seconds = Math.floor(milliseconds / 1000);
		if (seconds < 60) return `${seconds} sec`;
		const minutes = Math.floor(seconds / 60);
		if (minutes < 60) return `${minutes} min`;
		const hours = Math.floor(minutes / 60);
		return `${hours} hr ${minutes % 60} min`;
	}

	function encodePrefix(prefix: string): string {
		return prefix
			.split('/')
			.map((segment) => encodeURIComponent(segment))
			.join('/');
	}

	function endpoint(action: string): string {
		return `/api/folders/${encodePrefix(folder.prefix)}/${action}`;
	}

	function ensureOk(response: ActionResponse, fallback: string): ActionResponse {
		if (response.ok === false) throw new Error(response.message || fallback);
		return response;
	}

	async function makeTest() {
		if (!selectedGoal || !goalSelectionConfirmed) {
			actionError = 'Choose how this legacy size should behave before creating the sample.';
			return;
		}
		if (!selectedOperatorIntent || !compressionIntentConfirmed) {
			actionError =
				'Choose how Mediaforce should balance size and quality before creating the sample.';
			return;
		}
		await startTest(
			testRequestWithInstructions(goalRequest(selectedGoal), operatorInstructions),
			selectedOperatorIntent
		);
	}

	async function retryMeasuredTarget() {
		const measuredRequest = measuredFollowupRequest(sizeTarget, underTargetIsAcceptable);
		const baseRequest =
			measuredRequest ||
			`Keep the ${sizeTargetLabel} whole-episode goal and current resolution. Make another representative test that addresses the operator's review concerns without changing the size target.`;
		const note = hasReviewFeedback
			? reviewFeedbackRequest(baseRequest, selectedConcerns, reviewFeedback, operatorInstructions)
			: testRequestWithInstructions(baseRequest, operatorInstructions);
		if (!note) {
			actionError =
				'The previous sample did not preserve its requested size. Choose a size and try again.';
			return;
		}
		const operatorIntent = hasReviewFeedback
			? reviewFeedbackIntent(currentOperatorIntent(folder), selectedConcerns, reviewFeedback)
			: currentOperatorIntent(folder);
		await startTest(note, operatorIntent);
	}

	async function submitReviewFeedback() {
		if (!hasReviewFeedback) {
			actionError =
				'Choose a concern or describe what should change before creating a revised sample.';
			return;
		}
		if (!revisionPaneForced && revisionMode === 'roomier') {
			await chooseReviewAdjustment('higher_quality');
			return;
		}
		await retryMeasuredTarget();
	}

	function toggleConcern(tag: QualityRiskTag) {
		selectedConcerns = selectedConcerns.includes(tag)
			? selectedConcerns.filter((selectedTag) => selectedTag !== tag)
			: [...selectedConcerns, tag];
	}

	async function startTest(
		note: string,
		operatorIntent: OperatorIntentRequestPayload | null = null
	) {
		actionError = '';
		actionMessage = '';
		actionMessageTone = 'success';
		blockerAction = null;
		actionStartedAt = Date.now();
		actionPhase = 'planning';
		let succeeded = false;
		try {
			const preview = ensureOk(
				await postJson<ActionResponse>(endpoint('ai-tune/preview'), {
					note,
					host_key: selectedHostKey,
					operator_intent: operatorIntent
				}),
				'We couldn’t prepare the sample.'
			);
			const proposal = asRecord(preview.proposal);
			const proposalId = asText(proposal.proposal_id);
			if (!proposalId || proposal.can_queue === false) {
				throw new Error(preview.message || 'The sample plan needs attention before it can start.');
			}
			actionPhase = 'starting';
			ensureOk(
				await postJson<ActionResponse>(endpoint('ai-tune/confirm'), {
					proposal_id: proposalId
				}),
				'We prepared the sample but couldn’t start it.'
			);
			retryMode = false;
			revisionPaneOpen = false;
			revisionMode = 'same_target';
			selectedConcerns = [];
			reviewFeedback = '';
			operatorInstructions = '';
			actionMessage = 'Your sample is starting.';
			await onMutate();
			succeeded = true;
		} catch (error) {
			actionError = humanActionError(error, 'We couldn’t start the sample.');
		} finally {
			actionPhase = 'idle';
			if (succeeded) await focusCurrentHeading();
		}
	}

	async function retryTest() {
		await runAction('recovering', 'We couldn’t restart the sample.', async () => {
			ensureOk(
				await postJson<ActionResponse>(endpoint('ai-tune/confirm'), { proposal_id: '' }),
				'We couldn’t restart the saved sample.'
			);
		});
	}

	async function approveTest(confirmHighImpact = false, confirmSizeTradeoff = false) {
		const draftHash = asText(calibration.draft_hash);
		actionError = '';
		actionMessage = '';
		actionMessageTone = 'success';
		blockerAction = null;
		actionStartedAt = Date.now();
		actionPhase = 'approving';
		let succeeded = false;
		try {
			ensureOk(
				await postJson<ActionResponse>(endpoint('save-profile'), {
					confirm_high_impact: confirmHighImpact,
					confirm_size_tradeoff: confirmSizeTradeoff,
					reviewed_draft_hash: draftHash
				}),
				'We couldn’t save your decision.'
			);
			await onMutate();
			succeeded = true;
		} catch (error) {
			const message = error instanceof Error ? error.message : '';
			const guard = approvalGuardFromMessage(message, confirmHighImpact, confirmSizeTradeoff);
			if (guard) {
				await openSafetyDialog({
					kind: 'approval',
					title: guard.title,
					detail: guard.detail,
					primaryLabel: 'Keep this sample',
					confirmHighImpact: guard.confirmHighImpact,
					confirmSizeTradeoff: guard.confirmSizeTradeoff,
					changes: guard.kind === 'high_impact' ? approvalChanges() : []
				});
			} else {
				actionError = humanActionError(error, 'We couldn’t accept the sample.');
			}
		} finally {
			actionPhase = 'idle';
			if (succeeded) await focusCurrentHeading();
		}
	}

	async function queueSeason(overridePolicyHolds = false) {
		await runAction('queueing', `We couldn’t start the ${scopeNoun}.`, async () => {
			ensureOk(
				await postJson<ActionResponse>(endpoint('queue-encode'), {
					notes: 'Approved after comparing the representative sample.',
					bypass_schedule: false,
					override_policy_holds: overridePolicyHolds
				}),
				isExactItemScope
					? 'We couldn’t start this episode.'
					: 'We couldn’t start the remaining episodes.'
			);
		});
	}

	async function requestQueueSeason() {
		if (!isSeriesScope && heldEpisodeCount > 0) {
			if (!canOverrideLifecycleHolds) return;
			await openSafetyDialog({
				kind: 'lifecycle_override',
				title: 'Override this season’s protection?',
				detail: `This will queue ${heldEpisodeCount} protected ${heldEpisodeCount === 1 ? 'episode' : 'episodes'} now. It bypasses only lifecycle timing holds; normal file, validation, and active-work safeguards still apply.`,
				primaryLabel: 'Override and queue',
				changes: lifecycleHoldReasons.map((reason) => `${reason.label}: ${reason.detail}`)
			});
			return;
		}
		await queueSeason(false);
	}

	async function queueOlderSeasons() {
		await runAction('queueing', 'We couldn’t start the older seasons.', async () => {
			const response = ensureOk(
				await postJson<ActionResponse>(endpoint('queue-older-seasons'), {
					notes: 'Approved after reviewing the explicit older-season lifecycle override.',
					bypass_schedule: false,
					confirmed: true
				}),
				'We couldn’t start the older seasons.'
			);
			actionMessage = response.message || 'Queued the safety-cleared older seasons.';
		});
	}

	async function requestQueueOlderSeasons() {
		const selection = olderSeasonOverride;
		if (!selection?.available) return;
		const latestSeason = selection.latest_season_label || 'The latest season';
		const changes = [
			`${selection.season_count} ${selection.season_count === 1 ? 'season' : 'seasons'} · ${selection.candidate_count} safety-cleared ${selection.candidate_count === 1 ? 'episode' : 'episodes'}.`,
			`Safety-cleared size: ${formatDecimalFileSize(selection.current_size_bytes)}.`,
			olderSeasonProjectedSavingsBytes !== null
				? `Estimated space saved: about ${formatDecimalFileSize(olderSeasonProjectedSavingsBytes)}.`
				: 'Estimated space saved is not available for this setup.',
			`${latestSeason} stays original.`,
			cadenceBlockedEpisodeCount > 0
				? `${cadenceBlockedEpisodeCount} ${cadenceBlockedEpisodeCount === 1 ? 'episode has' : 'episodes have'} a measured motion pattern that Mediaforce cannot convert automatically and will stay original.`
				: '',
			cadenceEvidenceRequiredEpisodeCount > 0
				? `${cadenceEvidenceRequiredEpisodeCount} ${cadenceEvidenceRequiredEpisodeCount === 1 ? 'episode still needs' : 'episodes still need'} motion-pattern analysis and will stay original for now.`
				: '',
			selection.overridden_candidate_count > 0
				? `${selection.overridden_candidate_count} ${selection.overridden_candidate_count === 1 ? 'episode bypasses' : 'episodes bypass'} lifecycle timing holds.`
				: 'No lifecycle hold is bypassed; this action only excludes the latest season.',
			selection.already_eligible_candidate_count > 0
				? `${selection.already_eligible_candidate_count} already-eligible ${selection.already_eligible_candidate_count === 1 ? 'episode is' : 'episodes are'} included.`
				: '',
			'The current-season policy does not change.',
			'Specials, ambiguous seasons, and episodes without motion-pattern clearance remain excluded.'
		].filter(Boolean);
		await openSafetyDialog({
			kind: 'older_seasons_override',
			title: `Process ${selection.season_count} older ${selection.season_count === 1 ? 'season' : 'seasons'} now?`,
			detail: `This queues ${selection.candidate_count} safety-cleared ${selection.candidate_count === 1 ? 'episode' : 'episodes'} with the approved setup. ${cadenceExcludedEpisodeCount > 0 ? `${cadenceExcludedEpisodeCount} ${cadenceExcludedEpisodeCount === 1 ? 'episode stays' : 'episodes stay'} original for motion-pattern safety. ` : ''}Mediaforce will recheck the selection before anything starts.`,
			primaryLabel: 'Process older seasons',
			changes
		});
	}

	async function checkOutputs() {
		await runAction(
			'checking',
			isExactItemScope ? 'We couldn’t check this episode.' : 'We couldn’t check the new episodes.',
			async () => {
				ensureOk(
					await postJson<ActionResponse>(endpoint('validate-outputs'), {}),
					isExactItemScope
						? 'We couldn’t check this episode.'
						: 'We couldn’t check the new episodes.'
				);
			}
		);
	}

	async function reconnectStorageAndCheckOutputs() {
		if (!storageRecoveryHost) return;
		await runAction(
			'checking',
			isExactItemScope
				? 'We couldn’t reconnect and check this episode.'
				: 'We couldn’t reconnect and check the new episodes.',
			async () => {
				ensureOk(
					await postJson<ActionResponse>(`${resolve('/')}api/hosts/prepare`, {
						host_key: storageRecoveryHost.key
					}),
					'We couldn’t reconnect the working folder.'
				);
				ensureOk(
					await postJson<ActionResponse>(endpoint('validate-outputs'), {}),
					isExactItemScope
						? 'Storage reconnected, but we couldn’t check this episode.'
						: 'Storage reconnected, but we couldn’t check the new episodes.'
				);
			}
		);
	}

	async function finishSeason() {
		await runAction('finishing', `We couldn’t finish the ${scopeNoun}.`, async () => {
			ensureOk(
				await postJson<ActionResponse>(endpoint('promote-outputs'), {}),
				isExactItemScope
					? 'We couldn’t put the new episode into your library.'
					: 'We couldn’t put the new episodes into your library.'
			);
		});
	}

	async function recoverSeason() {
		if (recoveryNeedsFreshGoal) {
			await chooseDifferentSize();
			return;
		}
		const reviewGateStatus = asText(asRecord(folder.review_gate).status);
		const incompleteSavedTest =
			(Boolean(asText(calibration.job_id)) && !asText(calibration.draft_hash)) ||
			reviewGateStatus === 'missing_review_media';
		if (incompleteSavedTest) {
			if (!goalSelectionConfirmed) {
				actionError = '';
				retryMode = true;
				await focusCurrentHeading();
				return;
			}
			const fallbackNote = selectedGoal ? goalRequest(selectedGoal) : '';
			await startTest(
				asText(calibration.notes) || fallbackNote,
				currentOperatorIntent(folder) ?? selectedGoal?.operatorIntent ?? null
			);
			return;
		}
		if (
			status.retryable_sample_job ||
			['failed', 'stopped'].includes(asText(activeSampleJob.status))
		) {
			await retryTest();
			return;
		}
		const failureAnalysis = folder.encode_job?.progress?.failure_analysis;
		if (failureAnalysis) {
			await openSafetyDialog({
				kind: 'recovery',
				title: isExactItemScope
					? 'This episode needs a small adjustment.'
					: 'The unfinished episodes need a small adjustment.',
				detail: isExactItemScope
					? 'Mediaforce found a measured setting that should let it finish. The result may not match the approved sample exactly; nothing in your library has changed.'
					: 'Mediaforce found a measured setting that should let them finish. Those episodes may not match the approved sample exactly; episodes already compressed will not change.',
				primaryLabel: 'Adjust and retry'
			});
			return;
		}
		await runAction(
			'recovering',
			isExactItemScope
				? 'We couldn’t restart this episode.'
				: 'We couldn’t restart the unfinished episodes.',
			async () => {
				ensureOk(
					await postJson<ActionResponse>(endpoint('queue-encode'), {
						notes: isExactItemScope ? 'Retry this episode.' : 'Retry unfinished episodes.',
						bypass_schedule: false
					}),
					isExactItemScope
						? 'We couldn’t retry this episode.'
						: 'We couldn’t retry the unfinished episodes.'
				);
			}
		);
	}

	async function performMeasuredRecovery() {
		await runAction(
			'recovering',
			isExactItemScope
				? 'We couldn’t adjust and retry this episode.'
				: 'We couldn’t adjust and retry the unfinished episodes.',
			async () => {
				ensureOk(
					await postJson<ActionResponse>(endpoint('approve-recovery'), {}),
					isExactItemScope
						? 'We couldn’t adjust and retry this episode.'
						: 'We couldn’t adjust and retry the unfinished episodes.'
				);
			}
		);
	}

	async function confirmSafetyDialog() {
		const dialog = safetyDialog;
		if (!dialog) return;
		safetyDialog = null;
		safetyDialogReturnFocus = null;
		if (dialog.kind === 'approval') {
			await approveTest(dialog.confirmHighImpact, dialog.confirmSizeTradeoff);
			return;
		}
		if (dialog.kind === 'lifecycle_override') {
			await queueSeason(true);
			return;
		}
		if (dialog.kind === 'older_seasons_override') {
			await queueOlderSeasons();
			return;
		}
		if (dialog.kind === 'review_adjustment') {
			if (!dialog.adjustment || !activeOperatorIntent) {
				actionError = '';
				actionMessage =
					'The guided adjustment is no longer available. Choose another size instead.';
				actionMessageTone = 'neutral';
				await chooseDifferentSize();
				return;
			}
			const operatorIntent = reviewAdjustmentIntent(activeOperatorIntent, dialog.adjustment);
			if (dialog.adjustmentKind === 'roomier') {
				await startTest(
					reviewFeedbackRequest(
						goalRequest(dialog.adjustment.goal),
						selectedConcerns,
						reviewFeedback,
						operatorInstructions
					),
					operatorIntent
				);
				return;
			}
			await startTest(
				testRequestWithInstructions(goalRequest(dialog.adjustment.goal), operatorInstructions),
				operatorIntent
			);
			return;
		}
		await performMeasuredRecovery();
	}

	async function dismissSafetyDialog() {
		safetyDialog = null;
		await tick();
		if (safetyDialogReturnFocus?.isConnected) {
			safetyDialogReturnFocus.focus();
			safetyDialogReturnFocus = null;
			return;
		}
		safetyDialogReturnFocus = null;
		const selector =
			humanState.key === 'ready_to_compare'
				? '.decision .primary-button'
				: '.help-room .primary-button';
		(document.querySelector(selector) as HTMLElement | null)?.focus();
	}

	function handleSafetyDialogKeydown(event: KeyboardEvent) {
		if (event.key === 'Escape') {
			event.preventDefault();
			void dismissSafetyDialog();
			return;
		}
		if (event.key !== 'Tab') return;
		const dialog = event.currentTarget as HTMLElement;
		const focusable = Array.from(
			dialog.querySelectorAll<HTMLElement>(
				'button:not(:disabled), a[href], [tabindex]:not([tabindex="-1"])'
			)
		).filter((element) => !element.hidden);
		if (focusable.length === 0) return;
		const first = focusable[0];
		const last = focusable[focusable.length - 1];
		const active = document.activeElement;
		if (event.shiftKey && (active === first || !dialog.contains(active))) {
			event.preventDefault();
			last.focus();
		} else if (!event.shiftKey && (active === last || !dialog.contains(active))) {
			event.preventDefault();
			first.focus();
		}
	}

	async function runAction(phase: ActionPhase, fallback: string, operation: () => Promise<void>) {
		actionError = '';
		actionMessage = '';
		actionMessageTone = 'success';
		blockerAction = null;
		actionStartedAt = Date.now();
		actionPhase = phase;
		let succeeded = false;
		try {
			await operation();
			await onMutate();
			succeeded = true;
		} catch (error) {
			actionError = humanActionError(error, fallback);
		} finally {
			actionPhase = 'idle';
			if (succeeded) await focusCurrentHeading();
		}
	}

	function humanActionError(error: unknown, fallback: string): string {
		if (error instanceof ApiError) {
			const route = String(error.payload?.next_route ?? '').trim();
			const label = String(error.payload?.next_action_label ?? '').trim();
			if (route === '/ops' && label) blockerAction = { route, label };
		}
		const message = error instanceof Error ? error.message.trim() : '';
		if (!message) return fallback;
		const lower = message.toLowerCase();
		if (lower.includes('timeout') || lower.includes('timed out')) {
			return 'That took longer than expected. Nothing was queued; please try again.';
		}
		if (lower.includes('permission') || lower.includes('denied')) {
			return 'The selected computer could not reach the files. Choose another computer in Details or try again.';
		}
		return message;
	}

	function phaseCopy(phase: ActionPhase): { title: string; detail: string } {
		const copy: Record<Exclude<ActionPhase, 'idle'>, { title: string; detail: string }> = {
			planning: {
				title: 'Preparing your sample',
				detail: 'Choosing settings for your size goal. This first step can take a few minutes.'
			},
			starting: {
				title: 'Starting your sample',
				detail: `Sending one representative episode to ${selectedHost?.label || 'an available computer'}.`
			},
			approving: {
				title: 'Saving your decision',
				detail: `Recording the sample you chose and checking whether compression can start.`
			},
			queueing: {
				title: 'Starting compression',
				detail: isExactItemScope
					? 'Preparing this episode and finding an available computer.'
					: 'Preparing the remaining episodes and finding available computers.'
			},
			checking: {
				title: isExactItemScope ? 'Checking the episode' : 'Checking every episode',
				detail: isExactItemScope
					? 'Confirming that the new file opens, plays, and matches the original length.'
					: 'Confirming that each new file opens, plays, and matches the original length.'
			},
			finishing: {
				title: isExactItemScope
					? 'Replacing the original episode'
					: 'Replacing the original episodes',
				detail: isExactItemScope
					? 'Moving the original to the cleanup folder and placing the checked smaller file in your library.'
					: 'Moving the originals to the cleanup folder and placing the checked smaller files in your library.'
			},
			recovering: {
				title: 'Trying again',
				detail: isExactItemScope
					? 'Keeping your original safe and restarting only this episode.'
					: 'Keeping completed work and restarting only what still needs attention.'
			}
		};
		return phase === 'idle' ? { title: '', detail: '' } : copy[phase];
	}

	function approvalChanges(): string[] {
		const baselinePolicy = asRecord(asRecord(calibration.sample_item).resolved_policy);
		const baselineVideo = asRecord(baselinePolicy.video);
		const draftVideo = asRecord(asRecord(calibration.policy).video);
		const changed = (key: string) =>
			JSON.stringify(baselineVideo[key] ?? null) !== JSON.stringify(draftVideo[key] ?? null);
		const changes: string[] = [];
		if (
			['quality_metric', 'target_vmaf', 'min_target_vmaf', 'target_xpsnr', 'min_target_xpsnr'].some(
				changed
			)
		) {
			changes.push('Picture quality checks will be updated.');
		}
		if (changed('max_encoded_percent')) {
			changes.push(
				`Largest allowed file: ${policyPercent(baselineVideo.max_encoded_percent)} → ${policyPercent(draftVideo.max_encoded_percent)}`
			);
		}
		if (changed('default_grain')) {
			changes.push('Natural texture handling will be updated.');
		}
		return changes;
	}

	function policyPercent(value: unknown): string {
		if (value === null || value === undefined || value === '') return 'not set';
		return `${asNumber(value)}% of the original`;
	}

	async function focusSafetyDialog() {
		await tick();
		(document.querySelector('.safety-dialog .secondary-button') as HTMLElement | null)?.focus();
	}

	async function openSafetyDialog(dialog: SafetyDialog) {
		safetyDialogReturnFocus =
			document.activeElement instanceof HTMLElement ? document.activeElement : null;
		safetyDialog = dialog;
		await focusSafetyDialog();
	}

	async function confirmSizeTradeoff() {
		await openSafetyDialog({
			kind: 'approval',
			title: `Accept ${formatDecimalFileSize(expectedEpisodeBytes)} per episode instead of ${sizeTargetLabel}?`,
			detail: `This saves the sample settings as the ${scopeNoun} profile. Production remains separate until you choose ${makeActionLabel}. It does not change this result to ${sizeTargetLabel} per episode.`,
			primaryLabel: 'Accept this result',
			confirmSizeTradeoff: true,
			changes: [
				`Requested: ${sizeTargetLabel} per episode`,
				`Measured estimate: ${formatDecimalFileSize(expectedEpisodeBytes)} per episode`,
				`Next action: choose ${makeActionLabel} when you are ready`
			]
		});
	}

	async function focusCurrentHeading() {
		await tick();
		const heading = document.querySelector('main h1') as HTMLElement | null;
		if (!heading) return;
		heading.setAttribute('tabindex', '-1');
		heading.focus();
	}

	async function chooseDifferentSize() {
		retryMode = true;
		await focusCurrentHeading();
	}

	async function openRevisionPane() {
		revisionPaneOpen = true;
		await tick();
		document.getElementById('revision-pane-title')?.focus();
	}

	async function closeRevisionPane() {
		revisionPaneOpen = false;
		await tick();
		(
			document.querySelector('[data-review-action="needs-improvement"]') as HTMLElement | null
		)?.focus();
	}

	async function chooseReviewAdjustment(direction: ReviewSizeAdjustmentDirection) {
		const adjustment =
			direction === 'smaller' ? smallerReviewAdjustment : higherQualityReviewAdjustment;
		if (!adjustment) {
			actionError = '';
			actionMessage =
				direction === 'smaller'
					? 'A guided smaller target is not available for this sample. Choose another size instead.'
					: 'A larger guided target is not available. Choose another size instead.';
			actionMessageTone = 'neutral';
			await chooseDifferentSize();
			return;
		}
		const target = formatDecimalFileSize(adjustment.goal.targetSizeBytes);
		const current = formatDecimalFileSize(expectedEpisodeBytes);
		const roomier = direction === 'higher_quality';
		await openSafetyDialog({
			kind: 'review_adjustment',
			title: roomier ? 'Allow a larger file for the next sample?' : 'Try a smaller version?',
			detail: roomier
				? `Mediaforce will create one sample targeting about ${target} instead of about ${sizeTargetLabel}. The episode will be larger, with more room for picture quality. Resolution and quality checks stay the same. Nothing will be replaced.`
				: `Mediaforce will create one smaller sample targeting about ${target} for this ${scopeNoun}. It keeps the same resolution and quality checks. Nothing will be replaced.`,
			primaryLabel: roomier ? 'Create the larger-file sample' : 'Create the smaller sample',
			changes: roomier
				? [
						`Reviewed target: about ${sizeTargetLabel}`,
						`Next target: about ${target} — not judged yet`,
						'Approach: use the added space for the highest measured fidelity',
						'Same resolution and quality checks'
					]
				: [
						...(expectedEpisodeBytes > 0 ? [`Current sample: about ${current}`] : []),
						`Next target: about ${target}`,
						'Approach: keep shrinking only while picture and sound remain acceptable',
						'Same resolution and quality checks',
						...(hasReviewFeedback
							? [
									'Revision concerns will be cleared after the smaller sample starts and are not sent with it'
								]
							: []),
						...(operatorInstructions.trim()
							? ['Other priorities are included with the smaller sample']
							: [])
					],
			adjustment,
			adjustmentKind: roomier ? 'roomier' : 'smaller'
		});
	}

	function selectGoal(key: SizeGoal['key']) {
		selectedGoalKey = key;
		selectedGoalPrefix = folder.prefix;
	}

	function selectCompressionIntent(level: CompressionIntentLevel) {
		selectedCompressionIntentLevel = level;
		selectedCompressionIntentPrefix = folder.prefix;
	}

	function isGoalSelected(goal: SizeGoal): boolean {
		if (requiresExplicitGoalSelection && selectedGoalPrefix !== folder.prefix) return false;
		return selectedGoal === goal;
	}

	function goalTabIndex(goal: SizeGoal, index: number): number {
		return isGoalSelected(goal) || (!goalSelectionConfirmed && index === 0) ? 0 : -1;
	}

	async function handleGoalKeydown(event: KeyboardEvent, index: number) {
		let nextIndex: number;
		if (event.key === 'ArrowRight' || event.key === 'ArrowDown') nextIndex = index + 1;
		else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') nextIndex = index - 1;
		else if (event.key === 'Home') nextIndex = 0;
		else if (event.key === 'End') nextIndex = goals.length - 1;
		else return;
		event.preventDefault();
		nextIndex = (nextIndex + goals.length) % goals.length;
		selectGoal(goals[nextIndex].key);
		await tick();
		goalButtons[nextIndex]?.focus();
	}

	async function handleCompressionIntentKeydown(event: KeyboardEvent, index: number) {
		let nextIndex: number;
		if (event.key === 'ArrowRight' || event.key === 'ArrowDown') nextIndex = index + 1;
		else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') nextIndex = index - 1;
		else if (event.key === 'Home') nextIndex = 0;
		else if (event.key === 'End') nextIndex = compressionIntentOptions.length - 1;
		else return;
		event.preventDefault();
		nextIndex = (nextIndex + compressionIntentOptions.length) % compressionIntentOptions.length;
		selectCompressionIntent(compressionIntentOptions[nextIndex].key);
		await tick();
		compressionIntentButtons[nextIndex]?.focus();
	}

	function chooseMoment(index: number) {
		selectedMoment = index;
	}

	async function openEpisode() {
		if (!selectedEpisode || episodeNavigationPending) return;
		episodeNavigationPending = true;
		try {
			await goto(resolve(selectedEpisode.href), { keepFocus: true, noScroll: true });
		} finally {
			episodeNavigationPending = false;
		}
	}

	function downloadComparison() {
		window.location.assign(apiDownloadHref(endpoint('review-compare/download')));
	}

	function technicalPolicy() {
		return technicalVideo;
	}
</script>

<svelte:head>
	<title>{scopeTitle} · Mediaforce</title>
</svelte:head>

<div class:cinematic={pageIsCinematic} class="experience-page">
	<div class="ambient" aria-hidden="true"></div>
	<header class="experience-header">
		<a class="back-link" href={backHref}>
			<svg viewBox="0 0 20 20" aria-hidden="true"><path d="m12.5 4.5-5.5 5.5 5.5 5.5" /></svg>
			{backLabel}
		</a>
		<a class="wordmark" href={resolve('/')}>Mediaforce</a>
		<span class="header-season">{scopeTitle}</span>
	</header>

	<main data-folder-ready-marker={!folder.pending ? scopeTitle : undefined}>
		{#if loadError || actionError}
			<div class="action-notice" role="alert">
				<span aria-hidden="true">!</span>
				<div>
					<strong>Something needs your attention</strong>
					<p>{actionError || loadError}</p>
					{#if blockerAction}
						<a class="action-notice__link" href={resolve(blockerAction.route)}
							>{blockerAction.label}</a
						>
					{/if}
				</div>
			</div>
		{:else if actionMessage}
			<div
				class="action-notice"
				class:action-notice--success={actionMessageTone === 'success'}
				class:action-notice--neutral={actionMessageTone === 'neutral'}
				role="status"
			>
				<span aria-hidden="true">{actionMessageTone === 'success' ? '✓' : 'i'}</span>
				<div><strong>{actionMessage}</strong></div>
			</div>
		{/if}

		{#if !folder.pending && heldEpisodeCount > 0 && humanState.key !== 'making_test'}
			<div class="lifecycle-notice" role="status">
				<span aria-hidden="true">◆</span>
				<div>
					<strong>
						{isSeriesScope
							? `${heldEpisodeCount} ${heldEpisodeCount === 1 ? 'episode is' : 'episodes are'} protected`
							: lifecycleHoldReasons.length > 1
								? `Season protected for ${lifecycleHoldReasons.length} reasons`
								: lifecycleHold?.label || 'Season protected'}
					</strong>
					{#if isSeriesScope}
						{#if olderSeasonOverride && cadenceExcludedEpisodeCount > 0 && !canQueueOlderSeasons}
							<p>
								No older-season episodes are cleared for automatic conversion yet.
								{#if cadenceBlockedEpisodeCount > 0}
									{cadenceBlockedEpisodeCount} have measured motion patterns that require review.
								{/if}
								{#if cadenceEvidenceRequiredEpisodeCount > 0}
									{cadenceEvidenceRequiredEpisodeCount} still need motion-pattern analysis.
								{/if}
							</p>
						{:else if canQueueOlderSeasons && olderSeasonOverride}
							<p>
								{eligibleEpisodeCount} episodes are eligible normally. A separate confirmed action can
								include {olderSeasonOverride.candidate_count} safety-cleared
								{olderSeasonOverride.candidate_count === 1 ? 'episode' : 'episodes'} across
								{olderSeasonOverride.season_count} older
								{olderSeasonOverride.season_count === 1 ? 'season' : 'seasons'} while
								{olderSeasonOverride.latest_season_label || 'the latest season'} stays original.
								{#if cadenceExcludedEpisodeCount > 0}
									{cadenceExcludedEpisodeCount} more
									{cadenceExcludedEpisodeCount === 1 ? 'episode stays' : 'episodes stay'} original for
									motion-pattern safety.
								{/if}
							</p>
						{:else}
							<p>
								{eligibleEpisodeCount} episodes remain eligible for this show-level action. Protected
								seasons stay visible and original.
							</p>
						{/if}
					{:else if lifecycleHoldReasons.length}
						{#each lifecycleHoldReasons as reason (reason.code)}
							<p><b>{reason.label}:</b> {reason.detail}</p>
						{/each}
					{:else}
						<p>Open the queue action to review an explicit override.</p>
					{/if}
				</div>
			</div>
		{/if}

		{#if !folder.pending && folder.media_scope?.kind === 'tv_season'}
			<section class="episode-selector" aria-labelledby="episode-selector-title">
				<div class="episode-selector__copy">
					<p class="eyebrow">Episode selection</p>
					<h2 id="episode-selector-title">Choose an episode</h2>
					<p>Opening an episode does not start a test or change any media.</p>
				</div>
				{#if episodeNavigationUnavailable}
					<p class="episode-selector__status" role="alert">
						Episode list unavailable. Refresh this workspace to load every episode.
					</p>
				{:else if episodeOptions.length > 0}
					<div class="episode-selector__actions">
						<label class="episode-selector__control">
							<span
								>{episodeOptions.length}
								{episodeOptions.length === 1 ? 'episode' : 'episodes'}</span
							>
							<select
								aria-label="Choose an episode"
								bind:value={selectedEpisodeHref}
								disabled={episodeNavigationPending}
							>
								<option value="">Select an episode…</option>
								{#each episodeOptions as episode (episode.itemId)}
									<option value={episode.href}>{episode.label} — {episode.statusLabel}</option>
								{/each}
							</select>
						</label>
						<button
							class="secondary-button episode-selector__open"
							type="button"
							disabled={!selectedEpisode || episodeNavigationPending}
							onclick={() => void openEpisode()}
						>
							{episodeNavigationPending ? 'Opening episode…' : 'Open episode'}
						</button>
					</div>
				{:else}
					<p class="episode-selector__status">No catalog episodes found.</p>
				{/if}
			</section>
		{/if}

		{#if folder.pending}
			<section class="loading-room" aria-live="polite">
				{#if loadError}
					<p class="eyebrow">{scopeTitle}</p>
					<h1>Couldn’t open this {scopeNoun}</h1>
					<p>Try again. Nothing has been queued or changed.</p>
					<button
						type="button"
						class="primary-button"
						disabled={folderPending}
						onclick={() => void onMutate()}>{folderPending ? 'Trying again…' : 'Try again'}</button
					>
				{:else}
					<div class="breathing-mark" aria-hidden="true"><i></i><i></i><i></i></div>
					<p>Opening {scopeName || `your ${scopeNoun}`}…</p>
				{/if}
			</section>
		{:else if actionPending}
			{@const copy = phaseCopy(actionPhase)}
			<section class="working-room" aria-live="polite">
				<p class="eyebrow">{scopeTitle}</p>
				<div class="working-orbit" aria-hidden="true">
					<span></span><i></i><b></b>
				</div>
				<h1>{copy.title}</h1>
				<p class="lede">{copy.detail}</p>
				<div class="elapsed"><i></i> {actionElapsed}</div>
				<p class="leave-note">
					You can leave this page open. The next screen appears when it is ready.
				</p>
			</section>
		{:else if showScopeActivity && scopeActivity && scopeActivityJob}
			<section
				class="scope-activity-room"
				aria-labelledby="scope-activity-heading"
				aria-live="polite"
			>
				<div class="scope-activity-room__copy">
					<p class="eyebrow">
						{scopeActivityFailure
							? 'Related sample needs attention'
							: scopeActivityReady
								? 'Related sample ready'
								: 'Related sample in progress'}
					</p>
					<h1 id="scope-activity-heading">
						{scopeActivityFailure
							? `The ${scopeActivityOwnerTitle} sample needs attention`
							: scopeActivityReady
								? `The ${scopeActivityOwnerTitle} sample is ready`
								: scopeActivityOwnerKind === 'show'
									? 'A show-level sample is running'
									: 'A season sample is running'}
					</h1>
					<p class="lede">
						{scopeActivityOwnerTitle} owns this shared sample. {scopeName} has no separate sample running,
						so its saved settings are not being used for this work.
					</p>
				</div>
				<div class="scope-activity-facts">
					<div><span>Activity scope</span><strong>{scopeActivityOwnerTitle}</strong></div>
					<div><span>Requested target</span><strong>{scopeActivityTargetLabel}</strong></div>
					<div>
						<span>Target mode</span><strong
							>{calibrationTargetModeLabel(scopeTargetContract)}</strong
						>
					</div>
					<div>
						<span>Resolution</span><strong>{calibrationResolutionLabel(scopeTargetContract)}</strong
						>
					</div>
					<div><span>Current step</span><strong>{scopeActivityStage}</strong></div>
					<div>
						<span>Step progress</span><strong>{scopeActivityWork || scopeActivityLiveness}</strong>
					</div>
					<div class:telemetry-attention={scopeActivityEta.tone === 'attention'}>
						<span>Estimated remaining</span><strong>{scopeActivityEta.value}</strong>
						<small>{scopeActivityEta.detail}</small>
					</div>
					<div><span>Computer</span><strong>{scopeActivityWorker}</strong></div>
					<div><span>Time so far</span><strong>{scopeActivityElapsed}</strong></div>
				</div>
				<div class="scope-activity-actions">
					<a class="primary-button" href={scopeActivityHref}
						>Open {scopeActivityOwnerKind} sample
						<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 10h11M11 5l5 5-5 5" /></svg>
					</a>
					<a class="secondary-button" href={resolve('/ops')}>Open Activity</a>
				</div>
			</section>
		{:else if showGoalScreen}
			<section class="goal-room">
				<div class="goal-intro">
					<p class="eyebrow">{scopeTitle}</p>
					<h1>
						{retryMode
							? 'Choose a size and settings'
							: isSeriesScope && olderSeasonOverride?.available
								? `Choose one size for ${olderSeasonOverride.season_count} older ${olderSeasonOverride.season_count === 1 ? 'season' : 'seasons'}`
								: isSeriesScope
									? `Choose one size for all ${seriesSeasonCount} ${seriesSeasonLabel}`
									: `Choose a size for ${scopeName}`}
					</h1>
					<p class="lede">
						{#if isSeriesScope && olderSeasonOverride?.available}
							{olderSeasonOverride.candidate_count} safety-cleared episodes across
							{olderSeasonOverride.season_count} older
							{olderSeasonOverride.season_count === 1 ? 'season' : 'seasons'} ·
							{formatDecimalFileSize(olderSeasonOverride.current_size_bytes)} now.
							{olderSeasonOverride.latest_season_label || 'The latest season'} stays original. You will
							compare one representative sample before Mediaforce compresses the selected older seasons.
							{#if cadenceExcludedEpisodeCount > 0}
								{cadenceExcludedEpisodeCount} episodes without motion-pattern clearance stay original.
							{/if}
						{:else if isExactItemScope}
							1 episode · {formatDecimalFileSize(originalSeasonSize)} now. You will compare one sample
							before Mediaforce compresses this episode.
						{:else}
							{episodeCount} episodes · {formatDecimalFileSize(originalSeasonSize)} now. You will compare
							one representative sample before Mediaforce compresses the rest.
						{/if}
					</p>
				</div>

				<div class="goal-options" role="radiogroup" aria-label="Size per episode">
					{#if goals.length === 0}
						<p class="goal-options__empty" role="status">
							Mediaforce needs a representative episode runtime before it can resolve this size
							goal.
						</p>
					{/if}
					{#each goals as goal, goalIndex (goal.key)}
						<button
							bind:this={goalButtons[goalIndex]}
							type="button"
							class:selected={isGoalSelected(goal)}
							onclick={() => selectGoal(goal.key)}
							onkeydown={(event) => handleGoalKeydown(event, goalIndex)}
							role="radio"
							aria-checked={isGoalSelected(goal)}
							tabindex={goalTabIndex(goal, goalIndex)}
						>
							<span class="goal-radio"><i></i></span>
							<span class="goal-copy">
								<span class="goal-label">{goal.title}</span>
								<strong>{goal.megabytesPerEpisode} MB</strong>
								<small
									>per episode · about {formatDecimalFileSize(goal.targetSizeBytes * episodeCount)} total</small
								>
								<p>{goal.detail}</p>
							</span>
						</button>
					{/each}
				</div>

				<div class="compression-intent">
					<div class="compression-intent__heading" aria-live="polite" aria-atomic="true">
						<div>
							<span>Quality preference</span>
							<strong>{selectedCompressionIntent?.title ?? 'Choose a goal'}</strong>
						</div>
						<p>
							{selectedCompressionIntent?.detail ??
								folder.resolved_operator_intent?.compression_intent?.detail ??
								'Choose a quality preference before Mediaforce can create a sample.'}
						</p>
					</div>
					<div
						class="compression-intent__options"
						role="radiogroup"
						aria-label="Quality preference"
					>
						{#each compressionIntentOptions as option, intentIndex (option.key)}
							<button
								bind:this={compressionIntentButtons[intentIndex]}
								type="button"
								class:selected={option.key === activeCompressionIntentLevel}
								onclick={() => selectCompressionIntent(option.key)}
								onkeydown={(event) => handleCompressionIntentKeydown(event, intentIndex)}
								role="radio"
								aria-checked={option.key === activeCompressionIntentLevel}
								aria-label={`${option.title}. ${option.detail}`}
								tabindex={option.key === activeCompressionIntentLevel ||
								(!compressionIntentConfirmed && intentIndex === 0)
									? 0
									: -1}
							>
								<span class="compression-intent__option-copy">
									<strong>{option.title}</strong>
									<small>{option.detail}</small>
								</span>
							</button>
						{/each}
					</div>
					<small
						>Saved when you create the sample so retries keep the same quality preference.</small
					>
				</div>

				{#if selectedGoal && selectedCompressionIntentContract}
					<p class="sr-only" aria-live="polite" aria-atomic="true">
						{selectedCompressionIntentContract.announcement}
					</p>
					<div class="goal-contract">
						<div class="goal-contract__size">
							<span>{selectedCompressionIntentContract.sizeLabel}</span>
							<strong>{formatDecimalFileSize(selectedGoal.targetSizeBytes)}</strong>
							<small>{selectedCompressionIntentContract.sizeRule}</small>
						</div>
						<div>
							<span>Sample search band</span>
							<strong
								>{formatDecimalFileSize(selectedGoalSampleLower)}–{formatDecimalFileSize(
									selectedGoalSampleUpper
								)}</strong
							>
							<small
								>{selectedCompressionIntentContract.searchLabel}. {selectedCompressionIntentContract.searchRule}</small
							>
						</div>
						<div>
							<span>Final acceptance band</span>
							<strong
								>{formatDecimalFileSize(selectedGoalFinalLower)}–{formatDecimalFileSize(
									selectedGoalFinalUpper
								)}</strong
							>
							<small
								>±{selectedGoal.operatorIntent.size_goal.final_output_tolerance_percent}% after the
								full encode</small
							>
						</div>
						<div>
							<span>Quality rule</span>
							<strong>{selectedCompressionIntentContract.qualityLabel}</strong>
							<small>{selectedCompressionIntentContract.qualityRule}</small>
						</div>
						<div class="goal-contract__truth">
							<span>Final acceptance</span>
							<strong>{selectedCompressionIntentContract.finalHeadline}</strong>
							<small>{selectedCompressionIntentContract.finalRule}</small>
							{#if isExactItemScope && targetProvenance}
								<small>Target source: {targetProvenance}</small>
							{/if}
						</div>
					</div>
				{/if}
				{#if isExactItemScope && folder.target_size_provenance?.blocker}
					<div class="target-provenance-blocker" role="alert">
						<strong>This saved target cannot start production.</strong>
						<span>{folder.target_size_provenance.blocker.message}</span>
					</div>
				{/if}

				<div class="optional-instructions">
					<button
						type="button"
						class="optional-instructions__toggle"
						onclick={() => (showInstructions = !showInstructions)}
						aria-expanded={showInstructions}
						aria-controls="operator-instructions"
					>
						<span>
							<strong>Tell us more</strong>
							<small>Optional priorities for picture, sound, subtitles, or source treatment</small>
						</span>
						<svg viewBox="0 0 20 20" aria-hidden="true"><path d="m5 7.5 5 5 5-5" /></svg>
					</button>
					{#if showInstructions}
						<label id="operator-instructions" class="operator-instructions">
							<span>Additional priorities</span>
							<textarea
								bind:value={operatorInstructions}
								maxlength="600"
								rows="3"
								placeholder="For example: preserve the original surround layout and keep intentional grain."
							></textarea>
							<small
								>{operatorInstructions.length}/600 · The selected size remains authoritative.</small
							>
						</label>
					{/if}
				</div>

				<div class="goal-action">
					<div class="goal-action__message">
						<div class="safety-note">
							<svg viewBox="0 0 24 24" aria-hidden="true">
								<path
									d="M12 3 5.5 5.5v5.8c0 4.2 2.6 7.6 6.5 9.7 3.9-2.1 6.5-5.5 6.5-9.7V5.5L12 3Z"
								/>
								<path d="m9.2 12 1.8 1.8 3.8-4.1" />
							</svg>
							<p>
								<strong>One sample first.</strong> The sample creates short comparison clips. It does
								not change any episode file.
							</p>
						</div>
						{#if noAvailableHosts}
							<p class="host-unavailable" role="status">
								No computers are available right now. Open Technical details to see what needs
								attention.
							</p>
						{:else if requiresExplicitGoalSelection && !goalSelectionConfirmed}
							<p class="host-unavailable" role="status">
								Choose whether the saved legacy size scales with runtime or stays fixed per episode.
							</p>
						{:else if !compressionIntentConfirmed}
							<p class="host-unavailable" role="status">
								Choose a quality preference before creating the sample.
							</p>
						{/if}
					</div>
					<div class="goal-action__button">
						<span class="mobile-safety">
							{noAvailableHosts
								? 'No computers available · Open Technical details'
								: requiresExplicitGoalSelection && !goalSelectionConfirmed
									? 'Choose one size behavior first'
									: !compressionIntentConfirmed
										? 'Choose a quality preference first'
										: 'Short comparison clips · No episode is changed'}
						</span>
						<button
							class="primary-button"
							type="button"
							onclick={makeTest}
							disabled={noAvailableHosts ||
								!selectedGoal ||
								!goalSelectionConfirmed ||
								!compressionIntentConfirmed}
						>
							Create sample
							<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 10h11M11 5l5 5-5 5" /></svg>
						</button>
					</div>
				</div>
			</section>
		{:else if humanState.key === 'sample_waiting'}
			<section class="active-room active-room--waiting">
				<div class="active-status">
					<span>{sampleEpisode || scopeTitle}</span>
					<strong>Sample waiting</strong>
					<span>{activeSampleWorker}</span>
					<span>{backendElapsed} waiting</span>
					<span>{activeSampleUpdated}</span>
				</div>
				<div class="active-copy">
					<h1>Sample waiting for {sampleEpisode || scopeTitle}</h1>
					<p class="lede">
						This sample has not started yet. It is waiting for an available computer. Nothing has
						been compressed and nothing in your library is changed.
					</p>
					<div class="active-facts">
						<div>
							<span>Episode target</span><strong>{activeSampleEpisodeTargetLabel}</strong>
						</div>
						<div><span>Computer</span><strong>{activeSampleWorker}</strong></div>
						<div><span>Waiting since</span><strong>{backendElapsed}</strong></div>
					</div>
				</div>
				<p class="active-note">
					Nothing starts until a computer picks this sample up. You can leave this page open or
					close it.
				</p>
			</section>
		{:else if humanState.key === 'making_test'}
			<section class="active-room">
				<div class="active-status">
					<span>{sampleEpisode || scopeTitle}</span>
					<strong>{activeSampleStatusLabel}</strong>
					<span>{activeSampleWorker}</span>
					<span>{backendElapsed} elapsed</span>
					<span>{activeSampleUpdated}</span>
					{#if heldEpisodeCount > 0}<span>Current-season hold remains in place</span>{/if}
				</div>
				<div class="active-copy">
					<h1>Creating sample for {sampleEpisode || scopeTitle}</h1>
					<p class="lede">
						{#if activeSampleBuildingComparison}
							Mediaforce found settings near {activeSampleEpisodeTargetLabel} and is building comparison
							clips.
						{:else}
							This episode is being compressed into comparison clips. Mediaforce is finding settings
							near
							{activeSampleEpisodeTargetLabel} while keeping picture and sound acceptable.
						{/if}
						Nothing in your library is replaced.
					</p>
					<div class="active-facts">
						<div>
							<span>Episode target</span><strong>{activeSampleEpisodeTargetLabel}</strong>
							{#if activeSampleRuntimeLabel}<small>{activeSampleRuntimeLabel} runtime</small>{/if}
						</div>
					</div>
				</div>
				<section class="active-progress" aria-labelledby="active-progress-title">
					<header class="active-progress__header">
						<div aria-live="polite">
							<span>Current step</span>
							<h2 id="active-progress-title">{activeSampleStage}</h2>
						</div>
						{#if activeSampleStageElapsed}<strong>{activeSampleStageElapsed} in this step</strong
							>{/if}
					</header>
					{#if activeSampleWorkProgress.determinate && activeSampleWorkTotal > 0}
						<div class="active-progress__work">
							<div
								class="active-progress__track"
								role="progressbar"
								aria-label={`${activeSampleStage}: ${activeSampleWork}`}
								aria-valuemin="0"
								aria-valuemax={activeSampleWorkTotal}
								aria-valuenow={activeSampleWorkCompleted}
							>
								<i style={`width: ${activeSampleWorkPercent}%`}></i>
							</div>
							<strong>{activeSampleWorkProgress.label}</strong>
						</div>
					{:else if activeSampleWorkProgress.label}
						<p class="active-progress__status">{activeSampleWorkProgress.label}</p>
					{/if}
					<div class="active-progress__facts">
						<div>
							<span>Estimated remaining</span>
							<strong>{activeSampleEta.value}</strong>
							<small>{activeSampleEta.detail}</small>
						</div>
						<div class:telemetry-attention={activeSampleEta.tone === 'attention'}>
							<span>Computer status</span>
							<strong>{activeSampleLiveness}</strong>
							<small>{activeSampleWorker} · {activeSampleUpdated}</small>
						</div>
						<div>
							<span>Total elapsed</span>
							<strong>{backendElapsed}</strong>
							<small>The page can be left open or closed.</small>
						</div>
					</div>
				</section>
				<div class="step-line">
					<span class="done"><i>✓</i> Goal set</span>
					<span
						class:done={activeSampleBuildingComparison}
						class:active={!activeSampleBuildingComparison}
						><i>{activeSampleBuildingComparison ? '✓' : ''}</i> Find settings</span
					>
					<span class:active={activeSampleBuildingComparison}><i></i> Build comparison clips</span>
				</div>
				<p class="active-note">
					The sample builds short comparison clips only. No episode file is changed.
				</p>
			</section>
		{:else if humanState.key === 'ready_to_compare'}
			<section class="compare-room">
				<div class="compare-heading">
					<div>
						<p class={sizeTargetMissed || targetConstraint ? 'eyebrow eyebrow--missed' : 'eyebrow'}>
							{targetConstraint
								? 'Target needs a change'
								: sizeTargetMissed
									? 'Size goal not met'
									: 'Ready to review'}
						</p>
						<h1>{targetConstraint ? targetConstraint.title : `Review ${reviewSubject}`}</h1>
						<p>{sampleEpisode} · Compare the same moment on both sides.</p>
					</div>
				</div>

				<section class="review-contract" aria-label="Episode size comparison">
					<dl>
						{#if isExactItemScope}
							<div>
								<dt>Current size</dt>
								<dd>{formatDecimalFileSize(exactReviewFacts.currentSizeBytes)}</dd>
							</div>
						{/if}
						<div>
							<dt>{isExactItemScope ? 'Estimated output' : 'Estimated episode output'}</dt>
							<dd>
								{exactReviewFacts.estimatedOutputBytes
									? formatDecimalFileSize(exactReviewFacts.estimatedOutputBytes)
									: 'No usable estimate'}
							</dd>
						</div>
						{#if isExactItemScope}
							<div>
								<dt>Estimated space saved</dt>
								<dd>
									{exactReviewFacts.estimatedSpaceSavedBytes !== null
										? exactExpectedSizeChange.direction === 'larger'
											? `None · grows by ${formatDecimalFileSize(exactExpectedSizeChange.bytes)}`
											: formatDecimalFileSize(exactReviewFacts.estimatedSpaceSavedBytes)
										: 'No usable estimate'}
								</dd>
							</div>
						{/if}
						<div>
							<dt>Episode target</dt>
							<dd>{sizeTargetLabel}</dd>
						</div>
					</dl>
				</section>

				{#if targetConstraint}
					<div class="target-warning target-warning--constraint" role="status">
						<div>
							<span
								>{targetConstraint.kind === 'arithmetic_infeasible'
									? 'Size cannot fit'
									: 'Quality floor'}</span
							>
							<strong>{targetConstraint.title}</strong>
						</div>
						<p>{targetConstraint.detail}</p>
					</div>
				{:else if sizeTarget.status === 'over_target'}
					<div class="target-warning" role="status">
						<div>
							<span>Requested size</span>
							<strong
								>This sample estimates {formatDecimalFileSize(expectedEpisodeBytes)} per episode, not
								{formatDecimalFileSize(sizeTarget.budgetBytes)}.</strong
							>
						</div>
						<p>
							{crfLimitReached
								? 'The sample reached its smallest practical setting before it reached your size goal.'
								: 'The measured result stayed above your size goal.'}
							Review this as a {reviewSubject} checkpoint, then create a smaller sample.
						</p>
					</div>
				{:else if underTargetIsAcceptable}
					<div class="target-warning target-warning--acceptable" role="status">
						<div>
							<span>Quality preference</span>
							<strong
								>This sample estimates {formatDecimalFileSize(expectedEpisodeBytes)} per episode, below
								your {formatDecimalFileSize(sizeTarget.budgetBytes)} goal.</strong
							>
						</div>
						<p>
							Review the {reviewSubject}. Mediaforce will not spend the unused size automatically.
						</p>
					</div>
				{:else if sizeTarget.status === 'under_target'}
					<div class="target-warning target-warning--under" role="status">
						<div>
							<span>Requested size</span>
							<strong
								>This sample estimates {formatDecimalFileSize(expectedEpisodeBytes)} per episode, below
								your
								{formatDecimalFileSize(sizeTarget.budgetBytes)} goal.</strong
							>
						</div>
						<p>Create another sample that spends the unused size on {reviewSubject} quality.</p>
					</div>
				{:else if sizeTarget.status === 'missing_prediction'}
					<div class="target-warning" role="status">
						<div>
							<span>Size estimate missing</span>
							<strong>This sample cannot be compared with your requested size yet.</strong>
						</div>
						<p>Create the sample again before approving a season-wide result.</p>
					</div>
				{/if}

				{#if currentPair}
					<ComparisonWorkspace
						pairs={reviewPairs}
						selectedMoment={displayedMoment}
						{audioChoice}
						reviewScopeLabel={sampleEpisode}
						originalClipLabel={actualSampleSizes.original
							? `${formatDecimalFileSize(actualSampleSizes.original)} clip`
							: 'Clip size unavailable'}
						sampleClipLabel={actualSampleSizes.smaller
							? `${formatDecimalFileSize(actualSampleSizes.smaller)} clip`
							: 'Clip size unavailable'}
						estimatedOutputLabel={expectedEpisodeBytes
							? `about ${formatDecimalFileSize(expectedEpisodeBytes)}`
							: ''}
						canCreateSoundSample={sampleHasAudio}
						soundSampleDisabled={actionPhase !== 'idle' || noAvailableHosts}
						onMomentChange={chooseMoment}
						onAudioChange={(side) => (audioChoice = side)}
						onRequestSoundSample={() => void retryMeasuredTarget()}
					/>
				{:else}
					<div class="missing-media">
						<h2>{review.recovery?.title ?? 'The comparison clips are unavailable'}</h2>
						<p>
							{review.recovery?.detail ??
								'Nothing was replaced. Create the sample again to rebuild the comparison.'}
						</p>
						{#if review.canDownload}
							<button class="detail-download" type="button" onclick={downloadComparison}>
								Download combined comparison
							</button>
						{/if}
					</div>
				{/if}

				{#if riskSummary}
					<div class={`risk-summary risk-summary--${riskSummary.tone}`}>
						<div class="risk-summary__headline">
							<span>What to check</span>
							<strong>{riskSummary.verdict}</strong>
						</div>
						<div class="risk-summary__fact">
							<span>Picture</span>
							<strong>{riskSummary.picture.label}</strong>
							<small>{riskSummary.picture.level} · {riskSummary.picture.detail}</small>
						</div>
						<div class="risk-summary__fact">
							<span>Sound</span>
							<strong>{riskSummary.sound.label}</strong>
							<small>{riskSummary.sound.level} · {riskSummary.sound.detail}</small>
						</div>
						{#if riskSummary.hasSavedDecision}
							<div class="risk-summary__fact">
								<span>Decision</span>
								<strong>{riskSummary.authority}</strong>
								<small>{riskSummary.authorityDetail}</small>
							</div>
						{/if}
						<p class="risk-summary__detail">{riskSummary.detail}</p>
						{#if riskSummary.focusMoments.length}
							<p class="risk-summary__focus">Review focus: {riskSummary.focusMoments[0]}</p>
						{/if}
						{#if riskSummary.requiresCadenceResolution}
							<div class="risk-summary__resolution">
								<div>
									<span>Next step</span>
									<strong>Resolve the selected file’s motion pattern</strong>
									<small>This is evidence work, not a size or quality-setting problem.</small>
								</div>
							</div>
						{/if}
					</div>
				{/if}

				{#if revisionPaneVisible}
					<div class="review-feedback-panel" id="revision-pane">
						<div class="review-feedback-panel__heading">
							<div>
								<span>Improve picture or sound</span>
								<h2 id="revision-pane-title" tabindex="-1">What should improve?</h2>
							</div>
							{#if !revisionPaneForced}
								<button class="text-link" type="button" onclick={closeRevisionPane}>
									Never mind
								</button>
							{/if}
						</div>
						<div class="concern-options" role="group" aria-label="Review concerns">
							{#each REVIEW_CONCERNS as concern (concern.tag)}
								<button
									type="button"
									class:active={selectedConcerns.includes(concern.tag)}
									onclick={() => toggleConcern(concern.tag)}
									aria-pressed={selectedConcerns.includes(concern.tag)}
								>
									{concern.label}
								</button>
							{/each}
						</div>
						<div class="review-feedback-fields">
							<label>
								<span>What did you notice?</span>
								<textarea
									bind:value={reviewFeedback}
									maxlength="600"
									rows="3"
									placeholder="For example: fast movement loses texture around faces in Moment 2."
								></textarea>
								<small>{reviewFeedback.length}/600</small>
							</label>
							<label>
								<span>Other priorities for the next sample <small>(optional)</small></span>
								<textarea
									bind:value={operatorInstructions}
									maxlength="600"
									rows="3"
									placeholder="Preserve surround audio, intentional grain, subtitle behavior, or another priority."
								></textarea>
								<small>{operatorInstructions.length}/600</small>
							</label>
						</div>
						{#if !revisionPaneForced}
							<fieldset class="revision-mode">
								<legend>How should Mediaforce create the next sample?</legend>
								<label class:active={revisionMode === 'same_target'}>
									<input type="radio" bind:group={revisionMode} value="same_target" />
									<span>Revise at the same size</span>
									<small>
										Keep the target at about {sizeTargetLabel}. Mediaforce will try to address what
										you noticed without increasing the size goal.
									</small>
								</label>
								<label
									class:active={revisionMode === 'roomier'}
									class:revision-mode__option--unavailable={!higherQualityReviewAdjustment}
								>
									<input
										type="radio"
										bind:group={revisionMode}
										value="roomier"
										disabled={!higherQualityReviewAdjustment}
									/>
									<span>Allow a larger file</span>
									<small>
										{higherQualityReviewAdjustment
											? `Raise the next target to about ${formatDecimalFileSize(higherQualityReviewAdjustment.goal.targetSizeBytes)}. The episode will be larger, with more room for picture quality.`
											: 'A larger guided target is not available for this sample.'}
									</small>
								</label>
								{#if !higherQualityReviewAdjustment}
									<p class="revision-mode__fallback">
										<button class="text-link" type="button" onclick={chooseDifferentSize}>
											Choose a different size
										</button>
										to set a larger target manually.
									</p>
								{/if}
							</fieldset>
						{/if}
						<div class="review-feedback-panel__action">
							<p>
								{#if !revisionPaneForced && revisionMode === 'roomier'}
									Your notes describe the version you reviewed. The next sample raises the target to
									about {higherQualityReviewAdjustment
										? formatDecimalFileSize(higherQualityReviewAdjustment.goal.targetSizeBytes)
										: 'a larger size'} and has not been judged yet. Nothing is replaced.
								{:else}
									This marks the current version as not acceptable and creates another sample at the
									same target. Nothing is replaced.
								{/if}
							</p>
							{#if revisionPaneForced}
								<button class="text-link" type="button" onclick={chooseDifferentSize}>
									Choose a different goal
								</button>
							{/if}
							<button
								class="secondary-button"
								type="button"
								onclick={submitReviewFeedback}
								disabled={!hasReviewFeedback || noAvailableHosts || actionPhase !== 'idle'}
							>
								Create a revised sample
							</button>
						</div>
					</div>
				{/if}

				<div
					class="decision"
					class:decision--target-miss={sizeTargetMissed || Boolean(targetConstraint)}
					class:decision--blocked={approvalBlocked}
				>
					<div>
						{#if targetConstraint}
							<h2>{targetConstraint.title}</h2>
							<p>{targetConstraint.detail}</p>
						{:else if riskSummary?.requiresCadenceResolution}
							<h2>Mediaforce needs a motion-pattern decision.</h2>
							<p>
								Resolve the selected file in Activity, then return here to finish your decision.
							</p>
						{:else if riskSummary?.blocked}
							<h2>This sample is not safe to approve yet.</h2>
							<p>{riskSummary.detail}</p>
						{:else if sizeTarget.status === 'over_target'}
							<h2>This is a quality checkpoint, not your requested-size result.</h2>
							<p>Compare it now, then create a smaller sample that moves toward your goal.</p>
						{:else if sizeTarget.status === 'under_target' && !underTargetIsAcceptable}
							<h2>This result is smaller than requested.</h2>
							<p>
								Create another sample that uses the available size for more {reviewSubject} quality.
							</p>
						{:else if sizeTarget.status === 'missing_prediction'}
							<h2>The size result is incomplete.</h2>
							<p>
								Create another sample before deciding whether to use this setting for the season.
							</p>
						{:else}
							<h2>What do you want to do with this version?</h2>
							<p>Keep it, see if it can use less space, or tell Mediaforce what should improve.</p>
						{/if}
					</div>
					<div class="decision-actions">
						{#if targetConstraint}
							<button
								class="primary-button primary-button--light"
								type="button"
								onclick={chooseDifferentSize}
							>
								{targetConstraint.recoveryLabel}
								<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 10h11M11 5l5 5-5 5" /></svg>
							</button>
						{:else if riskSummary?.requiresCadenceResolution}
							<a class="primary-button primary-button--light" href={resolve('/ops')}>
								Open Activity
								<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 10h11M11 5l5 5-5 5" /></svg>
							</a>
						{:else if riskSummary?.blocked}
							<button class="secondary-button" type="button" onclick={chooseDifferentSize}>
								Choose a different goal
							</button>
							<button
								class="primary-button primary-button--light"
								type="button"
								onclick={retryMeasuredTarget}
								disabled={actionPhase !== 'idle' || noAvailableHosts}
							>
								Create another sample after fixing this
								<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 10h11M11 5l5 5-5 5" /></svg>
							</button>
						{:else if sizeTargetMissed}
							<button class="text-link" type="button" onclick={chooseDifferentSize}>
								Choose a different goal
							</button>
							{#if sizeTarget.status !== 'missing_prediction'}
								<button
									class="secondary-button"
									type="button"
									onclick={confirmSizeTradeoff}
									disabled={!currentPair || actionPhase !== 'idle'}
								>
									Accept {sizeTarget.status === 'over_target' ? 'larger' : 'smaller'} result
								</button>
							{/if}
							<button
								class="primary-button primary-button--light"
								type="button"
								onclick={retryMeasuredTarget}
								disabled={actionPhase !== 'idle' || noAvailableHosts}
							>
								Create another {sizeTargetLabel} sample
								<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 10h11M11 5l5 5-5 5" /></svg>
							</button>
						{:else}
							<button
								class="primary-button primary-button--light"
								type="button"
								onclick={() => approveTest()}
								disabled={!currentPair || approvalBlocked}
							>
								Keep this version
								<svg viewBox="0 0 20 20" aria-hidden="true"><path d="m4 10 3.5 3.5L16 5" /></svg>
							</button>
							<button
								class="secondary-button"
								type="button"
								onclick={() => chooseReviewAdjustment('smaller')}
								disabled={actionPhase !== 'idle' || noAvailableHosts}
							>
								Use less space
							</button>
							<button
								class="secondary-button"
								type="button"
								data-review-action="needs-improvement"
								onclick={openRevisionPane}
								aria-expanded={revisionPaneOpen}
								aria-controls={revisionPaneVisible ? 'revision-pane' : undefined}
							>
								Improve picture or sound
							</button>
						{/if}
					</div>
				</div>
			</section>
		{:else if humanState.key === 'ready_to_make' && isExactItemScope}
			<section class="exact-approved" aria-labelledby="exact-approved-title">
				<div class="exact-approved__summary">
					<header class="exact-approved__header">
						<p class="exact-approved__status">
							<span aria-hidden="true">✓</span> Sample approved
						</p>
						<h1 id="exact-approved-title">{scopeTitle}</h1>
						<p class="exact-approved__filename">{exactFilename}</p>
					</header>

					<div class="exact-approved__outcome" aria-label="Expected size result">
						<p>
							<strong>{formatDecimalFileSize(originalSeasonSize)}</strong>
							<span aria-hidden="true">→</span>
							<strong>about {formatDecimalFileSize(expectedEpisodeBytes)}</strong>
						</p>
						{#if exactExpectedSizeChange.direction === 'smaller'}
							<span>Saves about {formatDecimalFileSize(exactExpectedSizeChange.bytes)}</span>
						{:else if exactExpectedSizeChange.direction === 'larger'}
							<span>Grows by about {formatDecimalFileSize(exactExpectedSizeChange.bytes)}</span>
						{:else}
							<span>Expected to stay about the same size</span>
						{/if}
					</div>

					<div class="exact-approved__target">
						<strong>Size goal {sizeTargetLabel}</strong>
						{#if exactApprovedRangeLabel}
							<span>
								{exactExpectedInsideApprovedRange
									? `Expected result is inside the approved ${exactApprovedRangeLabel} range.`
									: `Approved final range: ${exactApprovedRangeLabel}.`}
							</span>
						{/if}
					</div>
				</div>

				<div class="exact-approved__decision">
					<div class="exact-approved__next">
						<p class="exact-approved__label">Next step</p>
						<h2>Compress the full episode</h2>
						<p id="exact-approved-action-description">
							Mediaforce will compress the complete episode into a separate file using the settings
							you approved. Your original file is not changed.
						</p>
					</div>

					<ol class="exact-approved__steps" aria-label="What happens next">
						<li><span>1</span><strong>Compress the full episode</strong></li>
						<li><span>2</span><strong>Check the compressed file</strong></li>
						<li>
							<span>3</span><strong>Choose whether to replace the original</strong>
						</li>
					</ol>

					<div class="exact-approved__actions">
						<button
							class="primary-button"
							type="button"
							onclick={requestQueueSeason}
							aria-describedby="exact-approved-action-description exact-approved-safety"
							disabled={heldEpisodeCount > 0 && !canOverrideLifecycleHolds}
						>
							Compress the full episode
							<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 10h11M11 5l5 5-5 5" /></svg>
						</button>
						{#if reviewPairs.length}
							<button class="secondary-button" type="button" onclick={downloadComparison}>
								Download approved comparison clips
							</button>
						{/if}
					</div>
					<p class="exact-approved__safety" id="exact-approved-safety">
						Nothing is queued yet. Nothing is replaced automatically.
					</p>
				</div>

				<details class="exact-approved__estimate">
					<summary>
						<span>Estimate details</span>
						<small>How the approved sample informs the full episode</small>
					</summary>
					<div>
						<p>
							The approved sample predicts about {formatDecimalFileSize(expectedEpisodeBytes)} for this
							episode. The finished size can vary within the approved range as Mediaforce preserves the
							picture and sound rules you reviewed.
						</p>
					</div>
				</details>
			</section>
		{:else if humanState.key === 'ready_to_make'}
			<section class="ready-room">
				<div class="ready-symbol" aria-hidden="true"><span>✓</span></div>
				<p class="eyebrow">Sample approved</p>
				<h1>
					{isSeriesScope
						? canQueueOlderSeasons
							? eligibleEpisodeCount > 0
								? 'Ready to choose which seasons to compress.'
								: 'Ready to compress the older seasons.'
							: 'Ready to compress the eligible seasons.'
						: isExactItemScope
							? 'Ready to compress this episode.'
							: heldEpisodeCount > 0
								? 'This season is ready, but protected.'
								: 'Ready to compress the season.'}
				</h1>
				{#if isSeriesScope && canQueueOlderSeasons && olderSeasonOverride}
					<p class="lede">
						The approved setup can compress {eligibleEpisodeCount} normally eligible
						{eligibleEpisodeCount === 1 ? 'episode' : 'episodes'}. The older-season option can
						include {olderSeasonOverride.candidate_count} safety-cleared
						{olderSeasonOverride.candidate_count === 1 ? 'episode' : 'episodes'} across
						{olderSeasonOverride.season_count} older
						{olderSeasonOverride.season_count === 1 ? 'season' : 'seasons'} with explicit confirmation.
						Nothing is queued until you choose an action.
					</p>
				{:else}
					<p class="lede">
						Mediaforce will compress {productionEpisodeCount}
						{productionEpisodeCount === 1 ? 'episode' : 'episodes'} with the same settings. {heldEpisodeCount >
						0
							? `${heldEpisodeCount} protected ${heldEpisodeCount === 1 ? 'episode stays' : 'episodes stay'} original unless you explicitly override this season.`
							: 'New files stay separate until they pass their checks.'} Nothing is queued until you choose
						the action below.
					</p>
				{/if}
				<div class="ready-summary">
					<div>
						<span
							>{isSeriesScope
								? 'Eligible episodes'
								: isExactItemScope
									? 'Episode'
									: 'Episodes'}</span
						><strong>{productionEpisodeCount}</strong>
					</div>
					<div><span>Approved episode target</span><strong>{sizeTargetLabel}</strong></div>
					<div>
						<span>Current size</span><strong>{formatDecimalFileSize(originalSeasonSize)}</strong>
						{#if isSeriesScope}<small>Whole selected show scope.</small>{/if}
					</div>
					<div>
						<span>Estimated output</span><strong
							>{expectedSeasonBytes
								? formatDecimalFileSize(expectedSeasonBytes)
								: 'Varies by episode'}</strong
						>
						<small
							>{isExactItemScope
								? 'This episode uses its runtime-derived target.'
								: 'Each episode gets its own runtime-derived target.'}</small
						>
					</div>
					{#if estimatedSeasonSavingsBytes !== null}
						<div>
							<span>Estimated space saved</span><strong
								>{formatDecimalFileSize(estimatedSeasonSavingsBytes)}</strong
							>
						</div>
					{/if}
				</div>
				{#if isSeriesScope && canQueueOlderSeasons && olderSeasonOverride}
					<div class="older-season-option">
						<div>
							<strong>Compress older seasons</strong>
							<p>
								{olderSeasonOverride.latest_season_label || 'The latest season'} stays original.
								{#if cadenceExcludedEpisodeCount > 0}
									{cadenceExcludedEpisodeCount} additional
									{cadenceExcludedEpisodeCount === 1 ? 'episode stays' : 'episodes stay'} original for
									motion-pattern safety.
								{/if}
								{#if olderSeasonOverride.overridden_candidate_count > 0}
									{olderSeasonOverride.overridden_candidate_count} protected
									{olderSeasonOverride.overridden_candidate_count === 1 ? 'episode' : 'episodes'} will
									bypass lifecycle timing holds; the policy itself stays unchanged.
								{:else}
									No lifecycle hold is bypassed; this action only keeps the latest season out of the
									queue.
								{/if}
							</p>
							<small>
								{formatDecimalFileSize(olderSeasonOverride.current_size_bytes)} current
								{#if olderSeasonProjectedSavingsBytes !== null}
									· about {formatDecimalFileSize(olderSeasonProjectedSavingsBytes)} estimated space saved
								{/if}
							</small>
						</div>
						<button class="primary-button" type="button" onclick={requestQueueOlderSeasons}>
							Compress {olderSeasonOverride.season_count} older
							{olderSeasonOverride.season_count === 1 ? 'season' : 'seasons'}
							<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 10h11M11 5l5 5-5 5" /></svg>
						</button>
					</div>
				{/if}
				{#if !isSeriesScope || eligibleEpisodeCount > 0 || !canQueueOlderSeasons}
					<button
						class="primary-button"
						type="button"
						onclick={requestQueueSeason}
						disabled={(isSeriesScope && eligibleEpisodeCount === 0) ||
							(!isSeriesScope && heldEpisodeCount > 0 && !canOverrideLifecycleHolds)}
					>
						{makeActionLabel}
						<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 10h11M11 5l5 5-5 5" /></svg>
					</button>
				{/if}
			</section>
		{:else if humanState.key === 'making_season'}
			<section class="season-progress-room" aria-live="polite">
				<div class="progress-copy">
					<p class="eyebrow">In progress</p>
					<h1>
						{activeOlderSeasonCount > 0
							? `Compressing ${activeOlderSeasonCount} older ${activeOlderSeasonCount === 1 ? 'season' : 'seasons'}`
							: isSeriesScope
								? `Compressing all ${seriesSeasonCount} ${seriesSeasonLabel}`
								: isExactItemScope
									? `Compressing ${exactEpisodeName}`
									: `Compressing ${identity.season}`}
					</h1>
					<p class="lede">
						{encodeProgress.currentEpisode === 'A representative episode'
							? `The ${scopeNoun} is waiting for its next available computer.`
							: isExactItemScope
								? `${encodeProgress.currentEpisode} is being compressed now.`
								: `${encodeProgress.currentEpisode} is being compressed now.`}
					</p>
					<div class="progress-facts">
						<strong>{seasonProgressCompleted} of {encodeProgress.total}</strong>
						<span>{isExactItemScope ? 'episode finished' : 'episodes finished'}</span>
						{#if encodeProgress.eta}<small>{encodeProgress.eta}</small>{/if}
					</div>
				</div>
				<div
					class="progress-ring"
					style={`--progress: ${seasonProgressPercent * 3.6}deg`}
					aria-label={`${Math.round(seasonProgressPercent)} percent complete`}
					role="progressbar"
					aria-valuemin="0"
					aria-valuemax="100"
					aria-valuenow={Math.round(seasonProgressPercent)}
				>
					<div>
						<strong>{Math.round(seasonProgressPercent)}%</strong>
						<span>complete</span>
					</div>
				</div>
				<div class="progress-track" aria-hidden="true">
					<i style={`width: ${seasonProgressPercent}%`}></i>
				</div>
				<p class="progress-note">
					{isExactItemScope
						? 'Your original is kept. If the computer disconnects, this episode can be retried.'
						: 'Completed episodes are kept. If a computer disconnects, unfinished work can be retried.'}
				</p>
			</section>
		{:else if humanState.key === 'ready_to_check'}
			<section class="ready-room ready-room--check">
				<div class="ready-symbol" aria-hidden="true">
					<span>{stagedAccessBlocked ? '!' : '···'}</span>
				</div>
				<p class="eyebrow">{isExactItemScope ? 'Episode compressed' : 'Episodes compressed'}</p>
				<h1>
					{stagedAccessBlocked
						? 'Reconnect the working folder.'
						: isExactItemScope
							? 'Let’s check this episode.'
							: 'Let’s check every new file.'}
				</h1>
				<p class="lede">
					{stagedAccessBlocked
						? isExactItemScope
							? 'The full compressed episode is finished in Mediaforce’s working folder, and your original library episode is unchanged. This computer must reconnect shared storage before it can run the technical check. Nothing is replaced by this check.'
							: 'The full compressed episodes are finished in Mediaforce’s working folder, and your original library episodes are unchanged. This computer must reconnect shared storage before it can run the technical checks. Nothing is replaced by these checks.'
						: isExactItemScope
							? 'The full encoded episode is finished, and your original library episode is unchanged. Mediaforce now checks that the new file opens, plays, and has the expected length. Nothing is replaced by this check.'
							: 'The full encoded episodes are finished, and your original library episodes are unchanged. Mediaforce now checks that each new file opens, plays, and has the expected length. Nothing is replaced by these checks.'}
				</p>
				{#if !isExactItemScope && encodedEpisodeLinks.length > 0}
					<div class="encoded-episode-links" aria-label="Encoded episodes waiting for checks">
						<span>{encodedEpisodeLinks.length === 1 ? 'Encoded episode' : 'Encoded episodes'}</span>
						{#each encodedEpisodeLinks as episode (episode.relPath)}
							<a href={resolve(episode.href)}>
								<strong>{episode.label}</strong>
								<small>Open episode</small>
							</a>
						{/each}
					</div>
				{/if}
				{#if stagedAccessBlocked && storageRecoveryHost}
					<button
						class="primary-button"
						type="button"
						onclick={reconnectStorageAndCheckOutputs}
						disabled={actionPhase !== 'idle'}
					>
						Reconnect storage and check
						<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 10h11M11 5l5 5-5 5" /></svg>
					</button>
					<p class="action-note">
						Mediaforce will reconnect {storageRecoveryHost.label}, then run the check.
					</p>
				{:else if stagedAccessBlocked}
					<a class="primary-button" href={resolve('/ops')}>Open computers and storage</a>
					<p class="action-note">
						Reconnect the working folder before checking. Your originals stay unchanged.
					</p>
				{:else}
					<button
						class="primary-button"
						type="button"
						onclick={checkOutputs}
						disabled={actionPhase !== 'idle'}
					>
						{isExactItemScope ? 'Check this episode' : 'Check the new episodes'}
						<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 10h11M11 5l5 5-5 5" /></svg>
					</button>
				{/if}
			</section>
		{:else if humanState.key === 'ready_to_finish'}
			<section class="ready-room ready-room--finish">
				<div class="ready-symbol" aria-hidden="true"><span>✓</span></div>
				<p class="eyebrow">Every check passed</p>
				<h1>
					{isExactItemScope
						? 'Ready to replace the original episode.'
						: 'Ready to replace the original episodes.'}
				</h1>
				<p class="lede">
					{isExactItemScope ? 'This episode is accounted for.' : 'Every episode is accounted for.'}
					{promotionIntegrity.readyCount === 0
						? 'No checked episodes still need replacement.'
						: promotionIntegrity.readyCount === 1
							? 'Replacing installs the checked episode.'
							: `Replacing installs all ${promotionIntegrity.readyCount} checked episodes together.`}
					{isExactItemScope
						? 'The current original moves to the cleanup folder so it can be recovered later.'
						: 'The current originals move to the cleanup folder so they can be recovered later.'}
				</p>
				<SeasonIntegrityPanel integrity={promotionIntegrity} tone="ready" />
				<button class="primary-button" type="button" onclick={finishSeason}>
					{isExactItemScope ? 'Replace the original episode' : 'Replace original episodes'}
					<svg viewBox="0 0 20 20" aria-hidden="true"><path d="m4 10 3.5 3.5L16 5" /></svg>
				</button>
				<p class="action-note">Nothing changes until you choose this action.</p>
			</section>
		{:else if humanState.key === 'finish_blocked'}
			<section class="ready-room ready-room--blocked">
				<div class="ready-symbol" aria-hidden="true"><span>!</span></div>
				<p class="eyebrow">
					{isExactItemScope ? 'Whole episode required' : 'Whole season required'}
				</p>
				<h1>{humanState.label}.</h1>
				<p class="lede">
					{humanState.detail} Mediaforce will not replace {isExactItemScope
						? 'a partial episode'
						: 'a partial season'} or adopt an untracked file. No originals move to the cleanup folder
					while this check is blocked.
				</p>
				{#if promotionIntegrity.available}
					<SeasonIntegrityPanel integrity={promotionIntegrity} tone="blocked" />
				{:else if promotionIntegrity.error}
					<div class="integrity-loading integrity-loading--error" role="alert">
						<strong
							>{isExactItemScope ? 'Episode check unavailable' : 'Season check unavailable'}</strong
						>
						<span
							>{promotionIntegrity.error} No files can be replaced while this check is unavailable.</span
						>
						<button class="secondary-button" type="button" onclick={onMutate}
							>Try the check again</button
						>
					</div>
				{:else}
					<div class="integrity-loading" role="status">
						<strong>Checking every staged file</strong>
						<span>The finish action stays unavailable until the complete inventory is loaded.</span>
					</div>
				{/if}
			</section>
		{:else if humanState.key === 'finished'}
			<section class="finished-room">
				<div class="finished-rings" aria-hidden="true"><i></i><i></i><span>✓</span></div>
				<p class="eyebrow">All finished</p>
				<h1>{scopeName} is ready.</h1>
				<p class="lede">
					{isExactItemScope
						? 'The smaller episode is in your library. The original remains in the cleanup folder until you choose to delete it.'
						: `All ${episodeCount} smaller episodes are in your library. The originals remain in the cleanup folder until you choose to delete them.`}
				</p>
				<div class="finished-actions">
					<a class="primary-button" href={resolve('/')}
						>{isExactItemScope
							? 'Choose another show or episode'
							: 'Choose another show or season'}</a
					>
					<a class="text-link" href={resolve('/completed')}
						>{isExactItemScope ? 'See finished media' : 'See finished seasons'}</a
					>
				</div>
			</section>
		{:else if humanState.key === 'needs_help'}
			<section class="help-room">
				<div class="help-mark" aria-hidden="true">!</div>
				<p class="eyebrow">
					{targetConstraint
						? 'Size needs a change'
						: humanState.recoveryKind === 'test'
							? 'Sample needs retry'
							: 'A small snag'}
				</p>
				<h1>{targetConstraint?.title || `${humanState.label}.`}</h1>
				<p class="lede">{targetConstraint?.detail || plainFailureMessage(folder, status)}</p>
				<div class="help-safety">
					{#if targetConstraint}
						<strong>No quality rule was silently relaxed.</strong>
						<span>Choose a viable goal, then Mediaforce can create a fresh sample.</span>
					{:else if humanState.recoveryKind === 'test'}
						<strong>Your library is safe.</strong>
						<span>Nothing was replaced. Trying again rebuilds the comparison.</span>
					{:else}
						<strong
							>{isExactItemScope ? 'Your episode is safe.' : 'Your completed work is safe.'}</strong
						>
						<span
							>{recoveryNeedsFreshGoal
								? `Nothing was replaced. Choosing a new size or quality preference creates a fresh sample before this ${scopeNoun} can run again.`
								: isExactItemScope
									? 'Nothing was replaced. Retrying starts only this episode.'
									: 'Retrying keeps finished episodes and starts only what still needs attention.'}</span
						>
					{/if}
				</div>
				{#if humanState.recoveryKind === 'test' && !targetConstraint}
					<div class="recovery-plan" aria-label="Saved sample plan">
						<div><span>Sample scope</span><strong>{recoveryScopeTitle}</strong></div>
						<div><span>Saved target</span><strong>{sizeTargetLabel}</strong></div>
						<div>
							<span>Target mode</span><strong
								>{calibrationTargetModeLabel(exactTargetContract)}</strong
							>
						</div>
						<div>
							<span>Resolution</span><strong
								>{calibrationResolutionLabel(exactTargetContract)}</strong
							>
						</div>
						<div class="recovery-plan__computer">
							<span>Computer</span><strong>{recoveryWorker}</strong>
						</div>
					</div>
				{/if}
				{#if humanState.recoveryKind === 'season' && finishedEpisodeCount > 0}
					<div class="recovery-count">
						<strong>{finishedEpisodeCount} of {episodeCount}</strong>
						<span>episodes already made</span>
					</div>
				{/if}
				<div class="help-actions">
					<button
						class="primary-button"
						type="button"
						onclick={() => (targetConstraint ? chooseDifferentSize() : recoverSeason())}
					>
						{targetConstraint?.recoveryLabel ||
							(recoveryNeedsFreshGoal
								? 'Choose size and settings'
								: recoveryNeedsAdjustment
									? 'Review retry'
									: humanState.recoveryKind === 'test'
										? 'Retry sample'
										: isExactItemScope
											? 'Retry this episode'
											: 'Retry unfinished episodes')}
					</button>
					{#if humanState.recoveryKind === 'test' && !targetConstraint}
						<button class="secondary-button" type="button" onclick={chooseDifferentSize}>
							Choose a different size or settings
						</button>
					{/if}
				</div>
			</section>
		{/if}

		{#if safetyDialog}
			<div class="safety-dialog-backdrop">
				<div
					class="safety-dialog"
					role={safetyDialog.kind === 'review_adjustment' ? 'dialog' : 'alertdialog'}
					aria-modal="true"
					aria-labelledby="safety-dialog-title"
					aria-describedby="safety-dialog-detail"
					tabindex="-1"
					onkeydown={handleSafetyDialogKeydown}
				>
					<p class={safetyDialog.confirmSizeTradeoff ? 'eyebrow eyebrow--missed' : 'eyebrow'}>
						{safetyDialog.kind === 'review_adjustment'
							? 'Another comparison'
							: 'Before you continue'}
					</p>
					<h2 id="safety-dialog-title">{safetyDialog.title}</h2>
					<p id="safety-dialog-detail">{safetyDialog.detail}</p>
					{#if safetyDialog.changes?.length}
						<ul>
							{#each safetyDialog.changes as change (change)}
								<li>{change}</li>
							{/each}
						</ul>
					{/if}
					<div class="safety-dialog__actions">
						<button class="secondary-button" type="button" onclick={dismissSafetyDialog}
							>Go back</button
						>
						<button class="primary-button" type="button" onclick={confirmSafetyDialog}>
							{safetyDialog.primaryLabel}
						</button>
					</div>
				</div>
			</div>
		{/if}

		<section
			hidden={['sample_waiting', 'making_test'].includes(humanState.key) ||
				(isExactItemScope && humanState.key === 'ready_to_make' && qualityMemory.state === 'empty')}
			class="quality-memory"
			class:quality-memory--empty={qualityMemory.state === 'empty'}
			class:quality-memory--attention={qualityMemory.tone === 'attention'}
			aria-labelledby="quality-memory-title"
		>
			<header class="quality-memory__header">
				<div>
					<span>Quality memory</span>
					<h2 id="quality-memory-title">{qualityMemory.title}</h2>
					{#if qualityMemory.source}<small>{qualityMemory.source}</small>{/if}
				</div>
				<strong class="quality-memory__badge quality-memory__badge--{qualityMemory.tone}"
					>{qualityMemory.badge}</strong
				>
			</header>

			{#if qualityMemory.state !== 'empty'}
				<div class="quality-memory__comparison">
					<div class="quality-memory__measured">
						<span>Measured production run</span>
						<div class="quality-memory__facts">
							{#each qualityMemory.measured as fact (fact.label)}
								<div>
									<small>{fact.label}</small>
									<strong>{fact.value}</strong>
									<p>{fact.detail}</p>
								</div>
							{/each}
						</div>
					</div>

					<div class="quality-memory__recommendation">
						<span>{qualityMemory.recommendation.label}</span>
						<strong>{qualityMemory.recommendation.value}</strong>
						<p>{qualityMemory.recommendation.detail}</p>
						<dl>
							<dt>Comparison</dt>
							<dd>{qualityMemory.comparison}</dd>
							<dt>Spread</dt>
							<dd>{qualityMemory.dispersion}</dd>
						</dl>
					</div>
				</div>
			{/if}

			<p class="quality-memory__reason">{qualityMemory.reason}</p>
			{#if qualityMemory.state !== 'empty'}
				<p class="quality-memory__policy">{qualityMemory.policyCopy}</p>
			{/if}
		</section>

		<details class="details-drawer">
			<summary>
				<span
					>{isExactItemScope && humanState.key === 'ready_to_make'
						? 'Technical details'
						: 'Details'}</span
				>
				<small>For computers, formats, and exact settings</small>
				<svg viewBox="0 0 20 20" aria-hidden="true"><path d="m5 7.5 5 5 5-5" /></svg>
			</summary>
			<div class="details-content">
				{#if showGoalScreen && hostOptions.length}
					<label class="host-select">
						<span>Computer for this sample</span>
						<select bind:value={selectedHostKey}>
							{#each hostOptions as host (host.key)}
								<option value={host.key} disabled={host.available === false}>
									{host.label || host.key}{host.available === false ? ' — unavailable' : ''}
								</option>
							{/each}
						</select>
						<small
							>{selectedHost?.detail ||
								selectedHost?.schedule_detail ||
								'Available for this sample.'}</small
						>
					</label>
				{/if}
				<div class="detail-grid">
					<div>
						<span>Source</span><strong
							>{asText(sampleItem.video_codec).toUpperCase() || 'Unknown'} · {asNumber(
								sampleItem.height
							) || '—'}p</strong
						>
					</div>
					<div>
						<span>Output</span><strong
							>{asText(technicalPolicy().encoder) || 'Configured encoder'}</strong
						>
					</div>
					<div>
						<span>Metric</span><strong
							>{asText(technicalPolicy().quality_metric).toUpperCase() || 'Automatic'}</strong
						>
					</div>
					<div>
						<span>Target</span><strong
							>{targetSummary?.targetBytes
								? `${formatDecimalFileSize(targetSummary.targetBytes)} / episode`
								: 'Not resolved'}</strong
						>
					</div>
					{#if targetSummary}
						<div>
							<span>Target mode</span><strong
								>{targetSummary.mode === 'normalized'
									? 'Runtime-normalized'
									: 'Absolute per episode'}</strong
							>
						</div>
						<div>
							<span>Sample band</span><strong
								>{formatDecimalFileSize(
									targetSummary.sampleLowerBoundBytes
								)}–{formatDecimalFileSize(targetSummary.sampleUpperBoundBytes)}</strong
							>
						</div>
						<div>
							<span>Final output band</span><strong
								>{formatDecimalFileSize(targetSummary.finalLowerBoundBytes)}–{formatDecimalFileSize(
									targetSummary.finalUpperBoundBytes
								)}</strong
							>
						</div>
					{/if}
					<div>
						<span>Stream feasibility</span><strong
							>{targetConstraint?.kind === 'arithmetic_infeasible'
								? 'Cannot fit required streams'
								: folder.stream_budget_ledger?.feasibility.status?.replaceAll('_', ' ') ||
									'Awaiting measurement'}</strong
						>
					</div>
					{#if riskSummary}
						<div>
							<span>Review authority</span><strong>{riskSummary.authority}</strong>
						</div>
					{/if}
					{#if asNumber(sampleResult.chosen_crf)}<div>
							<span>Chosen CRF</span><strong>{asNumber(sampleResult.chosen_crf)}</strong>
						</div>{/if}
					{#if asNumber(sampleResult.quality_score)}<div>
							<span>Measured score</span><strong
								>{asNumber(sampleResult.quality_score).toFixed(1)}</strong
							>
						</div>{/if}
				</div>
				{#if review.canDownload}
					<button class="detail-download" type="button" onclick={downloadComparison}>
						Download the combined comparison
					</button>
				{/if}
				{#if humanState.key === 'needs_help'}
					<p class="detail-error">
						<strong>Recorded outcome</strong>
						<span>{targetConstraint?.detail || plainFailureMessage(folder, status)}</span>
					</p>
				{/if}
			</div>
		</details>
	</main>

	<footer class="experience-footer">
		<span>{identity.show} · {identity.season}</span>
		<a href={resolve('/settings')}>Settings</a>
	</footer>
</div>

<style>
	:global(html) {
		background: #f2ece2;
	}

	:global(body) {
		margin: 0;
	}

	:global(*) {
		box-sizing: border-box;
	}

	.experience-page {
		--paper: #f3eee5;
		--ink: #20211d;
		--muted: #6c6e66;
		--line: rgb(44 45 39 / 14%);
		background: var(--paper);
		color: var(--ink);
		font-family:
			Inter,
			ui-sans-serif,
			-apple-system,
			BlinkMacSystemFont,
			'Segoe UI',
			sans-serif;
		isolation: isolate;
		min-height: 100svh;
		overflow: hidden;
		position: relative;
		transition: background 300ms ease;
	}

	.experience-page.cinematic {
		--ink: #f3f0e9;
		--muted: #a7aaa5;
		--line: rgb(255 255 255 / 12%);
		background: #101412;
	}

	.ambient {
		background:
			radial-gradient(circle at 14% 20%, rgb(181 104 67 / 13%), transparent 30rem),
			radial-gradient(circle at 85% 10%, rgb(76 122 107 / 11%), transparent 27rem);
		inset: 0;
		pointer-events: none;
		position: absolute;
		z-index: -1;
	}

	.cinematic .ambient {
		background:
			radial-gradient(circle at 12% 30%, rgb(62 112 91 / 19%), transparent 34rem),
			radial-gradient(circle at 82% 12%, rgb(145 83 54 / 14%), transparent 30rem),
			linear-gradient(180deg, rgb(255 255 255 / 2%), transparent 35rem);
	}

	.experience-header,
	main,
	.experience-footer {
		margin-inline: auto;
		max-width: 1380px;
		width: calc(100% - 64px);
	}

	.experience-header {
		align-items: center;
		border-bottom: 1px solid var(--line);
		display: grid;
		grid-template-columns: 1fr auto 1fr;
		min-height: 78px;
	}

	.back-link,
	.wordmark,
	.header-season,
	.experience-footer {
		color: var(--muted);
		font-size: 12px;
		font-weight: 650;
		text-decoration: none;
	}

	.back-link {
		align-items: center;
		display: inline-flex;
		gap: 7px;
		justify-self: start;
	}

	.back-link svg,
	.primary-button svg,
	.details-drawer svg,
	.safety-note svg {
		fill: none;
		stroke: currentColor;
		stroke-linecap: round;
		stroke-linejoin: round;
		stroke-width: 1.7;
	}

	.back-link svg {
		height: 17px;
		width: 17px;
	}

	.back-link:hover,
	.wordmark:hover,
	.experience-footer a:hover {
		color: var(--ink);
	}

	.wordmark {
		color: var(--ink);
		font-size: 13px;
		font-weight: 800;
		letter-spacing: -0.01em;
	}

	.header-season {
		justify-self: end;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	main {
		min-height: calc(100svh - 150px);
		padding: 54px 0 70px;
	}

	h1,
	h2,
	p {
		margin-top: 0;
	}

	h1,
	h2 {
		color: var(--ink);
	}

	h1 {
		font-family: 'Iowan Old Style', 'Baskerville', Georgia, serif;
		font-size: clamp(48px, 6.5vw, 88px);
		font-weight: 500;
		letter-spacing: -0.055em;
		line-height: 0.98;
		margin-bottom: 26px;
	}

	.eyebrow {
		color: #8a604c;
		font-size: 11px;
		font-weight: 800;
		letter-spacing: 0.14em;
		margin-bottom: 18px;
		text-transform: uppercase;
	}

	.cinematic .eyebrow {
		color: #9cc1ab;
	}

	.lede {
		color: var(--muted);
		font-family: 'Iowan Old Style', 'Baskerville', Georgia, serif;
		font-size: clamp(18px, 2vw, 24px);
		line-height: 1.5;
		max-width: 760px;
	}

	.action-notice {
		align-items: flex-start;
		background: rgb(142 65 49 / 8%);
		border: 1px solid rgb(142 65 49 / 24%);
		border-radius: 16px;
		display: flex;
		gap: 13px;
		margin: 0 auto 34px;
		max-width: 980px;
		padding: 16px 18px;
	}

	.action-notice > span {
		align-items: center;
		background: #9d5c45;
		border-radius: 50%;
		color: white;
		display: flex;
		font-size: 12px;
		font-weight: 800;
		height: 23px;
		justify-content: center;
		width: 23px;
	}

	.action-notice strong {
		font-size: 14px;
	}

	.action-notice p {
		color: var(--muted);
		font-size: 13px;
		line-height: 1.45;
		margin: 4px 0 0;
	}

	.action-notice--success {
		background: rgb(65 126 89 / 9%);
		border-color: rgb(65 126 89 / 22%);
	}

	.action-notice--success > span {
		background: #4e8063;
	}

	.action-notice--neutral {
		background: rgb(90 97 107 / 9%);
		border-color: rgb(90 97 107 / 24%);
	}

	.action-notice--neutral > span {
		background: #65717d;
	}

	.goal-room,
	.ready-room,
	.finished-room,
	.help-room,
	.working-room,
	.loading-room {
		margin-inline: auto;
		max-width: 980px;
	}

	.goal-intro {
		max-width: 850px;
	}

	.goal-options {
		display: grid;
		gap: 12px;
		grid-template-columns: repeat(3, minmax(0, 1fr));
		margin: 48px 0 38px;
	}

	.goal-options button {
		background: rgb(255 255 255 / 40%);
		border: 1px solid var(--line);
		border-radius: 20px;
		color: var(--ink);
		cursor: pointer;
		display: flex;
		font: inherit;
		gap: 14px;
		min-height: 230px;
		padding: 22px;
		text-align: left;
		transition:
			background 160ms ease,
			border-color 160ms ease,
			box-shadow 160ms ease,
			transform 160ms ease;
	}

	.goal-options button:hover {
		background: rgb(255 255 255 / 72%);
		transform: translateY(-2px);
	}

	.goal-options button.selected {
		background: #242722;
		border-color: #242722;
		box-shadow: 0 22px 52px rgb(39 39 34 / 15%);
		color: #f5efe5;
		transform: translateY(-3px);
	}

	.goal-radio {
		align-items: center;
		border: 1.5px solid currentColor;
		border-radius: 50%;
		display: flex;
		flex: 0 0 auto;
		height: 18px;
		justify-content: center;
		margin-top: 2px;
		opacity: 0.55;
		width: 18px;
	}

	.selected .goal-radio {
		opacity: 1;
	}

	.selected .goal-radio i {
		background: #8fc3a2;
		border-radius: 50%;
		height: 8px;
		width: 8px;
	}

	.goal-copy {
		display: flex;
		flex-direction: column;
		min-width: 0;
	}

	.goal-label {
		font-size: 12px;
		font-weight: 750;
		letter-spacing: 0.06em;
		margin-bottom: 25px;
		text-transform: uppercase;
	}

	.goal-copy strong {
		font-family: 'Iowan Old Style', Georgia, serif;
		font-size: 37px;
		font-weight: 500;
		letter-spacing: -0.04em;
		line-height: 1;
	}

	.goal-copy small {
		font-size: 11px;
		margin-top: 8px;
		opacity: 0.66;
	}

	.goal-copy p {
		font-size: 13px;
		line-height: 1.5;
		margin: auto 0 0;
		opacity: 0.72;
		padding-top: 23px;
	}

	.goal-action {
		align-items: center;
		display: flex;
		gap: 28px;
		justify-content: space-between;
	}

	.goal-action__message {
		flex: 1;
	}

	.goal-action__button {
		align-items: flex-end;
		display: flex;
		flex-direction: column;
		gap: 7px;
	}

	.mobile-safety {
		display: none;
	}

	.host-unavailable {
		color: #8c5746;
		font-size: 12px;
		font-weight: 650;
		margin: 10px 0 0 43px;
	}

	.safety-note {
		align-items: center;
		color: #656860;
		display: flex;
		gap: 12px;
		max-width: 550px;
	}

	.safety-note svg {
		color: #517660;
		flex: 0 0 auto;
		height: 31px;
		width: 31px;
	}

	.safety-note p {
		font-size: 13px;
		line-height: 1.45;
		margin: 0;
	}

	.primary-button,
	.secondary-button,
	.text-link {
		align-items: center;
		border-radius: 999px;
		display: inline-flex;
		font: inherit;
		font-size: 13px;
		font-weight: 750;
		gap: 10px;
		justify-content: center;
		text-decoration: none;
	}

	.primary-button {
		background: #232620;
		border: 1px solid #232620;
		color: #f7f1e7;
		cursor: pointer;
		min-height: 50px;
		padding: 0 23px;
		transition:
			background 150ms ease,
			transform 150ms ease;
	}

	.primary-button:hover:not(:disabled) {
		background: #393e35;
		transform: translateY(-2px);
	}

	.primary-button:disabled {
		cursor: not-allowed;
		opacity: 0.45;
	}

	.primary-button svg {
		height: 18px;
		width: 18px;
	}

	button:focus-visible,
	a:focus-visible,
	select:focus-visible,
	summary:focus-visible {
		outline: 3px solid #4b8060;
		outline-offset: 3px;
	}

	.cinematic button:focus-visible,
	.cinematic a:focus-visible,
	.cinematic summary:focus-visible {
		outline-color: #a8d7b9;
	}

	.loading-room,
	.working-room {
		align-items: center;
		display: flex;
		flex-direction: column;
		justify-content: center;
		min-height: 610px;
		text-align: center;
	}

	.breathing-mark {
		display: flex;
		gap: 7px;
		margin-bottom: 20px;
	}

	.breathing-mark i {
		animation: breath 1.3s ease-in-out infinite;
		background: #657265;
		border-radius: 50%;
		height: 8px;
		width: 8px;
	}

	.breathing-mark i:nth-child(2) {
		animation-delay: 130ms;
	}

	.breathing-mark i:nth-child(3) {
		animation-delay: 260ms;
	}

	.working-room h1 {
		font-size: clamp(48px, 6vw, 76px);
		margin-bottom: 18px;
	}

	.working-room .lede {
		max-width: 600px;
	}

	.working-orbit {
		height: 150px;
		margin: 10px 0 34px;
		position: relative;
		width: 150px;
	}

	.working-orbit span,
	.working-orbit i,
	.working-orbit b {
		border: 1px solid rgb(78 101 82 / 24%);
		border-radius: 50%;
		inset: 0;
		position: absolute;
	}

	.working-orbit i {
		animation: orbit 5s linear infinite;
		border-color: transparent;
		border-top-color: #62836c;
		inset: 14px;
	}

	.working-orbit b {
		background: #34463a;
		border: 0;
		box-shadow: 0 0 50px rgb(69 103 80 / 23%);
		inset: 54px;
	}

	.elapsed {
		align-items: center;
		color: #5f665e;
		display: inline-flex;
		font-size: 12px;
		font-weight: 700;
		gap: 8px;
		margin-top: 18px;
	}

	.elapsed i {
		animation: breath 1.4s ease-in-out infinite;
		background: #4b8060;
		border-radius: 50%;
		height: 7px;
		width: 7px;
	}

	.leave-note {
		color: #85877f;
		font-size: 12px;
		margin-top: 34px;
	}

	.active-room {
		display: grid;
		gap: 34px 50px;
		grid-template-columns: minmax(0, 1.1fr) minmax(320px, 0.8fr);
		margin: 30px auto 0;
		max-width: 1160px;
	}

	.active-copy {
		align-self: center;
	}

	.active-copy h1 {
		font-size: clamp(58px, 7vw, 94px);
	}

	.active-facts {
		border-top: 1px solid var(--line);
		display: grid;
		gap: 25px;
		grid-template-columns: repeat(3, minmax(0, 1fr));
		margin-top: 43px;
		padding-top: 21px;
	}

	.active-facts div {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.active-facts span {
		color: var(--muted);
		font-size: 10px;
		font-weight: 750;
		letter-spacing: 0.1em;
		text-transform: uppercase;
	}

	.active-facts strong {
		font-size: 13px;
		font-weight: 650;
	}

	.step-line {
		border-top: 1px solid var(--line);
		display: flex;
		grid-column: 1 / -1;
		justify-content: space-between;
		padding-top: 20px;
		position: relative;
	}

	.step-line span {
		align-items: center;
		color: #777e78;
		display: flex;
		font-size: 11px;
		font-weight: 700;
		gap: 8px;
	}

	.step-line span i {
		align-items: center;
		border: 1px solid #5d665f;
		border-radius: 50%;
		display: flex;
		height: 16px;
		justify-content: center;
		width: 16px;
	}

	.step-line .done,
	.step-line .active {
		color: #c5d1c8;
	}

	.step-line .done i {
		background: #416d53;
		border-color: #416d53;
		font-size: 9px;
	}

	.step-line .active i {
		animation: breath 1.4s ease-in-out infinite;
		background: #8bb79a;
		border: 4px solid #31473a;
	}

	.active-note {
		color: var(--muted);
		font-size: 12px;
		grid-column: 1 / -1;
		margin: -14px 0 0;
		text-align: center;
	}

	.compare-room {
		margin-inline: auto;
		max-width: 1280px;
	}

	.compare-heading {
		align-items: flex-end;
		display: flex;
		gap: 30px;
		justify-content: space-between;
		margin-bottom: 34px;
	}

	.compare-heading h1 {
		font-size: clamp(46px, 5.3vw, 72px);
		margin-bottom: 11px;
	}

	.compare-heading > div > p:last-child {
		color: var(--muted);
		font-size: 13px;
		margin-bottom: 0;
	}

	.missing-media {
		align-items: center;
		background: rgb(255 255 255 / 4%);
		border: 1px solid var(--line);
		border-radius: 20px;
		display: flex;
		flex-direction: column;
		justify-content: center;
		min-height: 330px;
		padding: 30px;
		text-align: center;
	}

	.missing-media h2 {
		font-family: 'Iowan Old Style', Georgia, serif;
		font-size: 30px;
		font-weight: 500;
		max-width: 600px;
	}

	.missing-media p {
		color: var(--muted);
	}

	.risk-summary {
		display: grid;
		gap: 18px;
		grid-template-columns: repeat(3, minmax(0, 1fr));
		margin-top: 26px;
		padding: 18px 20px;
		border: 1px solid rgba(125, 137, 128, 0.35);
		border-radius: 18px;
		background: rgba(20, 25, 22, 0.82);
	}

	.risk-summary--attention {
		border-color: rgba(177, 98, 95, 0.6);
		background: rgba(43, 21, 21, 0.84);
	}

	.risk-summary--ready {
		border-color: rgba(86, 150, 137, 0.5);
		background: rgba(20, 40, 36, 0.82);
	}

	.risk-summary span {
		color: #9ba49e;
		display: block;
		font-size: 10px;
		font-weight: 750;
		letter-spacing: 0.09em;
		margin-bottom: 6px;
		text-transform: uppercase;
	}

	.risk-summary strong {
		display: block;
		font-size: 18px;
		font-weight: 650;
	}

	.risk-summary p {
		color: #d5dbd6;
		font-size: 12px;
		line-height: 1.5;
		margin: 6px 0 0;
	}

	.risk-summary__moments {
		grid-column: 1 / -1;
	}

	.decision {
		align-items: center;
		background: #e8eee8;
		border-radius: 20px;
		color: #1e251f;
		display: flex;
		gap: 30px;
		justify-content: space-between;
		margin-top: 35px;
		padding: 24px 26px;
	}

	.decision h2 {
		color: #1e251f;
		font-family: 'Iowan Old Style', Georgia, serif;
		font-size: 26px;
		font-weight: 600;
		letter-spacing: -0.025em;
		margin-bottom: 4px;
	}

	.decision p {
		color: #68716a;
		font-size: 12px;
		margin-bottom: 0;
	}

	.decision-actions {
		display: flex;
		gap: 9px;
	}

	.secondary-button {
		background: transparent;
		border: 1px solid rgb(30 37 31 / 22%);
		color: #374038;
		cursor: pointer;
		min-height: 47px;
		padding: 0 18px;
	}

	.primary-button--light {
		background: #1f3125;
		border-color: #1f3125;
		min-height: 47px;
	}

	.ready-room,
	.finished-room,
	.help-room {
		align-items: center;
		display: flex;
		flex-direction: column;
		justify-content: center;
		min-height: 620px;
		text-align: center;
	}

	.ready-symbol,
	.help-mark {
		align-items: center;
		background: #dfe8dd;
		border: 1px solid rgb(62 102 73 / 18%);
		border-radius: 50%;
		color: #42624b;
		display: flex;
		height: 78px;
		justify-content: center;
		margin-bottom: 31px;
		width: 78px;
	}

	.ready-symbol span {
		font-family: Georgia, serif;
		font-size: 32px;
	}

	.ready-room h1,
	.finished-room h1,
	.help-room h1 {
		font-size: clamp(52px, 7vw, 88px);
	}

	.ready-room .lede,
	.finished-room .lede,
	.help-room .lede {
		max-width: 720px;
	}

	.ready-summary {
		border-bottom: 1px solid var(--line);
		border-top: 1px solid var(--line);
		display: grid;
		gap: 30px;
		grid-template-columns: repeat(3, minmax(0, 1fr));
		margin: 33px 0;
		max-width: 720px;
		padding: 20px 0;
		width: 100%;
	}

	.ready-summary div {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.ready-summary span {
		color: var(--muted);
		font-size: 10px;
		font-weight: 750;
		letter-spacing: 0.1em;
		text-transform: uppercase;
	}

	.ready-summary strong {
		font-family: 'Iowan Old Style', Georgia, serif;
		font-size: 22px;
		font-weight: 500;
	}

	.season-progress-room {
		align-items: center;
		display: grid;
		gap: 40px 70px;
		grid-template-columns: minmax(0, 1fr) 360px;
		margin: 55px auto 0;
		max-width: 1120px;
	}

	.progress-copy h1 {
		font-size: clamp(60px, 7vw, 94px);
	}

	.progress-facts {
		align-items: baseline;
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
		margin-top: 36px;
	}

	.progress-facts strong {
		font-family: 'Iowan Old Style', Georgia, serif;
		font-size: 34px;
		font-weight: 500;
	}

	.progress-facts span,
	.progress-facts small {
		color: var(--muted);
		font-size: 12px;
	}

	.progress-facts small {
		flex-basis: 100%;
		margin-top: 8px;
	}

	.progress-ring {
		align-items: center;
		background: conic-gradient(#91c8a7 var(--progress), rgb(255 255 255 / 8%) 0);
		border-radius: 50%;
		display: flex;
		height: 320px;
		justify-content: center;
		padding: 15px;
		width: 320px;
	}

	.progress-ring > div {
		align-items: center;
		background: #151b17;
		border-radius: 50%;
		display: flex;
		flex-direction: column;
		height: 100%;
		justify-content: center;
		width: 100%;
	}

	.progress-ring strong {
		font-family: 'Iowan Old Style', Georgia, serif;
		font-size: 72px;
		font-weight: 400;
		letter-spacing: -0.07em;
		line-height: 1;
	}

	.progress-ring span {
		color: #9da7a0;
		font-size: 10px;
		font-weight: 750;
		letter-spacing: 0.12em;
		margin-top: 9px;
		text-transform: uppercase;
	}

	.progress-track {
		background: rgb(255 255 255 / 8%);
		border-radius: 999px;
		grid-column: 1 / -1;
		height: 5px;
		overflow: hidden;
	}

	.progress-track i {
		background: #91c8a7;
		border-radius: inherit;
		display: block;
		height: 100%;
		transition: width 500ms ease;
	}

	.progress-note {
		color: var(--muted);
		font-size: 12px;
		grid-column: 1 / -1;
		margin: -15px 0 0;
		text-align: center;
	}

	.finished-rings {
		align-items: center;
		display: flex;
		height: 130px;
		justify-content: center;
		margin-bottom: 28px;
		position: relative;
		width: 130px;
	}

	.finished-rings i {
		border: 1px solid rgb(67 120 82 / 28%);
		border-radius: 50%;
		inset: 0;
		position: absolute;
	}

	.finished-rings i:nth-child(2) {
		inset: 15px;
	}

	.finished-rings span {
		align-items: center;
		background: #4e7e5b;
		border-radius: 50%;
		color: white;
		display: flex;
		font-size: 25px;
		height: 66px;
		justify-content: center;
		width: 66px;
	}

	.finished-actions {
		align-items: center;
		display: flex;
		gap: 24px;
		margin-top: 28px;
	}

	.text-link {
		color: #5e655d;
	}

	.help-mark {
		background: #eee0d5;
		border-color: rgb(150 81 58 / 22%);
		color: #9a5a43;
		font-family: Georgia, serif;
		font-size: 34px;
	}

	.help-safety {
		background: rgb(255 255 255 / 44%);
		border: 1px solid var(--line);
		border-radius: 16px;
		display: flex;
		flex-direction: column;
		gap: 4px;
		margin: 26px 0 30px;
		max-width: 620px;
		padding: 16px 20px;
		width: 100%;
	}

	.help-safety strong {
		font-size: 13px;
	}

	.help-safety span {
		color: var(--muted);
		font-size: 12px;
	}

	.recovery-count {
		align-items: baseline;
		display: flex;
		gap: 8px;
		margin: -10px 0 27px;
	}

	.recovery-count strong {
		font-family: 'Iowan Old Style', Georgia, serif;
		font-size: 27px;
		font-weight: 500;
	}

	.recovery-count span {
		color: var(--muted);
		font-size: 12px;
	}

	.safety-dialog-backdrop {
		align-items: center;
		background: rgb(13 16 14 / 62%);
		display: flex;
		inset: 0;
		justify-content: center;
		padding: 22px;
		position: fixed;
		z-index: 80;
	}

	.safety-dialog {
		background: #f3eee5;
		border: 1px solid rgb(44 45 39 / 18%);
		border-radius: 24px;
		box-shadow: 0 30px 100px rgb(0 0 0 / 35%);
		color: #20211d;
		max-width: 620px;
		padding: 34px;
		width: 100%;
	}

	.safety-dialog h2 {
		color: #20211d;
		font-family: 'Iowan Old Style', Georgia, serif;
		font-size: clamp(32px, 5vw, 46px);
		font-weight: 500;
		letter-spacing: -0.04em;
		line-height: 1.02;
		margin-bottom: 17px;
	}

	.safety-dialog > p:not(.eyebrow) {
		color: #666a62;
		font-size: 15px;
		line-height: 1.55;
	}

	.safety-dialog ul {
		background: rgb(255 255 255 / 55%);
		border: 1px solid rgb(44 45 39 / 12%);
		border-radius: 14px;
		color: #4f554d;
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 11px;
		line-height: 1.5;
		list-style: none;
		margin: 22px 0 0;
		padding: 14px 16px;
	}

	.safety-dialog li + li {
		border-top: 1px solid rgb(44 45 39 / 10%);
		margin-top: 8px;
		padding-top: 8px;
	}

	.safety-dialog__actions {
		display: flex;
		gap: 10px;
		justify-content: flex-end;
		margin-top: 28px;
	}

	.details-drawer {
		border-top: 1px solid var(--line);
		margin: 62px auto 0;
		max-width: 980px;
	}

	.details-drawer summary {
		align-items: center;
		cursor: pointer;
		display: grid;
		gap: 12px;
		grid-template-columns: auto 1fr 20px;
		list-style: none;
		min-height: 62px;
	}

	.details-drawer summary::-webkit-details-marker {
		display: none;
	}

	.details-drawer summary span {
		font-size: 12px;
		font-weight: 750;
	}

	.details-drawer summary small {
		color: var(--muted);
		font-size: 11px;
	}

	.details-drawer summary svg {
		color: var(--muted);
		transition: transform 160ms ease;
		width: 18px;
	}

	.details-drawer[open] summary svg {
		transform: rotate(180deg);
	}

	.details-content {
		background: rgb(255 255 255 / 28%);
		border: 1px solid var(--line);
		border-radius: 16px;
		margin-bottom: 16px;
		padding: 22px;
	}

	.cinematic .details-content {
		background: rgb(255 255 255 / 4%);
	}

	.host-select {
		display: flex;
		flex-direction: column;
		gap: 7px;
		margin-bottom: 24px;
	}

	.host-select > span,
	.detail-grid span {
		color: var(--muted);
		font-size: 10px;
		font-weight: 750;
		letter-spacing: 0.09em;
		text-transform: uppercase;
	}

	.host-select select {
		appearance: none;
		background: transparent;
		border: 1px solid var(--line);
		border-radius: 10px;
		color: var(--ink);
		font: inherit;
		font-size: 13px;
		max-width: 420px;
		min-height: 42px;
		padding: 0 12px;
	}

	.host-select small {
		color: var(--muted);
		font-size: 11px;
	}

	.detail-grid {
		display: grid;
		gap: 20px;
		grid-template-columns: repeat(3, minmax(0, 1fr));
	}

	.detail-grid div {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.detail-grid strong {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 12px;
		font-weight: 500;
	}

	.detail-download {
		background: transparent;
		border: 0;
		color: #618570;
		cursor: pointer;
		display: inline-flex;
		font: inherit;
		font-size: 12px;
		font-weight: 700;
		margin-top: 22px;
		padding: 0;
		text-decoration: underline;
	}

	.experience-footer {
		align-items: center;
		border-top: 1px solid var(--line);
		display: flex;
		justify-content: space-between;
		min-height: 72px;
	}

	.experience-footer a {
		color: inherit;
		text-decoration: none;
	}

	@keyframes breath {
		0%,
		100% {
			opacity: 0.35;
			transform: scale(0.88);
		}
		50% {
			opacity: 1;
			transform: scale(1);
		}
	}

	@keyframes orbit {
		to {
			transform: rotate(360deg);
		}
	}

	@media (max-width: 950px) {
		.experience-header,
		main,
		.experience-footer {
			width: calc(100% - 40px);
		}

		.goal-options {
			grid-template-columns: 1fr;
		}

		.goal-contract,
		.review-feedback-fields {
			grid-template-columns: 1fr;
		}

		.goal-contract > div + div {
			border-left: 0;
			border-top: 1px solid var(--mf-line-muted);
		}

		.comparison-ledger,
		.risk-summary,
		.risk-summary--attention,
		.risk-summary--ready {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}

		.comparison-ledger > div:nth-child(3) {
			border-left: 0;
			border-top: 1px solid var(--mf-line-muted);
		}

		.comparison-ledger > div:nth-child(4) {
			border-top: 1px solid var(--mf-line-muted);
		}

		.review-feedback-panel__heading,
		.review-feedback-panel__action {
			align-items: stretch;
			flex-direction: column;
		}

		.season-estimate-note {
			grid-template-columns: 1fr;
		}

		.goal-options button {
			min-height: 0;
		}

		.goal-copy p {
			margin-top: 20px;
		}

		.active-room,
		.season-progress-room {
			grid-template-columns: 1fr;
		}

		.progress-ring {
			justify-self: center;
		}

		.compare-heading {
			align-items: flex-start;
			flex-direction: column;
		}

		.risk-summary {
			grid-template-columns: 1fr;
		}

		.decision {
			align-items: stretch;
			flex-direction: column;
		}
	}

	@media (max-width: 650px) {
		.experience-header,
		main,
		.experience-footer {
			width: calc(100% - 28px);
		}

		.experience-header {
			grid-template-columns: 1fr auto;
			min-height: 64px;
		}

		.wordmark {
			justify-self: end;
		}

		.header-season {
			display: none;
		}

		main {
			padding-top: 38px;
		}

		h1 {
			font-size: clamp(46px, 14vw, 66px);
		}

		.lede {
			font-size: 18px;
		}

		.goal-options {
			margin-top: 34px;
		}

		.goal-room {
			padding-bottom: 82px;
		}

		.goal-options button {
			padding: 19px;
		}

		.goal-copy strong {
			font-size: 32px;
		}

		.goal-action,
		.decision-actions,
		.finished-actions {
			align-items: stretch;
			flex-direction: column;
		}

		.goal-action {
			backdrop-filter: blur(14px);
			background: rgb(243 238 229 / 92%);
			border: 1px solid rgb(44 45 39 / 14%);
			border-radius: 18px;
			bottom: 10px;
			box-shadow: 0 18px 50px rgb(35 37 31 / 18%);
			left: 10px;
			padding: 9px;
			position: fixed;
			right: 10px;
			z-index: 20;
		}

		.goal-action__message {
			display: none;
		}

		.goal-action__button {
			align-items: stretch;
			width: 100%;
		}

		.mobile-safety {
			color: #666a62;
			display: block;
			font-size: 10px;
			font-weight: 700;
			letter-spacing: 0.04em;
			text-align: center;
		}

		.primary-button,
		.secondary-button {
			width: 100%;
		}

		.active-facts,
		.ready-summary,
		.detail-grid {
			grid-template-columns: 1fr;
		}

		.step-line span {
			font-size: 9px;
			gap: 4px;
		}

		.compare-heading h1 {
			font-size: 46px;
		}

		.safety-dialog {
			border-radius: 19px;
			padding: 24px 20px;
		}

		.safety-dialog__actions {
			align-items: stretch;
			flex-direction: column-reverse;
		}

		.decision {
			padding: 20px;
		}

		.progress-ring {
			height: 260px;
			width: 260px;
		}

		.progress-ring strong {
			font-size: 60px;
		}

		.experience-footer span {
			max-width: 75%;
			overflow: hidden;
			text-overflow: ellipsis;
			white-space: nowrap;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.breathing-mark i,
		.working-orbit i,
		.elapsed i,
		.step-line .active i {
			animation: none;
		}

		.experience-page,
		.goal-options button,
		.primary-button,
		.active-progress__track i,
		.progress-track i {
			transition: none;
		}
	}

	/* Whole-product reset: one light application, with darkness contained to media. */
	:global(html),
	:global(body) {
		background: var(--mf-bg-base);
		color: var(--mf-fg-primary);
		font-family: var(--mf-font-sans);
	}

	.experience-page,
	.experience-page.cinematic {
		background: var(--mf-bg-base);
		color: var(--mf-fg-primary);
		font-family: var(--mf-font-sans);
		min-height: calc(100vh - 58px);
		transition: none;
	}

	.ambient,
	.experience-footer {
		display: none;
	}

	.experience-header {
		align-items: center;
		background: transparent;
		border: 0;
		color: var(--mf-fg-secondary);
		display: flex;
		height: auto;
		justify-content: space-between;
		margin: 0 auto;
		max-width: 1088px;
		min-height: 0;
		padding: 18px 24px 0;
		position: static;
	}

	.experience-header .wordmark {
		display: none;
	}

	.back-link {
		color: var(--mf-active-fg);
		font-size: 13px;
		font-weight: 600;
		gap: 5px;
	}

	.back-link:hover {
		color: var(--mf-active-fg-bright);
	}

	.back-link svg {
		height: 15px;
		width: 15px;
	}

	.header-season {
		color: var(--mf-fg-tertiary);
		font-size: 13px;
		font-weight: 500;
	}

	.experience-page main {
		margin: 0 auto;
		max-width: 1088px;
		min-height: 0;
		padding: 18px 24px 64px;
	}

	.experience-page h1,
	.experience-page.cinematic h1,
	.goal-room h1,
	.active-room h1,
	.compare-room h1,
	.ready-room h1,
	.exact-approved h1,
	.season-progress-room h1,
	.finished-room h1,
	.help-room h1,
	.working-room h1 {
		color: var(--mf-fg-primary);
		font-family: var(--mf-font-sans);
		font-size: clamp(22px, 2.5vw, 28px);
		font-style: normal;
		font-weight: 600;
		letter-spacing: -0.02em;
		line-height: 1.25;
		margin: 0;
	}

	.experience-page h2,
	.experience-page.cinematic h2 {
		color: var(--mf-fg-primary);
		font-family: var(--mf-font-sans);
		font-size: 17px;
		font-weight: 600;
		letter-spacing: -0.01em;
		line-height: 1.35;
	}

	.experience-page p,
	.experience-page.cinematic p,
	.lede {
		color: var(--mf-fg-secondary);
		font-family: var(--mf-font-sans);
		font-size: 14px;
		line-height: 1.55;
	}

	.eyebrow,
	.cinematic .eyebrow {
		background: var(--mf-active-bg);
		border-radius: 999px;
		color: var(--mf-active-fg);
		display: inline-flex;
		font-family: var(--mf-font-sans);
		font-size: 11px;
		font-weight: 700;
		letter-spacing: 0.02em;
		line-height: 1;
		padding: 6px 9px;
		text-transform: none;
		width: fit-content;
	}

	.eyebrow.eyebrow--missed,
	.cinematic .eyebrow.eyebrow--missed {
		background: var(--mf-fail-bg);
		box-shadow: inset 0 0 0 1px var(--mf-fail-line);
		color: var(--mf-fail-fg);
	}

	.action-notice {
		align-items: flex-start;
		background: var(--mf-fail-bg);
		border: 1px solid var(--mf-fail-line);
		border-radius: var(--mf-radius-3);
		color: var(--mf-fail-fg);
		margin: 0 0 18px;
		padding: 13px 15px;
	}

	.action-notice--success {
		background: var(--mf-ready-bg);
		border-color: var(--mf-ready-line);
		color: var(--mf-ready-fg);
	}

	.action-notice--neutral {
		background: var(--mf-idle-bg);
		border-color: var(--mf-idle-line);
		color: var(--mf-idle-fg);
	}

	.action-notice--neutral > span {
		background: var(--mf-idle-fg);
	}

	.action-notice p {
		color: inherit;
	}

	.action-notice__link {
		color: inherit;
		display: inline-flex;
		font-size: 12px;
		font-weight: 700;
		margin-top: 8px;
		text-underline-offset: 3px;
	}

	.lifecycle-notice {
		align-items: flex-start;
		background: var(--mf-wait-bg);
		border: 1px solid var(--mf-wait-line);
		border-radius: var(--mf-radius-2);
		color: var(--mf-wait-fg);
		display: flex;
		gap: 12px;
		margin: 0 0 18px;
		padding: 12px 14px;
	}

	.lifecycle-notice > span {
		font-size: 12px;
		line-height: 1.5;
	}

	.lifecycle-notice strong {
		font-size: 13px;
	}

	.lifecycle-notice p {
		color: var(--mf-fg-secondary);
		font-size: 12px;
		line-height: 1.45;
		margin: 3px 0 0;
	}

	.lifecycle-notice p + p {
		margin-top: 7px;
	}

	.lifecycle-notice b {
		color: var(--mf-wait-fg);
	}

	.episode-selector {
		align-items: center;
		background: var(--mf-bg-panel);
		border: 1px solid var(--mf-line);
		border-radius: var(--mf-radius-3);
		display: grid;
		gap: 18px;
		grid-template-columns: minmax(0, 1fr) minmax(280px, 380px);
		margin: 0 0 18px;
		padding: 16px 18px;
	}

	.episode-selector__copy {
		display: grid;
		gap: 3px;
	}

	.episode-selector__copy .eyebrow {
		margin: 0;
	}

	.episode-selector__copy h2 {
		font-size: 18px;
		letter-spacing: -0.02em;
		line-height: 1.2;
		margin: 0;
	}

	.episode-selector__copy > p:last-child,
	.episode-selector__status {
		color: var(--mf-fg-secondary);
		font-size: 12px;
		line-height: 1.45;
		margin: 0;
	}

	.episode-selector__control {
		display: grid;
		gap: 6px;
	}

	.episode-selector__actions {
		display: grid;
		gap: 8px;
	}

	.episode-selector__control > span {
		color: var(--mf-fg-tertiary);
		font-size: 10px;
		font-weight: 700;
		letter-spacing: 0.08em;
		text-transform: uppercase;
	}

	.episode-selector__control select {
		background: var(--mf-bg-input);
		border: 1px solid var(--mf-line-strong);
		border-radius: var(--mf-radius-2);
		color: var(--mf-fg-primary);
		font: inherit;
		font-size: 13px;
		font-weight: 650;
		min-height: 44px;
		padding: 0 38px 0 12px;
		width: 100%;
	}

	.episode-selector__control select:disabled {
		cursor: wait;
		opacity: 0.65;
	}

	.episode-selector__open {
		width: 100%;
	}

	.loading-room,
	.working-room,
	.goal-room,
	.scope-activity-room,
	.active-room,
	.compare-room,
	.ready-room,
	.exact-approved,
	.season-progress-room,
	.finished-room,
	.help-room {
		background: var(--mf-bg-panel);
		border: 1px solid var(--mf-line);
		border-radius: var(--mf-radius-3);
		box-shadow: var(--mf-shadow-popover);
		color: var(--mf-fg-primary);
		margin: 0;
		max-width: none;
		min-height: 0;
		padding: 24px;
	}

	.scope-activity-room {
		display: grid;
		gap: 20px;
	}

	.scope-activity-room__copy {
		display: grid;
		gap: 8px;
		max-width: 760px;
	}

	.scope-activity-facts,
	.recovery-plan {
		background: var(--mf-line-muted);
		border: 1px solid var(--mf-line-muted);
		border-radius: var(--mf-radius-3);
		display: grid;
		gap: 1px;
		grid-template-columns: repeat(3, minmax(0, 1fr));
		overflow: hidden;
	}

	.scope-activity-facts div,
	.recovery-plan div {
		background: var(--mf-bg-panel-2);
		display: grid;
		gap: 3px;
		min-width: 0;
		padding: 13px 14px;
	}

	.scope-activity-facts span,
	.recovery-plan span {
		color: var(--mf-fg-tertiary);
		font-size: 11px;
	}

	.scope-activity-facts strong,
	.recovery-plan strong {
		color: var(--mf-fg-primary);
		font-size: 13px;
		overflow-wrap: anywhere;
	}

	.scope-activity-facts small,
	.active-facts small {
		color: var(--mf-fg-tertiary);
		font-size: 10px;
		line-height: 1.4;
	}

	.scope-activity-facts .telemetry-attention {
		background: var(--mf-wait-bg);
	}

	.scope-activity-actions,
	.help-actions {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
	}

	.loading-room {
		align-items: center;
		display: flex;
		gap: 12px;
		justify-content: center;
		min-height: 220px;
	}

	.breathing-mark {
		display: none;
	}

	.working-room {
		align-items: flex-start;
		display: grid;
		gap: 11px;
		justify-items: start;
		min-height: 260px;
		place-content: center;
		text-align: left;
	}

	.working-orbit {
		background: var(--mf-active-bg);
		border: 0;
		border-radius: 999px;
		height: 8px;
		margin: 3px 0 8px;
		overflow: hidden;
		width: min(360px, 72vw);
	}

	.working-orbit span {
		animation: working-bar 1.6s ease-in-out infinite;
		background: var(--mf-active-solid);
		border-radius: inherit;
		display: block;
		height: 100%;
		width: 38%;
	}

	.working-orbit i,
	.working-orbit b {
		display: none;
	}

	.elapsed {
		background: var(--mf-bg-raised);
		border-radius: 999px;
		color: var(--mf-fg-secondary);
		font-size: 12px;
		padding: 6px 9px;
	}

	.elapsed i {
		background: var(--mf-active-fg);
	}

	.leave-note {
		color: var(--mf-fg-tertiary);
		font-size: 12px;
	}

	.goal-room {
		display: grid;
		gap: 22px;
		grid-template-columns: 1fr;
	}

	.goal-intro {
		display: grid;
		gap: 8px;
		max-width: 720px;
	}

	.goal-options {
		display: grid;
		gap: 10px;
		grid-template-columns: repeat(3, minmax(0, 1fr));
		margin: 0;
		max-width: none;
	}

	.goal-options__empty {
		background: var(--mf-attention-bg);
		border: 1px solid var(--mf-attention-line);
		border-radius: var(--mf-radius-3);
		color: var(--mf-attention-fg);
		grid-column: 1 / -1;
		margin: 0;
		padding: 16px;
	}

	.goal-options button {
		align-items: flex-start;
		background: var(--mf-bg-panel);
		border: 1px solid var(--mf-line);
		border-radius: var(--mf-radius-3);
		box-shadow: none;
		color: var(--mf-fg-primary);
		display: flex;
		gap: 10px;
		min-height: 150px;
		padding: 15px;
		text-align: left;
		transform: none;
	}

	.goal-options button:hover {
		background: var(--mf-bg-panel-2);
		border-color: var(--mf-active-line);
		transform: none;
	}

	.goal-options button.selected {
		background: var(--mf-active-bg);
		border-color: var(--mf-active-fg);
		box-shadow: 0 0 0 1px var(--mf-active-fg);
	}

	.goal-radio {
		border-color: var(--mf-line-strong);
		flex: 0 0 auto;
		height: 18px;
		margin-top: 2px;
		width: 18px;
	}

	.selected .goal-radio {
		border-color: var(--mf-active-fg);
	}

	.selected .goal-radio i {
		background: var(--mf-active-fg);
	}

	.goal-copy {
		display: grid;
		gap: 2px;
	}

	.goal-label {
		color: var(--mf-fg-secondary);
		font-size: 11px;
		font-weight: 700;
		letter-spacing: 0.03em;
		margin-bottom: 5px;
		text-transform: uppercase;
	}

	.goal-copy strong {
		color: var(--mf-fg-primary);
		font-family: var(--mf-font-sans);
		font-size: 24px;
		font-weight: 600;
		letter-spacing: -0.02em;
	}

	.goal-copy small,
	.goal-copy p {
		color: var(--mf-fg-secondary);
		font-size: 12px;
	}

	.goal-copy small {
		margin-top: 3px;
	}

	.goal-copy p {
		margin: 12px 0 0;
		padding: 0;
	}

	.compression-intent {
		background: var(--mf-bg-panel-2);
		border: 1px solid var(--mf-line-muted);
		border-radius: var(--mf-radius-3);
		display: grid;
		gap: 12px;
		padding: 14px 15px;
	}

	.compression-intent__heading {
		align-items: start;
		display: grid;
		gap: 18px;
		grid-template-columns: minmax(180px, 0.45fr) minmax(0, 1fr);
	}

	.compression-intent__heading > div {
		display: grid;
		gap: 3px;
	}

	.compression-intent__heading span {
		color: var(--mf-fg-tertiary);
		font-size: 10px;
		font-weight: 700;
		letter-spacing: 0.05em;
		text-transform: uppercase;
	}

	.compression-intent__heading strong {
		color: var(--mf-fg-primary);
		font-size: 15px;
	}

	.compression-intent__heading p,
	.compression-intent > small {
		color: var(--mf-fg-secondary);
		font-size: 12px;
		line-height: 1.45;
		margin: 0;
	}

	.compression-intent__options {
		display: grid;
		gap: 7px;
		grid-template-columns: repeat(4, minmax(0, 1fr));
	}

	.compression-intent__options button {
		background: var(--mf-bg-panel);
		border: 1px solid var(--mf-line);
		border-radius: var(--mf-radius-2);
		color: var(--mf-fg-secondary);
		font-size: 12px;
		font-weight: 650;
		min-height: 92px;
		padding: 10px 11px;
		text-align: left;
	}

	.compression-intent__option-copy {
		display: grid;
		gap: 5px;
	}

	.compression-intent__option-copy strong {
		color: inherit;
		font-size: 12px;
		line-height: 1.25;
	}

	.compression-intent__option-copy small {
		color: var(--mf-fg-tertiary);
		font-size: 10px;
		font-weight: 500;
		line-height: 1.35;
	}

	.compression-intent__options button.selected .compression-intent__option-copy small {
		color: var(--mf-fg-secondary);
	}

	.compression-intent__options button:hover {
		background: var(--mf-bg-raised);
		border-color: var(--mf-active-line);
	}

	.compression-intent__options button.selected {
		background: var(--mf-active-bg);
		border-color: var(--mf-active-fg);
		color: var(--mf-active-fg);
	}

	.goal-action {
		align-items: center;
		background: var(--mf-bg-panel-2);
		border: 1px solid var(--mf-line-muted);
		border-radius: var(--mf-radius-3);
		display: flex;
		gap: 20px;
		justify-content: space-between;
		margin: 0;
		max-width: none;
		padding: 14px 15px;
		position: static;
	}

	.safety-note {
		color: var(--mf-fg-secondary);
	}

	.safety-note svg {
		stroke: var(--mf-ready-fg);
	}

	.safety-note p,
	.mobile-safety,
	.host-unavailable {
		color: var(--mf-fg-secondary);
		font-size: 12px;
	}

	.mobile-safety {
		display: none;
	}

	.primary-button,
	.primary-button--light,
	.secondary-button,
	.text-link {
		align-items: center;
		border-radius: var(--mf-radius-2);
		display: inline-flex;
		font-family: var(--mf-font-sans);
		font-size: 14px;
		font-weight: 600;
		justify-content: center;
		min-height: 40px;
		padding: 0 16px;
		text-decoration: none;
		transform: none;
	}

	.primary-button,
	.primary-button--light {
		background: var(--mf-active-solid);
		border: 1px solid var(--mf-active-solid);
		box-shadow: none;
		color: var(--mf-fg-on-accent);
	}

	.primary-button:hover,
	.primary-button--light:hover {
		background: var(--mf-active-solid-hi);
		border-color: var(--mf-active-solid-hi);
		box-shadow: none;
		color: var(--mf-fg-on-accent);
		transform: none;
	}

	.primary-button:disabled,
	.primary-button--light:disabled {
		background: var(--mf-bg-raised);
		border-color: var(--mf-line);
		color: var(--mf-fg-quaternary);
	}

	.secondary-button {
		background: var(--mf-bg-panel);
		border: 1px solid var(--mf-line-strong);
		color: var(--mf-fg-primary);
	}

	.secondary-button:hover {
		background: var(--mf-bg-panel-2);
		border-color: var(--mf-active-line);
		color: var(--mf-active-fg);
	}

	.active-room {
		align-items: stretch;
		display: grid;
		gap: 16px;
		grid-template-columns: minmax(0, 1fr);
		justify-items: stretch;
		min-height: 0;
		text-align: left;
	}

	.active-status {
		align-items: center;
		background: var(--mf-bg-panel-2);
		border: 1px solid var(--mf-line-muted);
		border-radius: var(--mf-radius-2);
		color: var(--mf-fg-tertiary);
		display: flex;
		flex-wrap: wrap;
		font-family: var(--mf-font-mono);
		font-size: 11px;
		gap: 7px 16px;
		padding: 10px 12px;
	}

	.active-status strong {
		color: var(--mf-active-fg);
		font-size: inherit;
	}

	.active-status > * + *::before {
		color: var(--mf-line-strong);
		content: '·';
		margin-right: 16px;
	}

	.active-copy {
		display: grid;
		gap: 9px;
		max-width: none;
	}

	.active-copy .lede {
		margin: 0;
		max-width: 760px;
	}

	.active-facts {
		background: var(--mf-line-muted);
		border: 1px solid var(--mf-line-muted);
		border-radius: var(--mf-radius-3);
		display: grid;
		gap: 1px;
		grid-template-columns: repeat(3, minmax(0, 1fr));
		margin-top: 8px;
		padding: 0;
	}

	.active-facts div {
		background: var(--mf-bg-panel-2);
		display: grid;
		gap: 3px;
		padding: 13px 14px;
	}

	.active-facts span,
	.active-facts strong {
		color: var(--mf-fg-secondary);
		font-family: var(--mf-font-sans);
		font-size: 12px;
	}

	.active-facts strong {
		color: var(--mf-fg-primary);
		font-size: 14px;
	}

	.active-progress {
		background: var(--mf-bg-panel-2);
		border: 1px solid var(--mf-line-muted);
		border-radius: var(--mf-radius-3);
		display: grid;
		gap: 14px;
		padding: 14px;
	}

	.active-progress__header {
		align-items: end;
		display: flex;
		gap: 20px;
		justify-content: space-between;
	}

	.active-progress__header > div {
		display: grid;
		gap: 3px;
	}

	.active-progress__header span,
	.active-progress__facts span {
		color: var(--mf-fg-tertiary);
		font-size: 10px;
		font-weight: 700;
		letter-spacing: 0.05em;
		text-transform: uppercase;
	}

	.active-progress__header h2 {
		font-size: 16px;
		margin: 0;
	}

	.active-progress__header > strong {
		color: var(--mf-fg-secondary);
		font-family: var(--mf-font-mono);
		font-size: 11px;
		font-weight: 500;
	}

	.active-progress__work {
		display: grid;
		gap: 7px;
	}

	.active-progress__work > strong {
		color: var(--mf-fg-secondary);
		font-size: 11px;
		font-weight: 500;
	}

	.active-progress__track {
		background: var(--mf-bg-raised);
		border-radius: 999px;
		height: 6px;
		overflow: hidden;
	}

	.active-progress__track i {
		background: var(--mf-active-solid);
		border-radius: inherit;
		display: block;
		height: 100%;
		min-width: 0;
		transition: width var(--mf-dur-slow) var(--mf-ease);
	}

	.active-progress__facts {
		background: var(--mf-line-muted);
		border: 1px solid var(--mf-line-muted);
		border-radius: var(--mf-radius-2);
		display: grid;
		gap: 1px;
		grid-template-columns: repeat(3, minmax(0, 1fr));
		overflow: hidden;
	}

	.active-progress__facts > div {
		background: var(--mf-bg-panel);
		display: grid;
		gap: 3px;
		min-width: 0;
		padding: 11px 12px;
	}

	.active-progress__facts strong {
		color: var(--mf-fg-primary);
		font-size: 13px;
	}

	.active-progress__facts small {
		color: var(--mf-fg-tertiary);
		font-size: 10px;
		line-height: 1.4;
	}

	.active-progress__facts .telemetry-attention {
		background: var(--mf-wait-bg);
	}

	.step-line {
		background: var(--mf-bg-raised);
		border-radius: 999px;
		display: grid;
		grid-template-columns: repeat(3, 1fr);
		margin: 0;
		max-width: 660px;
		overflow: hidden;
		padding: 3px;
	}

	.step-line span {
		align-items: center;
		border-radius: 999px;
		color: var(--mf-fg-tertiary);
		display: flex;
		font-size: 12px;
		gap: 5px;
		justify-content: center;
		padding: 7px;
	}

	.step-line span.active {
		background: var(--mf-bg-panel);
		color: var(--mf-active-fg);
	}

	.step-line span.done {
		color: var(--mf-ready-fg);
	}

	.active-note {
		color: var(--mf-fg-tertiary);
		font-size: 12px;
	}

	.compare-room {
		display: grid;
		gap: 18px;
		padding: 22px;
	}

	.compare-heading {
		align-items: flex-end;
		display: flex;
		gap: 18px;
		justify-content: space-between;
		margin: 0;
	}

	.compare-heading > div:first-child {
		display: grid;
		gap: 7px;
	}

	.compare-heading p {
		color: var(--mf-fg-secondary);
	}

	.review-contract {
		background: var(--mf-bg-panel-2);
		border: 1px solid var(--mf-line);
		overflow: hidden;
	}

	.review-contract dt {
		color: var(--mf-fg-tertiary);
		font-size: 10px;
		font-weight: 750;
		letter-spacing: 0.055em;
		text-transform: uppercase;
	}

	.review-contract dl {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		margin: 0;
	}

	.review-contract dl > div {
		display: grid;
		gap: 4px;
		min-width: 0;
		padding: 11px 14px;
	}

	.review-contract dl > div + div {
		border-left: 1px solid var(--mf-line-muted);
	}

	.review-contract dd {
		color: var(--mf-fg-primary);
		font-family: var(--mf-font-mono);
		font-size: 15px;
		font-variant-numeric: tabular-nums;
		font-weight: 720;
		line-height: 1.2;
		margin: 0;
		white-space: nowrap;
	}

	.target-warning {
		align-items: center;
		background: var(--mf-fail-bg);
		border: 1px solid var(--mf-fail-line);
		border-radius: var(--mf-radius-3);
		display: grid;
		gap: 18px;
		grid-template-columns: minmax(260px, 0.9fr) minmax(0, 1.1fr);
		padding: 13px 15px;
	}

	.target-warning > div {
		display: grid;
		gap: 3px;
	}

	.target-warning span {
		color: var(--mf-fail-fg);
		font-size: 11px;
		font-weight: 700;
		letter-spacing: 0.04em;
		text-transform: uppercase;
	}

	.target-warning strong {
		color: var(--mf-fg-primary);
		font-size: 15px;
	}

	.target-warning p {
		color: var(--mf-fail-fg);
		font-size: 12px;
		line-height: 1.45;
	}

	.target-warning--under {
		background: var(--mf-wait-bg);
		border-color: var(--mf-wait-line);
	}

	.target-warning--under span,
	.target-warning--under p {
		color: var(--mf-wait-fg);
	}

	.target-warning--acceptable {
		background: var(--mf-ready-bg);
		border-color: var(--mf-ready-line);
	}

	.target-warning--acceptable span,
	.target-warning--acceptable p {
		color: var(--mf-ready-fg);
	}

	.missing-media {
		background: var(--mf-wait-bg);
		border: 1px solid var(--mf-wait-line);
		border-radius: var(--mf-radius-3);
		color: var(--mf-wait-fg);
		padding: 18px;
	}

	.missing-media p {
		color: var(--mf-wait-fg);
		margin-top: 4px;
	}

	.decision {
		align-items: center;
		background: var(--mf-active-bg);
		border: 1px solid #c6ddd7;
		border-radius: var(--mf-radius-3);
		color: var(--mf-fg-primary);
		display: flex;
		gap: 18px;
		justify-content: space-between;
		margin: 0;
		padding: 16px;
	}

	.decision p {
		color: var(--mf-fg-secondary);
		font-size: 12px;
		margin-top: 3px;
	}

	.decision--target-miss {
		background: var(--mf-wait-bg);
		border-color: var(--mf-wait-line);
	}

	.decision--blocked {
		background: var(--mf-fail-bg);
		border-color: var(--mf-fail-line);
	}

	.decision-actions {
		display: flex;
		flex: 0 0 auto;
		gap: 8px;
	}

	.ready-room,
	.finished-room,
	.help-room {
		align-items: flex-start;
		display: grid;
		gap: 11px;
		justify-items: start;
		min-height: 300px;
		place-content: center;
		text-align: left;
	}

	.ready-symbol,
	.finished-rings,
	.help-mark {
		align-items: center;
		background: var(--mf-ready-bg);
		border: 0;
		border-radius: 10px;
		color: var(--mf-ready-fg);
		display: flex;
		height: 42px;
		justify-content: center;
		margin: 0 0 5px;
		position: static;
		width: 42px;
	}

	.ready-symbol::before,
	.ready-symbol::after,
	.finished-rings i {
		display: none;
	}

	.ready-symbol span,
	.finished-rings span,
	.help-mark {
		font-family: var(--mf-font-sans);
		font-size: 20px;
		font-weight: 700;
		position: static;
	}

	.ready-room--check .ready-symbol {
		background: #e7edfb;
		color: #2456c9;
	}

	.ready-room--blocked .ready-symbol {
		background: var(--mf-fail-bg);
		color: var(--mf-fail-fg);
	}

	.encoded-episode-links {
		border-block: 1px solid var(--mf-line-subtle);
		display: grid;
		margin: 8px 0 4px;
		max-width: 560px;
		width: 100%;
	}

	.encoded-episode-links > span {
		color: var(--mf-fg-tertiary);
		font-size: 10px;
		font-weight: 700;
		letter-spacing: 0.08em;
		padding: 10px 0 7px;
		text-transform: uppercase;
	}

	.encoded-episode-links a {
		align-items: center;
		border-top: 1px solid var(--mf-line-subtle);
		color: var(--mf-fg-primary);
		display: flex;
		justify-content: space-between;
		padding: 10px 0;
		text-decoration: none;
	}

	.encoded-episode-links a:hover strong {
		text-decoration: underline;
		text-underline-offset: 3px;
	}

	.encoded-episode-links small {
		color: var(--mf-fg-tertiary);
		font-size: 11px;
	}

	.action-note {
		color: var(--mf-fg-tertiary);
		font-size: 11px;
		margin: -2px 0 0;
	}

	.integrity-loading {
		background: var(--mf-wait-bg);
		border: 1px solid var(--mf-wait-line);
		display: grid;
		gap: 3px;
		max-width: 680px;
		padding: 12px 14px;
		width: 100%;
	}

	.integrity-loading strong {
		color: var(--mf-wait-fg);
		font-size: 12px;
	}

	.integrity-loading span {
		color: var(--mf-fg-secondary);
		font-size: 11px;
	}

	.ready-summary {
		background: var(--mf-bg-panel-2);
		border: 1px solid var(--mf-line-muted);
		border-radius: var(--mf-radius-3);
		display: grid;
		grid-template-columns: repeat(3, minmax(0, 1fr));
		margin: 4px 0;
		max-width: 680px;
		width: 100%;
	}

	.ready-summary div {
		border-right: 1px solid var(--mf-line-muted);
		display: grid;
		gap: 2px;
		padding: 12px 14px;
	}

	.ready-summary div:last-child {
		border-right: 0;
	}

	.ready-summary span {
		color: var(--mf-fg-tertiary);
		font-size: 11px;
	}

	.ready-summary strong {
		color: var(--mf-fg-primary);
		font-family: var(--mf-font-sans);
		font-size: 15px;
	}

	.exact-approved {
		padding: 0;
		overflow: hidden;
	}

	.exact-approved__summary {
		display: grid;
		gap: 14px 24px;
		grid-template-columns: minmax(0, 1fr) minmax(260px, 0.46fr);
		padding: 22px 24px;
	}

	.exact-approved__header {
		display: grid;
		gap: 7px;
		grid-row: 1 / span 2;
		min-width: 0;
	}

	.exact-approved__status {
		align-items: center;
		color: var(--mf-ready-fg);
		display: flex;
		font-size: 12px;
		font-weight: 700;
		gap: 7px;
		margin: 0;
	}

	.exact-approved__status span {
		align-items: center;
		background: var(--mf-ready-bg);
		border: 1px solid var(--mf-ready-line);
		border-radius: 50%;
		display: inline-flex;
		height: 24px;
		justify-content: center;
		width: 24px;
	}

	.exact-approved__header h1 {
		font-size: clamp(21px, 2.4vw, 27px);
		overflow-wrap: anywhere;
	}

	.exact-approved__filename {
		color: var(--mf-fg-tertiary);
		font-family: var(--mf-font-mono), monospace;
		font-size: 11px;
		line-height: 1.45;
		margin: 0;
		overflow-wrap: anywhere;
	}

	.exact-approved__outcome,
	.exact-approved__target {
		background: var(--mf-bg-panel-2);
		border: 1px solid var(--mf-line-muted);
		border-radius: var(--mf-radius-2);
		display: grid;
		gap: 3px;
		padding: 11px 13px;
	}

	.exact-approved__outcome p {
		align-items: baseline;
		color: var(--mf-fg-primary);
		display: flex;
		font-size: 17px;
		gap: 8px;
		margin: 0;
	}

	.exact-approved__outcome p span {
		color: var(--mf-fg-quaternary);
	}

	.exact-approved__outcome > span,
	.exact-approved__target span {
		color: var(--mf-fg-secondary);
		font-size: 11px;
		line-height: 1.4;
	}

	.exact-approved__target strong {
		color: var(--mf-fg-primary);
		font-size: 12px;
	}

	.exact-approved__decision {
		border-top: 1px solid var(--mf-line-muted);
		display: grid;
		gap: 14px;
		padding: 20px 24px 18px;
	}

	.exact-approved__next {
		display: grid;
		gap: 4px;
		max-width: 760px;
	}

	.exact-approved__label {
		color: var(--mf-active-fg);
		font-size: 11px;
		font-weight: 700;
		letter-spacing: 0.04em;
		margin: 0;
		text-transform: uppercase;
	}

	.exact-approved__next h2 {
		font-size: 18px;
		margin: 0;
	}

	.exact-approved__next p:last-child {
		font-size: 13px;
		margin: 0;
	}

	.exact-approved__steps {
		display: grid;
		gap: 1px;
		grid-template-columns: repeat(3, minmax(0, 1fr));
		list-style: none;
		margin: 0;
		overflow: hidden;
		padding: 0;
	}

	.exact-approved__steps li {
		align-items: center;
		background: var(--mf-bg-panel-2);
		border: 1px solid var(--mf-line-muted);
		display: grid;
		gap: 9px;
		grid-template-columns: 24px minmax(0, 1fr);
		min-height: 54px;
		padding: 9px 11px;
	}

	.exact-approved__steps li:first-child {
		border-radius: var(--mf-radius-2) 0 0 var(--mf-radius-2);
	}

	.exact-approved__steps li:last-child {
		border-radius: 0 var(--mf-radius-2) var(--mf-radius-2) 0;
	}

	.exact-approved__steps span {
		align-items: center;
		background: var(--mf-active-bg);
		border-radius: 50%;
		color: var(--mf-active-fg);
		display: flex;
		font-size: 11px;
		font-weight: 700;
		height: 24px;
		justify-content: center;
	}

	.exact-approved__steps strong {
		color: var(--mf-fg-primary);
		font-size: 11px;
		line-height: 1.35;
	}

	.exact-approved__actions {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
	}

	.exact-approved__safety {
		color: var(--mf-fg-tertiary);
		font-size: 11px;
		margin: -4px 0 0;
	}

	.exact-approved__estimate {
		border-top: 1px solid var(--mf-line-muted);
	}

	.exact-approved__estimate summary {
		align-items: center;
		cursor: pointer;
		display: flex;
		gap: 10px;
		justify-content: space-between;
		list-style: none;
		min-height: 44px;
		padding: 0 24px;
	}

	.exact-approved__estimate summary::-webkit-details-marker {
		display: none;
	}

	.exact-approved__estimate summary span {
		color: var(--mf-fg-primary);
		font-size: 12px;
		font-weight: 650;
	}

	.exact-approved__estimate summary small,
	.exact-approved__estimate > div p {
		color: var(--mf-fg-tertiary);
		font-size: 11px;
	}

	.exact-approved__estimate > div {
		border-top: 1px solid var(--mf-line-muted);
		padding: 12px 24px 14px;
	}

	.exact-approved__estimate > div p {
		margin: 0;
		max-width: 740px;
	}

	.season-progress-room {
		align-items: center;
		display: grid;
		gap: 26px;
		grid-template-columns: minmax(0, 1fr) 160px;
		min-height: 0;
	}

	.progress-copy {
		display: grid;
		gap: 9px;
	}

	.progress-facts {
		align-items: baseline;
		display: flex;
		gap: 7px;
		margin-top: 8px;
	}

	.progress-facts strong {
		color: var(--mf-fg-primary);
		font-family: var(--mf-font-sans);
		font-size: 20px;
	}

	.progress-facts span,
	.progress-facts small {
		color: var(--mf-fg-secondary);
		font-size: 13px;
	}

	.progress-ring {
		background: conic-gradient(var(--mf-active-fg) var(--progress), var(--mf-bg-raised) 0);
		box-shadow: none;
		height: 150px;
		width: 150px;
	}

	.progress-ring::before {
		background: var(--mf-bg-panel);
	}

	.progress-ring strong {
		color: var(--mf-fg-primary);
		font-family: var(--mf-font-sans);
		font-size: 28px;
		font-weight: 600;
	}

	.progress-ring span {
		color: var(--mf-fg-tertiary);
		font-size: 11px;
	}

	.progress-track {
		background: var(--mf-bg-raised);
		border-radius: 999px;
		grid-column: 1 / -1;
		height: 7px;
		overflow: hidden;
	}

	.progress-track i {
		background: var(--mf-active-fg);
	}

	.progress-note {
		color: var(--mf-fg-tertiary);
		font-size: 12px;
		grid-column: 1 / -1;
	}

	.finished-actions {
		display: flex;
		gap: 9px;
	}

	.text-link {
		color: var(--mf-active-fg);
		padding-inline: 6px;
	}

	.help-mark {
		background: var(--mf-wait-bg);
		color: var(--mf-wait-fg);
	}

	.help-safety {
		background: var(--mf-bg-panel-2);
		border: 1px solid var(--mf-line-muted);
		border-radius: var(--mf-radius-3);
		display: grid;
		gap: 2px;
		max-width: 650px;
		padding: 13px 14px;
	}

	.help-safety strong {
		color: var(--mf-fg-primary);
	}

	.help-safety span {
		color: var(--mf-fg-secondary);
		font-size: 13px;
	}

	.recovery-count {
		background: var(--mf-wait-bg);
		border: 1px solid var(--mf-wait-line);
		border-radius: var(--mf-radius-3);
		color: var(--mf-wait-fg);
		padding: 10px 13px;
	}

	.recovery-count strong {
		color: var(--mf-wait-fg);
		font-family: var(--mf-font-sans);
		font-size: 20px;
		font-weight: 600;
	}

	.recovery-count span {
		color: var(--mf-wait-fg);
		font-size: 12px;
	}

	.quality-memory {
		background: var(--mf-bg-panel);
		border: 1px solid var(--mf-line);
		border-left: 3px solid var(--mf-ready-fg);
		border-radius: var(--mf-radius-3);
		display: grid;
		gap: 13px;
		margin: 18px 0 0;
		padding: 16px;
	}

	.quality-memory[hidden] {
		display: none;
	}

	.quality-memory--empty {
		border-left-color: var(--mf-idle-fg);
		gap: 8px;
	}

	.quality-memory--attention {
		border-left-color: var(--mf-wait-fg);
	}

	.quality-memory__header {
		align-items: start;
		display: flex;
		gap: 14px;
		justify-content: space-between;
	}

	.quality-memory__header > div {
		display: grid;
		gap: 3px;
	}

	.quality-memory__header span,
	.quality-memory__measured > span,
	.quality-memory__recommendation > span,
	.quality-memory__facts small,
	.quality-memory__recommendation dt {
		color: var(--mf-fg-tertiary);
		font-size: 10px;
		font-weight: 700;
		letter-spacing: 0.08em;
		text-transform: uppercase;
	}

	.quality-memory__header h2 {
		font-size: 15px;
		margin: 0;
	}

	.quality-memory__header small {
		color: var(--mf-fg-tertiary);
		font-size: 11px;
	}

	.quality-memory__badge {
		border-radius: 999px;
		font-size: 11px;
		font-weight: 700;
		padding: 6px 9px;
		white-space: nowrap;
	}

	.quality-memory__badge--quiet {
		background: var(--mf-idle-bg);
		color: var(--mf-idle-fg);
	}

	.quality-memory__badge--ready,
	.quality-memory__badge--success {
		background: var(--mf-ready-bg);
		color: var(--mf-ready-fg);
	}

	.quality-memory__badge--attention {
		background: var(--mf-wait-bg);
		color: var(--mf-wait-fg);
	}

	.quality-memory__comparison {
		border: 1px solid var(--mf-line-muted);
		display: grid;
		grid-template-columns: minmax(0, 1.45fr) minmax(250px, 0.75fr);
	}

	.quality-memory__measured,
	.quality-memory__recommendation {
		display: grid;
		gap: 10px;
		min-width: 0;
		padding: 13px;
	}

	.quality-memory__recommendation {
		background: var(--mf-bg-panel-2);
		border-left: 1px solid var(--mf-line-muted);
	}

	.quality-memory__facts {
		display: grid;
		gap: 12px;
		grid-template-columns: repeat(3, minmax(0, 1fr));
	}

	.quality-memory__facts div {
		display: grid;
		gap: 3px;
		min-width: 0;
	}

	.quality-memory__facts strong,
	.quality-memory__recommendation > strong,
	.quality-memory__recommendation dd {
		color: var(--mf-fg-primary);
		font-family: var(--mf-font-mono), monospace;
	}

	.quality-memory__facts p,
	.quality-memory__recommendation p,
	.quality-memory__reason,
	.quality-memory__policy {
		font-size: 12px;
		margin: 0;
	}

	.quality-memory__recommendation > strong {
		font-size: 22px;
	}

	.quality-memory__recommendation dl {
		display: grid;
		gap: 4px 10px;
		grid-template-columns: max-content minmax(0, 1fr);
		margin: 0;
	}

	.quality-memory__recommendation dd {
		font-size: 11px;
		margin: 0;
		overflow-wrap: anywhere;
	}

	.quality-memory__reason {
		border-left: 2px solid var(--mf-line-strong);
		color: var(--mf-fg-secondary);
		padding-left: 10px;
	}

	.quality-memory__policy {
		color: var(--mf-fg-tertiary);
		font-weight: 600;
	}

	.details-drawer,
	.cinematic .details-drawer {
		background: transparent;
		border: 1px solid var(--mf-line);
		border-radius: var(--mf-radius-3);
		color: var(--mf-fg-primary);
		margin: 14px 0 0;
		max-width: none;
		overflow: hidden;
	}

	.details-drawer summary,
	.cinematic .details-drawer summary {
		align-items: center;
		background: var(--mf-bg-panel);
		color: var(--mf-fg-primary);
		display: flex;
		font-family: var(--mf-font-sans);
		min-height: 48px;
		padding: 0 15px;
	}

	.details-drawer summary small {
		color: var(--mf-fg-tertiary);
		font-size: 12px;
	}

	.details-drawer summary svg {
		stroke: var(--mf-fg-secondary);
	}

	.details-content,
	.cinematic .details-content {
		background: var(--mf-bg-panel-2);
		border-top: 1px solid var(--mf-line-muted);
		color: var(--mf-fg-primary);
		padding: 16px;
	}

	.host-select select {
		background: var(--mf-bg-input);
		border: 1px solid var(--mf-line-strong);
		border-radius: var(--mf-radius-2);
		color: var(--mf-fg-primary);
	}

	.host-select span,
	.host-select small,
	.detail-grid span {
		color: var(--mf-fg-secondary);
	}

	.detail-grid {
		gap: 8px;
	}

	.detail-grid div {
		background: var(--mf-bg-panel);
		border: 1px solid var(--mf-line-muted);
		border-radius: var(--mf-radius-2);
	}

	.detail-grid strong {
		color: var(--mf-fg-primary);
	}

	.detail-download {
		color: var(--mf-active-fg);
	}

	.safety-dialog-backdrop {
		background: var(--mf-bg-overlay);
	}

	.safety-dialog {
		background: var(--mf-bg-panel);
		border: 1px solid var(--mf-line);
		border-radius: var(--mf-radius-3);
		box-shadow: var(--mf-shadow-modal);
		color: var(--mf-fg-primary);
		max-width: 520px;
		padding: 22px;
	}

	.safety-dialog p,
	.safety-dialog li {
		color: var(--mf-fg-secondary);
	}

	.safety-dialog ul {
		background: var(--mf-bg-panel-2);
		border-color: var(--mf-line-muted);
	}

	.safety-dialog li {
		color: var(--mf-fg-primary);
	}

	.goal-contract {
		background: var(--mf-bg-panel-2);
		border: 1px solid var(--mf-line-muted);
		border-radius: var(--mf-radius-3);
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
	}

	.goal-contract > div {
		display: grid;
		gap: 3px;
		padding: 13px 14px;
	}

	.goal-contract > div + div {
		border-left: 1px solid var(--mf-line-muted);
	}

	.goal-contract span,
	.goal-contract small {
		color: var(--mf-fg-secondary);
		font-size: 11px;
	}

	.goal-contract strong {
		color: var(--mf-fg-primary);
		font-size: 15px;
	}

	.goal-contract__truth {
		background: var(--mf-active-bg);
		grid-column: 1 / -1;
	}

	.sr-only {
		height: 1px;
		margin: -1px;
		overflow: hidden;
		padding: 0;
		position: absolute;
		width: 1px;
		clip: rect(0, 0, 0, 0);
		white-space: nowrap;
	}

	.target-provenance-blocker {
		background: var(--mf-fail-bg);
		border: 1px solid var(--mf-fail-line);
		border-radius: var(--mf-radius-3);
		display: grid;
		gap: 4px;
		padding: 12px 14px;
	}

	.target-provenance-blocker strong {
		color: var(--mf-fail-fg);
		font-size: 13px;
	}

	.target-provenance-blocker span {
		color: var(--mf-fg-secondary);
		font-size: 12px;
		line-height: 1.45;
	}

	.optional-instructions {
		border: 1px solid var(--mf-line-muted);
		border-radius: var(--mf-radius-3);
		overflow: hidden;
	}

	.optional-instructions__toggle {
		align-items: center;
		background: var(--mf-bg-panel);
		border: 0;
		color: var(--mf-fg-primary);
		display: flex;
		justify-content: space-between;
		min-height: 52px;
		padding: 9px 14px;
		text-align: left;
		width: 100%;
	}

	.optional-instructions__toggle > span {
		display: grid;
		gap: 2px;
	}

	.optional-instructions__toggle strong {
		font-size: 13px;
	}

	.optional-instructions__toggle small {
		color: var(--mf-fg-tertiary);
		font-size: 11px;
	}

	.optional-instructions__toggle svg {
		fill: none;
		height: 18px;
		stroke: currentColor;
		stroke-width: 1.7;
		transition: transform 140ms ease;
		width: 18px;
	}

	.optional-instructions__toggle[aria-expanded='true'] svg {
		transform: rotate(180deg);
	}

	.operator-instructions,
	.review-feedback-fields label {
		background: var(--mf-bg-panel-2);
		border-top: 1px solid var(--mf-line-muted);
		display: grid;
		gap: 7px;
		padding: 13px 14px;
	}

	.operator-instructions > span,
	.review-feedback-fields label > span {
		color: var(--mf-fg-primary);
		font-size: 12px;
		font-weight: 650;
	}

	.operator-instructions textarea,
	.review-feedback-fields textarea {
		background: var(--mf-bg-input);
		border: 1px solid var(--mf-line-strong);
		border-radius: var(--mf-radius-2);
		color: var(--mf-fg-primary);
		font: inherit;
		line-height: 1.45;
		min-height: 76px;
		padding: 10px 11px;
		resize: vertical;
		width: 100%;
	}

	.operator-instructions small,
	.review-feedback-fields small {
		color: var(--mf-fg-tertiary);
		font-size: 10px;
	}

	.active-facts {
		grid-template-columns: repeat(3, minmax(0, 1fr));
	}

	@media (min-width: 761px) {
		.recovery-plan__computer {
			grid-column: span 2;
		}
	}

	.target-warning--constraint {
		background: var(--mf-fail-bg);
		border-color: var(--mf-fail-line);
	}

	.risk-summary,
	.risk-summary--attention,
	.risk-summary--ready {
		background: var(--mf-bg-panel-2);
		border: 1px solid var(--mf-line-muted);
		border-radius: var(--mf-radius-3);
		color: var(--mf-fg-primary);
		display: grid;
		gap: 12px;
		grid-template-columns: repeat(4, minmax(0, 1fr));
		margin: 0;
		padding: 14px 15px;
	}

	.risk-summary--attention {
		background: var(--mf-fail-bg);
		border-color: var(--mf-fail-line);
	}

	.risk-summary--ready {
		background: var(--mf-ready-bg);
		border-color: var(--mf-ready-line);
	}

	.risk-summary span {
		color: var(--mf-fg-tertiary);
		font-size: 10px;
		margin-bottom: 4px;
	}

	.risk-summary strong {
		color: var(--mf-fg-primary);
		font-size: 14px;
	}

	.risk-summary small {
		color: var(--mf-fg-secondary);
		display: block;
		font-size: 10px;
		line-height: 1.4;
		margin-top: 3px;
	}

	.risk-summary__detail,
	.risk-summary__focus {
		color: var(--mf-fg-secondary) !important;
		grid-column: 1 / -1;
		margin: 0 !important;
	}

	.risk-summary__resolution {
		border-top: 1px solid var(--mf-fail-line);
		grid-column: 1 / -1;
		padding-top: 12px;
	}

	.comparison-ledger {
		border: 1px solid var(--mf-line-muted);
		border-radius: var(--mf-radius-3);
		display: grid;
		grid-template-columns: repeat(4, minmax(0, 1fr));
		overflow: hidden;
	}

	.comparison-ledger > div {
		background: var(--mf-bg-panel-2);
		display: grid;
		gap: 3px;
		padding: 13px 14px;
	}

	.comparison-ledger > div + div {
		border-left: 1px solid var(--mf-line-muted);
	}

	.comparison-ledger span,
	.season-estimate-note span {
		color: var(--mf-fg-tertiary);
		font-size: 10px;
		font-weight: 700;
		letter-spacing: 0.04em;
		text-transform: uppercase;
	}

	.comparison-ledger strong,
	.season-estimate-note strong {
		color: var(--mf-fg-primary);
		font-size: 15px;
	}

	.comparison-ledger small,
	.season-estimate-note small {
		color: var(--mf-fg-secondary);
		font-size: 10px;
		line-height: 1.4;
	}

	.comparison-ledger__missed {
		background: var(--mf-wait-bg) !important;
	}

	.season-estimate-note {
		align-items: center;
		background: var(--mf-bg-panel-2);
		border: 1px solid var(--mf-line-muted);
		border-radius: var(--mf-radius-3);
		display: grid;
		gap: 3px 14px;
		grid-template-columns: auto auto minmax(0, 1fr);
		padding: 10px 14px;
	}

	.review-feedback-panel {
		background: var(--mf-bg-panel-2);
		border: 1px solid var(--mf-line-muted);
		border-radius: var(--mf-radius-3);
		display: grid;
		gap: 13px;
		padding: 15px;
	}

	.review-feedback-panel__heading,
	.review-feedback-panel__action {
		align-items: center;
		display: flex;
		gap: 16px;
		justify-content: space-between;
	}

	.review-feedback-panel__heading > div {
		display: grid;
		gap: 3px;
	}

	.review-feedback-panel__heading > .text-link {
		flex: 0 0 auto;
		min-height: 36px;
	}

	.review-feedback-panel__heading span {
		color: var(--mf-active-fg);
		font-size: 10px;
		font-weight: 700;
		letter-spacing: 0.04em;
		text-transform: uppercase;
	}

	.review-feedback-panel__heading h2,
	.review-feedback-panel__heading p,
	.review-feedback-panel__action p {
		margin: 0;
	}

	.review-feedback-panel__heading p,
	.review-feedback-panel__action p {
		font-size: 11px;
		max-width: 420px;
	}

	.concern-options {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
	}

	.concern-options button {
		background: var(--mf-bg-panel);
		border: 1px solid var(--mf-line-strong);
		border-radius: 999px;
		color: var(--mf-fg-secondary);
		font-size: 11px;
		font-weight: 600;
		min-height: 32px;
		padding: 0 11px;
	}

	.concern-options button.active {
		background: var(--mf-fail-bg);
		border-color: var(--mf-fail-line);
		color: var(--mf-fail-fg);
	}

	.review-feedback-fields {
		display: grid;
		gap: 10px;
		grid-template-columns: repeat(2, minmax(0, 1fr));
	}

	.review-feedback-fields label {
		border: 1px solid var(--mf-line-muted);
		border-radius: var(--mf-radius-2);
	}

	.revision-mode {
		border: 0;
		display: grid;
		gap: 8px;
		margin: 0;
		padding: 0;
	}

	.revision-mode legend {
		color: var(--mf-fg-primary);
		font-size: 12px;
		font-weight: 650;
		margin-bottom: 2px;
		padding: 0;
	}

	.revision-mode > label {
		align-items: start;
		background: var(--mf-bg-panel);
		border: 1px solid var(--mf-line-strong);
		border-radius: var(--mf-radius-2);
		column-gap: 9px;
		cursor: pointer;
		display: grid;
		grid-template-columns: auto minmax(0, 1fr);
		padding: 10px 12px;
	}

	.revision-mode > label.active {
		background: var(--mf-active-bg);
		border-color: var(--mf-active-line);
	}

	.revision-mode > label > input {
		margin-top: 2px;
	}

	.revision-mode > label > span {
		color: var(--mf-fg-primary);
		font-size: 12px;
		font-weight: 650;
	}

	.revision-mode > label > small {
		color: var(--mf-fg-secondary);
		font-size: 11px;
		grid-column: 2;
		line-height: 1.45;
	}

	.revision-mode__option--unavailable {
		cursor: not-allowed;
		opacity: 0.58;
	}

	.revision-mode__fallback {
		color: var(--mf-fg-secondary);
		font-size: 11px;
		margin: 0;
	}

	.review-feedback-panel__action {
		border-top: 1px solid var(--mf-line-muted);
		padding-top: 12px;
	}

	.ready-summary {
		grid-template-columns: repeat(4, minmax(0, 1fr));
		max-width: 820px;
	}

	.ready-summary small {
		color: var(--mf-fg-tertiary);
		font-size: 10px;
	}

	.older-season-option {
		align-items: center;
		background: var(--mf-wait-bg);
		border: 1px solid var(--mf-wait-line);
		border-radius: var(--mf-radius-3);
		display: flex;
		gap: 20px;
		justify-content: space-between;
		margin-bottom: 16px;
		max-width: 820px;
		padding: 16px;
		text-align: left;
		width: 100%;
	}

	.older-season-option > div {
		display: grid;
		gap: 4px;
	}

	.older-season-option strong {
		color: var(--mf-wait-fg);
		font-size: 14px;
	}

	.older-season-option p {
		color: var(--mf-fg-secondary);
		font-size: 12px;
		margin: 0;
	}

	.older-season-option small {
		color: var(--mf-fg-tertiary);
		font-size: 11px;
	}

	.older-season-option .primary-button {
		flex: 0 0 auto;
		white-space: nowrap;
	}

	.detail-error {
		background: var(--mf-wait-bg);
		border: 1px solid var(--mf-wait-line);
		border-radius: var(--mf-radius-2);
		display: grid;
		gap: 3px;
		padding: 11px 12px;
	}

	.detail-error strong,
	.detail-error span {
		color: var(--mf-wait-fg);
		font-size: 12px;
	}

	@keyframes working-bar {
		0% {
			transform: translateX(-105%);
		}
		60%,
		100% {
			transform: translateX(280%);
		}
	}

	@media (max-width: 760px) {
		.experience-header {
			padding: 15px 16px 0;
		}

		.header-season {
			display: block;
			max-width: 58%;
			overflow: hidden;
			text-overflow: ellipsis;
			white-space: nowrap;
		}

		.experience-page main {
			padding: 15px 16px 48px;
		}

		.active-status {
			align-items: start;
			display: grid;
			gap: 4px;
		}

		.active-status > * + *::before {
			display: none;
		}

		.active-progress__header {
			align-items: start;
			flex-direction: column;
			gap: 6px;
		}

		.goal-options {
			grid-template-columns: 1fr;
		}

		.goal-options button {
			min-height: 0;
		}

		.compression-intent__heading {
			gap: 7px;
			grid-template-columns: 1fr;
		}

		.compression-intent__options {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}

		.active-facts,
		.active-progress__facts,
		.scope-activity-facts,
		.recovery-plan,
		.ready-summary {
			grid-template-columns: 1fr;
		}

		.exact-approved__summary {
			grid-template-columns: 1fr;
		}

		.exact-approved__header {
			grid-row: auto;
		}

		.exact-approved__steps {
			grid-template-columns: 1fr;
		}

		.exact-approved__steps li:first-child {
			border-radius: var(--mf-radius-2) var(--mf-radius-2) 0 0;
		}

		.exact-approved__steps li:last-child {
			border-radius: 0 0 var(--mf-radius-2) var(--mf-radius-2);
		}

		.older-season-option {
			align-items: stretch;
			flex-direction: column;
		}

		.quality-memory__header {
			align-items: flex-start;
			flex-direction: column;
		}

		.quality-memory__comparison,
		.quality-memory__facts {
			grid-template-columns: minmax(0, 1fr);
		}

		.quality-memory__recommendation {
			border-left: 0;
			border-top: 1px solid var(--mf-line-muted);
		}

		.active-facts div,
		.active-progress__facts > div,
		.scope-activity-facts div,
		.recovery-plan div,
		.ready-summary div {
			border-right: 0;
		}

		.active-facts div:last-child,
		.active-progress__facts > div:last-child,
		.scope-activity-facts div:last-child,
		.recovery-plan div:last-child,
		.ready-summary div:last-child {
			border-bottom: 0;
		}

		.compare-heading,
		.decision,
		.goal-action {
			align-items: stretch;
			flex-direction: column;
		}

		.review-contract dl {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}

		.review-contract dl > div:nth-child(3) {
			border-left: 0;
			border-top: 1px solid var(--mf-line-muted);
		}

		.review-contract dl > div:nth-child(4) {
			border-top: 1px solid var(--mf-line-muted);
		}

		.review-contract dd {
			white-space: normal;
		}

		.target-warning {
			align-items: start;
			grid-template-columns: 1fr;
		}

		.season-progress-room {
			grid-template-columns: minmax(0, 1fr) 130px;
		}

		.progress-ring {
			height: 120px;
			width: 120px;
		}
	}

	@media (max-width: 520px) {
		.experience-page,
		.experience-page.cinematic {
			min-height: calc(100vh - 62px);
		}

		.experience-header,
		.experience-page main {
			padding-inline: 12px;
		}

		.loading-room,
		.working-room,
		.goal-room,
		.scope-activity-room,
		.active-room,
		.compare-room,
		.ready-room,
		.season-progress-room,
		.finished-room,
		.help-room {
			padding: 18px;
		}

		.episode-selector {
			align-items: stretch;
			gap: 12px;
			grid-template-columns: 1fr;
			padding: 14px;
		}

		.exact-approved__summary,
		.exact-approved__decision {
			padding: 16px;
		}

		.exact-approved__summary {
			gap: 10px;
		}

		.exact-approved__decision {
			gap: 11px;
		}

		.exact-approved__steps li {
			min-height: 42px;
			padding-block: 7px;
		}

		.exact-approved__actions {
			align-items: stretch;
			flex-direction: column;
		}

		.exact-approved__actions .primary-button,
		.exact-approved__actions .secondary-button {
			width: 100%;
		}

		.exact-approved__estimate summary {
			align-items: flex-start;
			flex-direction: column;
			gap: 2px;
			justify-content: center;
			padding: 8px 16px;
		}

		.exact-approved__estimate > div {
			padding-inline: 16px;
		}

		.comparison-ledger,
		.risk-summary,
		.risk-summary--attention,
		.risk-summary--ready {
			grid-template-columns: 1fr;
		}

		.review-feedback-fields,
		.season-estimate-note {
			align-items: start;
			grid-template-columns: 1fr;
		}

		.comparison-ledger > div + div,
		.comparison-ledger > div:nth-child(3),
		.comparison-ledger > div:nth-child(4) {
			border-left: 0;
			border-top: 1px solid var(--mf-line-muted);
		}

		.concern-options button {
			flex: 1 1 calc(50% - 6px);
		}

		.decision-actions,
		.finished-actions,
		.safety-dialog__actions {
			align-items: stretch;
			flex-direction: column-reverse;
			width: 100%;
		}

		.primary-button,
		.primary-button--light,
		.secondary-button,
		.finished-actions .text-link {
			width: 100%;
		}

		.goal-action {
			background: var(--mf-bg-panel-2);
			border: 1px solid var(--mf-line-muted);
			bottom: auto;
			box-shadow: none;
			margin: 0;
			padding: 14px;
			position: static;
		}

		.goal-action .safety-note {
			display: flex;
		}

		.mobile-safety {
			display: block;
			font-size: 10px;
			font-weight: 700;
			letter-spacing: 0.04em;
			text-align: center;
		}

		.season-progress-room {
			grid-template-columns: 1fr;
		}

		.progress-ring {
			grid-row: 2;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.working-orbit span {
			animation: none;
			transform: none;
			width: 58%;
		}
	}
</style>
