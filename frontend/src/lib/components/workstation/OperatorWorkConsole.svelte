<script lang="ts">
	import { postJson } from '$lib/api/client';
	import type {
		OperatorEvidenceBacklogRow,
		OperatorWorkActionResult,
		OperatorWorkPayload
	} from '$lib/api/types';
	import StateBadge from './StateBadge.svelte';
	import WorkstationPanel from './WorkstationPanel.svelte';
	import {
		catalogStateView,
		evidenceKindLabel,
		evidenceReasonCopy,
		evidenceStateLabel,
		evidenceStateTone,
		evidenceStateView,
		evidenceWorkStatusView,
		formatCount,
		formatTimestamp,
		scopeLabel,
		type OperatorWorkQuery
	} from './operator-work';

	let {
		work,
		onRefresh = async () => {}
	}: {
		work: OperatorWorkPayload | null;
		onRefresh?: (query?: OperatorWorkQuery) => Promise<void>;
	} = $props();

	const catalogView = $derived(catalogStateView(work?.catalog));
	const evidenceView = $derived(evidenceStateView(work?.evidence));
	const backgroundPaused = $derived(work?.background.is_paused ?? false);
	const backlog = $derived(work?.evidence.backlog ?? null);
	const queue = $derived(work?.evidence.queue ?? null);
	const progress = $derived(work?.evidence.progress ?? null);
	const currentItem = $derived(progress?.running ?? null);
	const overallLabel = $derived(
		backgroundPaused
			? 'Background work paused'
			: work?.refresh.mode === 'active'
				? 'Work active'
				: 'Quiet'
	);
	const overallTone = $derived(
		backgroundPaused ? 'wait' : work?.refresh.mode === 'active' ? 'active' : 'idle'
	);

	let actionPending = $state('');
	let actionMessage = $state('');
	let actionError = $state('');
	let cancelArmed = $state(false);
	let prepareOpen = $state(false);
	let prepareScope = $state('');
	let prepareLimit = $state('5');
	let prepareCadence = $state(true);
	let prepareFingerprint = $state(true);
	let evidenceKindFilter = $state('');
	let evidenceStateFilter = $state('');
	let mediaRootFilter = $state('');
	let workStatusFilter = $state('');

	function currentQuery(offset = backlog?.offset ?? 0): OperatorWorkQuery {
		return {
			offset,
			limit: backlog?.limit ?? 25,
			evidenceKind: evidenceKindFilter || undefined,
			state: evidenceStateFilter || undefined,
			mediaRoot: mediaRootFilter || undefined,
			workStatus: workStatusFilter || undefined
		};
	}

	async function runAction(id: string, path: string, body: unknown = {}) {
		actionPending = id;
		actionMessage = '';
		actionError = '';
		cancelArmed = false;
		try {
			const result = await postJson<OperatorWorkActionResult>(path, body);
			actionMessage = result.message;
			await onRefresh(currentQuery());
		} catch (error) {
			actionError = error instanceof Error ? error.message : 'The action could not be completed.';
		} finally {
			actionPending = '';
		}
	}

	async function prepareAnalysis() {
		const prefix = prepareScope.trim().replace(/^\/+|\/+$/g, '');
		const evidenceKinds = [
			...(prepareCadence ? ['cadence_analysis'] : []),
			...(prepareFingerprint ? ['media_fingerprint'] : [])
		];
		if (!prefix) {
			actionError = 'Enter one library, folder, or file scope before preparing analysis.';
			return;
		}
		if (!evidenceKinds.length) {
			actionError = 'Choose at least one kind of evidence.';
			return;
		}
		await runAction('prepare', '/api/evidence-work/prepare', {
			prefix,
			evidence_kinds: evidenceKinds,
			limit: Number(prepareLimit)
		});
		if (!actionError) prepareOpen = false;
	}

	async function applyFilters() {
		await onRefresh(currentQuery(0));
	}

	function useRowScope(row: OperatorEvidenceBacklogRow) {
		if (!work?.evidence.can_prepare) return;
		prepareScope = row.parent_dir || row.rel_path;
		prepareOpen = true;
		actionMessage = `Scope set to ${prepareScope}. Review the batch options before preparing it.`;
		actionError = '';
	}

	function catalogProgressCopy(): string {
		const stats = work?.catalog.job?.stats;
		const seen = stats?.total_seen ?? stats?.items_seen ?? 0;
		if (work?.catalog.status === 'running') {
			return seen ? `${formatCount(seen)} files checked so far` : 'Starting the library walk';
		}
		return `Last safe refresh ${formatTimestamp(work?.catalog.last_completed_at)}`;
	}

	function analysisMetric(): string {
		if (!work) return '—';
		if (queue?.batch_id && progress?.total_count) {
			return `${formatCount(progress.done_count)} of ${formatCount(progress.total_count)} updates complete`;
		}
		return `${formatCount(work.evidence.inventory.backlog_total)} need analysis`;
	}
</script>

<WorkstationPanel eyebrow="Catalog & analysis" title="Remembered media state">
	<div class="work-console">
		<div class="work-console__lead">
			<div class="work-console__lead-copy">
				<StateBadge tone={overallTone} label={overallLabel} />
				<div>
					<strong>Heavy media reads happen only when you ask.</strong>
					<p>
						Mediaforce keeps catalog facts and analysis results, then updates only the scope you
						prepare. Browsing this page never starts FFmpeg.
					</p>
				</div>
			</div>
			<button
				type="button"
				class="work-button work-button--quiet"
				disabled={Boolean(actionPending)}
				onclick={() =>
					runAction(
						'background',
						backgroundPaused ? '/api/background-work/resume' : '/api/background-work/pause'
					)}
			>
				{actionPending === 'background'
					? 'Working…'
					: backgroundPaused
						? 'Resume background work'
						: 'Pause new background work'}
			</button>
		</div>

		<div class="work-lanes">
			<section class="work-lane work-lane--catalog" aria-labelledby="catalog-work-title">
				<header class="work-lane__header">
					<div>
						<span class="work-lane__eyebrow">Catalog inventory</span>
						<h4 id="catalog-work-title">
							{formatCount(work?.catalog.item_count ?? 0)} remembered files
						</h4>
					</div>
					<StateBadge tone={catalogView.tone} label={catalogView.label} />
				</header>
				<p class="work-lane__detail">{catalogView.detail}</p>
				<div class="work-lane__metric">
					<strong>{catalogProgressCopy()}</strong>
					<span>
						{work?.catalog.source_roots
							.map((root) => `${root.label} ${formatCount(root.item_count)}`)
							.join(' · ') || 'No production libraries configured'}
					</span>
				</div>
				{#if work?.catalog.warnings.length}
					<div class="source-warnings" aria-label="Catalog source warnings">
						{#each work.catalog.warnings as warning (warning.library_key + warning.code)}
							<div class="source-warning">
								<strong>{warning.label} stayed remembered</strong>
								<span>{warning.message}</span>
							</div>
						{/each}
					</div>
				{/if}
				<div class="work-lane__actions">
					<button
						type="button"
						class="work-button"
						disabled={!work?.catalog.can_refresh || Boolean(actionPending)}
						title={backgroundPaused
							? 'Resume background work before refreshing the catalog.'
							: work?.catalog.status === 'running' || work?.catalog.status === 'queued'
								? 'A catalog refresh is already active.'
								: 'Read library folders and update changed file facts.'}
						onclick={() => runAction('catalog', '/api/catalog/refresh')}
					>
						{actionPending === 'catalog' ? 'Starting…' : 'Refresh catalog'}
					</button>
					<span>No cadence or fingerprint analysis starts here.</span>
				</div>
			</section>

			<section class="work-lane work-lane--evidence" aria-labelledby="evidence-work-title">
				<header class="work-lane__header">
					<div>
						<span class="work-lane__eyebrow">Evidence analysis</span>
						<h4 id="evidence-work-title">{analysisMetric()}</h4>
					</div>
					<StateBadge tone={evidenceView.tone} label={evidenceView.label} />
				</header>
				<p class="work-lane__detail">{evidenceView.detail}</p>
				<div class="work-lane__metric">
					<strong>{scopeLabel(work?.evidence)}</strong>
					<span>
						{currentItem
							? `${evidenceKindLabel(currentItem.evidence_kind)} · ${currentItem.rel_path}`
							: queue?.batch_id
								? `${formatCount(progress?.remaining_count ?? 0)} remaining · updated ${formatTimestamp(queue.updated_at)}`
								: 'Prepare a bounded folder or library scope when evidence needs an update.'}
					</span>
				</div>
				{#if progress?.total_count}
					<div class="progress-track" aria-label={`${progress.percent ?? 0}% complete`}>
						<span style={`width: ${progress.percent ?? 0}%`}></span>
					</div>
				{/if}
				<div class="work-lane__actions work-lane__actions--cluster">
					{#if work?.evidence.can_pause}
						<button
							type="button"
							class="work-button"
							disabled={Boolean(actionPending)}
							onclick={() => runAction('pause-evidence', '/api/evidence-work/pause')}
						>
							{actionPending === 'pause-evidence' ? 'Pausing…' : 'Pause after this item'}
						</button>
					{:else if work?.evidence.can_resume}
						<button
							type="button"
							class="work-button work-button--primary"
							disabled={Boolean(actionPending)}
							onclick={() => runAction('resume-evidence', '/api/evidence-work/resume')}
						>
							{actionPending === 'resume-evidence'
								? 'Starting…'
								: evidenceView.label === 'Prepared'
									? 'Start analysis'
									: 'Resume analysis'}
						</button>
					{/if}
					{#if work?.evidence.can_cancel}
						<button
							type="button"
							class="work-button work-button--danger"
							class:work-button--armed={cancelArmed}
							disabled={Boolean(actionPending)}
							onclick={() =>
								cancelArmed
									? runAction('cancel-evidence', '/api/evidence-work/cancel')
									: (cancelArmed = true)}
						>
							{actionPending === 'cancel-evidence'
								? 'Cancelling…'
								: cancelArmed
									? 'Confirm cancel batch'
									: 'Cancel batch'}
						</button>
					{:else if work?.evidence.can_prepare}
						<button
							type="button"
							class="work-button work-button--primary"
							disabled={Boolean(actionPending) || backgroundPaused}
							onclick={() => (prepareOpen = !prepareOpen)}
						>
							{prepareOpen ? 'Close preparation' : 'Prepare analysis'}
						</button>
					{/if}
				</div>
			</section>
		</div>

		{#if prepareOpen}
			<form
				class="prepare-form"
				onsubmit={(event) => (event.preventDefault(), void prepareAnalysis())}
			>
				<div class="prepare-form__intro">
					<span class="work-lane__eyebrow">Prepare, then run</span>
					<strong>Create a small paused batch</strong>
					<p>This step only chooses work. FFmpeg does not start until you press Start analysis.</p>
				</div>
				<label class="field field--scope">
					<span>Library, folder, or file scope</span>
					<input bind:value={prepareScope} placeholder="tv/Show/Season 1" autocomplete="off" />
				</label>
				<fieldset class="kind-picker">
					<legend>Evidence to update</legend>
					<label><input type="checkbox" bind:checked={prepareCadence} /> Motion pattern</label>
					<label
						><input type="checkbox" bind:checked={prepareFingerprint} /> Media fingerprint</label
					>
				</fieldset>
				<label class="field">
					<span>Maximum updates</span>
					<select bind:value={prepareLimit}>
						<option value="1">1 update</option>
						<option value="5">5 updates</option>
						<option value="10">10 updates</option>
						<option value="25">25 updates</option>
					</select>
				</label>
				<button
					type="submit"
					class="work-button work-button--primary"
					disabled={Boolean(actionPending) || !work?.evidence.can_prepare}
					title={work?.evidence.can_prepare
						? 'Prepare this bounded evidence batch.'
						: 'Finish or cancel the active batch before preparing another scope.'}
				>
					{actionPending === 'prepare' ? 'Preparing…' : 'Prepare paused batch'}
				</button>
			</form>
		{/if}

		{#if actionMessage || actionError}
			<div class="action-note" class:action-note--error={Boolean(actionError)} aria-live="polite">
				{actionError || actionMessage}
			</div>
		{/if}

		<section class="backlog" aria-labelledby="evidence-backlog-title">
			<header class="backlog__header">
				<div>
					<span class="work-lane__eyebrow">Reachable backlog</span>
					<h4 id="evidence-backlog-title">Evidence that needs an update</h4>
				</div>
				<strong>
					{backlog?.total
						? `${formatCount(backlog.range_start)}–${formatCount(backlog.range_end)} of ${formatCount(backlog.total)}`
						: 'No matching items'}
				</strong>
			</header>

			<div class="backlog-filters">
				<label>
					<span>Evidence</span>
					<select bind:value={evidenceKindFilter} onchange={() => void applyFilters()}>
						<option value="">All evidence</option>
						<option value="cadence_analysis">Motion pattern</option>
						<option value="media_fingerprint">Media fingerprint</option>
					</select>
				</label>
				<label>
					<span>Reason</span>
					<select bind:value={evidenceStateFilter} onchange={() => void applyFilters()}>
						<option value="">All reasons</option>
						<option value="analysis_required">Needs analysis</option>
						<option value="classification_required">Needs update only</option>
					</select>
				</label>
				<label>
					<span>Library</span>
					<select bind:value={mediaRootFilter} onchange={() => void applyFilters()}>
						<option value="">All libraries</option>
						{#each work?.catalog.source_roots ?? [] as root (root.key)}
							<option value={root.key}>{root.label}</option>
						{/each}
					</select>
				</label>
				<label>
					<span>Batch state</span>
					<select bind:value={workStatusFilter} onchange={() => void applyFilters()}>
						<option value="">All batch states</option>
						<option value="not_prepared">Not prepared</option>
						<option value="queued">Prepared</option>
						<option value="running">Running</option>
						<option value="retry_wait">Retry scheduled</option>
						<option value="waiting_source">Source unavailable</option>
						<option value="failed">Failed</option>
						<option value="cancelled">Cancelled</option>
					</select>
				</label>
			</div>

			<div class="backlog-table-wrap">
				<table class="backlog-table">
					<thead>
						<tr>
							<th>Media</th>
							<th>Evidence</th>
							<th>Why</th>
							<th>Batch</th>
							<th>Attempts</th>
						</tr>
					</thead>
					<tbody>
						{#each backlog?.rows ?? [] as row (row.library_item_id + ':' + row.evidence_kind)}
							{@const workState = evidenceWorkStatusView(row)}
							<tr>
								<td data-label="Media">
									<strong>{row.file_name}</strong>
									<span class="path-copy">{row.parent_dir}</span>
									<button
										type="button"
										class="text-action"
										disabled={!work?.evidence.can_prepare}
										title={work?.evidence.can_prepare
											? 'Use this folder as the next bounded analysis scope.'
											: 'Finish or cancel the active batch before preparing another scope.'}
										onclick={() => useRowScope(row)}
									>
										Use this folder
									</button>
								</td>
								<td data-label="Evidence">
									<strong>{evidenceKindLabel(row.evidence_kind)}</strong>
									<StateBadge
										compact
										tone={evidenceStateTone(row.state)}
										label={evidenceStateLabel(row.state)}
									/>
								</td>
								<td data-label="Why">
									<span>{evidenceReasonCopy(row.reason)}</span>
								</td>
								<td data-label="Batch">
									<StateBadge compact tone={workState.tone} label={workState.label} />
									<span>{row.last_error || workState.detail}</span>
								</td>
								<td data-label="Attempts">
									<strong>{row.attempt_count}</strong>
									<span>
										{row.retry_not_before
											? `Next ${formatTimestamp(row.retry_not_before)}`
											: `Updated ${formatTimestamp(row.updated_at)}`}
									</span>
								</td>
							</tr>
						{:else}
							<tr>
								<td colspan="5" class="backlog-empty">
									<strong>No evidence matches these filters.</strong>
									<span>Change a filter or refresh the catalog to update remembered state.</span>
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>

			<footer class="backlog__footer">
				<span>Every number above is reachable here; pages never start analysis.</span>
				<div>
					<button
						type="button"
						class="work-button work-button--quiet"
						disabled={!backlog?.has_previous || Boolean(actionPending)}
						onclick={() =>
							void onRefresh(
								currentQuery(Math.max(0, (backlog?.offset ?? 0) - (backlog?.limit ?? 25)))
							)}
					>
						Previous
					</button>
					<button
						type="button"
						class="work-button work-button--quiet"
						disabled={!backlog?.has_next || Boolean(actionPending)}
						onclick={() =>
							void onRefresh(currentQuery((backlog?.offset ?? 0) + (backlog?.limit ?? 25)))}
					>
						Next
					</button>
				</div>
			</footer>
		</section>
	</div>
</WorkstationPanel>

<style>
	.work-console {
		color: var(--mf-fg-primary);
	}

	.work-console__lead {
		align-items: center;
		background: var(--mf-bg-strip);
		border-bottom: var(--mf-border-muted);
		display: flex;
		gap: var(--mf-space-6);
		justify-content: space-between;
		padding: var(--mf-space-5) var(--mf-space-6);
	}

	.work-console__lead-copy {
		align-items: flex-start;
		display: flex;
		gap: var(--mf-space-5);
		min-width: 0;
	}

	.work-console__lead-copy strong,
	.prepare-form__intro strong {
		display: block;
		font-size: var(--mf-text-sm);
		font-weight: var(--mf-weight-semibold);
	}

	.work-console__lead-copy p,
	.prepare-form__intro p {
		color: var(--mf-fg-secondary);
		font-size: var(--mf-text-xs);
		line-height: var(--mf-leading-normal);
		margin: 2px 0 0;
		max-width: 760px;
	}

	.work-lanes {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
	}

	.work-lane {
		display: flex;
		flex-direction: column;
		gap: var(--mf-space-5);
		min-width: 0;
		padding: var(--mf-space-6);
	}

	.work-lane + .work-lane {
		border-left: var(--mf-border-muted);
	}

	.work-lane--catalog {
		box-shadow: inset 3px 0 0 var(--mf-ready-line);
	}

	.work-lane--evidence {
		box-shadow: inset 3px 0 0 var(--mf-active-line);
	}

	.work-lane__header {
		align-items: flex-start;
		display: flex;
		gap: var(--mf-space-5);
		justify-content: space-between;
	}

	.work-lane__eyebrow {
		color: var(--mf-fg-tertiary);
		display: block;
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-semibold);
		letter-spacing: 0.08em;
		margin-bottom: var(--mf-space-2);
		text-transform: uppercase;
	}

	.work-lane h4,
	.backlog h4 {
		font-size: var(--mf-text-lg);
		font-weight: var(--mf-weight-semibold);
		letter-spacing: -0.01em;
		margin: 0;
	}

	.work-lane__detail {
		color: var(--mf-fg-secondary);
		font-size: var(--mf-text-sm);
		line-height: var(--mf-leading-normal);
		margin: 0;
	}

	.work-lane__metric {
		background: var(--mf-bg-panel-2);
		border: var(--mf-border-muted);
		border-radius: var(--mf-radius-2);
		display: flex;
		flex-direction: column;
		gap: var(--mf-space-2);
		min-height: 58px;
		padding: var(--mf-space-4) var(--mf-space-5);
	}

	.work-lane__metric strong {
		font-family: var(--mf-font-mono);
		font-size: var(--mf-text-xs);
		font-weight: var(--mf-weight-medium);
	}

	.work-lane__metric span,
	.work-lane__actions span,
	.source-warning span,
	.backlog-table td span,
	.backlog__footer > span {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-xs);
		line-height: var(--mf-leading-normal);
	}

	.source-warnings {
		display: grid;
		gap: var(--mf-space-3);
	}

	.source-warning {
		background: var(--mf-wait-bg);
		border-left: 3px solid var(--mf-wait-line);
		display: flex;
		flex-direction: column;
		gap: var(--mf-space-1);
		padding: var(--mf-space-4) var(--mf-space-5);
	}

	.source-warning strong {
		color: var(--mf-wait-fg);
		font-size: var(--mf-text-xs);
	}

	.work-lane__actions {
		align-items: center;
		display: flex;
		gap: var(--mf-space-4);
		margin-top: auto;
	}

	.work-lane__actions--cluster {
		flex-wrap: wrap;
	}

	.progress-track {
		background: var(--mf-bg-raised);
		border-radius: 999px;
		height: 6px;
		overflow: hidden;
	}

	.progress-track span {
		background: var(--mf-active-solid);
		display: block;
		height: 100%;
		transition: width var(--mf-dur-slow) var(--mf-ease);
	}

	.work-button {
		align-items: center;
		background: var(--mf-bg-panel);
		border: var(--mf-border-strong);
		border-radius: var(--mf-radius-1);
		color: var(--mf-fg-primary);
		cursor: pointer;
		display: inline-flex;
		font-family: var(--mf-font-sans);
		font-size: var(--mf-text-xs);
		font-weight: var(--mf-weight-semibold);
		justify-content: center;
		min-height: var(--mf-control-md);
		padding: 0 var(--mf-space-5);
		transition:
			background var(--mf-dur-fast) var(--mf-ease),
			border-color var(--mf-dur-fast) var(--mf-ease);
	}

	.work-button:hover:not(:disabled) {
		background: var(--mf-bg-raised);
	}

	.work-button:focus-visible,
	.text-action:focus-visible,
	input:focus-visible,
	select:focus-visible {
		outline: none;
		box-shadow: var(--mf-ring-focus);
	}

	.work-button:disabled {
		cursor: not-allowed;
		opacity: 0.46;
	}

	.work-button--primary {
		background: var(--mf-active-solid);
		border-color: var(--mf-active-solid);
		color: var(--mf-fg-on-accent);
	}

	.work-button--primary:hover:not(:disabled) {
		background: var(--mf-active-solid-hi);
	}

	.work-button--quiet {
		background: transparent;
	}

	.work-button--danger {
		color: var(--mf-fail-fg);
	}

	.work-button--danger.work-button--armed {
		background: var(--mf-fail-solid);
		border-color: var(--mf-fail-solid);
		color: var(--mf-fg-on-accent);
	}

	.prepare-form {
		align-items: end;
		background: var(--mf-active-bg);
		border-bottom: var(--mf-border-muted);
		border-top: 1px solid var(--mf-active-line);
		display: grid;
		gap: var(--mf-space-5);
		grid-template-columns: minmax(220px, 1.2fr) minmax(240px, 1.4fr) minmax(190px, 1fr) 130px auto;
		padding: var(--mf-space-6);
	}

	.prepare-form__intro {
		align-self: center;
	}

	.field,
	.kind-picker,
	.backlog-filters label {
		display: flex;
		flex-direction: column;
		gap: var(--mf-space-2);
		min-width: 0;
	}

	.field > span,
	.kind-picker legend,
	.backlog-filters label > span {
		color: var(--mf-fg-secondary);
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-semibold);
		letter-spacing: 0.03em;
	}

	.kind-picker {
		border: 0;
		margin: 0;
		padding: 0;
	}

	.kind-picker label {
		align-items: center;
		display: flex;
		font-size: var(--mf-text-xs);
		gap: var(--mf-space-3);
	}

	input,
	select {
		background: var(--mf-bg-input);
		border: var(--mf-border-strong);
		border-radius: var(--mf-radius-1);
		color: var(--mf-fg-primary);
		font-family: var(--mf-font-sans);
		font-size: var(--mf-text-xs);
		height: var(--mf-control-lg);
		min-width: 0;
		padding: 0 var(--mf-space-4);
	}

	.action-note {
		background: var(--mf-ready-bg);
		border-bottom: 1px solid var(--mf-ready-line);
		color: var(--mf-ready-fg);
		font-size: var(--mf-text-xs);
		font-weight: var(--mf-weight-medium);
		padding: var(--mf-space-4) var(--mf-space-6);
	}

	.action-note--error {
		background: var(--mf-fail-bg);
		border-color: var(--mf-fail-line);
		color: var(--mf-fail-fg);
	}

	.backlog {
		border-top: var(--mf-border-muted);
	}

	.backlog__header,
	.backlog__footer {
		align-items: center;
		display: flex;
		gap: var(--mf-space-5);
		justify-content: space-between;
		padding: var(--mf-space-5) var(--mf-space-6);
	}

	.backlog__header strong {
		color: var(--mf-fg-secondary);
		font-family: var(--mf-font-mono);
		font-size: var(--mf-text-xs);
	}

	.backlog-filters {
		background: var(--mf-bg-panel-2);
		border-bottom: var(--mf-border-muted);
		border-top: var(--mf-border-muted);
		display: grid;
		gap: var(--mf-space-4);
		grid-template-columns: repeat(4, minmax(130px, 1fr));
		padding: var(--mf-space-4) var(--mf-space-6);
	}

	.backlog-table-wrap {
		overflow-x: auto;
	}

	.backlog-table {
		border-collapse: collapse;
		font-size: var(--mf-text-xs);
		min-width: 720px;
		table-layout: fixed;
		width: 100%;
	}

	.backlog-table th:nth-child(1) {
		width: 25%;
	}

	.backlog-table th:nth-child(2) {
		width: 18%;
	}

	.backlog-table th:nth-child(3) {
		width: 24%;
	}

	.backlog-table th:nth-child(4) {
		width: 23%;
	}

	.backlog-table th:nth-child(5) {
		width: 10%;
	}

	.backlog-table th {
		background: var(--mf-bg-strip);
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-semibold);
		letter-spacing: 0.05em;
		padding: var(--mf-space-3) var(--mf-space-5);
		text-align: left;
		text-transform: uppercase;
	}

	.backlog-table td {
		border-top: var(--mf-border-muted);
		max-width: 300px;
		padding: var(--mf-space-4) var(--mf-space-5);
		vertical-align: top;
	}

	.backlog-table td > strong,
	.backlog-table td > span {
		display: block;
	}

	.backlog-table td > strong {
		font-weight: var(--mf-weight-semibold);
		margin-bottom: var(--mf-space-2);
	}

	.path-copy {
		font-family: var(--mf-font-mono);
		max-width: 280px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.text-action {
		background: none;
		border: 0;
		color: var(--mf-active-fg);
		cursor: pointer;
		font-family: var(--mf-font-sans);
		font-size: var(--mf-text-xs);
		font-weight: var(--mf-weight-semibold);
		margin-top: var(--mf-space-3);
		padding: 0;
	}

	.text-action:disabled {
		color: var(--mf-fg-quaternary);
		cursor: not-allowed;
	}

	.backlog-empty {
		padding: var(--mf-space-8) !important;
		text-align: center;
	}

	.backlog-empty span {
		margin-top: var(--mf-space-2);
	}

	.backlog__footer {
		background: var(--mf-bg-strip);
		border-top: var(--mf-border-muted);
	}

	.backlog__footer div {
		display: flex;
		gap: var(--mf-space-3);
	}

	@media (max-width: 1080px) {
		.prepare-form {
			grid-template-columns: 1fr 1.5fr 1fr;
		}

		.prepare-form__intro {
			grid-column: 1 / -1;
		}
	}

	@media (max-width: 760px) {
		.work-console__lead {
			align-items: stretch;
			flex-direction: column;
		}

		.work-console__lead-copy {
			flex-direction: column;
		}

		.work-lanes {
			grid-template-columns: 1fr;
		}

		.work-lane + .work-lane {
			border-left: 0;
			border-top: var(--mf-border-muted);
		}

		.prepare-form,
		.backlog-filters {
			grid-template-columns: 1fr 1fr;
		}

		.prepare-form__intro,
		.field--scope {
			grid-column: 1 / -1;
		}
	}

	@media (max-width: 520px) {
		.work-console__lead,
		.work-lane,
		.prepare-form,
		.backlog__header,
		.backlog__footer,
		.backlog-filters {
			padding-left: var(--mf-space-5);
			padding-right: var(--mf-space-5);
		}

		.work-lane__header,
		.backlog__header,
		.backlog__footer {
			align-items: flex-start;
			flex-direction: column;
		}

		.work-lane__actions {
			align-items: stretch;
			flex-direction: column;
		}

		.work-button {
			width: 100%;
		}

		.prepare-form,
		.backlog-filters {
			grid-template-columns: 1fr;
		}

		.prepare-form__intro,
		.field--scope {
			grid-column: auto;
		}

		.backlog-table-wrap {
			overflow: visible;
		}

		.backlog-table {
			min-width: 0;
		}

		.backlog-table thead {
			display: none;
		}

		.backlog-table,
		.backlog-table tbody,
		.backlog-table tr,
		.backlog-table td {
			display: block;
			width: 100%;
		}

		.backlog-table tr {
			border-top: var(--mf-border-muted);
			padding: var(--mf-space-4) var(--mf-space-5);
		}

		.backlog-table td {
			border: 0;
			display: grid;
			grid-template-columns: 92px minmax(0, 1fr);
			max-width: none;
			padding: var(--mf-space-3) 0;
		}

		.backlog-table td::before {
			color: var(--mf-fg-tertiary);
			content: attr(data-label);
			font-size: var(--mf-text-2xs);
			font-weight: var(--mf-weight-semibold);
			letter-spacing: 0.05em;
			text-transform: uppercase;
		}

		.backlog-empty {
			display: block !important;
		}

		.path-copy {
			white-space: normal;
		}

		.backlog__footer div {
			width: 100%;
		}
	}
</style>
