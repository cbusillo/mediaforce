<script lang="ts">
	import { resolve } from '$app/paths';
	import { onMount, tick } from 'svelte';

	import { apiDownloadHref, postJson } from '$lib/api/client';
	import type {
		FolderPayload,
		FolderStatusPayload,
		OperatorIntentRequestPayload,
		QualityRiskTag
	} from '$lib/api/types';
	import {
		REVIEW_CONCERNS,
		approvalGuardFromMessage,
		compareRiskSummary,
		currentEncodeProgress,
		currentOperatorIntent,
		detailSeasonState,
		episodeLabel,
		folderSizeTargetAnalysis,
		formatDecimalFileSize,
		goalRequest,
		isSizeGoalSelectionConfirmed,
		isSeriesPrefix,
		measuredFollowupRequest,
		normalizeReviewPairs,
		plainFailureMessage,
		predictedEpisodeSize,
		resolvedTargetSummary,
		reviewFeedbackIntent,
		reviewFeedbackRequest,
		reviewSampleSizes,
		seasonIdentity,
		sizeGoals,
		targetConstraintSummary,
		technicalVideoPolicy,
		testRequestWithInstructions,
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
		kind: 'approval' | 'recovery' | 'lifecycle_override';
		title: string;
		detail: string;
		primaryLabel: string;
		confirmHighImpact?: boolean;
		confirmSizeTradeoff?: boolean;
		changes?: string[];
	};

	let {
		folder,
		status,
		folderPending = false,
		loadError,
		onMutate
	}: {
		folder: FolderPayload;
		status: FolderStatusPayload;
		folderPending?: boolean;
		loadError?: string;
		onMutate: () => Promise<void>;
	} = $props();

	let selectedGoalKey = $state<SizeGoal['key']>('recommended');
	let selectedGoalPrefix = $state('');
	let selectedHostKey = $state('');
	let retryMode = $state(false);
	let selectedMoment = $state(0);
	let audioChoice = $state<'original' | 'new'>('new');
	let actionPhase = $state<ActionPhase>('idle');
	let actionError = $state('');
	let actionMessage = $state('');
	let actionStartedAt = $state(0);
	let clock = $state(Date.now());
	let showInstructions = $state(false);
	let operatorInstructions = $state('');
	let selectedConcerns = $state<QualityRiskTag[]>([]);
	let reviewFeedback = $state('');
	let sourceVideo = $state<HTMLVideoElement | null>(null);
	let previewVideo = $state<HTMLVideoElement | null>(null);
	let goalButtons = $state<HTMLButtonElement[]>([]);
	let safetyDialog = $state<SafetyDialog | null>(null);
	let safetyDialogReturnFocus = $state<HTMLElement | null>(null);

	const identity = $derived(seasonIdentity(folder.prefix));
	const isSeriesScope = $derived(isSeriesPrefix(folder.prefix));
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
	const lifecycleHoldReasons = $derived(currentSeasonLifecycle?.hold_reasons ?? []);
	const lifecycleHold = $derived(lifecycleHoldReasons[0] ?? null);
	const scopeTitle = $derived(
		isSeriesScope ? identity.show : `${identity.show} · ${identity.season}`
	);
	const scopeName = $derived(isSeriesScope ? identity.show : identity.season);
	const scopeNoun = $derived(isSeriesScope ? 'show' : 'season');
	const makeActionLabel = $derived(
		isSeriesScope
			? `Make ${eligibleEpisodeCount} eligible ${eligibleEpisodeCount === 1 ? 'episode' : 'episodes'}`
			: heldEpisodeCount > 0
				? canOverrideLifecycleHolds
					? 'Override hold and make the season'
					: 'Season remains protected'
				: 'Make the season'
	);
	const humanState = $derived(detailSeasonState(folder, status));
	const goals = $derived(sizeGoals(folder));
	const selectedGoal = $derived(goals.find((goal) => goal.key === selectedGoalKey) ?? goals[0]);
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
	const sampleEpisode = $derived(episodeLabel(asText(sampleItem.rel_path)));
	const reviewPairs = $derived(normalizeReviewPairs(folder));
	const currentPair = $derived(reviewPairs[Math.min(selectedMoment, reviewPairs.length - 1)]);
	const calibration = $derived(asRecord(folder.calibration));
	const sampleResult = $derived(asRecord(calibration.sample_result));
	const encodeProgress = $derived(currentEncodeProgress(folder.encode_job));
	const episodeCount = $derived(folder.summary?.item_count ?? 0);
	const productionEpisodeCount = $derived(isSeriesScope ? eligibleEpisodeCount : episodeCount);
	const originalSeasonSize = $derived(folder.summary?.total_size_bytes ?? 0);
	const expectedEpisodeBytes = $derived(predictedEpisodeSize(folder));
	const actualSampleSizes = $derived(reviewSampleSizes(folder));
	const sizeTarget = $derived(folderSizeTargetAnalysis(folder));
	const targetSummary = $derived(resolvedTargetSummary(folder));
	const targetConstraint = $derived(targetConstraintSummary(folder));
	const technicalVideo = $derived(technicalVideoPolicy(folder));
	const expectedSeasonBytes = $derived(expectedEpisodeBytes * productionEpisodeCount);
	const currentTargetBytes = $derived(
		targetSummary?.targetBytes || sizeTarget.budgetBytes || selectedGoal?.targetSizeBytes || 0
	);
	const sizeTargetLabel = $derived(
		currentTargetBytes > 0 ? formatDecimalFileSize(currentTargetBytes) : 'the requested size'
	);
	const sizeTargetMissed = $derived(
		['over_target', 'under_target', 'missing_prediction'].includes(sizeTarget.status)
	);
	const riskSummary = $derived(compareRiskSummary(folder));
	const approvalBlocked = $derived(Boolean(targetConstraint || riskSummary?.blocked));
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
	const activeSampleJob = $derived(asRecord(status.calibration_job ?? folder.calibration_job));
	const finishedEpisodeCount = $derived(
		asNumber(folder.summary?.statuses.encoded) +
			asNumber(folder.summary?.statuses.validated) +
			asNumber(folder.summary?.statuses.promoted)
	);
	const seasonProgressCompleted = $derived(
		Math.max(finishedEpisodeCount, encodeProgress.completed)
	);
	const seasonProgressPercent = $derived(
		Math.max(
			encodeProgress.percent,
			episodeCount > 0 ? (finishedEpisodeCount / episodeCount) * 100 : 0
		)
	);
	const recoveryNeedsAdjustment = $derived(
		humanState.recoveryKind === 'season' && Boolean(folder.encode_job?.progress?.failure_analysis)
	);
	const pageIsCinematic = $derived(
		['making_test', 'ready_to_compare', 'making_season'].includes(humanState.key) && !retryMode
	);
	const actionElapsed = $derived(elapsedCopy(actionStartedAt ? clock - actionStartedAt : 0));
	const backendElapsed = $derived(
		elapsedCopy(clock - parseTimestamp(activeSampleJob.started_at ?? activeSampleJob.created_at))
	);
	const showGoalScreen = $derived(humanState.key === 'needs_test' || retryMode);
	const actionPending = $derived(actionPhase !== 'idle');

	$effect(() => {
		if (selectedMoment >= reviewPairs.length) selectedMoment = 0;
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
			actionError = 'Choose how this legacy size should behave before making a test.';
			return;
		}
		await startTest(
			testRequestWithInstructions(goalRequest(selectedGoal), operatorInstructions),
			selectedGoal.operatorIntent
		);
	}

	async function retryMeasuredTarget() {
		const measuredRequest = measuredFollowupRequest(sizeTarget);
		const baseRequest =
			measuredRequest ||
			`Keep the ${sizeTargetLabel} whole-episode goal and current resolution. Make another representative test that addresses the operator's review concerns without changing the size target.`;
		const note = hasReviewFeedback
			? reviewFeedbackRequest(baseRequest, selectedConcerns, reviewFeedback, operatorInstructions)
			: testRequestWithInstructions(baseRequest, operatorInstructions);
		if (!note) {
			actionError =
				'The previous test did not preserve its requested size. Choose a size and try again.';
			return;
		}
		const operatorIntent = hasReviewFeedback
			? reviewFeedbackIntent(currentOperatorIntent(folder), selectedConcerns, reviewFeedback)
			: currentOperatorIntent(folder);
		await startTest(note, operatorIntent);
	}

	async function submitReviewFeedback() {
		if (!hasReviewFeedback) {
			actionError = 'Choose a concern or describe what should change before making a revised test.';
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
				'We couldn’t prepare the test.'
			);
			const proposal = asRecord(preview.proposal);
			const proposalId = asText(proposal.proposal_id);
			if (!proposalId || proposal.can_queue === false) {
				throw new Error(preview.message || 'The test plan needs attention before it can start.');
			}
			actionPhase = 'starting';
			ensureOk(
				await postJson<ActionResponse>(endpoint('ai-tune/confirm'), {
					proposal_id: proposalId
				}),
				'We prepared the test but couldn’t start it.'
			);
			retryMode = false;
			actionMessage = 'Your test is starting.';
			await onMutate();
			succeeded = true;
		} catch (error) {
			actionError = humanActionError(error, 'We couldn’t start the test.');
		} finally {
			actionPhase = 'idle';
			if (succeeded) await focusCurrentHeading();
		}
	}

	async function retryTest() {
		await runAction('recovering', 'We couldn’t restart the test.', async () => {
			ensureOk(
				await postJson<ActionResponse>(endpoint('ai-tune/confirm'), { proposal_id: '' }),
				'We couldn’t restart the saved test.'
			);
		});
	}

	async function approveTest(confirmHighImpact = false, confirmSizeTradeoff = false) {
		const draftHash = asText(calibration.draft_hash);
		actionError = '';
		actionMessage = '';
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
					primaryLabel: 'Keep this test',
					confirmHighImpact: guard.confirmHighImpact,
					confirmSizeTradeoff: guard.confirmSizeTradeoff,
					changes: guard.kind === 'high_impact' ? approvalChanges() : []
				});
			} else {
				actionError = humanActionError(error, 'We couldn’t accept the test.');
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
					notes: 'Approved after comparing the representative test.',
					bypass_schedule: false,
					override_policy_holds: overridePolicyHolds
				}),
				'We couldn’t start the remaining episodes.'
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

	async function checkOutputs() {
		await runAction('checking', 'We couldn’t check the new episodes.', async () => {
			ensureOk(
				await postJson<ActionResponse>(endpoint('validate-outputs'), {}),
				'We couldn’t check the new episodes.'
			);
		});
	}

	async function finishSeason() {
		await runAction('finishing', `We couldn’t finish the ${scopeNoun}.`, async () => {
			ensureOk(
				await postJson<ActionResponse>(endpoint('promote-outputs'), {}),
				'We couldn’t put the new episodes into your library.'
			);
		});
	}

	async function recoverSeason() {
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
				title: 'The unfinished episodes need a small adjustment.',
				detail:
					'Mediaforce found a measured setting that should let them finish. Those episodes may not match the approved test exactly; episodes already made will not change.',
				primaryLabel: 'Adjust and retry'
			});
			return;
		}
		await runAction('recovering', 'We couldn’t restart the unfinished episodes.', async () => {
			ensureOk(
				await postJson<ActionResponse>(endpoint('queue-encode'), {
					notes: 'Retry unfinished episodes.',
					bypass_schedule: false
				}),
				'We couldn’t retry the unfinished episodes.'
			);
		});
	}

	async function performMeasuredRecovery() {
		await runAction(
			'recovering',
			'We couldn’t adjust and retry the unfinished episodes.',
			async () => {
				ensureOk(
					await postJson<ActionResponse>(endpoint('approve-recovery'), {}),
					'We couldn’t adjust and retry the unfinished episodes.'
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
				title: 'Preparing your test',
				detail: 'Choosing settings for your size goal. This first step can take a few minutes.'
			},
			starting: {
				title: 'Starting your test',
				detail: `Sending one representative episode to ${selectedHost?.label || 'an available computer'}.`
			},
			approving: {
				title: 'Saving your decision',
				detail: `Recording the test you chose and checking whether the ${scopeNoun} can start.`
			},
			queueing: {
				title: isSeriesScope ? 'Starting every season' : 'Starting the season',
				detail: 'Preparing the remaining episodes and finding available computers.'
			},
			checking: {
				title: 'Checking every episode',
				detail: 'Confirming that each new file opens, plays, and matches the original length.'
			},
			finishing: {
				title: isSeriesScope ? 'Putting the show in place' : 'Putting the season in place',
				detail:
					'Moving the originals to the backup area and placing the checked smaller files in your library.'
			},
			recovering: {
				title: 'Trying again',
				detail: 'Keeping completed work and restarting only what still needs attention.'
			}
		};
		return phase === 'idle' ? { title: '', detail: '' } : copy[phase];
	}

	function approvalChanges(): string[] {
		const baselinePolicy = asRecord(asRecord(calibration.sample_item).resolved_policy);
		const baselineVideo = asRecord(baselinePolicy.video);
		const draftVideo = asRecord(asRecord(calibration.policy).video);
		const fields: Array<[string, string, string]> = [
			['quality_metric', 'Picture quality method', 'text'],
			['target_vmaf', 'VMAF target', 'number'],
			['min_target_vmaf', 'VMAF floor', 'number'],
			['target_xpsnr', 'XPSNR target', 'number'],
			['min_target_xpsnr', 'XPSNR floor', 'number'],
			['max_encoded_percent', 'Largest allowed size', 'percent'],
			['default_grain', 'Film grain', 'number']
		];
		return fields.flatMap(([key, label, kind]) => {
			const before = baselineVideo[key];
			const after = draftVideo[key];
			if (JSON.stringify(before ?? null) === JSON.stringify(after ?? null)) return [];
			return [`${label}: ${policyValue(before, kind)} → ${policyValue(after, kind)}`];
		});
	}

	function policyValue(value: unknown, kind: string): string {
		if (value === null || value === undefined || value === '') return 'not set';
		if (kind === 'percent') return `${asNumber(value)}% of the original`;
		if (kind === 'number') return String(asNumber(value));
		return asText(value).toUpperCase() || String(value);
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
			detail: `This saves the tested settings as the ${scopeNoun} profile. Production remains separate until you choose ${makeActionLabel}. It does not change this result to ${sizeTargetLabel} per episode.`,
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

	function selectGoal(key: SizeGoal['key']) {
		selectedGoalKey = key;
		selectedGoalPrefix = folder.prefix;
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

	async function chooseMoment(index: number) {
		selectedMoment = index;
		await tick();
		previewVideo?.pause();
		sourceVideo?.pause();
	}

	function downloadComparison() {
		window.location.assign(apiDownloadHref(endpoint('review-compare/download')));
	}

	function syncPlay() {
		if (!previewVideo || !sourceVideo) return;
		sourceVideo.currentTime = previewVideo.currentTime;
		void sourceVideo.play().catch(() => undefined);
	}

	function syncPause() {
		sourceVideo?.pause();
	}

	function syncSeek() {
		if (!previewVideo || !sourceVideo) return;
		sourceVideo.currentTime = previewVideo.currentTime;
	}

	function keepVideosTogether() {
		if (!previewVideo || !sourceVideo) return;
		if (Math.abs(previewVideo.currentTime - sourceVideo.currentTime) > 0.18) {
			sourceVideo.currentTime = previewVideo.currentTime;
		}
	}

	function technicalPolicy() {
		return technicalVideo;
	}

	function jobStageLabel(value: unknown): string {
		const currentStatus = typeof value === 'string' ? value.toLowerCase() : '';
		if (currentStatus === 'queued') return 'Waiting for a computer';
		if (currentStatus === 'starting') return 'Opening the episode';
		if (currentStatus === 'running') return 'Making test moments';
		if (currentStatus === 'stopping') return 'Stopping safely';
		return 'Preparing the test';
	}
</script>

<svelte:head>
	<title>{scopeTitle} · Mediaforce</title>
</svelte:head>

<div class:cinematic={pageIsCinematic} class="experience-page">
	<div class="ambient" aria-hidden="true"></div>
	<header class="experience-header">
		<a class="back-link" href={resolve('/')}>
			<svg viewBox="0 0 20 20" aria-hidden="true"><path d="m12.5 4.5-5.5 5.5 5.5 5.5" /></svg>
			Library
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
				</div>
			</div>
		{:else if actionMessage}
			<div class="action-notice action-notice--success" role="status">
				<span aria-hidden="true">✓</span>
				<div><strong>{actionMessage}</strong></div>
			</div>
		{/if}

		{#if !folder.pending && heldEpisodeCount > 0}
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
						<p>
							{eligibleEpisodeCount} episodes remain eligible for this show-level action. Protected seasons
							stay visible and original.
						</p>
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
		{:else if showGoalScreen}
			<section class="goal-room">
				<div class="goal-intro">
					<p class="eyebrow">{scopeTitle}</p>
					<h1>
						{retryMode
							? 'Choose a different size'
							: isSeriesScope
								? `Choose one size for all ${seriesSeasonCount} ${seriesSeasonLabel}`
								: `Choose a size for ${identity.season}`}
					</h1>
					<p class="lede">
						{episodeCount} episodes{isSeriesScope
							? ` across ${seriesSeasonCount} ${seriesSeasonLabel}`
							: ''} · {formatDecimalFileSize(originalSeasonSize)} now. You will compare one representative
						test before Mediaforce makes the rest.
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

				{#if selectedGoal}
					<div class="goal-contract" aria-live="polite">
						<div>
							<span>Whole-episode target</span>
							<strong>{formatDecimalFileSize(selectedGoal.targetSizeBytes)}</strong>
						</div>
						<div>
							<span>Representative test band</span>
							<strong
								>{formatDecimalFileSize(selectedGoalSampleLower)}–{formatDecimalFileSize(
									selectedGoalSampleUpper
								)}</strong
							>
							<small
								>±{selectedGoal.operatorIntent.size_goal.sample_projection_tolerance_percent}% while
								testing</small
							>
						</div>
						<div class="goal-contract__truth">
							<strong>Size is the target.</strong>
							<span>Picture and sound decide whether that size is worth keeping.</span>
						</div>
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
								<strong>One test first.</strong> The test creates short review clips. It does not replace
								any episode.
							</p>
						</div>
						{#if noAvailableHosts}
							<p class="host-unavailable" role="status">
								No computers are available right now. Open Details to see what needs attention.
							</p>
						{:else if requiresExplicitGoalSelection && !goalSelectionConfirmed}
							<p class="host-unavailable" role="status">
								Choose whether the saved legacy size scales with runtime or stays fixed per episode.
							</p>
						{/if}
					</div>
					<div class="goal-action__button">
						<span class="mobile-safety">
							{noAvailableHosts
								? 'No computers available · Open Details'
								: requiresExplicitGoalSelection && !goalSelectionConfirmed
									? 'Choose one size behavior first'
									: 'One short test · Nothing is replaced'}
						</span>
						<button
							class="primary-button"
							type="button"
							onclick={makeTest}
							disabled={noAvailableHosts || !selectedGoal || !goalSelectionConfirmed}
						>
							Make a test
							<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 10h11M11 5l5 5-5 5" /></svg>
						</button>
					</div>
				</div>
			</section>
		{:else if humanState.key === 'making_test'}
			<section class="active-room" aria-live="polite">
				<div class="active-copy">
					<p class="eyebrow">Test in progress</p>
					<h1>Making your test</h1>
					<p class="lede">
						{sampleEpisode} is being used to target {sizeTargetLabel} for the whole episode, then check
						whether the picture and sound hold up.
					</p>
					<div class="active-facts">
						<div>
							<span>Whole-episode target</span><strong>{sizeTargetLabel}</strong>
						</div>
						{#if targetSummary}
							<div>
								<span>Representative test band</span><strong
									>{formatDecimalFileSize(
										targetSummary.sampleLowerBoundBytes
									)}–{formatDecimalFileSize(targetSummary.sampleUpperBoundBytes)}</strong
								>
							</div>
						{/if}
						<div>
							<span>Current step</span><strong>{jobStageLabel(activeSampleJob.status)}</strong>
						</div>
						<div>
							<span>Computer</span><strong
								>{asText(asRecord(activeSampleJob.host).label) ||
									selectedHost?.label ||
									'Choosing one'}</strong
							>
						</div>
						<div><span>Time so far</span><strong>{backendElapsed}</strong></div>
					</div>
				</div>
				<div class="test-visual" aria-hidden="true">
					<div class="test-disc"><span>1</span><small>episode</small></div>
					<i class="orbit orbit-one"></i><i class="orbit orbit-two"></i>
				</div>
				<div class="step-line">
					<span class="done"><i>✓</i> Size chosen</span>
					<span class="active"><i></i> Making test moments</span>
					<span><i></i> Compare</span>
				</div>
				<p class="active-note">The test does not replace anything in your library.</p>
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
									: 'Looks good'}
						</p>
						<h1>{targetConstraint ? targetConstraint.title : 'Review picture and sound'}</h1>
						<p>{sampleEpisode} · Compare the same moment on both sides.</p>
					</div>
					{#if reviewPairs.length > 1}
						<div class="moment-picker" role="group" aria-label="Test moments">
							{#each reviewPairs as pair, index (`${pair.source.path}-${index}`)}
								<button
									type="button"
									class:active={selectedMoment === index}
									onclick={() => chooseMoment(index)}
									aria-pressed={selectedMoment === index}
								>
									Moment {index + 1}
								</button>
							{/each}
						</div>
					{/if}
				</div>

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
								>This test estimates {formatDecimalFileSize(expectedEpisodeBytes)} per episode, not
								{formatDecimalFileSize(sizeTarget.budgetBytes)}.</strong
							>
						</div>
						<p>
							{crfLimitReached
								? `The search reached its CRF ${asNumber(sampleResult.chosen_crf)} limit before it reached your size goal.`
								: 'The measured result stayed above your size goal.'}
							Review this as a picture-and-sound checkpoint, then make a smaller test.
						</p>
					</div>
				{:else if sizeTarget.status === 'under_target'}
					<div class="target-warning target-warning--under" role="status">
						<div>
							<span>Requested size</span>
							<strong
								>This test estimates {formatDecimalFileSize(expectedEpisodeBytes)} per episode, below
								your
								{formatDecimalFileSize(sizeTarget.budgetBytes)} goal.</strong
							>
						</div>
						<p>Make another test that spends the unused size on picture and sound quality.</p>
					</div>
				{:else if sizeTarget.status === 'missing_prediction'}
					<div class="target-warning" role="status">
						<div>
							<span>Size estimate missing</span>
							<strong>This test cannot be compared with your requested size yet.</strong>
						</div>
						<p>Make the test again before approving a season-wide result.</p>
					</div>
				{/if}

				{#if currentPair}
					<div
						class="video-stage"
						role="group"
						aria-label="Synchronized original and new video comparison. Use the controls on the new video to play both."
					>
						<div class="video-card">
							<div class="video-label">
								<span>Original</span><small
									>{formatDecimalFileSize(asNumber(sampleItem.source_size_bytes))} episode</small
								>
							</div>
							<video
								bind:this={sourceVideo}
								src={currentPair.source.path}
								muted={audioChoice !== 'original'}
								playsinline
								preload="metadata"
								aria-hidden="true"
								tabindex="-1"
							></video>
						</div>
						<div class="video-card video-card--new">
							<div class="video-label">
								<span>New</span>
								<small
									>{expectedEpisodeBytes
										? `about ${formatDecimalFileSize(expectedEpisodeBytes)} episode`
										: 'test version'}</small
								>
							</div>
							<video
								bind:this={previewVideo}
								src={currentPair.preview.path}
								muted={audioChoice !== 'new'}
								playsinline
								preload="metadata"
								controls
								aria-label="Play the synchronized original and new test videos"
								onplay={syncPlay}
								onpause={syncPause}
								onseeking={syncSeek}
								ontimeupdate={keepVideosTogether}
							></video>
						</div>
					</div>
					<div class="sound-choice" role="group" aria-label="Sound source">
						<span>Listen to</span>
						<button
							type="button"
							class:active={audioChoice === 'original'}
							onclick={() => (audioChoice = 'original')}
							aria-pressed={audioChoice === 'original'}>Original sound</button
						>
						<button
							type="button"
							class:active={audioChoice === 'new'}
							onclick={() => (audioChoice = 'new')}
							aria-pressed={audioChoice === 'new'}>New sound</button
						>
						<small>The right-hand controls play both videos together.</small>
					</div>
				{:else}
					<div class="missing-media">
						<h2>The test finished, but the comparison clips are missing.</h2>
						<p>Nothing was replaced. Make the test again to rebuild the comparison.</p>
					</div>
				{/if}

				{#if riskSummary}
					<div class={`risk-summary risk-summary--${riskSummary.tone}`}>
						<div class="risk-summary__headline">
							<span>Quality risk</span>
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
						<div class="risk-summary__fact">
							<span>Authority</span>
							<strong>{riskSummary.authority}</strong>
							<small>{riskSummary.authorityDetail}</small>
						</div>
						<p class="risk-summary__detail">{riskSummary.detail}</p>
						{#if riskSummary.focusMoments.length}
							<p class="risk-summary__focus">Review focus: {riskSummary.focusMoments[0]}</p>
						{/if}
					</div>
				{/if}

				<div class="comparison-ledger" aria-label="Size comparison facts">
					<div>
						<span>Whole-episode target</span>
						<strong>{sizeTargetLabel}</strong>
						<small
							>{targetSummary?.mode === 'normalized'
								? 'Runtime-normalized goal'
								: 'Per-episode goal'}</small
						>
					</div>
					<div>
						<span>Representative test band</span>
						<strong
							>{targetSummary
								? `${formatDecimalFileSize(targetSummary.sampleLowerBoundBytes)}–${formatDecimalFileSize(targetSummary.sampleUpperBoundBytes)}`
								: 'Not available'}</strong
						>
						<small
							>{targetSummary
								? `±${targetSummary.sampleTolerancePercent}% while testing`
								: 'Waiting for target evidence'}</small
						>
					</div>
					<div class:comparison-ledger__missed={sizeTargetMissed || Boolean(targetConstraint)}>
						<span>Predicted whole episode</span>
						<strong
							>{expectedEpisodeBytes
								? formatDecimalFileSize(expectedEpisodeBytes)
								: 'No usable estimate'}</strong
						>
						<small>
							{sizeTarget.status === 'inside_target_band'
								? 'Inside the representative band'
								: sizeTarget.status === 'over_target'
									? 'Above the representative band'
									: sizeTarget.status === 'under_target'
										? 'Below the representative band'
										: 'Estimate requires another test'}
						</small>
					</div>
					<div>
						<span>Actual review clips</span>
						<strong
							>{actualSampleSizes.original && actualSampleSizes.smaller
								? `${formatDecimalFileSize(actualSampleSizes.original)} → ${formatDecimalFileSize(actualSampleSizes.smaller)}`
								: 'Clip bytes unavailable'}</strong
						>
						<small
							>{actualSampleSizes.durationSeconds
								? `${Math.round(actualSampleSizes.durationSeconds)} seconds sampled · not the episode size`
								: 'Short review media only · not the episode size'}</small
						>
					</div>
				</div>

				<div class="season-estimate-note">
					<span>{isSeriesScope ? 'Estimated eligible output' : 'Estimated season total'}</span>
					<strong
						>{expectedSeasonBytes
							? formatDecimalFileSize(expectedSeasonBytes)
							: 'Still estimating'}</strong
					>
					<small
						>Representative estimate only; each production episode uses its own runtime target.</small
					>
				</div>

				<div class="review-feedback-panel">
					<div class="review-feedback-panel__heading">
						<div>
							<span>Want a revision?</span>
							<h2>Tell Mediaforce what should change.</h2>
						</div>
						<p>The same size target stays in place unless you choose a different goal.</p>
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
							<span>Other priorities for the next test <small>(optional)</small></span>
							<textarea
								bind:value={operatorInstructions}
								maxlength="600"
								rows="3"
								placeholder="Preserve surround audio, intentional grain, subtitle behavior, or another priority."
							></textarea>
							<small>{operatorInstructions.length}/600</small>
						</label>
					</div>
					<div class="review-feedback-panel__action">
						<p>
							Submitting a concern records the current evidence as rejected and starts a revised
							representative test.
						</p>
						<button
							class="secondary-button"
							type="button"
							onclick={submitReviewFeedback}
							disabled={!hasReviewFeedback || noAvailableHosts}
						>
							Make a revised test
						</button>
					</div>
				</div>

				<div
					class="decision"
					class:decision--target-miss={sizeTargetMissed || Boolean(targetConstraint)}
					class:decision--blocked={approvalBlocked}
				>
					<div>
						{#if targetConstraint}
							<h2>{targetConstraint.title}</h2>
							<p>{targetConstraint.detail}</p>
						{:else if riskSummary?.blocked}
							<h2>This test is not safe to approve yet.</h2>
							<p>{riskSummary.detail}</p>
						{:else if sizeTarget.status === 'over_target'}
							<h2>This is a quality checkpoint, not your requested-size result.</h2>
							<p>Compare it now, then make a smaller test that moves toward your goal.</p>
						{:else if sizeTarget.status === 'under_target'}
							<h2>This result is smaller than requested.</h2>
							<p>
								Make another test that uses the available size for more picture and sound quality.
							</p>
						{:else if sizeTarget.status === 'missing_prediction'}
							<h2>The size result is incomplete.</h2>
							<p>Make another test before deciding whether to use this setting for the season.</p>
						{:else}
							<h2>Does the new version look and sound right?</h2>
							<p>Look at faces, motion, dark scenes, and listen for anything distracting.</p>
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
								Run another test after fixing this
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
								Run another {sizeTargetLabel} test
								<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 10h11M11 5l5 5-5 5" /></svg>
							</button>
						{:else}
							<button class="secondary-button" type="button" onclick={chooseDifferentSize}
								>Try a different size</button
							>
							<button
								class="primary-button primary-button--light"
								type="button"
								onclick={() => approveTest()}
								disabled={!currentPair || approvalBlocked}
							>
								Looks good
								<svg viewBox="0 0 20 20" aria-hidden="true"><path d="m4 10 3.5 3.5L16 5" /></svg>
							</button>
						{/if}
					</div>
				</div>
			</section>
		{:else if humanState.key === 'ready_to_make'}
			<section class="ready-room">
				<div class="ready-symbol" aria-hidden="true"><span>✓</span></div>
				<p class="eyebrow">Test approved</p>
				<h1>
					{isSeriesScope
						? 'Ready to make the eligible seasons.'
						: heldEpisodeCount > 0
							? 'This season is ready, but protected.'
							: 'Ready to make the season.'}
				</h1>
				<p class="lede">
					Mediaforce will make {productionEpisodeCount}
					{productionEpisodeCount === 1 ? 'episode' : 'episodes'} with the same settings. {heldEpisodeCount >
					0
						? `${heldEpisodeCount} protected ${heldEpisodeCount === 1 ? 'episode stays' : 'episodes stay'} original unless you explicitly override this season.`
						: 'New files stay separate until they pass their checks.'} Nothing is queued until you choose
					the action below.
				</p>
				<div class="ready-summary">
					<div>
						<span>{isSeriesScope ? 'Eligible episodes' : 'Episodes'}</span><strong
							>{productionEpisodeCount}</strong
						>
					</div>
					<div><span>Approved episode target</span><strong>{sizeTargetLabel}</strong></div>
					<div>
						<span>{isSeriesScope ? 'Current scope size' : 'Current size'}</span><strong
							>{formatDecimalFileSize(originalSeasonSize)}</strong
						>
					</div>
					<div>
						<span>{isSeriesScope ? 'Estimated eligible output' : 'Estimated season total'}</span
						><strong
							>{expectedSeasonBytes
								? formatDecimalFileSize(expectedSeasonBytes)
								: 'Varies by episode'}</strong
						>
						<small>Each episode gets its own runtime-derived target.</small>
					</div>
				</div>
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
			</section>
		{:else if humanState.key === 'making_season'}
			<section class="season-progress-room" aria-live="polite">
				<div class="progress-copy">
					<p class="eyebrow">In progress</p>
					<h1>
						{isSeriesScope
							? `Making all ${seriesSeasonCount} ${seriesSeasonLabel}`
							: `Making ${identity.season}`}
					</h1>
					<p class="lede">
						{encodeProgress.currentEpisode === 'A representative episode'
							? `The ${scopeNoun} is waiting for its next available computer.`
							: `${encodeProgress.currentEpisode} is being made now.`}
					</p>
					<div class="progress-facts">
						<strong>{seasonProgressCompleted} of {episodeCount || encodeProgress.total}</strong>
						<span>episodes finished</span>
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
					Completed episodes are kept. If a computer disconnects, unfinished work can be retried.
				</p>
			</section>
		{:else if humanState.key === 'ready_to_check'}
			<section class="ready-room ready-room--check">
				<div class="ready-symbol" aria-hidden="true"><span>···</span></div>
				<p class="eyebrow">Episodes made</p>
				<h1>Let’s check every new file.</h1>
				<p class="lede">
					Before anything changes in your library, Mediaforce checks that each episode opens, plays,
					and has the expected length.
				</p>
				<button class="primary-button" type="button" onclick={checkOutputs}>
					Check the new episodes
					<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 10h11M11 5l5 5-5 5" /></svg>
				</button>
			</section>
		{:else if humanState.key === 'ready_to_finish'}
			<section class="ready-room ready-room--finish">
				<div class="ready-symbol" aria-hidden="true"><span>✓</span></div>
				<p class="eyebrow">Every check passed</p>
				<h1>Ready to finish.</h1>
				<p class="lede">
					The checked smaller episodes will take their place in your library. The current originals
					move to the backup area so they can be recovered later.
				</p>
				<button class="primary-button" type="button" onclick={finishSeason}>
					{isSeriesScope ? 'Finish the show' : 'Finish the season'}
					<svg viewBox="0 0 20 20" aria-hidden="true"><path d="m4 10 3.5 3.5L16 5" /></svg>
				</button>
			</section>
		{:else if humanState.key === 'finished'}
			<section class="finished-room">
				<div class="finished-rings" aria-hidden="true"><i></i><i></i><span>✓</span></div>
				<p class="eyebrow">All finished</p>
				<h1>{scopeName} is ready.</h1>
				<p class="lede">
					All {episodeCount} smaller episodes are in your library. The originals remain in the backup
					area until you choose to remove them.
				</p>
				<div class="finished-actions">
					<a class="primary-button" href={resolve('/')}>Choose another show or season</a>
					<a class="text-link" href={resolve('/completed')}>See finished seasons</a>
				</div>
			</section>
		{:else if humanState.key === 'needs_help'}
			<section class="help-room">
				<div class="help-mark" aria-hidden="true">!</div>
				<p class="eyebrow">{targetConstraint ? 'Size needs a change' : 'A small snag'}</p>
				<h1>{targetConstraint?.title || `${humanState.label}.`}</h1>
				<p class="lede">{targetConstraint?.detail || plainFailureMessage(folder, status)}</p>
				<div class="help-safety">
					{#if targetConstraint}
						<strong>No quality rule was silently relaxed.</strong>
						<span>Choose a viable goal, then Mediaforce can make a fresh representative test.</span>
					{:else if humanState.recoveryKind === 'test'}
						<strong>Your library is safe.</strong>
						<span>Nothing was replaced. Trying again rebuilds the comparison.</span>
					{:else}
						<strong>Your completed work is safe.</strong>
						<span>Retrying keeps finished episodes and starts only what still needs attention.</span
						>
					{/if}
				</div>
				{#if humanState.recoveryKind === 'season' && finishedEpisodeCount > 0}
					<div class="recovery-count">
						<strong>{finishedEpisodeCount} of {episodeCount}</strong>
						<span>episodes already made</span>
					</div>
				{/if}
				<button
					class="primary-button"
					type="button"
					onclick={() => (targetConstraint ? chooseDifferentSize() : recoverSeason())}
				>
					{targetConstraint?.recoveryLabel ||
						(recoveryNeedsAdjustment
							? 'Review retry'
							: humanState.recoveryKind === 'test'
								? 'Retry the sample'
								: 'Retry unfinished episodes')}
				</button>
			</section>
		{/if}

		{#if safetyDialog}
			<div class="safety-dialog-backdrop">
				<div
					class="safety-dialog"
					role="alertdialog"
					aria-modal="true"
					aria-labelledby="safety-dialog-title"
					aria-describedby="safety-dialog-detail"
					tabindex="-1"
					onkeydown={handleSafetyDialogKeydown}
				>
					<p class={safetyDialog.confirmSizeTradeoff ? 'eyebrow eyebrow--missed' : 'eyebrow'}>
						Before you continue
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

		<details class="details-drawer">
			<summary>
				<span>Details</span>
				<small>For computers, formats, and exact settings</small>
				<svg viewBox="0 0 20 20" aria-hidden="true"><path d="m5 7.5 5 5 5-5" /></svg>
			</summary>
			<div class="details-content">
				{#if showGoalScreen && hostOptions.length}
					<label class="host-select">
						<span>Computer for the test</span>
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
								'Available for this test.'}</small
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
				{#if reviewPairs.length}
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
	summary:focus-visible,
	video:focus-visible {
		outline: 3px solid #4b8060;
		outline-offset: 3px;
	}

	.cinematic button:focus-visible,
	.cinematic a:focus-visible,
	.cinematic summary:focus-visible,
	.cinematic video:focus-visible {
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

	.test-visual {
		align-items: center;
		aspect-ratio: 1;
		display: flex;
		justify-content: center;
		max-width: 430px;
		position: relative;
	}

	.test-disc {
		align-items: center;
		background: radial-gradient(circle at 38% 30%, #688775, #26372e 70%);
		border: 1px solid rgb(255 255 255 / 13%);
		border-radius: 50%;
		box-shadow: 0 35px 100px rgb(30 80 55 / 28%);
		display: flex;
		flex-direction: column;
		height: 210px;
		justify-content: center;
		position: relative;
		width: 210px;
		z-index: 2;
	}

	.test-disc span {
		font-family: 'Iowan Old Style', Georgia, serif;
		font-size: 82px;
		font-weight: 400;
		letter-spacing: -0.08em;
		line-height: 0.85;
	}

	.test-disc small {
		color: #b9c6bd;
		font-size: 10px;
		font-weight: 750;
		letter-spacing: 0.12em;
		margin-top: 14px;
		text-transform: uppercase;
	}

	.orbit {
		animation: orbit 10s linear infinite;
		border: 1px solid rgb(141 183 158 / 24%);
		border-radius: 50%;
		inset: 28px;
		position: absolute;
	}

	.orbit::after {
		background: #91c5a5;
		border-radius: 50%;
		box-shadow: 0 0 18px #75a98b;
		content: '';
		height: 8px;
		left: 50%;
		position: absolute;
		top: -4px;
		width: 8px;
	}

	.orbit-two {
		animation-direction: reverse;
		animation-duration: 14s;
		inset: 0;
		opacity: 0.45;
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

	.moment-picker {
		background: rgb(255 255 255 / 5%);
		border: 1px solid var(--line);
		border-radius: 999px;
		display: flex;
		padding: 4px;
	}

	.moment-picker button,
	.sound-choice button {
		background: transparent;
		border: 0;
		border-radius: 999px;
		color: var(--muted);
		cursor: pointer;
		font: inherit;
		font-size: 11px;
		font-weight: 700;
		padding: 8px 12px;
	}

	.moment-picker button.active,
	.sound-choice button.active {
		background: #e8eee8;
		color: #1d241f;
	}

	.video-stage {
		display: grid;
		gap: 13px;
		grid-template-columns: repeat(2, minmax(0, 1fr));
	}

	.video-card {
		background: #070908;
		border: 1px solid rgb(255 255 255 / 10%);
		border-radius: 18px;
		overflow: hidden;
	}

	.video-card--new {
		border-color: rgb(137 190 157 / 48%);
		box-shadow:
			0 0 0 1px rgb(137 190 157 / 10%),
			0 28px 80px rgb(0 0 0 / 32%);
	}

	.video-label {
		align-items: center;
		color: #efede7;
		display: flex;
		justify-content: space-between;
		min-height: 48px;
		padding: 0 16px;
	}

	.video-label span {
		font-size: 11px;
		font-weight: 800;
		letter-spacing: 0.12em;
		text-transform: uppercase;
	}

	.video-label small {
		color: #929792;
		font-size: 11px;
	}

	.video-card video {
		aspect-ratio: 16 / 9;
		background: #000;
		display: block;
		object-fit: contain;
		width: 100%;
	}

	.sound-choice {
		align-items: center;
		color: var(--muted);
		display: flex;
		font-size: 11px;
		gap: 5px;
		justify-content: center;
		margin-top: 14px;
	}

	.sound-choice > span {
		font-weight: 700;
		margin-right: 3px;
	}

	.sound-choice small {
		margin-left: 8px;
		opacity: 0.7;
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

		.test-visual,
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

		.test-visual {
			max-width: 320px;
			width: 100%;
		}

		.test-disc {
			height: 170px;
			width: 170px;
		}

		.step-line span {
			font-size: 9px;
			gap: 4px;
		}

		.compare-heading h1 {
			font-size: 46px;
		}

		.moment-picker {
			overflow-x: auto;
			width: 100%;
		}

		.moment-picker button {
			flex: 1 0 auto;
		}

		.sound-choice {
			align-items: stretch;
			flex-wrap: wrap;
		}

		.sound-choice > span {
			flex-basis: 100%;
			text-align: center;
		}

		.sound-choice button {
			flex: 1;
		}

		.sound-choice small {
			flex-basis: 100%;
			margin: 5px 0 0;
			text-align: center;
		}

		.video-stage {
			gap: 6px;
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}

		.video-label {
			min-height: 38px;
			padding-inline: 8px;
		}

		.video-label small {
			display: none;
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
		.orbit,
		.step-line .active i {
			animation: none;
		}

		.experience-page,
		.goal-options button,
		.primary-button,
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

	.action-notice p {
		color: inherit;
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

	.loading-room,
	.working-room,
	.goal-room,
	.active-room,
	.compare-room,
	.ready-room,
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
		align-items: center;
		display: grid;
		gap: 22px;
		grid-template-columns: minmax(0, 1fr);
		justify-items: stretch;
		min-height: 0;
		text-align: left;
	}

	.active-copy {
		display: grid;
		gap: 9px;
		max-width: 720px;
	}

	.active-facts {
		background: var(--mf-bg-panel-2);
		border: 1px solid var(--mf-line-muted);
		border-radius: var(--mf-radius-3);
		display: grid;
		gap: 0;
		grid-template-columns: repeat(3, minmax(0, 1fr));
		margin-top: 8px;
		padding: 0;
	}

	.active-facts div {
		border-right: 1px solid var(--mf-line-muted);
		display: grid;
		gap: 3px;
		padding: 13px 14px;
	}

	.active-facts div:last-child {
		border-right: 0;
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

	.test-visual {
		display: none;
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

	.moment-picker {
		background: var(--mf-bg-raised);
		border: 1px solid var(--mf-line);
		border-radius: 999px;
		gap: 2px;
		padding: 3px;
	}

	.moment-picker button {
		border-radius: 999px;
		color: var(--mf-fg-secondary);
		font-family: var(--mf-font-sans);
		font-size: 12px;
		font-weight: 600;
		min-height: 32px;
		padding: 0 12px;
	}

	.moment-picker button.active {
		background: var(--mf-bg-panel);
		box-shadow: 0 1px 2px rgb(30 34 39 / 10%);
		color: var(--mf-active-fg);
	}

	.video-stage {
		background: var(--mf-bg-stage);
		border: 0;
		border-radius: 12px;
		box-shadow: 0 8px 28px rgb(20 22 25 / 16%);
		display: grid;
		gap: 10px;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		margin: 0;
		overflow: hidden;
		padding: 10px;
	}

	.video-card,
	.video-card--new {
		background: #090a0c;
		border: 1px solid rgb(255 255 255 / 12%);
		border-radius: 8px;
		overflow: hidden;
	}

	.video-card--new {
		border-color: rgb(120 208 190 / 62%);
	}

	.video-label {
		background: #111316;
		color: #f4f5f2;
		font-family: var(--mf-font-sans);
		min-height: 38px;
		padding: 0 12px;
	}

	.video-label span {
		font-size: 11px;
		letter-spacing: 0.08em;
	}

	.video-label small {
		color: #a8ada8;
		font-size: 11px;
	}

	.video-card video {
		aspect-ratio: 16 / 9;
		display: block;
		width: 100%;
	}

	.sound-choice {
		align-items: center;
		color: var(--mf-fg-secondary);
		display: flex;
		font-family: var(--mf-font-sans);
		font-size: 12px;
		gap: 4px;
		justify-content: center;
		margin: -7px 0 0;
	}

	.sound-choice button {
		background: transparent;
		border: 1px solid transparent;
		border-radius: 999px;
		color: var(--mf-fg-secondary);
		font-size: 12px;
		font-weight: 600;
		min-height: 32px;
		padding: 0 11px;
	}

	.sound-choice button.active {
		background: var(--mf-active-bg);
		border-color: #c6ddd7;
		color: var(--mf-active-fg);
	}

	.sound-choice small {
		color: var(--mf-fg-tertiary);
		font-size: 11px;
		margin-left: 7px;
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
		grid-template-columns: repeat(3, minmax(0, 1fr));
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
		grid-template-columns: repeat(5, minmax(0, 1fr));
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

		.goal-options {
			grid-template-columns: 1fr;
		}

		.goal-options button {
			min-height: 0;
		}

		.active-facts,
		.ready-summary {
			grid-template-columns: 1fr;
		}

		.active-facts div,
		.ready-summary div {
			border-bottom: 1px solid var(--mf-line-muted);
			border-right: 0;
		}

		.active-facts div:last-child,
		.ready-summary div:last-child {
			border-bottom: 0;
		}

		.compare-heading,
		.decision,
		.goal-action {
			align-items: stretch;
			flex-direction: column;
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
		.active-room,
		.compare-room,
		.ready-room,
		.season-progress-room,
		.finished-room,
		.help-room {
			padding: 18px;
		}

		.video-stage {
			grid-template-columns: 1fr;
			padding: 7px;
		}

		.sound-choice {
			align-items: stretch;
			flex-wrap: wrap;
		}

		.sound-choice > span {
			flex-basis: 100%;
			text-align: center;
		}

		.sound-choice button {
			flex: 1;
		}

		.sound-choice small {
			flex-basis: 100%;
			margin: 4px 0 0;
			text-align: center;
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
			display: none;
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
