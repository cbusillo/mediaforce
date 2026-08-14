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
	let confirmedMembershipToken = $state('');

	const context = $derived(folder.other_context ?? null);
	const workflow = $derived(status.workflow_state ?? folder.workflow_state ?? null);
	const calibration = $derived(asRecord(folder.calibration));
	const pendingProposal = $derived(asRecord(folder.pending_proposal));
	const pendingProposalCanQueue = $derived(pendingProposal.can_queue === true);
	const calibrationJob = $derived(asRecord(folder.calibration_job));
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
	const reviewPackHref = $derived(
		apiDownloadHref(`/api/folders/${folderRoutePrefix(folder.prefix)}/review-compare/download`)
	);
	const workflowDetail = $derived(
		normalizeWorkflowDetail(
			workflow?.detail ?? 'Workflow details are loading.',
			context?.item_count ?? 0
		)
	);
	const decisionTitle = $derived.by(() => {
		if (workflow?.primary_lane === 'processing') return 'Encoding in progress';
		if (workflow?.primary_lane === 'validate') return 'Validate encoded output';
		if (workflow?.primary_lane === 'promote') return 'Promote validated output';
		if (['blocked', 'attention'].includes(workflow?.primary_lane ?? ''))
			return 'Resolve the workflow blocker';
		return `Prepare and deliver this ${scopeNoun}`;
	});
	const activeOperationCopy = $derived.by(() => {
		const host = asRecord(encodeJob?.host);
		const worker =
			asText(host.label) ||
			asText(host.key) ||
			encodeJob?.progress?.active_host_labels?.[0] ||
			'Unassigned';
		const percent = encodeJob?.progress?.percent_complete;
		const parts = [
			percent != null ? `${Math.round(percent)}% complete` : 'Progress pending',
			`Worker: ${worker}`,
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
			return response.message || 'Sample plan ready. Review it before starting work.';
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
			return response.message || 'Sample run queued.';
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
			return response.message || 'Sample retry queued.';
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
			return response.message || `Approved the sample for this ${scopeNoun}.`;
		});
	}

	function openReviewPack() {
		window.location.assign(reviewPackHref);
	}

	async function queueApproved() {
		if (!actionReady || isBusy) return;
		await runAction('queue-work', async () => {
			const response = await postJson<{ ok: boolean; message?: string }>(
				`/api/folders/${folderRoutePrefix(folder.prefix)}/queue-encode`,
				{
					notes: '',
					bypass_schedule: false,
					scope_membership_token: scopeMembershipToken()
				}
			);
			if (!response.ok) throw new Error(response.message || 'This work could not be queued.');
			return response.message || `Queued this ${scopeNoun}.`;
		});
	}

	async function validateOutputs() {
		await folderAction('validate-outputs', 'Validated the ready Other output.');
	}

	async function promoteOutputs() {
		await folderAction('promote-outputs', 'Promoted the validated Other output.');
	}

	async function folderAction(action: string, successMessage: string) {
		if (isBusy) return;
		await runAction(action, async () => {
			const response = await postJson<{ ok: boolean; message?: string; target_prefix?: string }>(
				`/api/folders/${folderRoutePrefix(folder.prefix)}/${action}`,
				{ scope_membership_token: scopeMembershipToken() }
			);
			if (!response.ok) throw new Error(response.message || `${action} failed.`);
			return {
				message: response.message || successMessage,
				targetPrefix: action === 'promote-outputs' ? response.target_prefix : undefined
			};
		});
	}

	type ActionResult = string | { message: string; targetPrefix?: string };

	async function runAction(action: string, execute: () => Promise<ActionResult>) {
		pendingAction = action;
		actionMessage = '';
		actionError = '';
		let actionCompleted = false;
		try {
			const result = await execute();
			actionMessage = typeof result === 'string' ? result : result.message;
			actionCompleted = true;
			await onMutate(typeof result === 'string' ? undefined : result.targetPrefix);
		} catch (error) {
			const message = error instanceof Error ? error.message : 'Studio could not refresh.';
			actionError = actionCompleted
				? `The action completed, but Studio could not refresh. ${message}`
				: message;
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

	function formatBytes(value: number | null | undefined): string {
		if (value == null) return 'Unknown';
		if (value < 1024) return `${value} B`;
		const units = ['KB', 'MB', 'GB', 'TB', 'PB'];
		let current = value / 1024;
		let unit = 0;
		while (current >= 1024 && unit < units.length - 1) {
			current /= 1024;
			unit += 1;
		}
		return `${current >= 10 ? current.toFixed(0) : current.toFixed(1)} ${units[unit]}`;
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

	function workflowTone(): string {
		if (readiness?.state === 'blocked') return 'fail';
		if (isBrowseOnly) return 'wait';
		if (workflow?.primary_lane === 'processing') return 'active';
		if (['encode', 'validate', 'promote'].includes(workflow?.primary_lane ?? '')) return 'ready';
		if (['blocked', 'attention'].includes(workflow?.primary_lane ?? '')) return 'fail';
		return 'idle';
	}

	function memberState(member: OtherMember): string {
		if (workflow?.primary_lane === 'processing') return 'processing';
		return member.workflow_state?.state.replaceAll('_', ' ') ?? member.status.replaceAll('_', ' ');
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

	function normalizeWorkflowDetail(detail: string, itemCount: number): string {
		if (itemCount !== 1) {
			return detail.replace('item(s)', 'items').replace('output(s)', 'outputs');
		}
		return detail
			.replace('item(s)', 'item')
			.replace('output(s)', 'output')
			.replace(' output need ', ' output needs ')
			.replace(' output are ', ' output is ');
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
				<span class="eyebrow"
					>{folder.media_scope.match === 'exact_item' ? 'Exact file' : 'Bounded folder'}</span
				>
				<h1>{title}</h1>
				<p>{folder.prefix}</p>
			</div>
		</div>
		<span class="state-badge" data-tone={workflowTone()}>
			{readiness?.state === 'ready'
				? (workflow?.label ?? 'Ready')
				: (readiness?.label ?? 'Loading')}
		</span>
	</header>

	<section class="status-strip" aria-label="Other Studio status">
		<div>
			<span>Scope</span><strong
				>{context?.item_count ?? 0} {context?.item_count === 1 ? 'file' : 'files'}</strong
			>
		</div>
		<div><span>Profile</span><strong>{readiness?.profile_label ?? 'Loading'}</strong></div>
		<div><span>Workflow</span><strong>{workflow?.label ?? 'Loading'}</strong></div>
		<div>
			<span>Sample</span><strong
				>{reviewReady
					? approved
						? 'Approved'
						: 'Review ready'
					: asText(calibrationJob.status) || 'Not started'}</strong
			>
		</div>
		<div>
			<span>Processing</span><strong>{encodeJob?.status?.replaceAll('_', ' ') ?? 'Idle'}</strong>
		</div>
		<div><span>Workers online</span><strong>{activeWorkerCount}</strong></div>
	</section>

	{#if loadError}
		<div class="notice notice--danger" role="alert">
			<strong>Studio update failed</strong><span>{loadError}</span>
		</div>
	{/if}
	{#if actionError}
		<div class="notice notice--danger" role="alert">
			<strong>Action failed</strong><span>{actionError}</span>
		</div>
	{/if}
	{#if actionMessage}
		<div class="notice notice--success" role="status">
			<strong>Updated</strong><span>{actionMessage}</span>
		</div>
	{/if}
	{#if isBrowseOnly}
		<div class="notice">
			<strong>Browse only</strong><span
				>Enable Production for this root in Settings before sampling or queueing work.</span
			>
		</div>
	{:else if readiness?.state === 'blocked'}
		<div class="notice notice--danger">
			<strong>{readiness.label}</strong><span>{readiness.detail}</span>
			{#if readiness.blockers.length}
				<ul>
					{#each readiness.blockers as blocker (blocker)}<li>{blocker}</li>{/each}
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
				<span>{folderPending ? 'Refreshing…' : workflowDetail}</span>
			</header>

			<div class="scope-contract">
				<div>
					<span>Exact membership</span><strong
						>{context?.membership_complete
							? `${context.item_count} indexed ${context.item_count === 1 ? 'file' : 'files'}`
							: `More than ${context?.membership_limit ?? 250} files`}</strong
					>
				</div>
				<div><span>Stored size</span><strong>{formatBytes(context?.total_size_bytes)}</strong></div>
				<div>
					<span>Grouping</span><strong
						>{folder.media_scope.match === 'exact_item'
							? 'One exact file'
							: 'All descendants'}</strong
					>
				</div>
				<div>
					<span>Semantics</span><strong>No title, season, edition, or spatial inference</strong>
				</div>
			</div>

			{#if requiresMembershipConfirmation}
				<label class="confirmation" class:is-disabled={!scopeReady}>
					<input
						type="checkbox"
						checked={membershipConfirmed}
						disabled={!scopeReady}
						onchange={(event) =>
							updateMembershipConfirmation((event.currentTarget as HTMLInputElement).checked)}
					/>
					<span>
						<strong>I reviewed all {context?.item_count ?? 0} files in this folder scope.</strong>
						<small>Sampling and production apply to every eligible file listed in Membership.</small
						>
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
						<span>Worker</span>
						<select bind:value={selectedHostKey} disabled={!hostOptions.length}>
							{#if !hostOptions.length}<option value="">No sample worker available</option>{/if}
							{#each hostOptions as host (asText(host.key))}
								<option value={asText(host.key)}>{asText(host.label) || asText(host.key)}</option>
							{/each}
						</select>
					</label>
				</div>
			{/if}

			<div class="action-row">
				{#if workflow?.primary_lane === 'processing'}
					<span class="operation-state"
						><strong>Production is active.</strong>{activeOperationCopy}</span
					>
				{:else if workflow?.primary_lane === 'validate'}
					<button class="primary" disabled={isBusy || isBrowseOnly} onclick={validateOutputs}
						>Validate ready output</button
					>
				{:else if workflow?.primary_lane === 'promote'}
					<button class="primary" disabled={isBusy || isBrowseOnly} onclick={promoteOutputs}
						>Promote validated output</button
					>
				{:else if reviewReady && !approved}
					<button class="secondary" type="button" onclick={openReviewPack}>Open review pack</button>
					<button class="primary" disabled={!actionReady || isBusy} onclick={approveSample}
						>Approve reviewed sample</button
					>
				{:else if approved && workflow?.primary_lane === 'encode'}
					<button class="primary" disabled={!actionReady || isBusy} onclick={queueApproved}
						>Queue {context?.eligible_item_count ?? 0} eligible files</button
					>
				{:else if pendingProposalCanQueue}
					<button class="primary" disabled={!actionReady || isBusy} onclick={startSample}
						>Start proposed sample</button
					>
				{:else if ['failed', 'stopped'].includes(asText(calibrationJob.status))}
					<button class="primary" disabled={!actionReady || isBusy} onclick={retrySample}
						>Retry sample</button
					>
				{:else}
					<button
						class="primary"
						disabled={!actionReady || isBusy || !hostOptions.length}
						onclick={prepareSample}>Prepare representative sample</button
					>
				{/if}
				{#if requiresMembershipConfirmation && !membershipConfirmed}
					<span class="action-help"
						>{hostOptions.length
							? 'Confirm membership to enable sample and queue actions.'
							: 'Confirm membership and bring a sample-capable worker online to prepare a sample.'}</span
					>
				{:else if readiness?.state !== 'ready'}
					<span class="action-help">Resolve profile readiness before starting work.</span>
				{:else if showSampleControls && !hostOptions.length && !reviewReady}
					<span class="action-help">Bring a sample-capable worker online.</span>
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
					<strong>Scope exceeds the safe membership limit.</strong>
					<span
						>Switch this root to exact-file grouping or split the source folder before processing.</span
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
							<a href={resolve(folderRoutePath(member.prefix))}>Open exact file</a>
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

	.state-badge {
		border: 1px solid var(--mf-line-strong);
		color: var(--mf-fg-secondary);
		font-size: 10px;
		font-weight: 800;
		letter-spacing: 0.05em;
		padding: 5px 7px;
		text-transform: uppercase;
	}

	.state-badge[data-tone='ready'] {
		border-color: var(--mf-ready-fg);
		color: var(--mf-ready-fg);
	}

	.state-badge[data-tone='active'] {
		background: var(--mf-active-bg-strong);
		border-color: var(--mf-active-fg);
		color: var(--mf-active-fg-bright);
	}

	.state-badge[data-tone='wait'] {
		border-color: var(--mf-wait-fg);
		color: var(--mf-wait-fg);
	}

	.state-badge[data-tone='fail'] {
		border-color: var(--mf-fail-fg);
		color: var(--mf-fail-fg);
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
		margin-top: 3px;
		overflow: hidden;
		text-overflow: ellipsis;
		text-transform: capitalize;
		white-space: nowrap;
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

		.state-badge {
			align-self: flex-start;
		}

		.status-strip {
			grid-template-columns: 1fr 1fr;
		}

		.status-strip div:nth-child(odd) {
			border-left: 0;
		}

		.status-strip div:nth-child(n + 3) {
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

		.scope-contract,
		.sample-form {
			grid-template-columns: 1fr;
		}

		.scope-contract div,
		.scope-contract div:nth-child(odd),
		.scope-contract div:nth-last-child(-n + 2) {
			border-bottom: 1px solid var(--mf-line);
			border-right: 0;
		}

		.scope-contract div:last-child {
			border-bottom: 0;
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
