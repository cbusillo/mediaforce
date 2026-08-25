<script lang="ts">
	import { resolve } from '$app/paths';

	import { apiDownloadHref, postJson } from '$lib/api/client';
	import type {
		FolderBenchConfirmResponse,
		FolderBenchPreviewResponse,
		FolderPayload,
		FolderStatusPayload,
		HostsPayload,
		OtherMember
	} from '$lib/api/types';
	import { folderRoutePath, folderRoutePrefix } from '$lib/folder-display';
	import { folderActionResponseCopy } from '$lib/folders/studio';
	import { formatFileSize } from '$lib/format';
	import { operatorStateCopy, safeOperatorErrorCopy } from '$lib/operator-copy';
	import {
		otherActionFileCount,
		otherReadinessBlockerCopy,
		otherSampleSetupResult,
		otherScopeSummary,
		otherWorkflowDetail,
		otherWorkflowLabel
	} from '$lib/other/library';
	import StateBadge from './StateBadge.svelte';

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
	let membershipConfirmed = $state(false);
	let pendingAction = $state('');
	let actionMessage = $state('');
	let actionError = $state('');
	let actionNeedsAttention = $state(false);
	let actionAttentionTitle = $state('');
	let confirmedMembershipToken = $state('');

	const context = $derived(folder.other_context ?? null);
	const workflow = $derived(status.workflow_state ?? folder.workflow_state ?? null);
	const calibration = $derived(asRecord(folder.calibration));
	const pendingProposal = $derived(asRecord(folder.pending_proposal));
	const pendingProposalCanQueue = $derived(pendingProposal.can_queue === true);
	const calibrationJob = $derived(asRecord(folder.calibration_job));
	const sampleWorkStatus = $derived(asText(calibrationJob.status));
	const sampleWorkActive = $derived(
		['queued', 'starting', 'running', 'retry_backoff'].includes(sampleWorkStatus)
	);
	const encodeJob = $derived(folder.encode_job ?? null);
	const hostOptions = $derived(
		(folder.sample_host_options ?? [])
			.map((host) => asRecord(host))
			.filter((host) => asText(host.key) && host.available !== false)
	);
	const title = $derived(
		context?.media_scope.title ??
			folder.media_scope.title ??
			folder.prefix.split('/').at(-1) ??
			'Other media'
	);
	const readiness = $derived(context?.profile_readiness ?? null);
	const isBrowseOnly = $derived(context?.availability === 'browse_only');
	const requiresMembershipConfirmation = $derived(
		context?.membership_requires_confirmation === true
	);
	const membershipComplete = $derived(context?.membership_complete !== false);
	const scopeReady = $derived(readiness?.state === 'ready' && membershipComplete);
	const actionReady = $derived(
		scopeReady && !isBrowseOnly && (!requiresMembershipConfirmation || membershipConfirmed)
	);
	const isBusy = $derived(Boolean(pendingAction));
	const activeWorkerCount = $derived(hosts.hosts.filter((host) => host.available).length);
	const reviewReady = $derived(
		Boolean(
			calibration.review_media_ready ||
			calibration.browser_review_ready ||
			calibration.compare_clips
		)
	);
	const approved = $derived(Boolean(calibration.accepted_at));
	const scopeNoun = $derived(folder.media_scope.match === 'exact_item' ? 'file' : 'folder');
	const scopeLabel = $derived(
		folder.media_scope.match === 'exact_item' ? 'One file' : 'Whole folder'
	);
	const itemCount = $derived(context?.item_count ?? 0);
	const eligibleItemCount = $derived(context?.eligible_item_count ?? 0);
	const blockedItemCount = $derived(context?.blocked_item_count ?? 0);
	const actionFileCount = $derived(otherActionFileCount(workflow, eligibleItemCount, itemCount));
	const untouchedFileCount = $derived(Math.max(0, itemCount - actionFileCount));
	const scopeSummary = $derived(
		otherScopeSummary(
			itemCount,
			actionFileCount,
			membershipComplete,
			context?.membership_limit ?? 250
		)
	);
	const membershipReviewLabel = $derived(
		itemCount === 1
			? 'I reviewed the file in this folder.'
			: `I reviewed all ${itemCount} files in this folder.`
	);
	const scopeConfirmationDetail = $derived.by(() => {
		const untouched =
			untouchedFileCount === 0
				? 'No files are left out.'
				: `${untouchedFileCount} ${untouchedFileCount === 1 ? 'file stays' : 'files stay'} untouched.`;
		if (workflow?.primary_lane === 'validate') {
			return `${actionFileCount} compressed ${actionFileCount === 1 ? 'file will' : 'files will'} be checked. ${untouched}`;
		}
		if (workflow?.primary_lane === 'promote') {
			return `${actionFileCount} checked ${actionFileCount === 1 ? 'file will replace its original' : 'files will replace their originals'}. ${untouched}`;
		}
		return scopeSummary.confirmation;
	});
	const reviewPackHref = $derived(
		apiDownloadHref(`/api/folders/${folderRoutePrefix(folder.prefix)}/review-compare/download`)
	);
	const workflowLabel = $derived(otherWorkflowLabel(workflow, folderPending));
	const workflowDetail = $derived(otherWorkflowDetail(workflow, actionFileCount));
	const decisionDetail = $derived.by(() => {
		if (sampleWorkStatus === 'retry_backoff')
			return 'The last attempt stopped. Mediaforce will retry this sample shortly.';
		if (sampleWorkStatus === 'queued')
			return 'This sample has not started. It is waiting for an available computer.';
		if (['starting', 'running'].includes(sampleWorkStatus))
			return 'Mediaforce is creating comparison clips. The original files remain unchanged.';
		if (['failed', 'stopped'].includes(sampleWorkStatus))
			return 'The previous sample stopped before its comparison clips were ready. Nothing was replaced.';
		if (reviewReady && !approved)
			return 'Compare the original and sample clips. Approval does not replace any files.';
		if (pendingProposalCanQueue)
			return 'Nothing has started. Choose Create sample when you are ready.';
		if (workflow?.primary_lane === 'encode' && !approved)
			return 'Set up and review a sample before compression begins.';
		return workflowDetail;
	});
	const readinessNotice = $derived.by(() => {
		if (!membershipComplete) {
			return {
				title: 'This folder is too large to confirm safely.',
				detail: 'Use one file at a time or split the folder before starting work.'
			};
		}
		if (blockedItemCount > 0) {
			return {
				title: 'Some files cannot use this compression profile.',
				detail: `${blockedItemCount} ${blockedItemCount === 1 ? 'file needs' : 'files need'} a compatible profile before work can start.`
			};
		}
		return {
			title: 'Cannot start yet.',
			detail: 'Fix the library settings before starting work.'
		};
	});
	const decisionTitle = $derived.by(() => {
		if (['queued', 'retry_backoff'].includes(sampleWorkStatus)) return 'Sample waiting';
		if (['starting', 'running'].includes(sampleWorkStatus)) return 'Creating sample';
		if (['failed', 'stopped'].includes(sampleWorkStatus)) return 'Sample needs retry';
		if (reviewReady && !approved) return 'Ready to review';
		if (pendingProposalCanQueue) return 'Sample setup ready';
		if (workflow?.primary_lane === 'processing') return 'Compressing now';
		if (workflow?.primary_lane === 'validate')
			return actionFileCount === 1 ? 'Check the compressed file' : 'Check the compressed files';
		if (workflow?.primary_lane === 'promote')
			return actionFileCount === 1 ? 'Replace the original file' : 'Replace the original files';
		if (['blocked', 'attention'].includes(workflow?.primary_lane ?? ''))
			return 'Fix this before work continues';
		if (approved && workflow?.primary_lane === 'encode')
			return actionFileCount === 1 ? 'Compress this file' : 'Compress these files';
		return `Get this ${scopeNoun} ready to compress`;
	});
	const activeOperationCopy = $derived.by(() => {
		const host = asRecord(encodeJob?.host);
		const computer =
			asText(host.label) ||
			asText(host.key) ||
			encodeJob?.progress?.active_host_labels?.[0] ||
			'Unassigned';
		const percent = encodeJob?.progress?.percent_complete;
		const parts = [
			percent != null ? `${Math.round(percent)}% complete` : 'Progress pending',
			`Computer: ${computer}`,
			formatElapsed(encodeJob?.created_at)
		].filter(Boolean);
		return parts.join(' · ');
	});
	const showSampleControls = $derived(
		scopeReady &&
			!isBrowseOnly &&
			!pendingProposalCanQueue &&
			!reviewReady &&
			!approved &&
			!sampleWorkActive &&
			!['failed', 'stopped'].includes(asText(calibrationJob.status)) &&
			!['processing', 'validate', 'promote'].includes(workflow?.primary_lane ?? '')
	);

	$effect(() => {
		if (confirmedMembershipToken && confirmedMembershipToken !== context?.membership_token) {
			confirmedMembershipToken = '';
			membershipConfirmed = false;
		}
		const folderHostKey = String(folder.sample_host_key ?? '').trim();
		if (!selectedHostKey || !hostOptions.some((host) => asText(host.key) === selectedHostKey)) {
			selectedHostKey = folderHostKey || asText(hostOptions[0]?.key);
		}
	});

	async function prepareSample() {
		if (!actionReady || isBusy) return;
		await runAction('prepare-sample', async () => {
			const response = await postJson<FolderBenchPreviewResponse>(
				`/api/folders/${folderRoutePrefix(folder.prefix)}/ai-tune/preview`,
				{
					note:
						note.trim() ||
						`Prepare a representative sample for this bounded ${scopeNoun} using the selected Other profile.`,
					host_key: selectedHostKey,
					scope_membership_token: scopeMembershipToken()
				}
			);
			if (!response.ok)
				throw new Error(response.message || 'This scope is not ready for sampling.');
			note = '';
			return otherSampleSetupResult(response.proposal?.can_queue === true);
		});
	}

	async function startSample() {
		if (!actionReady || isBusy || !pendingProposalCanQueue) return;
		await runAction('start-sample', async () => {
			const response = await postJson<FolderBenchConfirmResponse>(
				`/api/folders/${folderRoutePrefix(folder.prefix)}/ai-tune/confirm`,
				{
					proposal_id: asText(pendingProposal.proposal_id),
					scope_membership_token: scopeMembershipToken()
				}
			);
			if (!response.ok) throw new Error(response.message || 'The sample could not start.');
			return 'Sample waiting.';
		});
	}

	async function retrySample() {
		if (!actionReady || isBusy) return;
		await runAction('retry-sample', async () => {
			const response = await postJson<FolderBenchConfirmResponse>(
				`/api/folders/${folderRoutePrefix(folder.prefix)}/ai-tune/confirm`,
				{
					proposal_id: '',
					scope_membership_token: scopeMembershipToken()
				}
			);
			if (!response.ok) throw new Error(response.message || 'The sample could not restart.');
			return 'Sample waiting.';
		});
	}

	async function approveSample() {
		if (!actionReady || isBusy) return;
		await runAction('approve-sample', async () => {
			const response = await postJson<{ ok: boolean; message?: string }>(
				`/api/folders/${folderRoutePrefix(folder.prefix)}/save-profile`,
				{
					confirm_high_impact: true,
					confirm_size_tradeoff: true,
					reviewed_draft_hash: asText(calibration.draft_hash),
					scope_membership_token: scopeMembershipToken()
				}
			);
			if (!response.ok)
				throw new Error(response.message || 'The sample approval could not be saved.');
			return `Sample approved for this ${scopeNoun}.`;
		});
	}

	function openReviewPack() {
		window.location.assign(reviewPackHref);
	}

	async function queueApproved() {
		if (!actionReady || isBusy) return;
		await runAction('queue-work', async () => {
			const response = await postJson<{
				ok: boolean;
				message?: string;
				recovered_item_count?: number | null;
				job?: { item_count?: number | null };
			}>(`/api/folders/${folderRoutePrefix(folder.prefix)}/queue-encode`, {
				notes: '',
				bypass_schedule: false,
				scope_membership_token: scopeMembershipToken()
			});
			if (!response.ok) throw new Error(response.message || 'This work could not be queued.');
			const queuedCount =
				safeCount(response.recovered_item_count) ||
				safeCount(response.job?.item_count) ||
				actionFileCount;
			return queuedCount === 1
				? 'This file is waiting to compress.'
				: `${queuedCount} files are waiting to compress.`;
		});
	}

	async function validateOutputs() {
		await folderAction('validate-outputs');
	}

	async function promoteOutputs() {
		await folderAction('promote-outputs');
	}

	async function folderAction(action: 'validate-outputs' | 'promote-outputs') {
		if (isBusy) return;
		await runAction(action, async () => {
			const response = await postJson<{
				ok: boolean;
				message?: string;
				target_prefix?: string;
				validated_count?: number | null;
				failed_count?: number | null;
				item_count?: number | null;
				promoted_count?: number | null;
			}>(`/api/folders/${folderRoutePrefix(folder.prefix)}/${action}`, {
				scope_membership_token: scopeMembershipToken()
			});
			if (!response.ok) throw new Error(response.message || `${action} failed.`);
			const result = folderActionResponseCopy(action, response);
			return {
				...result,
				targetPrefix: action === 'promote-outputs' ? response.target_prefix : undefined
			};
		});
	}

	type ActionResult =
		| string
		| {
				message: string;
				targetPrefix?: string;
				attention?: boolean;
				attentionTitle?: string;
		  };

	async function runAction(action: string, execute: () => Promise<ActionResult>) {
		pendingAction = action;
		actionMessage = '';
		actionError = '';
		actionNeedsAttention = false;
		actionAttentionTitle = '';
		let actionCompleted = false;
		try {
			const result = await execute();
			actionMessage = typeof result === 'string' ? result : result.message;
			actionNeedsAttention = typeof result === 'string' ? false : result.attention === true;
			actionAttentionTitle = typeof result === 'string' ? '' : (result.attentionTitle ?? '');
			actionCompleted = true;
			await onMutate(typeof result === 'string' ? undefined : result.targetPrefix);
		} catch (error) {
			actionError = actionCompleted
				? 'The action completed, but Studio could not refresh.'
				: safeOperatorErrorCopy(error, 'Studio could not complete that action.');
		} finally {
			pendingAction = '';
		}
	}

	function asRecord(value: unknown): Record<string, unknown> {
		return value && typeof value === 'object' && !Array.isArray(value)
			? (value as Record<string, unknown>)
			: {};
	}

	function asText(value: unknown): string {
		return typeof value === 'string' ? value : '';
	}

	function safeCount(value: unknown): number {
		const count = Number(value);
		return Number.isFinite(count) ? Math.max(0, Math.trunc(count)) : 0;
	}

	function formatBytes(value: number | null | undefined): string {
		return formatFileSize(value, 'Unknown');
	}

	function formatElapsed(timestamp: string | null | undefined): string {
		if (!timestamp) return '';
		const startedAt = Date.parse(timestamp);
		if (!Number.isFinite(startedAt)) return '';
		const elapsedSeconds = Math.max(Math.floor((Date.now() - startedAt) / 1000), 0);
		if (elapsedSeconds < 60) return `Active for ${elapsedSeconds}s`;
		const elapsedMinutes = Math.floor(elapsedSeconds / 60);
		if (elapsedMinutes < 60) return `Active for ${elapsedMinutes}m`;
		return `Active for ${Math.floor(elapsedMinutes / 60)}h ${elapsedMinutes % 60}m`;
	}

	function workflowTone(): 'active' | 'ready' | 'wait' | 'fail' | 'idle' {
		if (readiness?.state === 'blocked') return 'fail';
		if (isBrowseOnly) return 'wait';
		if (workflow?.primary_lane === 'processing') return 'active';
		if (['encode', 'validate', 'promote'].includes(workflow?.primary_lane ?? '')) return 'ready';
		if (['blocked', 'attention'].includes(workflow?.primary_lane ?? '')) return 'fail';
		return 'idle';
	}

	function memberState(member: OtherMember): string {
		if (workflow?.primary_lane === 'processing') return 'Compressing';
		return operatorStateCopy(member.workflow_state?.state ?? member.status);
	}

	function sampleState(): string {
		if (reviewReady) return approved ? 'Sample approved' : 'Ready to review';
		if (['processing', 'validate', 'promote', 'complete'].includes(workflow?.primary_lane ?? ''))
			return 'Not needed now';
		return operatorStateCopy(
			calibrationJob.status,
			{
				failed: 'Sample needs retry',
				queued: 'Sample waiting',
				retry_backoff: 'Sample waiting',
				running: 'Creating sample',
				starting: 'Creating sample',
				stopped: 'Sample needs retry'
			},
			'Needs sample'
		);
	}

	function compressionState(): string {
		if (workflow?.primary_lane === 'validate') return 'Ready to check';
		if (workflow?.primary_lane === 'promote') return 'Ready to replace';
		if (workflow?.primary_lane === 'complete') return 'Finished';
		if (workflow?.primary_lane === 'processing' && !encodeJob?.status) return 'Compressing';
		return operatorStateCopy(
			encodeJob?.status,
			{
				failed: 'Compression needs attention',
				queued: 'Compression waiting',
				retry_backoff: 'Compression waiting',
				running: 'Compressing',
				starting: 'Compressing',
				stopped: 'Compression needs attention'
			},
			'Idle'
		);
	}

	function scopeMembershipToken(): string {
		return membershipConfirmed || !requiresMembershipConfirmation
			? (context?.membership_token ?? '')
			: '';
	}

	function updateMembershipConfirmation(checked: boolean) {
		membershipConfirmed = checked;
		confirmedMembershipToken = checked ? (context?.membership_token ?? '') : '';
	}
</script>

<svelte:head>
	<title>{title} · Other Studio · Mediaforce</title>
</svelte:head>

<main class="other-studio" data-folder-ready-marker={title}>
	<header class="studio-header">
		<div class="studio-header__identity">
			<a href={resolve('/other')}>← Other Library</a>
			<div>
				<span class="eyebrow">{scopeLabel}</span>
				<h1>{title}</h1>
				<p>{folder.prefix}</p>
			</div>
		</div>
		<div class="studio-state">
			<StateBadge
				tone={workflowTone()}
				label={readiness?.state === 'ready' ? workflowLabel : (readiness?.label ?? 'Loading')}
			/>
		</div>
	</header>

	<section class="status-strip" aria-label="Other Studio status">
		<div>
			<span>Scope</span><strong
				>{context?.item_count ?? 0} {context?.item_count === 1 ? 'file' : 'files'}</strong
			>
		</div>
		<div><span>Profile</span><strong>{readiness?.profile_label ?? 'Loading'}</strong></div>
		<div><span>Workflow</span><strong>{workflowLabel}</strong></div>
		<div>
			<span>Sample</span><strong>{sampleState()}</strong>
		</div>
		<div>
			<span>Compression</span><strong>{compressionState()}</strong>
		</div>
		<div><span>Computers ready</span><strong>{activeWorkerCount}</strong></div>
	</section>
	<div class="sr-only" aria-live="polite">
		Sample: {sampleState()}. Compression: {compressionState()}.
	</div>

	{#if loadError}
		<div class="notice notice--danger" role="alert">
			<strong>Studio update failed</strong><span
				>{safeOperatorErrorCopy(loadError, 'Studio could not load the latest state.')}</span
			>
		</div>
	{/if}
	{#if actionError}
		<div class="notice notice--danger" role="alert">
			<strong>Action failed</strong><span>{actionError}</span>
		</div>
	{/if}
	{#if actionMessage}
		<div
			class:notice--danger={actionNeedsAttention}
			class:notice--success={!actionNeedsAttention}
			class="notice"
			role={actionNeedsAttention ? 'alert' : 'status'}
		>
			<strong
				>{actionNeedsAttention
					? actionAttentionTitle || 'Action needs attention'
					: 'Updated'}</strong
			><span>{actionMessage}</span>
		</div>
	{/if}
	{#if isBrowseOnly}
		<div class="notice">
			<strong>Browse only</strong><span
				>Turn on changes for this root in Settings before creating a sample or starting work.</span
			>
		</div>
	{:else if readiness?.state === 'blocked'}
		<div class="notice notice--danger">
			<strong>{readinessNotice.title}</strong><span>{readinessNotice.detail}</span>
			{#if readiness.blockers.length}
				<ul>
					{#each readiness.blockers as blocker (blocker)}<li>
							{otherReadinessBlockerCopy(blocker)}
						</li>{/each}
				</ul>
			{/if}
		</div>
	{/if}

	<div class="studio-grid">
		<section class="decision-panel">
			<header class="panel-heading">
				<div>
					<span class="eyebrow">Decision</span>
					<h2>{decisionTitle}</h2>
				</div>
				<span>{folderPending ? 'Refreshing…' : decisionDetail}</span>
			</header>

			<div class="scope-contract">
				<div>
					<span>Files included now</span><strong>{scopeSummary.included}</strong>
				</div>
				<div>
					<span>Current size</span><strong>{formatBytes(context?.total_size_bytes)}</strong>
				</div>
				<div>
					<span>What is included</span><strong
						>{folder.media_scope.match === 'exact_item'
							? 'Only this file'
							: 'Files in this folder and its subfolders'}</strong
					>
				</div>
				<div>
					<span>Files left untouched now</span><strong>{scopeSummary.untouched}</strong>
				</div>
			</div>

			{#if !membershipComplete}
				<div class="scope-warning" role="status">
					<strong>This folder is too large to confirm safely.</strong>
					<span>{scopeSummary.confirmation}</span>
				</div>
			{:else if requiresMembershipConfirmation}
				<label class="confirmation" class:is-disabled={!scopeReady}>
					<input
						type="checkbox"
						checked={membershipConfirmed}
						disabled={!scopeReady}
						onchange={(event) =>
							updateMembershipConfirmation((event.currentTarget as HTMLInputElement).checked)}
					/>
					<span>
						<strong>{membershipReviewLabel}</strong>
						<small>{scopeConfirmationDetail}</small>
					</span>
				</label>
			{/if}

			{#if showSampleControls}
				<div class="sample-form">
					<label>
						<span>Sample note</span>
						<textarea
							bind:value={note}
							rows="3"
							placeholder="Optional quality or size direction for this scope"></textarea>
					</label>
					<label>
						<span>Computer</span>
						<select bind:value={selectedHostKey} disabled={!hostOptions.length}>
							{#if !hostOptions.length}<option value="">No computer available for samples</option
								>{/if}
							{#each hostOptions as host (asText(host.key))}
								<option value={asText(host.key)}>{asText(host.label) || asText(host.key)}</option>
							{/each}
						</select>
					</label>
				</div>
			{/if}

			<div class="action-row">
				{#if sampleWorkActive}
					<span class="operation-state"><strong>{sampleState()}.</strong>{decisionDetail}</span>
				{:else if workflow?.primary_lane === 'processing'}
					<span class="operation-state"><strong>Compressing now.</strong>{activeOperationCopy}</span
					>
				{:else if workflow?.primary_lane === 'validate'}
					<button class="primary" disabled={!actionReady || isBusy} onclick={validateOutputs}
						>{actionFileCount === 1 ? 'Check compressed file' : 'Check compressed files'}</button
					>
				{:else if workflow?.primary_lane === 'promote'}
					<span class="action-consequence">
						Runs immediately. Mediaforce keeps {actionFileCount === 1
							? 'the original backup'
							: 'original backups'}
						in the cleanup folder first. Files left out stay untouched.
					</span>
					<button class="primary" disabled={!actionReady || isBusy} onclick={promoteOutputs}
						>{actionFileCount === 1 ? 'Replace original file' : 'Replace original files'}</button
					>
				{:else if reviewReady && !approved}
					<button class="secondary" type="button" onclick={openReviewPack}>Compare clips</button>
					<button class="primary" disabled={!actionReady || isBusy} onclick={approveSample}
						>Approve sample</button
					>
				{:else if approved && workflow?.primary_lane === 'encode'}
					<button class="primary" disabled={!actionReady || isBusy} onclick={queueApproved}
						>{!membershipComplete
							? 'Compress files'
							: actionFileCount === 1
								? 'Compress this file'
								: `Compress ${actionFileCount} files`}</button
					>
				{:else if pendingProposalCanQueue}
					<button class="primary" disabled={!actionReady || isBusy} onclick={startSample}
						>Create sample</button
					>
				{:else if ['failed', 'stopped'].includes(asText(calibrationJob.status))}
					<button class="primary" disabled={!actionReady || isBusy} onclick={retrySample}
						>Retry sample</button
					>
				{:else}
					<button
						class="primary"
						disabled={!actionReady || isBusy || !hostOptions.length}
						onclick={prepareSample}>Set up sample</button
					>
				{/if}
				{#if requiresMembershipConfirmation && membershipComplete && !membershipConfirmed}
					<span class="action-help"
						>{showSampleControls && !hostOptions.length
							? 'Confirm the included files and bring a computer online for samples.'
							: 'Confirm the included files to enable this action.'}</span
					>
				{:else if readiness?.state !== 'ready'}
					<span class="action-help">Fix the blocker above before starting work.</span>
				{:else if showSampleControls && !hostOptions.length && !reviewReady}
					<span class="action-help">Bring a computer online for samples.</span>
				{/if}
			</div>
		</section>

		<aside class="membership-panel">
			<header class="panel-heading">
				<div>
					<span class="eyebrow">Membership</span>
					<h2>{context?.item_count === 1 ? 'File in this scope' : 'Files in this scope'}</h2>
				</div>
				<span
					>{context?.members.length ?? 0}{context?.membership_complete === false ? '+' : ''} shown</span
				>
			</header>

			{#if context?.membership_complete === false}
				<div class="membership-warning">
					<strong>Too many files to confirm safely.</strong>
					<span
						>Switch this root to one-file-at-a-time in Settings or split the source folder before
						compressing.</span
					>
				</div>
			{/if}

			<div class="member-list">
				{#each context?.members ?? [] as member (member.item_id)}
					<div class="member-row" data-supported={member.profile_supported}>
						<div>
							<strong>{member.label}</strong>
							<code>{member.rel_path}</code>
							<small>
								{formatBytes(member.size_bytes)}
								{#if member.video_codec}
									· {member.video_codec.toUpperCase()}{/if}
								{#if member.width && member.height}
									· {member.width}×{member.height}{/if}
							</small>
							{#if member.profile_blocker}<span class="member-blocker"
									>{member.profile_blocker}</span
								>{/if}
						</div>
						<div class="member-row__actions">
							<span>{memberState(member)}</span>
							<a href={resolve(folderRoutePath(member.prefix))} aria-label={`Open ${member.label}`}
								>Open</a
							>
						</div>
					</div>
				{/each}
			</div>
		</aside>
	</div>
</main>

<style>
	.other-studio {
		margin: 0 auto;
		max-width: 1440px;
		padding: 24px 28px 54px;
	}

	h1,
	h2,
	p {
		margin: 0;
	}

	.studio-header {
		align-items: end;
		display: flex;
		gap: 24px;
		justify-content: space-between;
	}

	.studio-header__identity {
		align-items: end;
		display: flex;
		gap: 18px;
		min-width: 0;
	}

	.studio-header__identity > a {
		color: var(--mf-fg-secondary);
		font-size: 12px;
		font-weight: 700;
		padding-bottom: 6px;
		text-decoration: none;
		white-space: nowrap;
	}

	.studio-header__identity > div {
		min-width: 0;
	}

	.studio-state {
		align-self: center;
	}

	.sr-only {
		clip: rect(0, 0, 0, 0);
		clip-path: inset(50%);
		height: 1px;
		overflow: hidden;
		position: absolute;
		white-space: nowrap;
		width: 1px;
	}

	.eyebrow,
	.scope-contract span,
	.sample-form label > span,
	.status-strip span {
		color: var(--mf-fg-muted);
		font-size: 10px;
		font-weight: 800;
		letter-spacing: 0.08em;
		text-transform: uppercase;
	}

	h1 {
		font-size: clamp(28px, 4vw, 42px);
		letter-spacing: -0.04em;
		margin-top: 3px;
	}

	.studio-header p {
		color: var(--mf-fg-secondary);
		font-family: var(--mf-font-mono);
		font-size: 12px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.status-strip {
		background: var(--mf-bg-panel);
		border: 1px solid var(--mf-line);
		display: grid;
		grid-template-columns: repeat(6, minmax(110px, 1fr));
		margin-top: 18px;
	}

	.status-strip div {
		border-left: 1px solid var(--mf-line);
		padding: 11px 12px;
	}

	.status-strip div:first-child {
		border-left: 0;
	}

	.status-strip span,
	.status-strip strong {
		display: block;
	}

	.status-strip strong {
		font-size: 12px;
		line-height: 1.35;
		margin-top: 3px;
		white-space: normal;
	}

	.notice {
		background: var(--mf-bg-panel);
		border: 1px solid var(--mf-line);
		border-left: 3px solid var(--mf-wait-fg);
		display: grid;
		gap: 3px;
		margin-top: 12px;
		padding: 11px 13px;
	}

	.notice--danger {
		border-left-color: var(--mf-fail-fg);
	}

	.notice--success {
		border-left-color: var(--mf-ready-fg);
	}

	.notice span,
	.notice li {
		color: var(--mf-fg-secondary);
		font-size: 12px;
		line-height: 1.5;
	}

	.notice ul {
		margin: 5px 0 0;
		padding-left: 18px;
	}

	.studio-grid {
		display: grid;
		gap: 14px;
		grid-template-columns: minmax(0, 1.15fr) minmax(360px, 0.85fr);
		margin-top: 14px;
	}

	.decision-panel,
	.membership-panel {
		background: var(--mf-bg-panel);
		border: 1px solid var(--mf-line);
		min-width: 0;
	}

	.decision-panel {
		padding: 20px;
	}

	.panel-heading {
		align-items: start;
		display: flex;
		gap: 18px;
		justify-content: space-between;
	}

	.panel-heading h2 {
		font-size: 20px;
		letter-spacing: -0.02em;
		margin-top: 3px;
	}

	.panel-heading > span {
		color: var(--mf-fg-secondary);
		font-size: 11px;
		line-height: 1.45;
		max-width: 260px;
		text-align: right;
	}

	.scope-contract {
		border: 1px solid var(--mf-line);
		display: grid;
		grid-template-columns: 1fr 1fr;
		margin-top: 18px;
	}

	.scope-contract div {
		border-bottom: 1px solid var(--mf-line);
		padding: 12px;
	}

	.scope-contract div:nth-child(odd) {
		border-right: 1px solid var(--mf-line);
	}

	.scope-contract div:nth-last-child(-n + 2) {
		border-bottom: 0;
	}

	.scope-contract span,
	.scope-contract strong {
		display: block;
	}

	.scope-contract strong {
		font-size: 13px;
		margin-top: 4px;
	}

	.scope-warning {
		border-left: 3px solid var(--mf-wait-fg);
		display: grid;
		font-size: 12px;
		gap: 3px;
		line-height: 1.45;
		margin-top: 16px;
		padding: 6px 0 6px 11px;
	}

	.scope-warning span,
	.action-consequence {
		color: var(--mf-fg-secondary);
	}

	.confirmation {
		align-items: start;
		background: var(--mf-bg-subtle);
		border: 1px solid var(--mf-line-strong);
		cursor: pointer;
		display: flex;
		gap: 10px;
		margin-top: 16px;
		padding: 12px;
	}

	.confirmation.is-disabled {
		cursor: not-allowed;
		opacity: 0.65;
	}

	.confirmation input {
		margin-top: 2px;
	}

	.confirmation strong,
	.confirmation small {
		display: block;
	}

	.confirmation small {
		color: var(--mf-fg-secondary);
		font-size: 11px;
		line-height: 1.45;
		margin-top: 3px;
	}

	.sample-form {
		display: grid;
		gap: 12px;
		grid-template-columns: 1fr 190px;
		margin-top: 16px;
	}

	.sample-form label {
		display: grid;
		gap: 5px;
	}

	.sample-form textarea,
	.sample-form select {
		background: var(--mf-bg-input);
		border: 1px solid var(--mf-line-strong);
		border-radius: 0;
		color: var(--mf-fg-primary);
		font: inherit;
		font-size: 13px;
		padding: 9px 10px;
	}

	.sample-form select {
		height: 38px;
	}

	.action-row {
		align-items: center;
		border-top: 1px solid var(--mf-line);
		display: flex;
		flex-wrap: wrap;
		gap: 9px;
		margin-top: 18px;
		padding-top: 16px;
	}

	.primary,
	.secondary {
		border: 1px solid var(--mf-active-fg);
		border-radius: 0;
		font: inherit;
		font-size: 12px;
		font-weight: 800;
		padding: 9px 12px;
		text-decoration: none;
	}

	.primary {
		background: var(--mf-active-fg);
		color: var(--mf-active-contrast);
		cursor: pointer;
	}

	.primary:disabled {
		cursor: not-allowed;
		opacity: 0.5;
	}

	.secondary {
		background: transparent;
		color: var(--mf-active-fg);
	}

	.action-help {
		color: var(--mf-fg-secondary);
		font-size: 11px;
	}

	.action-consequence {
		flex-basis: 100%;
		font-size: 11px;
		line-height: 1.5;
	}

	.operation-state {
		border-left: 3px solid var(--mf-active-fg);
		color: var(--mf-fg-secondary);
		display: grid;
		font-size: 12px;
		gap: 2px;
		line-height: 1.5;
		padding: 5px 0 5px 10px;
	}

	.operation-state strong {
		color: var(--mf-fg-primary);
	}

	.membership-panel {
		display: flex;
		flex-direction: column;
		max-height: 720px;
	}

	.membership-panel > .panel-heading {
		border-bottom: 1px solid var(--mf-line);
		padding: 18px;
	}

	.membership-warning {
		border-bottom: 1px solid var(--mf-line);
		border-left: 3px solid var(--mf-fail-fg);
		display: grid;
		gap: 4px;
		padding: 11px 13px;
	}

	.membership-warning span {
		color: var(--mf-fg-secondary);
		font-size: 11px;
		line-height: 1.45;
	}

	.member-list {
		overflow: auto;
	}

	.member-row {
		align-items: start;
		border-bottom: 1px solid var(--mf-line);
		display: grid;
		gap: 12px;
		grid-template-columns: minmax(0, 1fr) auto;
		padding: 12px 14px;
	}

	.member-row[data-supported='false'] {
		box-shadow: inset 3px 0 0 var(--mf-fail-fg);
	}

	.member-row > div:first-child {
		min-width: 0;
	}

	.member-row strong,
	.member-row code,
	.member-row small,
	.member-blocker {
		display: block;
	}

	.member-row strong {
		font-size: 13px;
	}

	.member-row code {
		color: var(--mf-fg-secondary);
		font-size: 10px;
		margin-top: 3px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.member-row small {
		color: var(--mf-fg-muted);
		font-size: 10px;
		margin-top: 4px;
	}

	.member-blocker {
		color: var(--mf-fail-fg);
		font-size: 10px;
		line-height: 1.4;
		margin-top: 5px;
	}

	.member-row__actions {
		align-items: end;
		display: grid;
		gap: 5px;
		justify-items: end;
	}

	.member-row__actions span {
		color: var(--mf-fg-muted);
		font-size: 9px;
		font-weight: 800;
		text-transform: uppercase;
	}

	.member-row__actions a {
		color: var(--mf-active-fg);
		font-size: 10px;
		font-weight: 700;
		text-decoration: none;
	}

	@media (max-width: 1020px) {
		.status-strip {
			grid-template-columns: repeat(3, 1fr);
		}

		.status-strip div:nth-child(4) {
			border-left: 0;
			border-top: 1px solid var(--mf-line);
		}

		.status-strip div:nth-child(n + 5) {
			border-top: 1px solid var(--mf-line);
		}

		.studio-grid {
			grid-template-columns: 1fr;
		}

		.membership-panel {
			max-height: none;
		}
	}

	@media (max-width: 680px) {
		.other-studio {
			padding: 20px 14px 40px;
		}

		.studio-header,
		.studio-header__identity {
			align-items: stretch;
			flex-direction: column;
		}

		.studio-header__identity {
			gap: 10px;
		}

		.studio-state {
			align-self: flex-start;
		}

		.status-strip {
			grid-template-columns: repeat(3, minmax(0, 1fr));
		}

		.status-strip div:nth-child(3n + 1) {
			border-left: 0;
		}

		.status-strip div:nth-child(n + 4) {
			border-top: 1px solid var(--mf-line);
		}

		.decision-panel {
			padding: 16px;
		}

		.panel-heading {
			flex-direction: column;
		}

		.panel-heading > span {
			max-width: none;
			text-align: left;
		}

		.sample-form {
			grid-template-columns: 1fr;
		}

		.member-row {
			grid-template-columns: 1fr;
		}

		.member-row__actions {
			align-items: start;
			justify-items: start;
		}
	}
</style>
