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
		onMutate?: () => Promise<void>;
		loadError?: string | null;
	} = $props();

	let note = $state('');
	let selectedHostKey = $state('');
	let pendingAction = $state('');
	let actionMessage = $state('');
	let actionError = $state('');

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
	const calibration = $derived(asRecord(folder.calibration));
	const pendingProposal = $derived(asRecord(folder.pending_proposal));
	const pendingProposalCanQueue = $derived(pendingProposal.can_queue === true);
	const reviewGate = $derived(asRecord(folder.review_gate));
	const calibrationJob = $derived(asRecord(folder.calibration_job));
	const encodeJob = $derived(folder.encode_job ?? null);
	const sampleItem = $derived(asRecord(folder.sample_item));
	const hostOptions = $derived(
		(folder.sample_host_options ?? [])
			.map((host) => asRecord(host))
			.filter((host) => asText(host.key) && host.available !== false)
	);
	const activeHostCount = $derived(hosts.hosts.filter((host) => host.available).length);
	const isBrowseOnly = $derived(context?.availability === 'browse_only');
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
	const scopeNoun = $derived(exactScope ? 'movie file' : 'movie title');

	$effect(() => {
		const folderHostKey = String(folder.sample_host_key ?? '').trim();
		if (!selectedHostKey || !hostOptions.some((host) => asText(host.key) === selectedHostKey)) {
			selectedHostKey = folderHostKey || asText(hostOptions[0]?.key);
		}
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
			note = '';
			return response.message || 'Sample plan ready. Review it before starting work.';
		});
	}

	async function startSample() {
		if (isBrowseOnly || isBusy || !pendingProposalCanQueue) return;
		await runAction('start-sample', async () => {
			const response = await postJson<FolderBenchConfirmResponse>(
				`/api/folders/${folderRoutePrefix(folder.prefix)}/ai-tune/confirm`,
				{ proposal_id: asText(pendingProposal.proposal_id) }
			);
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
			return response.message || `Queued this ${scopeNoun}.`;
		});
	}

	async function validateOutputs() {
		await folderAction('validate-outputs', 'Validated the ready movie output.');
	}

	async function promoteOutputs() {
		if (conflicts.length) return;
		await folderAction('promote-outputs', 'Promoted the validated movie output.');
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
			}>(`/api/folders/${folderRoutePrefix(folder.prefix)}/${endpoint}`, {});
			if (!response.ok) throw new Error(response.message || 'The movie action could not run.');
			return response.message || fallback;
		});
	}

	async function runAction(action: string, operation: () => Promise<string>) {
		pendingAction = action;
		actionMessage = '';
		actionError = '';
		try {
			actionMessage = await operation();
			await onMutate();
		} catch (error) {
			actionError = error instanceof Error ? error.message : 'The movie action could not run.';
		} finally {
			pendingAction = '';
		}
	}

	function primaryAction():
		| 'prepare'
		| 'start'
		| 'monitor-sample'
		| 'queue'
		| 'validate'
		| 'promote'
		| 'retry'
		| 'monitor'
		| 'none' {
		if (isBrowseOnly) return 'none';
		const calibrationStatus = asText(calibrationJob.status);
		if (['queued', 'running'].includes(calibrationStatus)) return 'monitor-sample';
		const encodeStatus = String(encodeJob?.status ?? '');
		if (['queued', 'running', 'retry_backoff'].includes(encodeStatus)) return 'monitor';
		if (['failed', 'stopped', 'needs_attention'].includes(encodeStatus)) return 'retry';
		if (workflow?.next_action.kind === 'validate_outputs') return 'validate';
		if (workflow?.next_action.kind === 'promote_outputs') return 'promote';
		if (Object.keys(pendingProposal).length && pendingProposalCanQueue) return 'start';
		if (reviewGate.status === 'accepted') return 'queue';
		if (reviewReady) return 'queue';
		return 'prepare';
	}

	function badgeTone(): 'active' | 'ready' | 'wait' | 'fail' | 'idle' {
		if (conflicts.length || workflow?.tone === 'attention') return 'fail';
		if (workflow?.tone === 'active') return 'active';
		if (workflow?.tone === 'ready' || workflow?.tone === 'success') return 'ready';
		if (workflow?.state === 'explicit_selection_required') return 'wait';
		return 'idle';
	}

	function formatBytes(value: unknown): string {
		let size = Number(value ?? 0);
		if (!Number.isFinite(size) || size <= 0) return 'Unknown';
		const units = ['B', 'KB', 'MB', 'GB', 'TB'];
		let unit = 0;
		while (size >= 1024 && unit < units.length - 1) {
			size /= 1024;
			unit += 1;
		}
		return `${size >= 10 ? size.toFixed(0) : size.toFixed(1)} ${units[unit]}`;
	}

	function memberRole(member: MovieMember): string {
		if (member.edition_label) return member.edition_label;
		if (member.role === 'feature') return 'Main feature';
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

<svelte:head>
	<title>{title} · Movie Studio · Mediaforce</title>
</svelte:head>

<main class="movie-studio" data-folder-ready-marker={title}>
	<nav class="breadcrumb" aria-label="Breadcrumb">
		<a href={resolve('/movies')}>Movies</a><span aria-hidden="true">/</span><span>{title}</span>
	</nav>

	<header class="studio-heading">
		<div>
			<span class="eyebrow">Movie Studio · {exactScope ? 'Exact file' : 'Title scope'}</span>
			<h1>{title}</h1>
			<p>{folder.prefix}</p>
		</div>
		<StateBadge
			tone={badgeTone()}
			label={conflicts.length ? 'Promotion conflict' : (workflow?.label ?? 'Loading')}
		/>
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
		<div class="notice" role="status">
			<strong>Movie state updated.</strong><span>{actionMessage}</span>
		</div>
	{/if}
	{#if isBrowseOnly}
		<div class="notice" role="status">
			<strong>Browse-only movie library</strong>
			<span
				>Files and evidence remain visible. Production actions stay disabled until this root is set
				to Production in Settings.</span
			>
		</div>
	{/if}
	{#if conflicts.length}
		<div class="notice notice--danger" role="alert">
			<strong>Promotion is blocked before any file is replaced.</strong>
			{#each conflicts as conflict (`${conflict.kind}:${conflict.destination_path}`)}
				<span>{conflict.detail} <code>{conflict.destination_path}</code></span>
			{/each}
		</div>
	{/if}

	<section class="status-strip" aria-label="Movie workflow status">
		<div><span>Scope</span><strong>{exactScope ? 'Exact movie file' : 'Movie title'}</strong></div>
		<div><span>Workflow</span><strong>{workflow?.label ?? 'Loading'}</strong></div>
		<div>
			<span>Sample</span><strong
				>{asText(calibrationJob.status) || (reviewReady ? 'Review ready' : 'Not prepared')}</strong
			>
		</div>
		<div>
			<span>Processing</span><strong
				>{String(encodeJob?.status ?? 'Idle').replaceAll('_', ' ')}</strong
			>
		</div>
		<div><span>Hosts online</span><strong>{activeHostCount}</strong></div>
	</section>

	<div class="studio-grid">
		<div class="studio-grid__main">
			<WorkstationPanel eyebrow="Decision" title="Next movie action" meta={scopeNoun}>
				<div class="decision-panel">
					<div class="decision-copy">
						<strong>{workflow?.detail ?? 'Reading workflow state and sample evidence.'}</strong>
						<p>
							{exactScope
								? 'This action applies only to the selected file.'
								: 'Title-wide work includes identified feature files. Extras and uncertain files remain exact-selection only.'}
						</p>
					</div>
					<div class="decision-actions">
						{#if primaryAction() === 'prepare'}
							<button
								class="primary"
								disabled={folderPending || isBusy || !selectedHostKey}
								onclick={prepareSample}
							>
								{pendingAction === 'prepare-sample' ? 'Preparing…' : 'Prepare review sample'}
							</button>
						{:else if primaryAction() === 'start'}
							<button class="primary" disabled={isBusy} onclick={startSample}>
								{pendingAction === 'start-sample' ? 'Starting…' : 'Start planned sample'}
							</button>
						{:else if primaryAction() === 'monitor-sample'}
							<a class="primary" href={resolve('/ops')}>Monitor sample</a>
							<button class="secondary" disabled={isBusy} onclick={stopSample}
								>Stop sample work</button
							>
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
								>Validate outputs</button
							>
						{:else if primaryAction() === 'promote'}
							<button
								class="primary"
								disabled={isBusy || conflicts.length > 0}
								onclick={promoteOutputs}>Promote safely</button
							>
						{:else if primaryAction() === 'retry'}
							<button class="primary" disabled={isBusy} onclick={retryEncode}
								>Resume unfinished work</button
							>
						{:else if primaryAction() === 'monitor'}
							<a class="primary" href={resolve('/ops')}>Monitor processing</a>
						{:else}
							<a class="primary" href={resolve('/settings')}>Open library settings</a>
						{/if}
					</div>
				</div>
			</WorkstationPanel>

			<WorkstationPanel
				eyebrow="Sample bench"
				title="Prepare and review"
				meta={reviewReady ? 'Evidence ready' : 'No approved sample'}
			>
				<div class="sample-bench">
					{#if Object.keys(pendingProposal).length}
						<div class:sample-plan--blocked={!pendingProposalCanQueue} class="sample-plan">
							<strong
								>{pendingProposalCanQueue
									? 'Sample plan is ready'
									: 'Sample plan needs another request'}</strong
							>
							<p>
								{pendingProposalCanQueue
									? 'Nothing starts until you confirm this plan.'
									: asText(pendingProposal.message) ||
										asText(pendingProposal.suggested_follow_up) ||
										'The bench did not produce a queueable sample plan.'}
							</p>
							{#if pendingProposalCanQueue}
								<button class="secondary" disabled={isBusy || isBrowseOnly} onclick={startSample}
									>Start sample</button
								>
							{/if}
						</div>
					{/if}
					<div class="sample-facts">
						<div>
							<span>Representative file</span><strong
								>{asText(sampleItem.rel_path) || activeMember?.label || 'Pending'}</strong
							>
						</div>
						<div>
							<span>Source size</span><strong
								>{formatBytes(sampleItem.source_size_bytes ?? activeMember?.size_bytes)}</strong
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
							<span>Review gate</span><strong
								>{asText(reviewGate.status).replaceAll('_', ' ') || 'Not reviewed'}</strong
							>
						</div>
					</div>

					<label class="request-field">
						<span>Operator request</span>
						<textarea
							bind:value={note}
							rows="4"
							placeholder="Example: preserve grain and make the feature about 35% smaller."
						></textarea>
					</label>
					<div class="bench-controls">
						<label>
							<span>Sample host</span>
							<select bind:value={selectedHostKey} disabled={!hostOptions.length}>
								{#if !hostOptions.length}<option value="">No sample host available</option>{/if}
								{#each hostOptions as host (asText(host.key))}
									<option value={asText(host.key)}>{asText(host.label) || asText(host.key)}</option>
								{/each}
							</select>
						</label>
						<button
							class="secondary"
							disabled={isBusy || isBrowseOnly || !selectedHostKey}
							onclick={prepareSample}>Prepare new sample</button
						>
						{#if status.retryable_sample_job}
							<button class="secondary" disabled={isBusy || isBrowseOnly} onclick={retrySample}
								>Retry failed sample</button
							>
						{/if}
						{#if reviewReady}
							<button class="secondary" type="button" onclick={downloadReviewPack}
								>Download review pack</button
							>
						{/if}
					</div>
				</div>
			</WorkstationPanel>
		</div>

		<aside class="studio-grid__rail">
			<WorkstationPanel
				eyebrow="Title scope"
				title="Files and editions"
				meta={`${context?.members.length ?? 0} indexed`}
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
									{formatBytes(member.size_bytes)} · {member.status.replaceAll('_', ' ')}
									{member.included_by_default ? ' · title work' : ' · exact only'}
								</small>
							</div>
							<a href={resolve(folderRoutePath(member.prefix))}>Open</a>
						</div>
					{/each}
					{#if !context?.members.length}
						<div class="panel-empty">Movie membership is still loading.</div>
					{/if}
				</div>
			</WorkstationPanel>

			<WorkstationPanel
				eyebrow="Safety"
				title="Selection contract"
				meta={exactScope ? 'Exact match' : 'Feature default'}
			>
				<div class="safety-list">
					<div>
						<strong>Features</strong><span>Included in title-wide sample and encode work.</span>
					</div>
					<div>
						<strong>Editions</strong><span
							>Remain separate files with independent destinations.</span
						>
					</div>
					<div>
						<strong>Extras</strong><span
							>Visible here; open an exact file to process one deliberately.</span
						>
					</div>
					<div>
						<strong>Promotion</strong><span
							>Destination collisions block the entire action before replacement.</span
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

	.status-strip {
		background: var(--mf-bg-strip);
		border: var(--mf-border);
		display: grid;
		grid-template-columns: repeat(5, minmax(0, 1fr));
		margin-bottom: var(--mf-space-6);
	}

	.status-strip div {
		border-left: var(--mf-border-muted);
		padding: 10px 12px;
	}

	.status-strip div:first-child {
		border-left: 0;
	}

	.status-strip span,
	.status-strip strong {
		display: block;
	}

	.status-strip span {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-bold);
		letter-spacing: 0.06em;
		text-transform: uppercase;
	}

	.status-strip strong {
		font-size: var(--mf-text-sm);
		margin-top: 3px;
		text-transform: capitalize;
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
		align-items: center;
		display: flex;
		gap: var(--mf-space-7);
		justify-content: space-between;
		padding: var(--mf-space-7);
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

	.decision-actions,
	.bench-controls {
		align-items: center;
		display: flex;
		flex-wrap: wrap;
		gap: var(--mf-space-4);
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
		background: var(--mf-attention-bg);
		border-color: var(--mf-attention-line);
	}

	.sample-plan--blocked strong {
		color: var(--mf-attention-fg);
	}

	.sample-plan .secondary {
		justify-self: start;
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

	@media (max-width: 980px) {
		.status-strip {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}

		.status-strip div:nth-child(odd) {
			border-left: 0;
		}

		.status-strip div {
			border-bottom: var(--mf-border-muted);
		}

		.status-strip div:last-child {
			grid-column: 1 / -1;
		}

		.studio-grid {
			grid-template-columns: minmax(0, 1fr);
		}
	}

	@media (max-width: 680px) {
		.movie-studio {
			padding: 18px 12px 44px;
		}

		.studio-heading,
		.decision-panel {
			align-items: stretch;
			flex-direction: column;
		}

		.status-strip,
		.sample-facts {
			grid-template-columns: minmax(0, 1fr);
		}

		.status-strip div,
		.status-strip div:nth-child(odd),
		.sample-facts div,
		.sample-facts div:nth-child(2n),
		.sample-facts div:nth-last-child(-n + 2) {
			border-bottom: var(--mf-border-muted);
			border-left: 0;
			border-right: 0;
		}

		.status-strip div:last-child,
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
