<script lang="ts">
	import { resolve } from '$app/paths';
	import { apiDownloadHref, postJson } from '$lib/api/client';
	import { folderRoutePath, folderRoutePrefix } from '$lib/folder-display';
	import {
		formatDateTimeCopy,
		pathFilename,
		type FolderCalibrationJob,
		type FolderCalibrationState,
		type FolderSampleItem,
		type PendingSampleProposal,
		type ReviewGate
	} from '$lib/folders/studio';
	import type {
		FolderBenchConfirmResponse,
		FolderBenchPreviewResponse,
		FolderPayload,
		FolderStatusPayload,
		HostsPayload
	} from '$lib/api/types';
	import OperatorShell from './OperatorShell.svelte';
	import StateBadge from './StateBadge.svelte';
	import WorkstationPanel from './WorkstationPanel.svelte';
	import {
		buildBenchMessages,
		buildBenchHostOptions,
		buildProcessingHostOptions,
		buildSeasonScopeRows,
		buildWorkflowSteps,
		buildFooterSignals,
		buildProposalRows,
		buildReviewWorkspaceView,
		buildDecisionFacts,
		buildBasicEncodeGuide,
		buildRuntimeFacts,
		buildSampleFacts,
		buildSampleVerdict,
		buildStatusTiles,
		formatBytes,
		predictedFolderSizeBytes,
		projectedReclaimBytes,
		record,
		REVIEW_ASSISTANT_PENDING_COPY,
		resolveBenchRequestState,
		resolvedMetricCopy,
		resolveFolderTitle,
		resolveReviewArtifacts,
		resolveQueueSubmissionMode,
		resolveWorkflowActionState,
		resolveWorkflow,
		outputScopeDisplayLabel,
		outputScopeLabel,
		summarizeOutputWorkflowPending,
		summarizeStatuses,
		type WorkflowAction
	} from './folder-studio-view';

	let {
		folder,
		status,
		hosts,
		folderPending = false,
		onMutate = async () => {},
		loadError = null
	}: {
		folder: FolderPayload;
		status: FolderStatusPayload;
		hosts: HostsPayload;
		folderPending?: boolean;
		onMutate?: () => Promise<void>;
		loadError?: string | null;
	} = $props();

	let benchNote = $state('');
	let selectedHostKey = $state('');
	let benchPending = $state(false);
	let workflowPending = $state<WorkflowAction | null>(null);
	let benchMessage = $state('');
	let benchError = $state('');
	let profileMessage = $state('');
	let profileError = $state('');
	let benchTextarea = $state<HTMLTextAreaElement | null>(null);
	let localPendingProposal = $state<Record<string, unknown> | null | undefined>(undefined);
	let localProposalPrefix = $state('');
	const studioFolder = $derived({
		...folder,
		pending_proposal:
			localPendingProposal === undefined || localProposalPrefix !== folder.prefix
				? folder.pending_proposal
				: localPendingProposal
	});
	const prefix = $derived(studioFolder.prefix);
	const encodedPrefix = $derived(folderRoutePrefix(prefix));
	const reviewDownloadHref = $derived(
		apiDownloadHref(`/api/folders/${encodedPrefix}/review-compare/download`)
	);
	const seriesContext = $derived(studioFolder.series_context ?? null);
	const seriesRoute = $derived(seriesContext ? folderRoutePath(seriesContext.prefix) : '/folders');
	const sampleHostOptions = $derived(buildBenchHostOptions(studioFolder.sample_host_options));
	const processingHostOptions = $derived(buildProcessingHostOptions(hosts));
	const folderSampleHostKey = $derived(String(folder.sample_host_key ?? '').trim());
	const fallbackHostKey = $derived(folderSampleHostKey || sampleHostOptions[0]?.key || '');

	$effect(() => {
		if (!selectedHostKey || !sampleHostOptions.some((host) => host.key === selectedHostKey)) {
			selectedHostKey = fallbackHostKey;
		}
		benchMessage = '';
		benchError = '';
	});
	const summary = $derived(studioFolder.summary);
	const title = $derived(resolveFolderTitle(prefix));
	const library = $derived(prefix.split('/')[0] || 'Library');
	const seasonScopeRows = $derived(buildSeasonScopeRows(studioFolder));
	const activeScopeLabel = $derived(outputScopeLabel(studioFolder));
	const scopePathLabel = $derived(
		activeScopeLabel === 'whole show'
			? 'Show path'
			: activeScopeLabel === 'season'
				? 'Season path'
				: 'Folder path'
	);
	const sampleItem = $derived(record<FolderSampleItem>(studioFolder.sample_item));
	const calibration = $derived(record<FolderCalibrationState>(studioFolder.calibration));
	const pendingProposal = $derived(record<PendingSampleProposal>(studioFolder.pending_proposal));
	const reviewGate = $derived(record<ReviewGate>(studioFolder.review_gate));
	const calibrationJob = $derived(record<FolderCalibrationJob>(studioFolder.calibration_job));
	const retryableSampleJob = $derived(record<FolderCalibrationJob>(status.retryable_sample_job));
	const encodeJob = $derived(studioFolder.encode_job ?? null);
	const reviewArtifacts = $derived(resolveReviewArtifacts(calibration, pendingProposal));
	const reviewPackReady = $derived(
		Boolean(
			reviewArtifacts.length > 0 ||
			calibration?.browser_review_ready ||
			calibration?.review_media_ready
		)
	);
	const approvalReviewReady = $derived(Boolean(calibration?.review_media_ready));
	const approvedProfileReady = $derived(reviewGate?.status === 'accepted');
	const approvedSeasonShortcut = $derived(studioFolder.approved_season_shortcut ?? null);
	const approvedSeasonNote = $derived(
		typeof approvedSeasonShortcut?.suggested_note === 'string'
			? approvedSeasonShortcut.suggested_note.trim()
			: ''
	);
	const workflow = $derived(
		resolveWorkflow(
			studioFolder,
			status,
			calibration,
			pendingProposal,
			reviewGate,
			calibrationJob,
			encodeJob,
			reviewPackReady,
			approvalReviewReady,
			folderPending
		)
	);
	const workflowSteps = $derived(buildWorkflowSteps(workflow, activeScopeLabel));
	const basicEncodeGuide = $derived(buildBasicEncodeGuide(workflow, activeScopeLabel));
	const activeSampleRunning = $derived(workflow.primaryAction === 'monitor-sample');
	const activeSampleHostLabel = $derived(String(calibrationJob?.host?.label ?? '').trim());
	const proposalRows = $derived(buildProposalRows(studioFolder, pendingProposal));
	const statusTiles = $derived(buildStatusTiles(studioFolder, status, hosts, workflow));
	const footerSignals = $derived(buildFooterSignals(studioFolder, status, hosts, workflow));
	const runtimeFacts = $derived(
		buildRuntimeFacts(studioFolder, status, reviewGate, encodeJob, workflow)
	);
	const sampleFacts = $derived(buildSampleFacts(sampleItem, summary));
	const sampleVerdict = $derived(buildSampleVerdict(studioFolder, calibration));
	const reviewWorkspace = $derived(
		buildReviewWorkspaceView(studioFolder, calibration, pendingProposal, workflow, reviewPackReady)
	);
	const reviewWorkspaceTone = $derived(
		reviewWorkspace.badgeTone ?? (reviewPackReady ? 'ready' : 'idle')
	);
	const outputReviewRows = $derived(reviewWorkspace.rows);
	const decisionFacts = $derived(
		buildDecisionFacts(studioFolder, calibration, pendingProposal, workflow)
	);
	const sampleResultRow = $derived(
		outputReviewRows.find((row) => row.label === 'Measured sample') ?? null
	);
	const draftReviewRow = $derived(
		outputReviewRows.find(
			(row) =>
				row.label === 'Next sample plan' ||
				row.label === 'Next sample draft' ||
				row.label === 'Video output'
		) ?? null
	);
	const guideActionState = $derived(workflowActionState(basicEncodeGuide.action));
	const visualReviewArtifacts = $derived(
		[...reviewArtifacts.filter((artifact) => artifact.category === 'visual')]
			.sort((left, right) => reviewArtifactPriority(left.kind) - reviewArtifactPriority(right.kind))
			.slice(0, 6)
	);
	const audioReviewArtifacts = $derived(
		reviewArtifacts.filter((artifact) => artifact.category === 'audio').slice(0, 2)
	);
	const benchMessages = $derived(
		buildBenchMessages(
			calibration,
			pendingProposal,
			retryableSampleJob,
			studioFolder.recent_tuning_sessions?.[0]
		)
	);
	const trimmedBenchNote = $derived(benchNote.trim());
	const benchRequestState = $derived(
		resolveBenchRequestState(
			benchNote,
			selectedHostKey,
			sampleHostOptions,
			calibrationJob,
			benchPending
		)
	);
	const benchRequestDisabled = $derived(benchRequestState.disabled);
	function workflowActionState(action: WorkflowAction) {
		return resolveWorkflowActionState(action, {
			reviewPackReady,
			approvalReviewReady,
			approvedProfileReady,
			pendingProposal,
			calibrationJob,
			pendingAction: workflowPending
		});
	}

	function reviewArtifactPriority(kind: string) {
		if (/contact_sheet/i.test(kind)) return 0;
		if (/timeline/i.test(kind)) return 1;
		return 2;
	}

	function mediaAssetHref(url: string) {
		return url;
	}

	function openReviewMedia(url: string) {
		window.open(mediaAssetHref(url), '_blank', 'noreferrer');
	}

	function focusBenchComposer() {
		benchTextarea?.focus();
		benchTextarea?.scrollIntoView({ block: 'center' });
	}

	function reviseSmaller() {
		const revisionPrompt = workflow.revisionPrompt?.trim();
		if (revisionPrompt) {
			const currentNote = benchNote.trim();
			benchNote = currentNote ? `${currentNote}\n\n${revisionPrompt}` : revisionPrompt;
		}
		focusBenchComposer();
	}

	function fillDraftFromApprovedPolicy() {
		if (!approvedSeasonNote) return;
		benchNote = approvedSeasonNote;
		focusBenchComposer();
	}

	async function sendBenchRequest() {
		if (benchRequestDisabled) return;
		benchPending = true;
		benchMessage = '';
		benchError = '';
		try {
			const response = await postJson<FolderBenchPreviewResponse>(
				`${resolve('/')}api/folders/${encodedPrefix}/ai-tune/preview`,
				{ note: trimmedBenchNote, host_key: selectedHostKey },
				fetch
			);
			localPendingProposal = response.proposal ?? studioFolder.pending_proposal ?? null;
			localProposalPrefix = prefix;
			benchMessage =
				response.message || 'Sample plan ready. Nothing is queued until you confirm it.';
			benchNote = '';
		} catch (error) {
			benchError = error instanceof Error ? error.message : 'Review request failed.';
		} finally {
			benchPending = false;
		}
	}

	async function confirmBenchProposal(action: WorkflowAction) {
		if (!['start-sample', 'retry-sample'].includes(action)) return;
		const actionState = workflowActionState(action);
		if (actionState.disabled) return;
		workflowPending = action;
		benchMessage = '';
		benchError = '';
		try {
			const response = await postJson<FolderBenchConfirmResponse>(
				`${resolve('/')}api/folders/${encodedPrefix}/ai-tune/confirm`,
				{ proposal_id: pendingProposal?.proposal_id ?? '' }
			);
			localPendingProposal = null;
			localProposalPrefix = prefix;
			benchMessage = response.message || 'Queued the sample run from the plan.';
			await onMutate();
		} catch (error) {
			benchError = error instanceof Error ? error.message : 'Sample could not be queued.';
		} finally {
			workflowPending = null;
		}
	}

	async function saveProfileAndQueue(action: WorkflowAction) {
		if (action !== 'queue-encode' && action !== 'approve-size-tradeoff') return;
		const actionState = workflowActionState(action);
		if (actionState.disabled) return;
		const submissionMode = resolveQueueSubmissionMode(action, reviewGate);
		if (!submissionMode) return;
		workflowPending = action;
		profileMessage = '';
		profileError = '';
		try {
			const response =
				submissionMode === 'queue-approved'
					? await postJson<{ ok: boolean; message?: string }>(
							`${resolve('/')}api/folders/${encodedPrefix}/queue-encode`,
							{ notes: '', bypass_schedule: false }
						)
					: await postJson<{ ok: boolean; message?: string }>(
							`${resolve('/')}api/folders/${encodedPrefix}/save-profile`,
							{
								confirm_high_impact: true,
								confirm_size_tradeoff: action === 'approve-size-tradeoff',
								reviewed_draft_hash: calibration?.draft_hash ?? ''
							}
						);
			profileMessage =
				response.message || 'Approved the sample plan and queued the folder process.';
			await onMutate();
		} catch (error) {
			profileError =
				error instanceof Error ? error.message : 'Folder profile could not be approved.';
		} finally {
			workflowPending = null;
		}
	}

	async function retryEncode() {
		const action = 'retry-encode';
		const actionState = workflowActionState(action);
		if (actionState.disabled) return;
		workflowPending = action;
		profileMessage = '';
		profileError = '';
		try {
			const response = await postJson<{ ok: boolean; message?: string }>(
				`${resolve('/')}api/encode-queue/retry-prefix`,
				{ prefix }
			);
			profileMessage = response.message || 'Queued retry for this folder.';
			await onMutate();
		} catch (error) {
			profileError =
				error instanceof Error ? error.message : 'Folder processing could not be retried.';
		} finally {
			workflowPending = null;
		}
	}

	async function runFolderWorkflowAction(action: WorkflowAction) {
		if (action !== 'validate-outputs' && action !== 'promote-outputs') return;
		const actionState = workflowActionState(action);
		if (actionState.disabled) return;
		workflowPending = action;
		profileMessage = '';
		profileError = '';
		try {
			const endpoint = action === 'validate-outputs' ? 'validate-outputs' : 'promote-outputs';
			const response = await postJson<{ ok: boolean; message?: string }>(
				`${resolve('/')}api/folders/${encodedPrefix}/${endpoint}`,
				{}
			);
			profileMessage = response.message || workflow.primary;
			await onMutate();
		} catch (error) {
			profileError = error instanceof Error ? error.message : 'Folder action could not run.';
		} finally {
			workflowPending = null;
		}
	}

	async function stopSample() {
		const action = 'stop-sample';
		const actionState = workflowActionState(action);
		if (actionState.disabled) return;
		workflowPending = action;
		benchMessage = '';
		benchError = '';
		try {
			const response = await postJson<{ ok?: boolean; message?: string }>(
				`${resolve('/')}api/calibration-queue/stop`,
				{}
			);
			benchMessage = response.message || 'Stopped running and queued sample work.';
			await onMutate();
		} catch (error) {
			benchError = error instanceof Error ? error.message : 'Sample work could not be stopped.';
		} finally {
			workflowPending = null;
		}
	}

	function downloadReviewPack() {
		window.location.assign(reviewDownloadHref);
	}
</script>

<OperatorShell
	route="studio"
	subject={title}
	crumb={`/folders/${prefix}`}
	{statusTiles}
	{footerSignals}
>
	<main class="studio">
		<section class="studio__main" aria-label="Folder decision surface">
			<header class="folder-header">
				<div>
					<h1>{title}</h1>
					<div class="folder-header__path">
						<span>{scopePathLabel}</span>
						<strong class="mf-path">{prefix}</strong>
						{#if seriesContext}
							<a class="scope-link" href={resolve(seriesRoute)}
								>Open {seriesContext.title} whole show</a
							>
						{/if}
					</div>
				</div>
				<div class="folder-header__facts">
					<div>
						<span>Source</span>
						<strong>{formatBytes(summary?.total_size_bytes)}</strong>
					</div>
					<div>
						<span>Projected reclaim</span>
						<strong>{formatBytes(projectedReclaimBytes(studioFolder))}</strong>
					</div>
					<div>
						<span>Projected output</span>
						<strong>{formatBytes(predictedFolderSizeBytes(studioFolder))}</strong>
					</div>
					<div class="folder-header__metric">
						<span>Metric</span>
						<strong>{resolvedMetricCopy(studioFolder)}</strong>
					</div>
				</div>
			</header>

			{#if benchPending || activeSampleRunning}
				<section class="active-operation" role="status" aria-live="polite" aria-busy="true">
					<div class="active-operation__state">
						<span aria-hidden="true"></span>
						<strong>{benchPending ? 'Review assistant working' : 'Sample encoding now'}</strong>
					</div>
					<div class="active-operation__copy">
						<h2>{benchPending ? 'Building the sample plan' : workflow.title}</h2>
						<p>{benchPending ? REVIEW_ASSISTANT_PENDING_COPY : workflow.copy}</p>
					</div>
					<div class="active-operation__meta">
						{#if benchPending && benchRequestState.selectedHost?.label}
							<span>Worker</span>
							<strong>{benchRequestState.selectedHost.label}</strong>
						{:else if activeSampleHostLabel}
							<span>Worker</span>
							<strong>{activeSampleHostLabel}</strong>
						{/if}
						{#if activeSampleRunning}
							<a href={resolve('/ops')} data-mf-action="active-operation-ops"
								>Open live operations</a
							>
						{:else}
							<small>Keep this page open</small>
						{/if}
					</div>
				</section>
			{/if}

			<nav class="workflow-strip" aria-label="Folder workflow">
				{#each workflowSteps as step, index (step.label)}
					<div
						class:workflow-strip__step--current={step.current}
						class="workflow-strip__step workflow-strip__step--{step.tone}"
					>
						<span>{index + 1}</span>
						<div>
							<strong>{step.label}</strong>
							<small>{step.detail}</small>
						</div>
					</div>
				{/each}
			</nav>

			<section class="basic-run" aria-labelledby="basic-run-title">
				<header class="basic-run__header">
					<div>
						<span>{basicEncodeGuide.label}</span>
						<h2 id="basic-run-title">{basicEncodeGuide.title}</h2>
						<p>{basicEncodeGuide.detail}</p>
					</div>
					{#if !basicEncodeGuide.primary}
						<!-- Active status only; no command belongs in this compact guide. -->
					{:else if basicEncodeGuide.action === 'focus-bench'}
						<button
							class="basic-run__action"
							type="button"
							onclick={focusBenchComposer}
							data-mf-action={`guide-${basicEncodeGuide.action}`}>{basicEncodeGuide.primary}</button
						>
					{:else if basicEncodeGuide.action === 'open-series' || basicEncodeGuide.action === 'open-folders'}
						<a
							class="basic-run__action"
							href={resolve(seriesRoute)}
							data-mf-action={`guide-${basicEncodeGuide.action}`}>{basicEncodeGuide.primary}</a
						>
					{:else if basicEncodeGuide.action === 'open-completed'}
						<a
							class="basic-run__action"
							href={resolve('/completed')}
							data-mf-action={`guide-${basicEncodeGuide.action}`}>{basicEncodeGuide.primary}</a
						>
					{:else if basicEncodeGuide.action === 'open-ops' || basicEncodeGuide.action.startsWith('monitor-')}
						<a
							class="basic-run__action basic-run__action--secondary"
							href={resolve('/ops')}
							data-mf-action={`guide-${basicEncodeGuide.action}`}>{basicEncodeGuide.primary}</a
						>
					{:else if basicEncodeGuide.action === 'download-review-pack' && !guideActionState.disabled}
						<button
							class="basic-run__action"
							type="button"
							onclick={downloadReviewPack}
							data-mf-action={`guide-${basicEncodeGuide.action}`}>{basicEncodeGuide.primary}</button
						>
					{:else if basicEncodeGuide.action === 'start-sample' || basicEncodeGuide.action === 'retry-sample'}
						<button
							class="basic-run__action"
							type="button"
							disabled={guideActionState.disabled}
							title={guideActionState.title}
							onclick={() => confirmBenchProposal(basicEncodeGuide.action)}
							data-mf-action={`guide-${basicEncodeGuide.action}`}
							data-mf-wire="live"
							>{workflowPending === basicEncodeGuide.action
								? 'Queueing'
								: basicEncodeGuide.primary}</button
						>
					{:else if basicEncodeGuide.action === 'queue-encode' || basicEncodeGuide.action === 'approve-size-tradeoff'}
						<button
							class="basic-run__action"
							type="button"
							disabled={guideActionState.disabled}
							title={guideActionState.title}
							onclick={() => saveProfileAndQueue(basicEncodeGuide.action)}
							data-mf-action={`guide-${basicEncodeGuide.action}`}
							data-mf-wire="live"
							>{workflowPending === basicEncodeGuide.action
								? 'Approving'
								: basicEncodeGuide.primary}</button
						>
					{:else if basicEncodeGuide.action === 'retry-encode'}
						<button
							class="basic-run__action"
							type="button"
							disabled={guideActionState.disabled}
							title={guideActionState.title}
							onclick={retryEncode}
							data-mf-action={`guide-${basicEncodeGuide.action}`}
							data-mf-wire="live"
							>{workflowPending === basicEncodeGuide.action
								? 'Retrying'
								: basicEncodeGuide.primary}</button
						>
					{:else if basicEncodeGuide.action === 'validate-outputs' || basicEncodeGuide.action === 'promote-outputs'}
						<button
							class="basic-run__action"
							type="button"
							disabled={guideActionState.disabled}
							title={guideActionState.title}
							onclick={() => runFolderWorkflowAction(basicEncodeGuide.action)}
							data-mf-action={`guide-${basicEncodeGuide.action}`}
							data-mf-wire="live"
							>{workflowPending === basicEncodeGuide.action
								? 'Running'
								: basicEncodeGuide.primary}</button
						>
					{:else}
						<button
							class="basic-run__action"
							type="button"
							disabled={guideActionState.disabled}
							title={guideActionState.title}
							data-mf-action={`guide-${basicEncodeGuide.action}`}
							data-mf-wire="pending">{basicEncodeGuide.primary}</button
						>
					{/if}
				</header>
				<ol class="basic-run__steps" aria-label={`${basicEncodeGuide.label} encode steps`}>
					{#each basicEncodeGuide.steps as step, index (step.label)}
						<li class="basic-run__step basic-run__step--{step.state}">
							<span>{index + 1}</span>
							<div>
								<strong>{step.label}</strong>
								<small>{step.detail}</small>
							</div>
						</li>
					{/each}
				</ol>
			</section>

			<section class="decision decision--{workflow.tone}" aria-labelledby="decision-title">
				{#if loadError}
					<div class="route-error" role="status">
						<strong>Folder state could not be refreshed.</strong>
						<span>{loadError}</span>
					</div>
				{/if}
				<div class="decision__summary">
					<StateBadge tone={workflow.tone} label={workflow.label} />
					<h2 id="decision-title">{workflow.title}</h2>
					<p>{workflow.copy}</p>
				</div>
				<div class="decision__facts" aria-label="Decision facts">
					{#each decisionFacts as fact (fact.label)}
						<div class="decision-fact">
							<span>{fact.label}</span>
							<strong>{fact.value}</strong>
							<small>{fact.detail}</small>
						</div>
					{/each}
				</div>
				<div class="decision__actions">
					{#if workflow.primaryAction === 'focus-bench'}
						<button
							class="control control--primary"
							type="button"
							onclick={focusBenchComposer}
							data-mf-action={workflow.primaryAction}>{workflow.primary}</button
						>
					{:else if workflow.primaryAction === 'open-series' || workflow.primaryAction === 'open-folders'}
						<a
							class="control control--primary"
							href={resolve(seriesRoute)}
							data-mf-action={workflow.primaryAction}>{workflow.primary}</a
						>
					{:else if workflow.primaryAction === 'open-completed'}
						<a
							class="control control--primary"
							href={resolve('/completed')}
							data-mf-action={workflow.primaryAction}>{workflow.primary}</a
						>
					{:else if workflow.primaryAction === 'open-ops' || workflow.primaryAction.startsWith('monitor-')}
						<a
							class="control control--primary"
							href={resolve('/ops')}
							data-mf-action={workflow.primaryAction}>{workflow.primary}</a
						>
					{:else if workflow.primaryAction === 'download-review-pack' && !workflowActionState(workflow.primaryAction).disabled}
						<button
							class="control control--primary"
							type="button"
							onclick={downloadReviewPack}
							data-mf-action={workflow.primaryAction}>{workflow.primary}</button
						>
					{:else if workflow.primaryAction === 'start-sample' || workflow.primaryAction === 'retry-sample'}
						<button
							class="control control--primary"
							type="button"
							disabled={workflowActionState(workflow.primaryAction).disabled}
							title={workflowActionState(workflow.primaryAction).title}
							onclick={() => confirmBenchProposal(workflow.primaryAction)}
							data-mf-action={workflow.primaryAction}
							data-mf-wire="live"
							>{workflowPending === workflow.primaryAction ? 'Queueing' : workflow.primary}</button
						>
					{:else if workflow.primaryAction === 'queue-encode' || workflow.primaryAction === 'approve-size-tradeoff'}
						<button
							class="control control--primary"
							type="button"
							disabled={workflowActionState(workflow.primaryAction).disabled}
							title={workflowActionState(workflow.primaryAction).title}
							onclick={() => saveProfileAndQueue(workflow.primaryAction)}
							data-mf-action={workflow.primaryAction}
							data-mf-wire="live"
							>{workflowPending === workflow.primaryAction ? 'Approving' : workflow.primary}</button
						>
					{:else if workflow.primaryAction === 'retry-encode'}
						<button
							class="control control--primary"
							type="button"
							disabled={workflowActionState(workflow.primaryAction).disabled}
							title={workflowActionState(workflow.primaryAction).title}
							onclick={retryEncode}
							data-mf-action={workflow.primaryAction}
							data-mf-wire="live"
							>{workflowPending === workflow.primaryAction ? 'Retrying' : workflow.primary}</button
						>
					{:else if workflow.primaryAction === 'validate-outputs' || workflow.primaryAction === 'promote-outputs'}
						<button
							class="control control--primary"
							type="button"
							disabled={workflowActionState(workflow.primaryAction).disabled}
							title={workflowActionState(workflow.primaryAction).title}
							onclick={() => runFolderWorkflowAction(workflow.primaryAction)}
							data-mf-action={workflow.primaryAction}
							data-mf-wire="live"
							>{workflowPending === workflow.primaryAction ? 'Running' : workflow.primary}</button
						>
					{:else}
						<button
							class="control control--primary"
							type="button"
							disabled={workflowActionState(workflow.primaryAction).disabled}
							title={workflowActionState(workflow.primaryAction).title}
							data-mf-action={workflow.primaryAction}
							data-mf-wire="pending">{workflow.primary}</button
						>
					{/if}
					{#if workflow.secondaryAction === 'focus-bench' || workflow.secondaryAction === 'revise-proposal'}
						<button
							class="control"
							type="button"
							onclick={focusBenchComposer}
							data-mf-action={workflow.secondaryAction}>{workflow.secondary}</button
						>
					{:else if workflow.secondaryAction === 'revise-smaller'}
						<button
							class="control"
							type="button"
							onclick={reviseSmaller}
							data-mf-action={workflow.secondaryAction}>{workflow.secondary}</button
						>
					{:else if workflow.secondaryAction === 'open-series' || workflow.secondaryAction === 'open-folders'}
						<a class="control" href={resolve(seriesRoute)} data-mf-action={workflow.secondaryAction}
							>{workflow.secondary}</a
						>
					{:else if workflow.secondaryAction === 'open-completed'}
						<a
							class="control"
							href={resolve('/completed')}
							data-mf-action={workflow.secondaryAction}>{workflow.secondary}</a
						>
					{:else if workflow.secondaryAction === 'open-ops'}
						<a class="control" href={resolve('/ops')} data-mf-action={workflow.secondaryAction}
							>{workflow.secondary}</a
						>
					{:else if workflow.secondaryAction.startsWith('monitor-')}
						<a class="control" href={resolve('/ops')} data-mf-action={workflow.secondaryAction}
							>{workflow.secondary}</a
						>
					{:else if workflow.secondaryAction === 'download-review-pack' && !workflowActionState(workflow.secondaryAction).disabled}
						<button
							class="control"
							type="button"
							onclick={downloadReviewPack}
							data-mf-action={workflow.secondaryAction}>{workflow.secondary}</button
						>
					{:else if workflow.secondaryAction === 'start-sample' || workflow.secondaryAction === 'retry-sample'}
						<button
							class="control"
							type="button"
							disabled={workflowActionState(workflow.secondaryAction).disabled}
							title={workflowActionState(workflow.secondaryAction).title}
							onclick={() => confirmBenchProposal(workflow.secondaryAction)}
							data-mf-action={workflow.secondaryAction}
							data-mf-wire="live"
							>{workflowPending === workflow.secondaryAction
								? 'Queueing'
								: workflow.secondary}</button
						>
					{:else if workflow.secondaryAction === 'stop-sample'}
						<button
							class="control"
							type="button"
							disabled={workflowActionState(workflow.secondaryAction).disabled}
							title={workflowActionState(workflow.secondaryAction).title}
							onclick={stopSample}
							data-mf-action={workflow.secondaryAction}
							data-mf-wire="live"
							>{workflowPending === workflow.secondaryAction
								? 'Stopping'
								: workflow.secondary}</button
						>
					{:else if workflow.secondaryAction === 'queue-encode' || workflow.secondaryAction === 'approve-size-tradeoff'}
						<button
							class="control"
							type="button"
							disabled={workflowActionState(workflow.secondaryAction).disabled}
							title={workflowActionState(workflow.secondaryAction).title}
							onclick={() => saveProfileAndQueue(workflow.secondaryAction)}
							data-mf-action={workflow.secondaryAction}
							data-mf-wire="live"
							>{workflowPending === workflow.secondaryAction
								? 'Approving'
								: workflow.secondary}</button
						>
					{:else if workflow.secondaryAction === 'retry-encode'}
						<button
							class="control"
							type="button"
							disabled={workflowActionState(workflow.secondaryAction).disabled}
							title={workflowActionState(workflow.secondaryAction).title}
							onclick={retryEncode}
							data-mf-action={workflow.secondaryAction}
							data-mf-wire="live"
							>{workflowPending === workflow.secondaryAction
								? 'Retrying'
								: workflow.secondary}</button
						>
					{:else if workflow.secondaryAction === 'validate-outputs' || workflow.secondaryAction === 'promote-outputs'}
						<button
							class="control"
							type="button"
							disabled={workflowActionState(workflow.secondaryAction).disabled}
							title={workflowActionState(workflow.secondaryAction).title}
							onclick={() => runFolderWorkflowAction(workflow.secondaryAction)}
							data-mf-action={workflow.secondaryAction}
							data-mf-wire="live"
							>{workflowPending === workflow.secondaryAction
								? 'Running'
								: workflow.secondary}</button
						>
					{:else}
						<button
							class="control"
							type="button"
							disabled={workflowActionState(workflow.secondaryAction).disabled}
							title={workflowActionState(workflow.secondaryAction).title}
							data-mf-action={workflow.secondaryAction}
							data-mf-wire="pending">{workflow.secondary}</button
						>
					{/if}
					{#if reviewPackReady && workflow.primaryAction !== 'download-review-pack' && workflow.secondaryAction !== 'download-review-pack'}
						<button
							class="control"
							type="button"
							onclick={downloadReviewPack}
							data-mf-action="download-review-pack">Download side-by-side video</button
						>
					{/if}
					{#if profileError}
						<p class="decision__status decision__status--fail">{profileError}</p>
					{:else if profileMessage}
						<p class="decision__status decision__status--ready">{profileMessage}</p>
					{/if}
				</div>
				{#if approvedSeasonNote}
					<div class="season-shortcut">
						<div>
							<span>Approved policy reference</span>
							<strong>{approvedSeasonShortcut?.root_label ?? title}</strong>
							<small>{approvedSeasonShortcut?.count ?? 1} approved season policy available</small>
						</div>
						<button class="control" type="button" onclick={fillDraftFromApprovedPolicy}
							>Use policy as sample plan</button
						>
					</div>
				{/if}
			</section>

			<section
				class="review-workspace review-workspace--{reviewWorkspaceTone}"
				aria-labelledby="review-workspace-title"
			>
				<header class="review-workspace__header">
					<div>
						<StateBadge tone={reviewWorkspaceTone} label={reviewWorkspace.badge} />
						<h2 id="review-workspace-title">{reviewWorkspace.title}</h2>
					</div>
					{#if reviewPackReady}
						<button
							class="control control--primary review-workspace__download"
							type="button"
							onclick={downloadReviewPack}
							data-mf-action="download-review-pack">Download side-by-side video</button
						>
					{/if}
				</header>

				{#if reviewWorkspace.layout === 'pipeline'}
					<div class="output-pipeline" aria-label="Output pipeline status">
						{#each outputReviewRows as row (row.label)}
							<div
								class:output-pipeline__lane--current={row.current}
								class="output-pipeline__lane output-pipeline__lane--{row.tone ?? 'idle'}"
							>
								<div class="output-pipeline__lane-head">
									<strong>{row.label}</strong>
									<span>{row.source}</span>
								</div>
								<p>{row.output}</p>
								<small>{row.detail}</small>
							</div>
						{/each}
					</div>
				{:else}
					<div class="output-review-table" aria-label="Output review facts">
						<div class="output-review-table__head">
							<span>Area</span>
							<span>Source</span>
							<span>Planned output</span>
							<span>Why it matters</span>
						</div>
						{#each outputReviewRows as row (row.label)}
							<div class:output-review-row--wait={row.tone === 'wait'} class="output-review-row">
								<strong>{row.label}</strong>
								<span>{row.source}</span>
								<span>{row.output}</span>
								<small>{row.detail}</small>
							</div>
						{/each}
					</div>

					<div class="review-media-grid" aria-label="Sample review media">
						{#each visualReviewArtifacts as artifact (artifact.imageUrl || artifact.label)}
							<button
								class="review-media-tile"
								type="button"
								onclick={() => openReviewMedia(artifact.imageUrl)}
							>
								<img src={artifact.imageUrl} alt={artifact.label || artifact.kind} loading="lazy" />
								<span>{artifact.label || artifact.kind}</span>
							</button>
						{:else}
							<div class="empty-note">
								Run a sample to generate source-versus-output review images.
							</div>
						{/each}
						{#each audioReviewArtifacts as artifact (artifact.imageUrl || artifact.label)}
							<button
								class="review-media-tile review-media-tile--audio"
								type="button"
								onclick={() => openReviewMedia(artifact.imageUrl)}
							>
								<img src={artifact.imageUrl} alt={artifact.label || artifact.kind} loading="lazy" />
								<span>{artifact.label || artifact.kind}</span>
							</button>
						{/each}
					</div>
				{/if}
			</section>

			{#if !workflow.isOutputWorkflow}
				<WorkstationPanel title="Review assistant">
					<div class="bench">
						<details
							class="bench__thread"
							open={!sampleVerdict}
							aria-label="Review assistant conversation"
						>
							<summary>
								<strong>Assistant transcript</strong>
								<span>{benchMessages.length} notes</span>
							</summary>
							<div class="bench__messages">
								{#each benchMessages as message (message.id)}
									<div
										class="bench-message bench-message--{message.role} bench-message--tone-{message.tone ??
											'neutral'}"
									>
										<header>
											<span>{message.label}</span>
											{#if message.meta}
												<small>{message.meta}</small>
											{/if}
										</header>
										<strong>{message.title}</strong>
										<p>{message.body}</p>
									</div>
								{/each}
							</div>
							{#if basicEncodeGuide.action === 'start-sample' || basicEncodeGuide.action === 'retry-sample'}
								<div class="bench-next-action">
									<div>
										<span>Sample plan ready</span>
										<strong>Run this sample before approving the {activeScopeLabel}.</strong>
									</div>
									<button
										class="control control--primary"
										type="button"
										disabled={guideActionState.disabled}
										title={guideActionState.title}
										onclick={() => confirmBenchProposal(basicEncodeGuide.action)}
										data-mf-action={`bench-${basicEncodeGuide.action}`}
										data-mf-wire="live"
										>{workflowPending === basicEncodeGuide.action
											? 'Queueing'
											: basicEncodeGuide.primary}</button
									>
								</div>
							{/if}
						</details>

						<div class="bench__composer" aria-label="Review request composer">
							<label>
								<span>Request</span>
								<textarea
									rows="5"
									placeholder="Describe the content and your quality or size goal, or ask for a standard sample to get started."
									bind:this={benchTextarea}
									bind:value={benchNote}
								></textarea>
							</label>
							<div class="bench__controls">
								<label>
									<span>Worker</span>
									<select bind:value={selectedHostKey}>
										{#each sampleHostOptions as host (host.key)}
											<option value={host.key}
												>{host.label}{host.available ? '' : ' · unavailable'}</option
											>
										{/each}
									</select>
								</label>
								<button
									class="control control--primary"
									type="button"
									disabled={benchRequestDisabled}
									title={benchRequestDisabled ? benchRequestState.blocker : ''}
									onclick={sendBenchRequest}
									data-mf-action="bench-request"
									data-mf-wire="live">{benchPending ? 'Sending' : 'Send request'}</button
								>
							</div>
							{#if benchError}
								<p class="bench__status bench__status--fail">{benchError}</p>
							{:else if benchMessage}
								<p class="bench__status bench__status--ready">{benchMessage}</p>
							{:else if benchPending}
								<p class="bench__status">{REVIEW_ASSISTANT_PENDING_COPY}</p>
							{:else if benchRequestState.blocker}
								<p class="bench__status">{benchRequestState.blocker}</p>
							{/if}
						</div>
					</div>
				</WorkstationPanel>
			{/if}

			<div
				class:support-grid--single={workflow.isOutputWorkflow}
				class="support-grid"
				aria-label="Folder supporting signals"
			>
				<WorkstationPanel title="Runtime">
					<dl class="kv kv--compact">
						{#each runtimeFacts as fact (fact.label)}
							<dt>{fact.label}</dt>
							<dd>{fact.value}</dd>
						{/each}
					</dl>
				</WorkstationPanel>

				{#if !workflow.isOutputWorkflow}
					<WorkstationPanel
						title={pendingProposal?.proposal_id ? 'Active sample plan' : 'Sample target'}
					>
						<dl class="kv kv--compact">
							<dt>{pendingProposal?.proposal_id ? 'Plan' : 'Source'}</dt>
							<dd>{draftReviewRow?.output ?? '—'}</dd>
							<dt>Reason</dt>
							<dd>{draftReviewRow?.detail ?? '—'}</dd>
							<dt>Sample</dt>
							<dd>
								{sampleResultRow ? `${sampleResultRow.output} from ${sampleResultRow.source}` : '—'}
							</dd>
							<dt>Metric</dt>
							<dd>{resolvedMetricCopy(studioFolder)}</dd>
						</dl>
					</WorkstationPanel>

					<WorkstationPanel title="Recent">
						<div class="history-list history-list--compact">
							{#each (studioFolder.recent_tuning_sessions ?? []).slice(0, 3) as session, index (`${session.session_id ?? session.created_at ?? index}`)}
								<div>
									<span>{formatDateTimeCopy(String(session.created_at ?? '')) || '—'}</span>
									<strong>{String(session.summary ?? session.note ?? 'Tuning session')}</strong>
								</div>
							{:else}
								<div class="empty-note empty-note--compact">
									No recent tuning sessions returned.
								</div>
							{/each}
						</div>
					</WorkstationPanel>
				{/if}
			</div>

			{#if !workflow.isOutputWorkflow}
				<div class="evidence-grid evidence-grid--sample-only">
					<WorkstationPanel title={reviewPackReady ? 'Review sample media' : 'Sample source'}>
						<div class:sample-card--source-only={!reviewPackReady} class="sample-card">
							<div class:sample-frame--source-only={!reviewPackReady} class="sample-frame">
								<span
									>{reviewPackReady
										? 'Review sample'
										: sampleItem
											? 'Sample source'
											: 'No sample selected'}</span
								>
								<strong
									>{sampleItem
										? pathFilename(sampleItem.rel_path)
										: 'Run a sample to populate review evidence.'}</strong
								>
							</div>
							<div class="sample-card__guidance">
								<strong
									>{reviewPackReady ? 'Review evidence is ready' : 'No sample video yet'}</strong
								>
								<p>
									{#if reviewPackReady}
										Download or open the side-by-side sample, review it, then approve or revise the
										settings from the decision panel above.
									{:else}
										This is the source item the assistant will use. Create a sample before approving
										anything across the {activeScopeLabel}.
									{/if}
								</p>
								{#if reviewPackReady}
									<button
										class="control control--primary"
										type="button"
										onclick={downloadReviewPack}
										data-mf-action="download-review-pack">Download side-by-side video</button
									>
								{:else}
									<button
										class="control control--primary"
										type="button"
										onclick={focusBenchComposer}
									>
										Ask review assistant
									</button>
								{/if}
							</div>
							<div class="sample-facts">
								{#each sampleFacts as fact (fact.label)}
									<div>
										<span>{fact.label}</span>
										<strong>{fact.value}</strong>
									</div>
								{/each}
							</div>
						</div>
					</WorkstationPanel>
				</div>
			{/if}

			{#if !workflow.isOutputWorkflow}
				<details class="technical-details">
					<summary>
						<strong>Proposed settings details</strong>
						<span
							>{proposalRows.length ? `${proposalRows.length} plan differences` : 'No plan'}</span
						>
					</summary>
					<WorkstationPanel
						title="Current settings vs. proposal"
						meta={proposalRows.length ? `${proposalRows.length} rows` : 'No plan'}
					>
						<div class="table-wrap">
							<table class="policy-table">
								<thead>
									<tr>
										<th>Area</th>
										<th>Setting</th>
										<th>Current</th>
										<th>Plan</th>
									</tr>
								</thead>
								<tbody>
									{#each proposalRows as row (row.section + row.label)}
										<tr class:changed={row.changed}>
											<td>{row.section}</td>
											<td>{row.label}</td>
											<td>{row.current}</td>
											<td>{row.draft}</td>
										</tr>
									{:else}
										<tr>
											<td colspan="4">No pending proposal was returned for this folder.</td>
										</tr>
									{/each}
								</tbody>
							</table>
						</div>
					</WorkstationPanel>
				</details>
			{/if}
		</section>

		<aside class="studio__left" aria-label="Folder workflow context">
			<WorkstationPanel eyebrow="Folder" title="Active context">
				<div class="context-list">
					{#if seriesContext}
						<div class="context-list__wide">
							<span>Series scope</span>
							<a class="scope-link" href={resolve(seriesRoute)}>{seriesContext.prefix}</a>
						</div>
					{/if}
					<div>
						<span>Scope</span>
						<strong>{outputScopeDisplayLabel(activeScopeLabel)}</strong>
					</div>
					<div>
						<span>Library</span>
						<strong>{library}</strong>
					</div>
					<div>
						<span>Items</span>
						<strong>{summary?.item_count?.toLocaleString('en-US') ?? '—'}</strong>
					</div>
					<div>
						<span>Pending</span>
						<strong>
							{#if workflow.isOutputWorkflow}
								{summarizeOutputWorkflowPending(
									studioFolder.workflow_state?.counts ?? status.workflow_state?.counts
								)}
							{:else}
								{summary?.statuses ? summarizeStatuses(summary.statuses) : '—'}
							{/if}
						</strong>
					</div>
					<div>
						<span>Source size</span>
						<strong>{formatBytes(summary?.total_size_bytes)}</strong>
					</div>
					{#if activeScopeLabel === 'whole show' && seasonScopeRows.length > 0}
						<div class="context-list__wide season-nav" aria-label="Season scopes">
							<span>Seasons</span>
							<div class="season-nav__rows">
								{#each seasonScopeRows as season (season.href)}
									<a class="season-nav__row" href={resolve(folderRoutePath(season.href))}>
										<strong>{season.label}</strong>
										<small>{season.count}</small>
									</a>
								{/each}
							</div>
						</div>
					{/if}
				</div>
			</WorkstationPanel>

			<WorkstationPanel
				eyebrow="Workers"
				title={workflow.isOutputWorkflow ? 'Processing capacity' : 'Sample readiness'}
			>
				<div class="host-list">
					{#if workflow.isOutputWorkflow}
						{#each processingHostOptions.slice(0, 6) as host (host.key)}
							<div class="host-row">
								<StateBadge compact tone={host.tone} label={host.state} />
								<span>{host.label}{host.detail ? ` · ${host.detail}` : ''}</span>
							</div>
						{/each}
						{#if processingHostOptions.length === 0}
							<div class="empty-note">Worker status is unavailable.</div>
						{/if}
					{:else}
						{#each sampleHostOptions.slice(0, 6) as host (host.key)}
							<div class="host-row">
								<StateBadge
									compact
									tone={host.available ? (host.scheduleOpen === false ? 'wait' : 'ready') : 'fail'}
									label={host.state}
								/>
								<span>{host.label}{host.detail ? ` · ${host.detail}` : ''}</span>
							</div>
						{/each}
						{#if sampleHostOptions.length === 0}
							<div class="empty-note">Worker status is unavailable.</div>
						{/if}
					{/if}
				</div>
			</WorkstationPanel>
		</aside>
	</main>
</OperatorShell>

<style>
	.studio {
		display: grid;
		grid-template-columns: minmax(0, 280px) minmax(0, 1fr);
		min-height: calc(100vh - 178px);
		overflow: hidden;
	}

	.studio__left {
		background: var(--mf-bg-shell);
		display: flex;
		flex-direction: column;
		gap: var(--mf-space-5);
		grid-column: 1;
		grid-row: 1;
		padding: var(--mf-space-5);
	}

	.studio__left {
		border-right: var(--mf-border);
	}

	.studio__main {
		display: grid;
		gap: var(--mf-space-5);
		grid-column: 2;
		grid-row: 1;
		min-width: 0;
		padding: var(--mf-space-6);
	}

	.studio__main > :global(*) {
		order: 5;
	}

	.folder-header {
		align-items: start;
		border-bottom: var(--mf-border);
		display: grid;
		gap: var(--mf-space-6);
		grid-template-columns: minmax(0, 1fr) auto;
		order: 2;
		padding-bottom: var(--mf-space-5);
	}

	.folder-header h1 {
		margin-top: var(--mf-space-3);
	}

	.active-operation {
		align-items: center;
		background: var(--mf-active-bg);
		border: var(--mf-border);
		border-left: 3px solid var(--mf-active-fg);
		display: grid;
		gap: var(--mf-space-5);
		grid-template-columns: max-content minmax(0, 1fr) max-content;
		order: 3;
		padding: var(--mf-space-4) var(--mf-space-5);
	}

	.active-operation__state {
		align-items: center;
		color: var(--mf-active-fg-bright);
		display: flex;
		font-size: var(--mf-text-2xs);
		gap: var(--mf-space-2);
		letter-spacing: var(--mf-tracking-wide);
		text-transform: uppercase;
	}

	.active-operation__state > span {
		background: var(--mf-active-fg);
		box-shadow: 0 0 0 3px color-mix(in srgb, var(--mf-active-fg) 20%, transparent);
		height: 8px;
		width: 8px;
	}

	.active-operation__copy {
		display: grid;
		gap: var(--mf-space-1);
		min-width: 0;
	}

	.active-operation__copy h2 {
		font-size: var(--mf-text-lg);
	}

	.active-operation__copy p {
		color: var(--mf-fg-secondary);
		font-size: var(--mf-text-sm);
	}

	.active-operation__meta {
		display: grid;
		gap: var(--mf-space-1);
		justify-items: end;
	}

	.active-operation__meta span,
	.active-operation__meta small {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
		text-transform: uppercase;
	}

	.active-operation__meta strong {
		font-family: var(--mf-font-mono), monospace;
		font-size: var(--mf-text-xs);
	}

	.active-operation__meta a {
		color: var(--mf-active-fg-bright);
		font-size: var(--mf-text-xs);
		font-weight: var(--mf-weight-semibold);
	}

	.folder-header__path {
		display: grid;
		gap: var(--mf-space-2);
		margin-top: var(--mf-space-4);
		max-width: 92ch;
	}

	.scope-link {
		align-items: center;
		background: var(--mf-bg-panel-2);
		border: var(--mf-border-muted);
		border-left: 2px solid var(--mf-active-fg);
		color: var(--mf-active-fg-bright);
		display: inline-flex;
		font-size: var(--mf-text-xs);
		font-weight: var(--mf-weight-semibold);
		justify-self: start;
		min-height: var(--mf-control-md);
		padding: 0 var(--mf-space-3);
	}

	.scope-link:hover {
		background: var(--mf-active-bg);
		border-color: var(--mf-active-line);
		border-left-color: var(--mf-active-fg);
		color: var(--mf-fg-primary);
	}

	.folder-header__facts,
	.decision__facts,
	.sample-facts {
		display: flex;
		gap: var(--mf-space-5);
		min-width: 0;
	}

	.folder-header__facts {
		align-self: start;
	}

	.sample-facts {
		display: grid;
		grid-template-columns: repeat(3, minmax(0, 1fr));
	}

	.decision__facts {
		background: var(--mf-bg-panel-2);
		border: var(--mf-border-muted);
		border-left: 2px solid var(--decision-line);
		padding: var(--mf-space-4);
	}

	.sample-facts div:first-child {
		grid-column: 1 / -1;
	}

	.folder-header__facts div,
	.decision-fact,
	.sample-facts div,
	.context-list div {
		display: grid;
		gap: var(--mf-space-2);
		min-width: 88px;
	}

	.context-list__wide {
		grid-column: 1 / -1;
	}

	.season-nav {
		border-top: var(--mf-border-muted);
		padding-top: var(--mf-space-3);
	}

	.season-nav__rows {
		display: grid;
		gap: var(--mf-space-2);
		min-width: 0;
	}

	.season-nav__row {
		align-items: center;
		background: var(--mf-bg-panel-2);
		border: var(--mf-border-muted);
		display: flex;
		gap: var(--mf-space-3);
		justify-content: space-between;
		min-height: var(--mf-control-md);
		padding: 0 var(--mf-space-3);
	}

	.season-nav__row:hover {
		background: var(--mf-active-bg);
		border-color: var(--mf-active-line);
	}

	.season-nav__row strong {
		color: var(--mf-fg-primary);
		font-size: var(--mf-text-xs);
		font-weight: var(--mf-weight-semibold);
	}

	.season-nav__row small {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-semibold);
		text-transform: uppercase;
	}

	.folder-header__metric {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-xs);
		justify-content: end;
	}

	.folder-header__metric strong {
		color: var(--mf-fg-secondary);
		font-size: var(--mf-text-xs);
	}

	.folder-header__facts span,
	.folder-header__path span,
	.decision-fact span,
	.sample-facts span,
	.context-list span {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-semibold);
		letter-spacing: 0.08em;
		text-transform: uppercase;
	}

	.folder-header__facts strong,
	.folder-header__path strong,
	.decision-fact strong,
	.sample-facts strong,
	.context-list strong {
		font-family: var(--mf-font-mono), monospace;
		font-size: var(--mf-text-sm);
		font-weight: var(--mf-weight-medium);
		overflow-wrap: anywhere;
	}

	.decision-fact small {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-xs);
	}

	.workflow-strip {
		background: var(--mf-bg-strip);
		border: var(--mf-border);
		display: grid;
		grid-template-columns: repeat(4, minmax(0, 1fr));
		order: 4;
	}

	.workflow-strip__step {
		align-items: start;
		border-right: var(--mf-border-muted);
		border-top: 2px solid var(--mf-line-strong);
		display: grid;
		gap: var(--mf-space-2);
		grid-template-columns: auto minmax(0, 1fr);
		min-width: 0;
		padding: var(--mf-space-3);
	}

	.workflow-strip__step:last-child {
		border-right: 0;
	}

	.workflow-strip__step > span {
		align-items: center;
		background: var(--mf-bg-input);
		border: var(--mf-border);
		color: var(--mf-fg-tertiary);
		display: inline-flex;
		font-family: var(--mf-font-mono), monospace;
		font-size: var(--mf-text-2xs);
		height: 20px;
		justify-content: center;
		width: 20px;
	}

	.workflow-strip__step div {
		display: grid;
		gap: var(--mf-space-1);
		min-width: 0;
	}

	.workflow-strip__step strong {
		font-size: var(--mf-text-xs);
		font-weight: var(--mf-weight-semibold);
	}

	.workflow-strip__step small {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
		line-height: var(--mf-leading-snug);
		overflow-wrap: anywhere;
	}

	.workflow-strip__step--current {
		background: var(--mf-active-bg);
	}

	.workflow-strip__step--active {
		border-top-color: var(--mf-active-fg);
	}

	.workflow-strip__step--ready {
		border-top-color: var(--mf-ready-fg);
	}

	.workflow-strip__step--wait {
		border-top-color: var(--mf-wait-fg);
	}

	.workflow-strip__step--fail {
		border-top-color: var(--mf-fail-fg);
	}

	.basic-run {
		background: var(--mf-bg-panel);
		border: var(--mf-border);
		display: grid;
		gap: var(--mf-space-3);
		order: 3;
		padding: var(--mf-space-4);
	}

	.basic-run__header {
		align-items: start;
		display: grid;
		gap: var(--mf-space-4);
		grid-template-columns: minmax(0, 1fr) max-content;
	}

	.basic-run__header div {
		display: grid;
		gap: var(--mf-space-1);
		min-width: 0;
	}

	.basic-run__header span {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-semibold);
		letter-spacing: 0;
		text-transform: uppercase;
	}

	.basic-run__header h2 {
		font-size: var(--mf-text-base);
		font-weight: var(--mf-weight-semibold);
		margin: 0;
	}

	.basic-run__header p {
		color: var(--mf-fg-muted);
		font-size: var(--mf-text-xs);
		line-height: var(--mf-leading-snug);
		margin: 0;
	}

	.basic-run__action,
	.basic-run__next {
		align-items: center;
		align-self: start;
		background: var(--mf-ready-bg);
		border: 1px solid var(--mf-ready-line);
		color: var(--mf-ready-fg);
		display: inline-flex;
		font-size: var(--mf-text-xs);
		font-weight: var(--mf-weight-semibold);
		justify-content: center;
		justify-self: start;
		line-height: 1;
		max-width: 100%;
		min-height: var(--mf-control-md);
		min-width: 140px;
		padding: var(--mf-space-2) var(--mf-space-3);
		text-decoration: none;
		white-space: nowrap;
		width: fit-content;
	}

	.basic-run__action {
		cursor: pointer;
	}

	.basic-run__action--secondary {
		background: var(--mf-bg-panel-2);
		border-color: var(--mf-line-strong);
		color: var(--mf-fg-primary);
		min-width: 0;
		padding: var(--mf-space-2) var(--mf-space-4);
	}

	button.basic-run__action {
		font: inherit;
	}

	.basic-run__steps {
		display: grid;
		gap: var(--mf-space-2);
		grid-template-columns: repeat(7, minmax(0, 1fr));
		list-style: none;
		margin: 0;
		padding: 0;
	}

	.basic-run__step {
		background: var(--mf-bg-strip);
		border: var(--mf-border-muted);
		border-top: 2px solid var(--mf-line-strong);
		display: grid;
		gap: var(--mf-space-1);
		grid-template-columns: minmax(0, 1fr);
		min-width: 0;
		padding: var(--mf-space-2);
	}

	.basic-run__step > span {
		align-items: center;
		background: var(--mf-bg-input);
		border: var(--mf-border-muted);
		color: var(--mf-fg-tertiary);
		display: inline-flex;
		font-family: var(--mf-font-mono), monospace;
		font-size: var(--mf-text-2xs);
		height: 18px;
		justify-content: center;
		width: 18px;
	}

	.basic-run__step div {
		display: grid;
		gap: var(--mf-space-1);
		min-width: 0;
	}

	.basic-run__step strong {
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-semibold);
	}

	.basic-run__step small {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
		line-height: var(--mf-leading-snug);
		overflow-wrap: anywhere;
	}

	.basic-run__step--done {
		border-top-color: var(--mf-ready-fg);
	}

	.basic-run__step--current {
		background: var(--mf-active-bg);
		border-top-color: var(--mf-active-fg);
	}

	.decision {
		--decision-line: var(--mf-idle-line);
		background: var(--mf-bg-panel);
		border: var(--mf-border);
		border-left: 3px solid var(--decision-line);
		display: grid;
		gap: var(--mf-space-6);
		grid-template-columns: minmax(0, 1fr);
		order: 5;
		padding: var(--mf-space-6);
	}

	.decision__summary,
	.decision__facts,
	.decision__actions {
		min-width: 0;
	}

	.decision--active {
		--decision-line: var(--mf-active-fg);
	}

	.decision--ready {
		--decision-line: var(--mf-ready-fg);
	}

	.decision--wait {
		--decision-line: var(--mf-wait-fg);
	}

	.decision--fail {
		--decision-line: var(--mf-fail-fg);
	}

	.decision h2 {
		margin-top: var(--mf-space-5);
	}

	.decision p {
		margin-top: var(--mf-space-3);
		max-width: 68ch;
	}

	.decision__facts {
		grid-column: 1 / -1;
	}

	.decision-fact {
		flex: 1 1 0;
	}

	.decision__actions {
		align-items: center;
		display: flex;
		gap: var(--mf-space-4);
		flex-wrap: wrap;
		grid-column: 1 / -1;
		justify-content: flex-start;
		min-width: 0;
	}

	.season-shortcut {
		align-items: center;
		background: var(--mf-bg-panel-2);
		border: var(--mf-border-muted);
		border-left: 2px solid var(--mf-wait-fg);
		display: flex;
		gap: var(--mf-space-4);
		grid-column: 1 / -1;
		justify-content: space-between;
		padding: var(--mf-space-4);
	}

	.season-shortcut div {
		display: grid;
		gap: var(--mf-space-1);
		min-width: 0;
	}

	.season-shortcut span,
	.season-shortcut small {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-semibold);
		letter-spacing: 0.08em;
		text-transform: uppercase;
	}

	.season-shortcut strong {
		font-size: var(--mf-text-sm);
		font-weight: var(--mf-weight-semibold);
		overflow-wrap: anywhere;
	}

	.decision__status {
		border-left: 2px solid var(--mf-idle-fg);
		flex-basis: 100%;
		font-size: var(--mf-text-xs);
		line-height: var(--mf-leading-normal);
		padding-left: var(--mf-space-3);
	}

	.decision__status--ready {
		border-left-color: var(--mf-ready-fg);
		color: var(--mf-ready-fg);
	}

	.decision__status--fail {
		border-left-color: var(--mf-fail-fg);
		color: var(--mf-fail-fg);
	}

	.review-workspace {
		background: var(--mf-bg-panel);
		border: var(--mf-border);
		border-left: 3px solid var(--review-workspace-tone, var(--mf-ready-fg));
		display: grid;
		gap: var(--mf-space-5);
		order: 4;
		padding: var(--mf-space-5);
	}

	.review-workspace--active {
		--review-workspace-tone: var(--mf-active-fg);
	}

	.review-workspace--fail {
		--review-workspace-tone: var(--mf-fail-fg);
	}

	.review-workspace--idle {
		--review-workspace-tone: var(--mf-idle-fg);
	}

	.review-workspace--ready {
		--review-workspace-tone: var(--mf-ready-fg);
	}

	.review-workspace--wait {
		--review-workspace-tone: var(--mf-wait-fg);
	}

	.review-workspace__header {
		align-items: start;
		display: grid;
		gap: var(--mf-space-5);
		grid-template-columns: minmax(0, 1fr);
	}

	.review-workspace h2 {
		font-size: var(--mf-text-lg);
		margin-top: var(--mf-space-3);
	}

	.output-review-table__head span {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-semibold);
		letter-spacing: 0.08em;
		text-transform: uppercase;
	}

	.output-review-table {
		border: var(--mf-border-muted);
		display: grid;
		overflow: hidden;
	}

	.output-review-table__head,
	.output-review-row {
		display: grid;
		gap: var(--mf-space-4);
		grid-template-columns: minmax(110px, 0.8fr) minmax(130px, 1fr) minmax(150px, 1.1fr) minmax(
				180px,
				1.4fr
			);
		min-width: 0;
	}

	.output-review-table__head {
		background: var(--mf-bg-strip);
		border-bottom: var(--mf-border-muted);
		padding: var(--mf-space-3) var(--mf-space-4);
	}

	.output-review-row {
		background: var(--mf-bg-panel-2);
		border-bottom: var(--mf-border-muted);
		padding: var(--mf-space-4);
	}

	.output-review-row:last-child {
		border-bottom: 0;
	}

	.output-review-row--wait {
		box-shadow: inset 3px 0 0 var(--mf-wait-fg);
	}

	.output-review-row strong,
	.output-review-row span {
		font-family: var(--mf-font-mono), monospace;
		font-size: var(--mf-text-xs);
		overflow-wrap: anywhere;
	}

	.output-review-row strong {
		font-family: inherit;
		font-weight: var(--mf-weight-semibold);
	}

	.output-review-row small {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-xs);
		line-height: var(--mf-leading-snug);
		overflow-wrap: anywhere;
	}

	.output-pipeline {
		display: grid;
		gap: var(--mf-space-3);
		grid-template-columns: repeat(4, minmax(0, 1fr));
	}

	.output-pipeline__lane {
		background: var(--mf-bg-panel-2);
		border: var(--mf-border-muted);
		display: grid;
		gap: var(--mf-space-3);
		min-width: 0;
		padding: var(--mf-space-4);
	}

	.output-pipeline__lane--current {
		border-color: color-mix(in srgb, var(--mf-ready-fg) 80%, transparent);
		box-shadow: inset 3px 0 0 var(--mf-ready-fg);
	}

	.output-pipeline__lane--active {
		border-color: color-mix(in srgb, var(--mf-active-fg) 70%, transparent);
		box-shadow: inset 3px 0 0 var(--mf-active-fg);
	}

	.output-pipeline__lane--fail {
		border-color: color-mix(in srgb, var(--mf-fail-fg) 70%, transparent);
		box-shadow: inset 3px 0 0 var(--mf-fail-fg);
	}

	.output-pipeline__lane--wait {
		border-color: color-mix(in srgb, var(--mf-wait-fg) 70%, transparent);
		box-shadow: inset 3px 0 0 var(--mf-wait-fg);
	}

	.output-pipeline__lane-head {
		display: grid;
		gap: var(--mf-space-1);
		min-width: 0;
	}

	.output-pipeline__lane-head strong,
	.output-pipeline__lane p {
		font-size: var(--mf-text-sm);
		overflow-wrap: anywhere;
	}

	.output-pipeline__lane-head span,
	.output-pipeline__lane small {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-xs);
		line-height: var(--mf-leading-snug);
		overflow-wrap: anywhere;
	}

	.output-pipeline__lane p {
		font-family: var(--mf-font-mono), monospace;
		margin: 0;
	}

	.review-media-grid {
		display: grid;
		gap: var(--mf-space-4);
		grid-template-columns: repeat(2, minmax(0, 1fr));
	}

	.review-media-tile {
		background: var(--mf-bg-input);
		border: var(--mf-border-muted);
		color: var(--mf-fg-primary);
		cursor: pointer;
		display: grid;
		gap: var(--mf-space-3);
		font: inherit;
		min-width: 0;
		padding: var(--mf-space-3);
		text-align: left;
		text-decoration: none;
	}

	.review-media-tile:hover {
		border-color: var(--mf-active-line);
	}

	.review-media-tile img {
		aspect-ratio: 16 / 9;
		background: var(--mf-bg-base);
		border: var(--mf-border-muted);
		object-fit: contain;
		width: 100%;
	}

	.review-media-tile span {
		font-size: var(--mf-text-xs);
		font-weight: var(--mf-weight-semibold);
		overflow-wrap: anywhere;
	}

	.review-media-tile--audio {
		grid-column: 1 / -1;
	}

	.action-form {
		margin: 0;
	}

	.control {
		align-items: center;
		background: var(--mf-bg-panel-2);
		border: var(--mf-border-strong);
		border-radius: var(--mf-radius-1);
		color: var(--mf-fg-primary);
		display: inline-flex;
		font-weight: var(--mf-weight-semibold);
		justify-content: center;
		min-height: var(--mf-control-lg);
		padding: 0 var(--mf-space-5);
		white-space: nowrap;
	}

	.control:hover {
		background: var(--mf-bg-raised);
	}

	.control:disabled {
		background: var(--mf-bg-input);
		border-color: var(--mf-line-muted);
		color: var(--mf-fg-quaternary);
	}

	.control--primary {
		background: var(--mf-active-solid);
		border-color: var(--mf-active-solid-hi);
		color: var(--mf-fg-on-accent);
	}

	.control--primary:disabled {
		background: var(--mf-active-bg);
		border-color: var(--mf-active-line);
		color: var(--mf-fg-tertiary);
	}

	.bench {
		display: grid;
		gap: var(--mf-space-5);
		grid-template-columns: minmax(0, 1fr) minmax(280px, 0.65fr);
		padding: var(--mf-space-5);
	}

	.bench__thread {
		background: var(--mf-bg-panel-2);
		border: var(--mf-border-muted);
		display: grid;
		min-width: 0;
	}

	.bench__thread summary {
		align-items: center;
		cursor: pointer;
		display: flex;
		gap: var(--mf-space-4);
		list-style-position: inside;
		min-height: var(--mf-row-comfy);
		padding: 0 var(--mf-space-5);
	}

	.bench__thread summary strong {
		font-size: var(--mf-text-sm);
		font-weight: var(--mf-weight-semibold);
	}

	.bench__thread summary span {
		color: var(--mf-fg-tertiary);
		font-family: var(--mf-font-mono), monospace;
		font-size: var(--mf-text-xs);
		margin-left: auto;
	}

	.bench__messages {
		display: grid;
		gap: var(--mf-space-4);
		padding: var(--mf-space-5);
		padding-top: 0;
	}

	.bench-next-action {
		align-items: center;
		background: var(--mf-ready-bg);
		border-top: var(--mf-border);
		display: grid;
		gap: var(--mf-space-4);
		grid-template-columns: minmax(0, 1fr) auto;
		padding: var(--mf-space-4) var(--mf-space-5);
	}

	.bench-next-action span {
		color: var(--mf-ready-fg);
		display: block;
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-semibold);
		letter-spacing: 0.08em;
		text-transform: uppercase;
	}

	.bench-next-action strong {
		font-size: var(--mf-text-sm);
		font-weight: var(--mf-weight-semibold);
	}

	.bench-message {
		background: var(--mf-bg-panel-2);
		border: var(--mf-border);
		border-left: 2px solid var(--mf-idle-fg);
		display: grid;
		gap: var(--mf-space-3);
		padding: var(--mf-space-5);
	}

	.bench-message--operator {
		border-left-color: var(--mf-ready-fg);
	}

	.bench-message--bench {
		border-left-color: var(--mf-active-fg);
	}

	.bench-message--fail {
		border-left-color: var(--mf-fail-fg);
	}

	.bench-message--system {
		border-left-color: var(--mf-wait-fg);
	}

	.bench-message--tone-ready {
		background: var(--mf-ready-bg);
	}

	.bench-message--tone-active {
		background: var(--mf-active-bg);
	}

	.bench-message--tone-wait {
		background: var(--mf-wait-bg);
	}

	.bench-message--tone-fail {
		background: var(--mf-fail-bg);
	}

	.bench-message header {
		align-items: center;
		display: flex;
		gap: var(--mf-space-4);
		justify-content: space-between;
		min-width: 0;
	}

	.bench-message span,
	.bench__composer label span {
		color: var(--mf-fg-tertiary);
		display: block;
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-semibold);
		letter-spacing: 0.08em;
		text-transform: uppercase;
	}

	.bench-message small {
		color: var(--mf-fg-tertiary);
		font-family: var(--mf-font-mono), monospace;
		font-size: var(--mf-text-2xs);
		overflow-wrap: anywhere;
	}

	.bench-message strong {
		color: var(--mf-fg-primary);
		font-size: var(--mf-text-sm);
		font-weight: var(--mf-weight-semibold);
	}

	.bench-message p {
		font-size: var(--mf-text-sm);
	}

	.bench__composer {
		background: var(--mf-bg-input);
		border: var(--mf-border-strong);
		display: grid;
		gap: var(--mf-space-4);
		padding: var(--mf-space-5);
	}

	.bench__composer textarea,
	.bench__composer select {
		background: var(--mf-bg-base);
		border: var(--mf-border);
		border-radius: var(--mf-radius-1);
		color: var(--mf-fg-primary);
		min-width: 0;
		width: 100%;
	}

	.bench__composer textarea {
		line-height: var(--mf-leading-normal);
		padding: var(--mf-space-4);
		resize: vertical;
	}

	.bench__composer select {
		min-height: var(--mf-control-md);
		padding: 0 var(--mf-space-4);
	}

	.bench__controls {
		align-items: end;
		display: grid;
		gap: var(--mf-space-4);
		grid-template-columns: minmax(0, 1fr) auto;
	}

	.bench__status {
		border-left: 2px solid var(--mf-idle-fg);
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-xs);
		line-height: var(--mf-leading-normal);
		padding-left: var(--mf-space-3);
	}

	.bench__status--ready {
		border-left-color: var(--mf-ready-fg);
		color: var(--mf-ready-fg);
	}

	.bench__status--fail {
		border-left-color: var(--mf-fail-fg);
		color: var(--mf-fail-fg);
	}

	.support-grid {
		display: grid;
		gap: var(--mf-space-5);
		grid-template-columns: repeat(3, minmax(0, 1fr));
	}

	.support-grid--single {
		grid-template-columns: minmax(0, 1fr);
	}

	.evidence-grid {
		display: grid;
		gap: var(--mf-space-5);
		grid-template-columns: minmax(0, 1.1fr) minmax(0, 0.9fr);
	}

	.evidence-grid--sample-only {
		grid-template-columns: minmax(0, 1fr);
	}

	.sample-card {
		display: grid;
		gap: var(--mf-space-5);
		padding: var(--mf-space-5);
	}

	.sample-card--source-only {
		align-items: start;
		grid-template-columns: minmax(0, 1fr) minmax(320px, 0.42fr);
	}

	.sample-card--source-only .sample-facts {
		grid-column: 1 / -1;
	}

	.sample-frame {
		align-content: center;
		aspect-ratio: 16 / 7;
		background:
			linear-gradient(var(--mf-line-muted) 1px, transparent 1px),
			linear-gradient(90deg, var(--mf-line-muted) 1px, transparent 1px), var(--mf-bg-input);
		background-size: 36px 36px;
		border: var(--mf-border-strong);
		color: var(--mf-fg-tertiary);
		display: grid;
		justify-items: center;
		padding: var(--mf-space-6);
		text-align: center;
	}

	.sample-frame--source-only {
		aspect-ratio: auto;
		min-height: var(--mf-row-comfy);
		justify-items: start;
		text-align: left;
	}

	.sample-card__guidance {
		align-content: start;
		background: var(--mf-bg-panel-2);
		border: var(--mf-border-muted);
		display: grid;
		gap: var(--mf-space-4);
		min-width: 0;
		padding: var(--mf-space-5);
	}

	.sample-card__guidance strong {
		font-size: var(--mf-text-sm);
		font-weight: var(--mf-weight-semibold);
	}

	.sample-card__guidance p {
		color: var(--mf-fg-secondary);
		font-size: var(--mf-text-sm);
		line-height: var(--mf-leading-normal);
	}

	.sample-card__guidance .control {
		justify-self: start;
	}

	.sample-frame strong {
		color: var(--mf-fg-primary);
		font-family: var(--mf-font-mono), monospace;
		font-size: var(--mf-text-sm);
		font-weight: var(--mf-weight-medium);
		margin-top: var(--mf-space-3);
		overflow-wrap: anywhere;
	}

	.host-list,
	.history-list,
	.step-list {
		display: grid;
		gap: var(--mf-space-4);
		padding: var(--mf-space-5);
	}

	.host-row {
		align-items: start;
		border-bottom: var(--mf-border-muted);
		display: grid;
		gap: var(--mf-space-4);
		grid-template-columns: auto minmax(0, 1fr);
		padding-bottom: var(--mf-space-4);
	}

	.history-list strong {
		display: block;
		font-size: var(--mf-text-sm);
		font-weight: var(--mf-weight-semibold);
		overflow-wrap: anywhere;
	}

	.host-row span,
	.history-list span {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-xs);
	}

	.step-row {
		border-left: 2px solid var(--mf-line-strong);
		display: grid;
		gap: var(--mf-space-2);
		padding: var(--mf-space-3) var(--mf-space-4);
	}

	.step-row--current {
		background: var(--mf-active-bg);
	}

	.step-row--active {
		border-left-color: var(--mf-active-fg);
	}

	.step-row--ready {
		border-left-color: var(--mf-ready-fg);
	}

	.step-row--wait {
		border-left-color: var(--mf-wait-fg);
	}

	.step-row--fail {
		border-left-color: var(--mf-fail-fg);
	}

	.step-row > span {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-semibold);
		letter-spacing: 0.08em;
		text-transform: uppercase;
	}

	.step-row strong {
		font-size: var(--mf-text-xs);
		font-weight: var(--mf-weight-semibold);
	}

	.step-row small {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-xs);
	}

	.empty-note {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-sm);
		padding: var(--mf-space-5);
	}

	.empty-note--compact {
		padding: 0;
	}

	.table-wrap {
		overflow: auto;
	}

	table {
		border-collapse: collapse;
		min-width: 720px;
		width: 100%;
	}

	th,
	td {
		border-bottom: var(--mf-border-muted);
		font-size: var(--mf-text-xs);
		padding: 0 var(--mf-space-5);
		text-align: left;
		vertical-align: middle;
	}

	th {
		background: var(--mf-bg-strip);
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-semibold);
		height: var(--mf-row-compact);
		letter-spacing: 0.08em;
		position: sticky;
		text-transform: uppercase;
		top: 0;
	}

	td {
		color: var(--mf-fg-secondary);
		height: var(--mf-row-default);
	}

	td:nth-child(3),
	td:nth-child(4) {
		font-family: var(--mf-font-mono), monospace;
	}

	tr.changed td:first-child {
		box-shadow: inset 2px 0 0 var(--mf-active-fg);
	}

	.kv {
		column-gap: var(--mf-space-5);
		display: grid;
		grid-template-columns: minmax(88px, auto) minmax(0, 1fr);
		padding: var(--mf-space-5);
		row-gap: var(--mf-space-4);
	}

	.kv dt {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-semibold);
		letter-spacing: 0;
		text-transform: uppercase;
	}

	.kv dd {
		color: var(--mf-fg-primary);
		font-family: var(--mf-font-mono), monospace;
		font-size: var(--mf-text-xs);
		overflow-wrap: anywhere;
	}

	.kv--compact {
		grid-template-columns: minmax(64px, auto) minmax(0, 1fr);
		padding: var(--mf-space-5);
		row-gap: var(--mf-space-3);
	}

	.kv--compact dd {
		min-width: 0;
		white-space: normal;
	}

	.history-list--compact {
		padding: var(--mf-space-5);
	}

	.technical-details {
		background: var(--mf-bg-panel);
		border: var(--mf-border);
	}

	.technical-details summary {
		align-items: center;
		cursor: pointer;
		display: flex;
		gap: var(--mf-space-4);
		list-style-position: inside;
		min-height: var(--mf-row-comfy);
		padding: 0 var(--mf-space-5);
	}

	.technical-details summary strong {
		font-size: var(--mf-text-sm);
		font-weight: var(--mf-weight-semibold);
	}

	.technical-details summary span {
		color: var(--mf-fg-tertiary);
		font-family: var(--mf-font-mono), monospace;
		font-size: var(--mf-text-xs);
		margin-left: auto;
	}

	.technical-details :global(.panel) {
		border-left: 0;
		border-right: 0;
		border-bottom: 0;
	}

	@media (max-width: 1180px) {
		.studio {
			grid-template-columns: minmax(190px, 240px) minmax(0, 1fr);
		}

		.basic-run__steps {
			grid-template-columns: repeat(7, minmax(0, 1fr));
		}

		.basic-run__step small {
			display: none;
		}

		.decision__facts {
			display: grid;
			grid-template-columns: repeat(3, minmax(0, 1fr));
		}
	}

	@media (max-width: 820px) {
		.studio {
			grid-template-columns: 1fr;
		}

		.studio__main {
			grid-column: auto;
			grid-row: auto;
		}

		.studio__left {
			border-right: 0;
			grid-column: auto;
			grid-row: auto;
		}

		.folder-header,
		.active-operation,
		.workflow-strip,
		.basic-run__header,
		.decision,
		.bench,
		.review-workspace__header,
		.support-grid,
		.evidence-grid {
			grid-template-columns: 1fr;
		}

		.basic-run__steps {
			grid-template-columns: repeat(7, minmax(84px, 1fr));
			overflow-x: auto;
		}

		.output-review-table__head {
			display: none;
		}

		.output-review-row {
			grid-template-columns: 1fr;
			gap: var(--mf-space-2);
		}

		.review-media-grid {
			grid-template-columns: 1fr;
		}

		.folder-header__facts,
		.decision__facts,
		.sample-facts,
		.decision__actions {
			flex-wrap: wrap;
		}

		.decision .decision__facts {
			display: grid;
			grid-template-columns: minmax(0, 1fr);
		}
	}

	@media (max-width: 680px) {
		.table-wrap {
			overflow: visible;
		}

		.policy-table {
			min-width: 0;
		}

		.policy-table thead {
			display: none;
		}

		.policy-table tbody,
		.policy-table tr,
		.policy-table td {
			display: block;
			width: 100%;
		}

		.policy-table tr {
			border-bottom: var(--mf-border-muted);
			display: grid;
			gap: var(--mf-space-3);
			grid-template-columns: minmax(0, 1fr);
			padding: var(--mf-space-4);
		}

		.policy-table td {
			border-bottom: 0;
			height: auto;
			min-width: 0;
			padding: 0;
		}

		.policy-table td:not(:only-child) {
			display: grid;
			gap: var(--mf-space-2);
		}

		.policy-table td:not(:only-child)::before {
			color: var(--mf-fg-tertiary);
			font-family: var(--mf-font-sans), sans-serif;
			font-size: var(--mf-text-2xs);
			font-weight: var(--mf-weight-semibold);
			letter-spacing: 0.08em;
			text-transform: uppercase;
		}

		.policy-table td:nth-child(1)::before {
			content: 'Area';
		}

		.policy-table td:nth-child(2)::before {
			content: 'Setting';
		}

		.policy-table td:nth-child(3)::before {
			content: 'Current';
		}

		.policy-table td:nth-child(4)::before {
			content: 'Plan';
		}
	}
</style>
