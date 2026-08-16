<script lang="ts">
	import { resolve } from '$app/paths';

	import { apiDownloadHref, postJson } from '$lib/api/client';
	import type {
		FolderBenchConfirmResponse,
		FolderBenchPreviewResponse,
		FolderPayload,
		FolderStatusPayload,
		HostsPayload,
		MovieMember
	} from '$lib/api/types';
	import { folderRoutePath, folderRoutePrefix } from '$lib/folder-display';
	import {
		noteAfterPrepareAgain,
		noteAfterPreview,
		noteAfterProposalHydration,
		prepareAgainRequest,
		proposalRecoveryView
	} from '$lib/folders/studio';
	import { movieWorkflowIsComplete, movieWorkflowLabel } from '$lib/movies/library';
	import {
		canRetrySampleJob,
		formatMovieBytes,
		movieCurrentWorkView,
		movieGoalContractView,
		movieGoalFactsView,
		movieReviewStatusLabel,
		movieSizeCapBlockView,
		parentSampleAppliesToExactItem
	} from './movie-studio-view';
	import StateBadge from './StateBadge.svelte';
	import WorkstationPanel from './WorkstationPanel.svelte';

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
		onMutate?: (targetPrefix?: string) => Promise<void>;
		loadError?: string | null;
	} = $props();

	let note = $state('');
	let selectedHostKey = $state('');
	let pendingAction = $state('');
	let actionMessage = $state('');
	let actionError = $state('');
	let actionNeedsAttention = $state(false);
	let noteInput = $state<HTMLTextAreaElement>();
	let goalEditor = $state<HTMLDetailsElement>();
	let noteHasNewerText = $state(false);
	let hydratedFolderPrefix = $state('');
	let hydratedProposalId = $state('');

	const context = $derived(folder.movie_context ?? null);
	const title = $derived(
		context?.title ?? folder.media_scope.title ?? folder.prefix.split('/').at(-1) ?? 'Movie'
	);
	const activeMember = $derived(
		context?.active_member ??
			context?.members.find((member) => member.prefix === folder.prefix) ??
			(context?.scope_mode === 'single_file' ? context.members[0] : undefined)
	);
	const workflow = $derived(
		status.workflow_state ?? folder.workflow_state ?? context?.workflow_state ?? null
	);
	const isComplete = $derived(movieWorkflowIsComplete(workflow));
	const calibration = $derived(asRecord(folder.calibration));
	const pendingProposal = $derived(asRecord(folder.pending_proposal));
	const hasPendingProposal = $derived(Object.keys(pendingProposal).length > 0);
	const pendingProposalCanQueue = $derived(pendingProposal.can_queue === true);
	const pendingProposalRecovery = $derived(proposalRecoveryView(pendingProposal));
	const pendingProposalIsStale = $derived(pendingProposalRecovery?.cause === 'stale_plan');
	const reviewGate = $derived(asRecord(folder.review_gate));
	const exactCalibrationJob = $derived(status.exact_calibration_job ?? folder.calibration_job);
	const overlappingCalibrationJob = $derived(status.calibration_job);
	const inheritedParentSample = $derived(
		folder.media_scope.match === 'exact_item' &&
			parentSampleAppliesToExactItem(folder.prefix, exactCalibrationJob, overlappingCalibrationJob)
	);
	const calibrationJob = $derived(
		asRecord(exactCalibrationJob ?? (inheritedParentSample ? overlappingCalibrationJob : null))
	);
	const sampleWorkStatus = $derived(asText(calibrationJob.status));
	const sampleWorkActive = $derived(['queued', 'starting', 'running'].includes(sampleWorkStatus));
	const retryableSampleJob = $derived(asRecord(status.retryable_sample_job));
	const canRetrySample = $derived(canRetrySampleJob(retryableSampleJob.job_id, hasPendingProposal));
	const encodeJob = $derived(folder.encode_job ?? null);
	const availableEncodeWorkerCount = $derived(
		hosts.hosts.filter((host) => host.available && host.capabilities.includes('encode_queue'))
			.length
	);
	const currentWork = $derived(
		isComplete
			? null
			: movieCurrentWorkView(
					encodeJob,
					folder.encode_queue_state ?? folder.encode_queue?.state,
					availableEncodeWorkerCount
				)
	);
	const sampleItem = $derived(asRecord(folder.sample_item));
	const hostOptions = $derived(
		(folder.sample_host_options ?? [])
			.map((host) => asRecord(host))
			.filter((host) => asText(host.key) && host.available !== false)
	);
	const streamBudgetLedger = $derived(folder.stream_budget_ledger);
	const movieGoalFacts = $derived(
		movieGoalFactsView(
			streamBudgetLedger?.source.duration_seconds ?? activeMember?.duration_seconds,
			streamBudgetLedger?.source.source_size_bytes ?? activeMember?.size_bytes,
			streamBudgetLedger?.size_goal ?? folder.resolved_operator_intent?.size_goal
		)
	);
	const movieGoalContract = $derived(
		movieGoalContractView(
			folder.resolved_operator_intent,
			streamBudgetLedger,
			folder.sample_item,
			folder.quality_risk,
			folder.resolved_metric
		)
	);
	const isBrowseOnly = $derived(context?.availability === 'browse_only');
	const isWorkflowBlocked = $derived(!isComplete && workflow?.primary_lane === 'blocked');
	const sizeCapBlock = $derived(movieSizeCapBlockView(workflow, streamBudgetLedger));
	const isSizeCapBlock = $derived(isWorkflowBlocked && sizeCapBlock.blocked);
	const canChangeGoals = $derived(
		!isBrowseOnly && !isComplete && !isSizeCapBlock && !sampleWorkActive && !currentWork
	);
	const isBusy = $derived(Boolean(pendingAction));
	const conflicts = $derived(context?.promotion_conflicts ?? []);
	const reviewReady = $derived(
		Boolean(
			calibration.review_media_ready ||
			calibration.browser_review_ready ||
			calibration.compare_clips
		)
	);
	const reviewPackHref = $derived(
		apiDownloadHref(`/api/folders/${folderRoutePrefix(folder.prefix)}/review-compare/download`)
	);
	const exactScope = $derived(folder.media_scope.match === 'exact_item');
	const parentTitlePrefix = $derived(asText(folder.media_scope.parent?.prefix));
	const parentTitleHref = $derived(
		parentTitlePrefix ? resolve(folderRoutePath(parentTitlePrefix)) : resolve('/movies')
	);
	const scopeNoun = $derived(exactScope ? 'movie file' : 'movie title');
	const scopeDisplay = $derived(exactScope ? 'Only this file' : 'The whole title');
	const memberCount = $derived(context?.members.length ?? 0);
	const memberCountLabel = $derived(`${memberCount} ${memberCount === 1 ? 'file' : 'files'}`);
	const needsReviewSample = $derived(
		!isComplete &&
			workflow?.primary_lane === 'encode' &&
			reviewGate.status !== 'accepted' &&
			!reviewReady &&
			!pendingProposalCanQueue &&
			!sampleWorkActive
	);
	const workflowDisplayLabel = $derived(
		isComplete
			? 'Finished'
			: currentWork
				? currentWork.label
				: sampleWorkActive
					? sampleWorkStatus === 'queued'
						? 'Sample queued'
						: 'Sampling now'
					: canRetrySample
						? 'Sample needs retry'
						: pendingProposalIsStale
							? 'Sample plan is out of date'
							: pendingProposalCanQueue
								? 'Sample plan ready'
								: needsReviewSample
									? 'Needs a review sample'
									: movieWorkflowLabel({
											workflow_state: workflow,
											promotion_conflicts: conflicts,
											details_loading: folderPending,
											availability: context?.availability ?? 'production'
										})
	);

	$effect(() => {
		const folderHostKey = String(folder.sample_host_key ?? '').trim();
		if (!selectedHostKey || !hostOptions.some((host) => asText(host.key) === selectedHostKey)) {
			selectedHostKey = hostOptions.some((host) => asText(host.key) === folderHostKey)
				? folderHostKey
				: asText(hostOptions[0]?.key);
		}
	});

	$effect(() => {
		if (folder.prefix !== hydratedFolderPrefix) {
			hydratedFolderPrefix = folder.prefix;
			hydratedProposalId = '';
			note = '';
			noteHasNewerText = false;
		}
		const proposalId = asText(pendingProposal.proposal_id);
		if (!proposalId || proposalId === hydratedProposalId) return;
		hydratedProposalId = proposalId;
		note = noteAfterProposalHydration(note, noteHasNewerText, pendingProposal);
	});

	async function prepareSample() {
		if (isBrowseOnly || isBusy) return;
		await runAction('prepare-sample', async () => {
			const response = await postJson<FolderBenchPreviewResponse>(
				`/api/folders/${folderRoutePrefix(folder.prefix)}/ai-tune/preview`,
				{
					note:
						note.trim() ||
						'Prepare a representative movie sample using the current size and quality policy.',
					host_key: selectedHostKey
				}
			);
			if (!response.ok)
				throw new Error(response.message || 'The movie target is not ready for sampling.');
			note = noteAfterPreview(note, response.proposal);
			if (response.proposal?.can_queue === true) noteHasNewerText = false;
			return {
				message: response.message || 'Sample plan ready. Review it before starting work.',
				attention: response.proposal != null && response.proposal.can_queue !== true
			};
		});
	}

	async function prepareAgain() {
		if (isBrowseOnly || isBusy) return;
		await runAction('prepare-again', async () => {
			const request = prepareAgainRequest(pendingProposal);
			const hostKey = request.hostKey || selectedHostKey;
			if (!hostKey) {
				throw new Error('The saved request is unavailable. Edit the request and prepare it again.');
			}
			const response = await postJson<FolderBenchPreviewResponse>(
				`/api/folders/${folderRoutePrefix(folder.prefix)}/ai-tune/preview`,
				{ note: request.note, host_key: hostKey }
			);
			if (!response.ok)
				throw new Error(response.message || 'Mediaforce could not prepare the sample.');
			const nextNote = noteAfterPrepareAgain(
				note,
				request.note,
				noteHasNewerText,
				response.proposal
			);
			const keptNewerText = response.proposal?.can_queue === true && nextNote !== '';
			note = nextNote;
			if (response.proposal?.can_queue === true) noteHasNewerText = keptNewerText;
			return {
				message: response.message || 'Mediaforce prepared the request again.',
				attention: response.proposal != null && response.proposal.can_queue !== true
			};
		});
	}

	function editRequest() {
		if (isBrowseOnly) return;
		if (goalEditor) goalEditor.open = true;
		requestAnimationFrame(() => {
			noteInput?.focus();
			noteInput?.scrollIntoView({ behavior: 'smooth', block: 'center' });
		});
	}

	async function startSample() {
		if (isBrowseOnly || isBusy || !pendingProposalCanQueue) return;
		await runAction('start-sample', async () => {
			const response = await postJson<FolderBenchConfirmResponse>(
				`/api/folders/${folderRoutePrefix(folder.prefix)}/ai-tune/confirm`,
				{ proposal_id: asText(pendingProposal.proposal_id) }
			);
			if (!response.ok) throw new Error(response.message || 'The movie sample could not start.');
			return response.message || 'Sample run queued.';
		});
	}

	async function retrySample() {
		if (isBrowseOnly || isBusy) return;
		await runAction('retry-sample', async () => {
			const response = await postJson<FolderBenchConfirmResponse>(
				`/api/folders/${folderRoutePrefix(folder.prefix)}/ai-tune/confirm`,
				{ proposal_id: '' }
			);
			if (!response.ok) throw new Error(response.message || 'The movie sample could not restart.');
			return response.message || 'Sample retry queued.';
		});
	}

	async function approveAndQueue() {
		if (isBrowseOnly || isBusy) return;
		await runAction('approve-queue', async () => {
			const response = await postJson<{ ok: boolean; message?: string }>(
				`/api/folders/${folderRoutePrefix(folder.prefix)}/save-profile`,
				{
					confirm_high_impact: true,
					confirm_size_tradeoff: true,
					reviewed_draft_hash: asText(calibration.draft_hash)
				}
			);
			if (!response.ok) throw new Error(response.message || 'The movie work could not be queued.');
			return response.message || `Approved the sample and queued this ${scopeNoun}.`;
		});
	}

	async function queueApproved() {
		if (isBrowseOnly || isBusy) return;
		await runAction('queue-title', async () => {
			const response = await postJson<{ ok: boolean; message?: string }>(
				`/api/folders/${folderRoutePrefix(folder.prefix)}/queue-encode`,
				{ notes: '', bypass_schedule: false }
			);
			if (!response.ok) throw new Error(response.message || 'The movie work could not be queued.');
			return response.message || `Queued this ${scopeNoun}.`;
		});
	}

	async function validateOutputs() {
		await folderAction('validate-outputs', 'Checked the compressed movie file.');
	}

	async function promoteOutputs() {
		if (conflicts.length) return;
		await folderAction(
			'promote-outputs',
			'Installed the checked replacement and kept a backup of the original.'
		);
	}

	async function retryEncode() {
		if (isBrowseOnly || isBusy) return;
		await runAction('retry-encode', async () => {
			const response = await postJson<{ ok: boolean; message?: string }>(
				'/api/encode-queue/retry-prefix',
				{ prefix: folder.prefix }
			);
			return response.message || `Queued a retry for this ${scopeNoun}.`;
		});
	}

	async function stopSample() {
		if (isBusy) return;
		await runAction('stop-sample', async () => {
			const response = await postJson<{ ok?: boolean; message?: string }>(
				'/api/calibration-queue/stop',
				{}
			);
			return response.message || 'Stopped queued and running sample work.';
		});
	}

	async function folderAction(endpoint: 'validate-outputs' | 'promote-outputs', fallback: string) {
		if (isBrowseOnly || isBusy) return;
		await runAction(endpoint, async () => {
			const response = await postJson<{
				ok: boolean;
				message?: string;
				conflicts?: Array<Record<string, unknown>>;
				target_prefix?: string;
			}>(`/api/folders/${folderRoutePrefix(folder.prefix)}/${endpoint}`, {});
			if (!response.ok) throw new Error(response.message || 'The movie action could not run.');
			return {
				message: response.message || fallback,
				targetPrefix: endpoint === 'promote-outputs' ? response.target_prefix : undefined
			};
		});
	}

	type ActionResult = string | { message: string; targetPrefix?: string; attention?: boolean };

	async function runAction(action: string, operation: () => Promise<ActionResult>) {
		pendingAction = action;
		actionMessage = '';
		actionError = '';
		actionNeedsAttention = false;
		let actionCompleted = false;
		try {
			const result = await operation();
			actionMessage = typeof result === 'string' ? result : result.message;
			actionNeedsAttention = typeof result === 'string' ? false : result.attention === true;
			actionCompleted = true;
			await onMutate(typeof result === 'string' ? undefined : result.targetPrefix);
		} catch (error) {
			const message = error instanceof Error ? error.message : 'Studio could not refresh.';
			actionError = actionCompleted
				? `The movie action completed, but Studio could not refresh. ${message}`
				: message;
		} finally {
			pendingAction = '';
		}
	}

	function primaryAction():
		| 'prepare'
		| 'prepare-again'
		| 'start'
		| 'monitor-sample'
		| 'retry-sample'
		| 'review-title-sample'
		| 'queue'
		| 'validate'
		| 'promote'
		| 'retry'
		| 'current-work'
		| 'complete'
		| 'none' {
		if (isComplete) return 'complete';
		if (isBrowseOnly) return 'none';
		if (currentWork) return 'current-work';
		if (sampleWorkActive) return 'monitor-sample';
		if (canRetrySample) return 'retry-sample';
		const encodeStatus = String(encodeJob?.status ?? '');
		if (['failed', 'stopped', 'needs_attention'].includes(encodeStatus)) return 'retry';
		if (workflow?.next_action.kind === 'validate_outputs') return 'validate';
		if (workflow?.next_action.kind === 'promote_outputs') return 'promote';
		if (pendingProposalIsStale) return 'prepare-again';
		if (hasPendingProposal && pendingProposalCanQueue) return 'start';
		if (inheritedParentSample && asText(calibrationJob.status) === 'completed') {
			return 'review-title-sample';
		}
		if (reviewGate.status === 'accepted') return 'queue';
		if (reviewReady) return 'queue';
		if (isWorkflowBlocked) return 'none';
		return 'prepare';
	}

	function badgeTone(): 'active' | 'ready' | 'wait' | 'fail' | 'idle' {
		if (isComplete) return 'ready';
		if (currentWork) return currentWork.tone;
		if (canRetrySample || conflicts.length || workflow?.tone === 'attention') return 'fail';
		if (pendingProposalIsStale) return 'wait';
		if (workflow?.tone === 'active') return 'active';
		if (workflow?.tone === 'ready' || workflow?.tone === 'success') return 'ready';
		if (workflow?.state === 'explicit_selection_required') return 'wait';
		return 'idle';
	}

	function workflowSummary(): string {
		const fileCount = exactScope ? 1 : (context?.included_item_count ?? context?.item_count ?? 1);
		const fileWord = fileCount === 1 ? 'file' : 'files';
		if (isComplete) return 'This movie is finished.';
		if (conflicts.length) {
			return 'A file already exists where this movie would be placed. Review the conflict before replacing anything.';
		}
		if (isBrowseOnly || workflow?.state === 'browse_only') {
			return 'You can review these files, but Mediaforce cannot change this library.';
		}
		if (workflow?.state === 'explicit_selection_required') {
			const explicitCount =
				context?.members.filter((member) => !member.included_by_default).length ?? 0;
			return `${explicitCount} ${explicitCount === 1 ? 'file needs' : 'files need'} you to choose ${explicitCount === 1 ? 'it' : 'them'} individually.`;
		}
		if (canRetrySample) {
			return 'The review sample did not finish.';
		}
		if (pendingProposalIsStale) {
			return pendingProposalRecovery?.headline || 'Sample plan is out of date';
		}
		switch (workflow?.primary_lane) {
			case 'promote':
				return `${fileCount} checked ${fileWord} can now replace the current library copy.`;
			case 'validate':
				return `${fileCount} compressed ${fileWord} ${fileCount === 1 ? 'needs' : 'need'} a final safety check.`;
			case 'encode':
				if (sampleWorkActive) {
					return sampleWorkStatus === 'queued'
						? 'The review sample is queued.'
						: 'Mediaforce is preparing the review sample now.';
				}
				if (pendingProposalCanQueue) {
					return `${fileCount} ${fileWord} ${fileCount === 1 ? 'is' : 'are'} ready for a review sample.`;
				}
				if (needsReviewSample) {
					return `${fileCount} ${fileWord} ${fileCount === 1 ? 'needs' : 'need'} a review sample before compressing.`;
				}
				return `${fileCount} ${fileWord} ${fileCount === 1 ? 'is' : 'are'} ready to compress.`;
			case 'processing':
				return 'Mediaforce is working on this movie now.';
			case 'attention':
				return 'This movie needs review before work can continue.';
			case 'mixed': {
				const lanes: Array<[number, string]> = [
					[workflow.lane_counts.encode ?? 0, 'ready to compress'],
					[workflow.lane_counts.validate ?? 0, 'ready to check'],
					[workflow.lane_counts.promote ?? 0, 'ready to replace']
				];
				const parts = lanes
					.filter(([laneCount]) => laneCount > 0)
					.map(([laneCount, label]) => `${laneCount} ${label}`);
				return parts.length
					? `${parts.join(', ')} across this title.`
					: 'This title has several steps ready for review.';
			}
			case 'complete':
				return 'This movie is finished.';
			case 'blocked':
				if (isSizeCapBlock) return sizeCapBlock.headline;
				return 'Mediaforce cannot start this movie until the issue below is resolved.';
			default:
				return folderPending
					? 'Mediaforce is checking what this movie needs next.'
					: 'No work is waiting for this movie.';
		}
	}

	function sampleFailureDetail(): string {
		const error = asText(retryableSampleJob.error);
		if (!error) {
			return 'Mediaforce stopped before review media was ready.';
		}
		if (error.toLowerCase().includes('containment cleanup is unproven')) {
			return 'Mediaforce stopped because it could not verify that every sample process was cleaned up safely.';
		}
		return error;
	}

	function memberStatusLabel(member: MovieMember): string {
		return (
			{
				discovered: 'Not started',
				planned: 'Ready to compress',
				encoding: 'Compressing',
				encoded: 'Ready to check',
				validated: 'Ready to replace',
				promoted: 'Finished',
				missing: 'File missing'
			}[member.status] ?? 'Needs review'
		);
	}

	function reviewStatusLabel(): string {
		return movieReviewStatusLabel(reviewGate.status, inheritedParentSample);
	}

	function memberRole(member: MovieMember): string {
		if (member.edition_label) return member.edition_label;
		if (member.role === 'feature') return 'Main movie';
		if (member.role === 'extra') return member.extra_category ?? 'Extra';
		return 'Uncertain file';
	}

	function asRecord(value: unknown): Record<string, unknown> {
		return value && typeof value === 'object' && !Array.isArray(value)
			? (value as Record<string, unknown>)
			: {};
	}

	function asText(value: unknown): string {
		return typeof value === 'string' ? value.trim() : '';
	}

	function downloadReviewPack() {
		window.location.assign(reviewPackHref);
	}
</script>

{#snippet goalContract(label: string, showChangeAction: boolean)}
	<section class="goal-contract" aria-label={label}>
		<div class="goal-contract__heading">
			<div>
				<span>{label}</span>
				<strong>Resolved movie goals</strong>
			</div>
			{#if showChangeAction && canChangeGoals && movieGoalContract.status === 'ready'}
				<button class="secondary" type="button" onclick={editRequest}>Change goals</button>
			{/if}
		</div>
		{#if movieGoalContract.status === 'resolving'}
			<div class="goal-contract__resolving" role="status">
				<strong>Resolving movie goals</strong>
				<span>Mediaforce is assembling the size, quality, stream, and review contract.</span>
			</div>
		{:else}
			<dl class="goal-contract__rows">
				{#each movieGoalContract.rows as row (row.label)}
					<div class:goal-contract__row--attention={row.tone === 'attention'}>
						<dt>{row.label}</dt>
						<dd>
							<strong>{row.value}</strong>
							<span>{row.detail}</span>
						</dd>
						<dd class="goal-contract__provenance">{row.provenance}</dd>
					</div>
				{/each}
			</dl>
			<details class="goal-contract__details">
				<summary>All findings and provenance detail</summary>
				<div class="goal-contract__detail-body">
					{#if movieGoalContract.findings.length}
						<ul>
							{#each movieGoalContract.findings as finding (`${finding.kind}:${finding.label}`)}
								<li>
									<strong>{finding.kind}: {finding.label}</strong>
									{#if finding.detail}<span>{finding.detail}</span>{/if}
								</li>
							{/each}
						</ul>
					{:else}
						<p>No movie-specific findings are active. Use the standard visual review.</p>
					{/if}
					<p>
						<strong>Provenance:</strong> You set this comes from the current request; Library setting
						comes from this library or folder; Mediaforce default comes from the configured baseline;
						Carried over comes from an earlier compatible plan.
					</p>
				</div>
			</details>
		{/if}
		{#if isBrowseOnly}
			<small class="goal-contract__browse-note">
				Enable changes in Settings when you want to choose different goals.
			</small>
		{/if}
	</section>
{/snippet}

<svelte:head>
	<title>{title} · Movie Studio · Mediaforce</title>
</svelte:head>

<main class="movie-studio" data-folder-ready-marker={title}>
	<nav class="breadcrumb" aria-label="Breadcrumb">
		<a href={resolve('/movies')}>Movies</a><span aria-hidden="true">/</span><span>{title}</span>
	</nav>

	<header class="studio-heading">
		<div>
			<span class="eyebrow">Movie Studio · {exactScope ? 'One file' : 'Whole title'}</span>
			<h1>{title}</h1>
			<p>{folder.prefix}</p>
		</div>
		<StateBadge tone={badgeTone()} label={workflowDisplayLabel} />
	</header>

	{#if loadError}
		<div class="notice notice--danger" role="alert">
			<strong>Studio could not open.</strong><span>{loadError}</span>
		</div>
	{/if}
	{#if actionError}
		<div class="notice notice--danger" role="alert">
			<strong>Action stopped.</strong><span>{actionError}</span>
		</div>
	{/if}
	{#if actionMessage}
		<div
			class:notice--danger={actionNeedsAttention}
			class="notice"
			role={actionNeedsAttention ? 'alert' : 'status'}
		>
			<strong>{actionNeedsAttention ? 'Sample needs attention.' : 'Movie state updated.'}</strong
			><span>{actionMessage}</span>
		</div>
	{/if}
	{#if isBrowseOnly}
		<div class="notice" role="status">
			<strong>View-only movie library</strong>
			<span
				>You can review these files, but Mediaforce cannot change this library. Enable changes in
				Settings when you are ready.</span
			>
		</div>
	{/if}
	{#if conflicts.length}
		<div class="notice notice--danger" role="alert">
			<strong>Mediaforce cannot replace this movie yet.</strong>
			{#each conflicts as conflict (`${conflict.kind}:${conflict.destination_path}`)}
				<span>{conflict.detail} <code>{conflict.destination_path}</code></span>
			{/each}
		</div>
	{/if}

	<section class:movie-facts--limited={isComplete} class="movie-facts" aria-label="Movie facts">
		<div><span>Runtime</span><strong>{movieGoalFacts.duration}</strong></div>
		<div>
			<span>{isComplete ? 'Original size' : 'Current size'}</span><strong
				>{movieGoalFacts.sourceSize}</strong
			>
		</div>
		{#if !isComplete}
			<div>
				<span>Expected output</span><strong>{movieGoalFacts.expectedOutput}</strong>
				<small>Target range {movieGoalFacts.targetRange}</small>
			</div>
			<div>
				<span>Expected savings</span><strong>{movieGoalFacts.expectedSavings}</strong>
				<small>{movieGoalFacts.estimateQuality}</small>
			</div>
		{/if}
	</section>

	<div class="studio-grid">
		<div class="studio-grid__main">
			{#if currentWork}
				<WorkstationPanel
					eyebrow="Movie work status"
					title={currentWork.headline}
					meta={currentWork.tone === 'active' || currentWork.queuePosition === 'Not available'
						? currentWork.label
						: currentWork.queuePosition}
				>
					<div class:current-work--active={currentWork.tone === 'active'} class="current-work">
						<div class="current-work__summary">
							<p>{currentWork.detail}</p>
						</div>
						{#if currentWork.tone === 'active'}
							<div class="current-work__progress">
								<div>
									<span>Movie progress</span><strong
										>{Math.round(currentWork.percentComplete)}%</strong
									>
								</div>
								<progress max="100" value={currentWork.percentComplete}
									>{currentWork.percentComplete}%</progress
								>
							</div>
						{/if}
						{#if currentWork.blockers.length}
							<div class="current-work__blockers">
								<strong>Why it has not started</strong>
								<ul>
									{#each currentWork.blockers as blocker (blocker)}
										<li>{blocker}</li>
									{/each}
								</ul>
							</div>
						{/if}
						<div class="current-work__next">
							<span>What happens next</span>
							<strong>{currentWork.nextCondition}</strong>
						</div>
						<div class="current-work__facts">
							<div><span>Queue position</span><strong>{currentWork.queuePosition}</strong></div>
							<div><span>Worker</span><strong>{currentWork.worker}</strong></div>
							{#if currentWork.tone === 'active'}
								<div><span>Elapsed</span><strong>{currentWork.elapsed}</strong></div>
								<div><span>Speed</span><strong>{currentWork.speed}</strong></div>
								<div><span>ETA</span><strong>{currentWork.eta}</strong></div>
							{:else}
								{#if currentWork.preferredWorker}
									<div>
										<span>Preferred worker</span><strong>{currentWork.preferredWorker}</strong>
										<small>Not assigned until work starts</small>
									</div>
								{/if}
								<div><span>Workers ready</span><strong>{currentWork.availableWorkers}</strong></div>
								{#if currentWork.eta}
									<div><span>ETA</span><strong>{currentWork.eta}</strong></div>
								{/if}
							{/if}
						</div>
						<div class="current-work__actions">
							<a class="secondary" href={resolve('/ops')}>Open Activity diagnostics</a>
							<small>Global queue and worker details</small>
						</div>
						{@render goalContract('Goals in use', false)}
					</div>
				</WorkstationPanel>
			{:else}
				<WorkstationPanel
					eyebrow="Next step"
					title="What to do next"
					meta={isWorkflowBlocked ? 'Cannot start' : scopeDisplay}
				>
					<div class="decision-panel">
						<div class="decision-panel__row">
							<div class="decision-copy">
								<strong>{workflowSummary()}</strong>
								{#if primaryAction() === 'retry-sample'}
									<p>{sampleFailureDetail()}</p>
									<small class="decision-note">
										Retry uses the same file, request, and worker. No full movie work was queued.
									</small>
								{:else if primaryAction() === 'review-title-sample'}
									<p>
										The completed title sample was prepared from this file. Review or approve it in
										the title workspace; this page stays scoped to only this file.
									</p>
								{:else if primaryAction() === 'complete'}
									<p>
										The checked replacement is installed. The original remains in Completed until
										you decide to delete backups.
									</p>
								{:else if primaryAction() === 'monitor-sample'}
									<p>
										{sampleWorkStatus === 'queued'
											? 'The review sample is queued and will start when its worker is ready.'
											: 'The review sample is running. Studio will refresh when review media is ready.'}
									</p>
								{:else if primaryAction() === 'prepare-again'}
									<p>{pendingProposalRecovery?.detail}</p>
								{:else if isSizeCapBlock}
									<p>{sizeCapBlock.remedy}</p>
								{:else if !isWorkflowBlocked}
									<p>
										{exactScope
											? 'Only the file you opened will change.'
											: 'This includes the main movie. Extras and files Mediaforce is unsure about stay untouched unless you open them directly.'}
									</p>
								{/if}
								{#if primaryAction() === 'prepare' && !hostOptions.length}
									<small class="decision-note">
										No sample worker is ready. Check Activity for worker availability before
										preparing the review sample.
									</small>
								{/if}
								{#if primaryAction() === 'promote'}
									<small class="decision-note">
										Runs immediately. Mediaforce keeps a backup of the original before installing
										the checked replacement.
									</small>
								{/if}
							</div>
							<div class="decision-actions">
								{#if primaryAction() === 'prepare'}
									<button
										class="primary"
										disabled={folderPending || isBusy || !hostOptions.length}
										onclick={prepareSample}
									>
										{pendingAction === 'prepare-sample'
											? 'Preparing…'
											: hostOptions.length
												? 'Prepare review sample'
												: 'No sample worker ready'}
									</button>
									{#if !hostOptions.length}
										<a class="secondary" href={resolve('/ops')}>Open Activity diagnostics</a>
									{/if}
								{:else if primaryAction() === 'prepare-again'}
									<button class="primary" disabled={isBusy} onclick={prepareAgain}>
										{pendingAction === 'prepare-again' ? 'Preparing…' : 'Prepare again'}
									</button>
								{:else if primaryAction() === 'start'}
									<button class="primary" disabled={isBusy} onclick={startSample}>
										{pendingAction === 'start-sample' ? 'Starting…' : 'Start sample'}
									</button>
								{:else if primaryAction() === 'monitor-sample'}
									<button class="secondary" disabled={isBusy} onclick={stopSample}
										>Stop sample work</button
									>
									<a class="secondary" href={resolve('/ops')}>Open Activity diagnostics</a>
								{:else if primaryAction() === 'retry-sample'}
									<button class="primary" disabled={isBusy} onclick={retrySample}>
										{pendingAction === 'retry-sample' ? 'Retrying…' : 'Retry sample'}
									</button>
								{:else if primaryAction() === 'review-title-sample'}
									<a class="primary" href={parentTitleHref}>Review title sample</a>
								{:else if primaryAction() === 'queue'}
									{#if reviewGate.status === 'accepted'}
										<button class="primary" disabled={isBusy} onclick={queueApproved}
											>Queue movie work</button
										>
									{:else}
										<button class="primary" disabled={isBusy} onclick={approveAndQueue}
											>Approve sample and queue</button
										>
									{/if}
								{:else if primaryAction() === 'validate'}
									<button class="primary" disabled={isBusy} onclick={validateOutputs}
										>Check compressed file</button
									>
								{:else if primaryAction() === 'promote'}
									<button
										class="primary"
										disabled={isBusy || conflicts.length > 0}
										onclick={promoteOutputs}>Replace original now</button
									>
								{:else if primaryAction() === 'retry'}
									<button class="primary" disabled={isBusy} onclick={retryEncode}
										>Resume unfinished work</button
									>
								{:else if primaryAction() === 'complete'}
									<a class="secondary" href={resolve('/movies')}>Back to Movies</a>
								{:else}
									<a class="primary" href={resolve('/settings')}>Open library settings</a>
								{/if}
							</div>
						</div>
						{#if !isComplete}
							{@render goalContract(isBrowseOnly ? 'Goals in view' : 'Before you prepare', true)}
						{/if}
					</div>
				</WorkstationPanel>
			{/if}

			<WorkstationPanel
				eyebrow="Review sample"
				title="Prepare and review"
				hidden={primaryAction() === 'complete' ||
					Boolean(currentWork) ||
					isSizeCapBlock ||
					sampleWorkActive}
				meta={reviewReady
					? 'Ready to review'
					: inheritedParentSample
						? 'Completed at title level'
						: 'No sample yet'}
			>
				<div class="sample-bench">
					<div class="sample-facts">
						<div>
							<span>Representative file</span><strong
								>{asText(sampleItem.rel_path) || activeMember?.label || 'Pending'}</strong
							>
						</div>
						<div>
							<span>Source size</span><strong
								>{formatMovieBytes(
									sampleItem.source_size_bytes ?? activeMember?.size_bytes
								)}</strong
							>
						</div>
						<div>
							<span>Video</span><strong
								>{asText(sampleItem.video_codec).toUpperCase() ||
									activeMember?.video_codec?.toUpperCase() ||
									'Unknown'}</strong
							>
						</div>
						<div>
							<span>Review status</span><strong>{reviewStatusLabel()}</strong>
						</div>
					</div>
					{#if Object.keys(pendingProposal).length}
						<div class:sample-plan--blocked={!pendingProposalCanQueue} class="sample-plan">
							<strong
								>{pendingProposalCanQueue
									? 'Sample plan is ready'
									: pendingProposalRecovery?.headline || 'Sample plan needs attention'}</strong
							>
							<p>
								{pendingProposalCanQueue
									? 'This is the current plan. Use Start sample above when you are ready.'
									: pendingProposalRecovery?.detail ||
										asText(pendingProposal.message) ||
										'The sample plan needs another request.'}
							</p>
							{#if !pendingProposalCanQueue}
								<p class="sample-plan__queue-state"><strong>Nothing was queued.</strong></p>
							{/if}
							{#if !pendingProposalCanQueue && pendingProposalRecovery}
								<div class="sample-plan__actions">
									{#if pendingProposalRecovery.action === 'prepare_again'}
										<button
											class="secondary"
											disabled={isBusy || isBrowseOnly}
											onclick={prepareAgain}>Prepare again</button
										>
									{:else}
										<button
											class="secondary"
											disabled={isBusy || isBrowseOnly}
											onclick={editRequest}
											>{pendingProposalRecovery.action === 'change_request'
												? 'Change request'
												: 'Edit request'}</button
										>
									{/if}
								</div>
							{/if}
						</div>
					{/if}

					<details
						bind:this={goalEditor}
						class="sample-plan-editor"
						open={!pendingProposalCanQueue && !canRetrySample}
					>
						<summary
							>{pendingProposalCanQueue || canRetrySample
								? 'Change sample plan'
								: isWorkflowBlocked
									? 'Revise sample plan'
									: 'Prepare sample plan'}</summary
						>
						<div class="sample-plan-editor__body">
							{#if pendingProposalCanQueue || canRetrySample}
								<p class="sample-plan-editor__note">
									This replaces the current plan. It does not start sample work.
								</p>
							{/if}
							<label class="request-field">
								<span>What should Mediaforce preserve?</span>
								<textarea
									bind:this={noteInput}
									bind:value={note}
									oninput={() => (noteHasNewerText = true)}
									rows="4"
									placeholder="Example: preserve grain and make the feature about 35% smaller."
								></textarea>
							</label>
							<div class="bench-controls">
								<label>
									<span>Worker</span>
									<select bind:value={selectedHostKey} disabled={!hostOptions.length}>
										{#if !hostOptions.length}<option value="">No worker available</option>{/if}
										{#each hostOptions as host (asText(host.key))}
											<option value={asText(host.key)}
												>{asText(host.label) || asText(host.key)}</option
											>
										{/each}
									</select>
								</label>
								<button
									class="secondary"
									disabled={isBusy || isBrowseOnly || !selectedHostKey}
									onclick={prepareSample}
									>{pendingProposalCanQueue || canRetrySample
										? 'Prepare replacement plan'
										: isWorkflowBlocked
											? 'Prepare revised sample'
											: 'Prepare sample plan'}</button
								>
								{#if reviewReady}
									<button class="secondary" type="button" onclick={downloadReviewPack}
										>Download review files</button
									>
								{/if}
							</div>
						</div>
					</details>
				</div>
			</WorkstationPanel>
		</div>

		<aside class="studio-grid__rail">
			<WorkstationPanel
				eyebrow="Files in this title"
				title="Files and editions"
				meta={memberCountLabel}
			>
				<div class="member-list">
					{#each context?.members ?? [] as member (member.item_id)}
						<div
							class="member-row"
							class:active={member.prefix === folder.prefix}
							data-role={member.role}
						>
							<div>
								<strong>{memberRole(member)}</strong>
								<span>{member.label}</span>
								<small>
									{formatMovieBytes(member.size_bytes)} · {memberStatusLabel(member)}
									{member.included_by_default
										? ' · Runs with the whole title'
										: ' · Only runs when opened directly'}
								</small>
							</div>
							<a href={resolve(folderRoutePath(member.prefix))}>Open</a>
						</div>
					{/each}
					{#if !context?.members.length}
						<div class="panel-empty">Movie files are still loading.</div>
					{/if}
				</div>
			</WorkstationPanel>

			<WorkstationPanel eyebrow="What will change" title="File rules" meta={scopeDisplay}>
				<div class="safety-list">
					<div>
						<strong>Main movie</strong><span>Runs when you work on the whole title.</span>
					</div>
					<div>
						<strong>Editions</strong><span>Stay separate from one another.</span>
					</div>
					<div>
						<strong>Extras</strong><span>Open one directly when you want to change it.</span>
					</div>
					<div>
						<strong>Replacement</strong><span
							>An existing destination file stops the action before anything changes.</span
						>
					</div>
				</div>
			</WorkstationPanel>
		</aside>
	</div>
</main>

<style>
	.movie-studio {
		margin: 0 auto;
		max-width: 1360px;
		padding: 24px 28px 56px;
	}

	.breadcrumb {
		align-items: center;
		color: var(--mf-fg-tertiary);
		display: flex;
		font-size: var(--mf-text-xs);
		gap: var(--mf-space-3);
		margin-bottom: var(--mf-space-6);
	}

	.breadcrumb a {
		color: var(--mf-active-fg);
		font-weight: var(--mf-weight-semibold);
		text-decoration: none;
	}

	.studio-heading {
		align-items: start;
		display: flex;
		gap: var(--mf-space-7);
		justify-content: space-between;
		margin-bottom: var(--mf-space-7);
	}

	.eyebrow {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-bold);
		letter-spacing: 0.1em;
		text-transform: uppercase;
	}

	h1,
	p {
		margin: 0;
	}

	h1 {
		font-size: clamp(28px, 4vw, 40px);
		letter-spacing: -0.035em;
		margin: 4px 0;
	}

	.studio-heading p {
		color: var(--mf-fg-tertiary);
		font-family: var(--mf-font-mono);
		font-size: var(--mf-text-2xs);
		word-break: break-all;
	}

	.notice {
		background: var(--mf-bg-panel);
		border: var(--mf-border);
		border-left: 4px solid var(--mf-active-line);
		display: grid;
		gap: 3px;
		margin-bottom: var(--mf-space-5);
		padding: 11px 13px;
	}

	.notice--danger {
		border-left-color: var(--mf-fail-fg);
	}

	.notice span {
		color: var(--mf-fg-secondary);
		font-size: var(--mf-text-xs);
	}

	.notice code {
		font-size: 11px;
		word-break: break-all;
	}

	.movie-facts {
		background: var(--mf-bg-strip);
		border: var(--mf-border);
		display: grid;
		grid-template-columns: repeat(4, minmax(0, 1fr));
		margin-bottom: var(--mf-space-6);
	}

	.movie-facts--limited {
		grid-template-columns: repeat(2, minmax(0, 1fr));
	}

	.movie-facts div {
		border-left: var(--mf-border-muted);
		padding: 10px 12px;
	}

	.movie-facts div:first-child {
		border-left: 0;
	}

	.movie-facts span,
	.movie-facts strong,
	.movie-facts small {
		display: block;
	}

	.movie-facts span {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-bold);
		letter-spacing: 0.06em;
		text-transform: uppercase;
	}

	.movie-facts strong {
		font-size: var(--mf-text-sm);
		margin-top: 3px;
	}

	.movie-facts small {
		color: var(--mf-fg-tertiary);
		font-size: 10px;
		line-height: var(--mf-leading-normal);
		margin-top: 2px;
	}

	.studio-grid {
		display: grid;
		gap: var(--mf-space-6);
		grid-template-columns: minmax(0, 1fr) minmax(320px, 0.38fr);
	}

	.studio-grid__main,
	.studio-grid__rail {
		display: grid;
		gap: var(--mf-space-6);
		min-width: 0;
	}

	.studio-grid__rail {
		align-content: start;
	}

	.decision-panel {
		display: grid;
		gap: var(--mf-space-6);
		padding: var(--mf-space-7);
	}

	.decision-panel__row {
		align-items: center;
		display: flex;
		gap: var(--mf-space-7);
		justify-content: space-between;
	}

	.decision-copy {
		max-width: 680px;
	}

	.decision-copy strong {
		font-size: var(--mf-text-xl);
		line-height: var(--mf-leading-snug);
	}

	.decision-copy p,
	.sample-plan p {
		color: var(--mf-fg-secondary);
		font-size: var(--mf-text-sm);
		line-height: var(--mf-leading-normal);
		margin-top: var(--mf-space-4);
	}

	.decision-note {
		border-left: 2px solid var(--mf-wait-fg);
		color: var(--mf-fg-secondary);
		display: block;
		font-size: var(--mf-text-xs);
		line-height: var(--mf-leading-normal);
		margin-top: var(--mf-space-4);
		max-width: 620px;
		padding-left: var(--mf-space-4);
	}

	.decision-actions,
	.bench-controls {
		align-items: center;
		display: flex;
		flex-wrap: wrap;
		gap: var(--mf-space-4);
	}

	.decision-actions {
		flex: 0 0 auto;
	}

	.decision-actions .primary {
		min-width: 150px;
	}

	.goal-contract {
		border-top: var(--mf-border-muted);
		display: grid;
		gap: var(--mf-space-4);
		padding: var(--mf-space-5) 0 0;
	}

	.goal-contract__heading {
		align-items: center;
		display: flex;
		gap: var(--mf-space-5);
		justify-content: space-between;
	}

	.goal-contract__heading span,
	.goal-contract__heading strong {
		display: block;
	}

	.goal-contract__heading span,
	.goal-contract__rows dt {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-bold);
		letter-spacing: 0.06em;
		text-transform: uppercase;
	}

	.goal-contract__heading strong {
		font-size: var(--mf-text-sm);
		margin-top: 2px;
	}

	.goal-contract__heading .secondary {
		min-height: 30px;
	}

	.goal-contract__rows {
		border: var(--mf-border-muted);
		margin: 0;
	}

	.goal-contract__rows > div {
		align-items: start;
		border-bottom: var(--mf-border-muted);
		display: grid;
		gap: var(--mf-space-4);
		grid-template-columns: minmax(88px, 0.3fr) minmax(0, 1fr) minmax(104px, auto);
		padding: 8px 10px;
	}

	.goal-contract__rows > div:last-child {
		border-bottom: 0;
	}

	.goal-contract__row--attention {
		box-shadow: inset 3px 0 0 var(--mf-wait-fg);
	}

	.goal-contract__rows dt,
	.goal-contract__rows dd {
		margin: 0;
	}

	.goal-contract__rows dd strong,
	.goal-contract__rows dd span {
		display: block;
	}

	.goal-contract__rows dd strong {
		font-size: var(--mf-text-xs);
		line-height: var(--mf-leading-normal);
	}

	.goal-contract__rows dd span,
	.goal-contract__provenance,
	.goal-contract__resolving span,
	.goal-contract__browse-note,
	.goal-contract__detail-body {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
		line-height: var(--mf-leading-normal);
	}

	.goal-contract__provenance {
		text-align: right;
	}

	.goal-contract__details {
		background: var(--mf-bg-strip);
		border: var(--mf-border-muted);
	}

	.goal-contract__details summary {
		color: var(--mf-active-fg);
		cursor: pointer;
		font-size: var(--mf-text-xs);
		font-weight: var(--mf-weight-bold);
		padding: 8px 10px;
	}

	.goal-contract__details[open] summary {
		border-bottom: var(--mf-border-muted);
	}

	.goal-contract__detail-body {
		display: grid;
		gap: var(--mf-space-4);
		padding: 10px;
	}

	.goal-contract__detail-body p,
	.goal-contract__detail-body ul {
		margin: 0;
	}

	.goal-contract__detail-body ul {
		display: grid;
		gap: var(--mf-space-3);
		padding-left: 18px;
	}

	.goal-contract__detail-body li span {
		display: block;
		margin-top: 2px;
	}

	.goal-contract__resolving {
		border-left: 3px solid var(--mf-wait-fg);
		display: grid;
		gap: 2px;
		padding: 8px 10px;
	}

	.goal-contract__browse-note {
		border-left: 2px solid var(--mf-active-line);
		padding-left: var(--mf-space-4);
	}

	.current-work {
		border-left: 4px solid var(--mf-wait-fg);
		display: grid;
		gap: var(--mf-space-6);
		padding: var(--mf-space-7);
	}

	.current-work--active {
		border-left-color: var(--mf-active-fg);
	}

	.current-work__summary p {
		color: var(--mf-fg-secondary);
		font-size: var(--mf-text-sm);
		line-height: var(--mf-leading-normal);
		margin: 0;
	}

	.current-work__progress {
		display: grid;
		gap: var(--mf-space-3);
	}

	.current-work__progress div {
		align-items: baseline;
		display: flex;
		justify-content: space-between;
	}

	.current-work__progress span,
	.current-work__facts span,
	.current-work__next span {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-bold);
		letter-spacing: 0.06em;
		text-transform: uppercase;
	}

	.current-work__progress progress {
		accent-color: var(--mf-active-fg);
		height: 10px;
		width: 100%;
	}

	.current-work__facts {
		background: var(--mf-line-muted);
		border: var(--mf-border-muted);
		display: grid;
		gap: 1px;
		grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
	}

	.current-work__facts div {
		background: var(--mf-bg-panel);
		padding: 10px 12px;
	}

	.current-work__facts strong,
	.current-work__facts small,
	.current-work__next strong {
		display: block;
		font-size: var(--mf-text-xs);
		line-height: var(--mf-leading-normal);
		margin-top: 3px;
	}

	.current-work__facts small {
		color: var(--mf-fg-tertiary);
		font-size: 10px;
	}

	.current-work__blockers {
		background: var(--mf-wait-bg);
		border: 1px solid var(--mf-wait-line);
		padding: 12px 14px;
	}

	.current-work__blockers > strong {
		color: var(--mf-wait-fg);
		font-size: var(--mf-text-sm);
	}

	.current-work__blockers ul {
		color: var(--mf-fg-secondary);
		font-size: var(--mf-text-xs);
		line-height: var(--mf-leading-normal);
		margin: var(--mf-space-3) 0 0;
		padding-left: 18px;
	}

	.current-work__next {
		border-left: 2px solid var(--mf-active-line);
		padding-left: var(--mf-space-4);
	}

	.current-work__actions {
		align-items: center;
		display: flex;
		flex-wrap: wrap;
		gap: var(--mf-space-4);
	}

	.current-work__actions small {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
	}

	.primary,
	.secondary {
		align-items: center;
		border: 1px solid var(--mf-active-fg);
		border-radius: var(--mf-radius-1);
		cursor: pointer;
		display: inline-flex;
		font: inherit;
		font-size: var(--mf-text-xs);
		font-weight: var(--mf-weight-bold);
		justify-content: center;
		min-height: 36px;
		padding: 0 13px;
		text-decoration: none;
	}

	.primary {
		background: var(--mf-active-solid);
		color: var(--mf-fg-on-accent);
	}

	.secondary {
		background: var(--mf-bg-panel);
		color: var(--mf-active-fg);
	}

	button:disabled {
		cursor: not-allowed;
		opacity: 0.48;
	}

	.sample-bench {
		display: grid;
		gap: var(--mf-space-6);
		padding: var(--mf-space-7);
	}

	.sample-plan {
		background: var(--mf-active-bg);
		border: 1px solid var(--mf-active-line);
		display: grid;
		gap: var(--mf-space-4);
		padding: var(--mf-space-6);
	}

	.sample-plan--blocked {
		background: var(--mf-wait-bg);
		border-color: var(--mf-wait-line);
	}

	.sample-plan--blocked strong {
		color: var(--mf-wait-fg);
	}

	.sample-plan .secondary {
		justify-self: start;
	}

	.sample-plan__queue-state {
		color: var(--mf-wait-fg);
		margin-top: 0;
	}

	.sample-plan__actions {
		display: flex;
		flex-wrap: wrap;
		gap: var(--mf-space-4);
	}

	.sample-plan-editor {
		background: var(--mf-bg-strip);
		border: var(--mf-border-muted);
	}

	.sample-plan-editor summary {
		color: var(--mf-active-fg);
		cursor: pointer;
		font-size: var(--mf-text-xs);
		font-weight: var(--mf-weight-bold);
		padding: 11px 13px;
	}

	.sample-plan-editor summary:focus-visible {
		outline: 2px solid var(--mf-active-fg);
		outline-offset: -2px;
	}

	.sample-plan-editor[open] summary {
		border-bottom: var(--mf-border-muted);
	}

	.sample-plan-editor__body {
		display: grid;
		gap: var(--mf-space-6);
		padding: var(--mf-space-6);
	}

	.sample-plan-editor__note {
		color: var(--mf-fg-secondary);
		font-size: var(--mf-text-xs);
		line-height: var(--mf-leading-normal);
		margin: 0;
	}

	.sample-facts {
		border: var(--mf-border-muted);
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
	}

	.sample-facts div {
		border-bottom: var(--mf-border-muted);
		border-right: var(--mf-border-muted);
		padding: 10px 12px;
	}

	.sample-facts div:nth-child(2n) {
		border-right: 0;
	}

	.sample-facts div:nth-last-child(-n + 2) {
		border-bottom: 0;
	}

	.sample-facts span,
	.sample-facts strong {
		display: block;
	}

	.sample-facts span,
	.request-field > span,
	.bench-controls label > span {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-bold);
		letter-spacing: 0.06em;
		text-transform: uppercase;
	}

	.sample-facts strong {
		font-size: var(--mf-text-xs);
		margin-top: 3px;
		word-break: break-word;
	}

	.request-field,
	.bench-controls label {
		display: grid;
		gap: var(--mf-space-3);
	}

	.request-field textarea,
	.bench-controls select {
		background: var(--mf-bg-input);
		border: 1px solid var(--mf-line-strong);
		border-radius: var(--mf-radius-1);
		color: var(--mf-fg-primary);
		font: inherit;
		font-size: var(--mf-text-sm);
		padding: 9px 10px;
	}

	.request-field textarea {
		resize: vertical;
	}

	.member-list,
	.safety-list {
		display: grid;
	}

	.member-row,
	.safety-list div {
		border-bottom: var(--mf-border-muted);
		padding: 11px 13px;
	}

	.member-row:last-child,
	.safety-list div:last-child {
		border-bottom: 0;
	}

	.member-row {
		align-items: center;
		display: flex;
		gap: var(--mf-space-4);
		justify-content: space-between;
	}

	.member-row.active {
		background: var(--mf-active-bg);
		box-shadow: inset 3px 0 0 var(--mf-active-fg);
	}

	.member-row[data-role='extra'],
	.member-row[data-role='uncertain'] {
		background: var(--mf-bg-panel-2);
	}

	.member-row strong,
	.member-row span,
	.member-row small,
	.safety-list strong,
	.safety-list span {
		display: block;
	}

	.member-row span {
		font-family: var(--mf-font-mono);
		font-size: 10px;
		margin-top: 2px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.member-row small,
	.safety-list span {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
		line-height: var(--mf-leading-normal);
		margin-top: 3px;
	}

	.member-row a {
		color: var(--mf-active-fg);
		font-size: var(--mf-text-xs);
		font-weight: var(--mf-weight-bold);
		text-decoration: none;
	}

	.safety-list strong {
		font-size: var(--mf-text-xs);
	}

	.panel-empty {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-xs);
		padding: var(--mf-space-6);
	}

	@media (max-width: 1100px) {
		.studio-grid {
			grid-template-columns: minmax(0, 1fr);
		}
	}

	@media (max-width: 680px) {
		.movie-studio {
			padding: 18px 12px 44px;
		}

		.studio-heading,
		.decision-panel__row {
			align-items: stretch;
			flex-direction: column;
		}

		.sample-facts {
			grid-template-columns: minmax(0, 1fr);
		}

		.movie-facts,
		.current-work__facts {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}

		.current-work__facts > div:last-child:nth-child(odd) {
			grid-column: 1 / -1;
		}

		.movie-facts div,
		.movie-facts div:nth-child(odd),
		.movie-facts div:nth-last-child(-n + 2) {
			border-bottom: var(--mf-border-muted);
			border-left: 0;
			grid-column: auto;
		}

		.movie-facts div:nth-last-child(-n + 2) {
			border-bottom: 0;
		}

		.current-work {
			padding: var(--mf-space-6);
		}

		.goal-contract {
			padding: var(--mf-space-5) 0 0;
		}

		.decision-panel .goal-contract {
			margin: 0;
			padding: var(--mf-space-5) 0 0;
		}

		.goal-contract__heading {
			align-items: stretch;
			flex-direction: column;
		}

		.goal-contract__rows > div {
			gap: 3px;
			grid-template-columns: minmax(0, 1fr);
		}

		.goal-contract__provenance {
			text-align: left;
		}

		.current-work__actions {
			align-items: stretch;
			display: grid;
		}

		.sample-facts div,
		.sample-facts div:nth-child(2n),
		.sample-facts div:nth-last-child(-n + 2) {
			border-bottom: var(--mf-border-muted);
			border-left: 0;
			border-right: 0;
		}

		.sample-facts div:last-child {
			border-bottom: 0;
			grid-column: auto;
		}

		.primary,
		.secondary {
			width: 100%;
		}

		.bench-controls {
			align-items: stretch;
			display: grid;
		}
	}
</style>
