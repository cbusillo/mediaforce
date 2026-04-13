<script lang="ts">
	import Button from '$lib/components/Button.svelte';
	import Pill from '$lib/components/Pill.svelte';
	import { compactScheduleCopy, hostCapacityCopy, type SampleHostCard } from '$lib/folders/studio';

	type PillVariant = 'default' | 'ok' | 'warn' | 'neutral' | 'ghost';

	type EncodeJobFact = {
		label: string;
		value: string;
	};

	type NextActionStatus = {
		label: string;
		variant: PillVariant;
	};

	type HostSearchSummary = {
		label: string;
		detail: string;
	};

	let {
		runReadinessHeading,
		runReadinessCopy,
		sampleQueueLabel,
		encodeQueueLabel,
		encodeJobStatus,
		encodeJobTone,
		encodeJobHeadline,
		encodeJobChipLabel,
		encodeJobDetail,
		encodeJobNextActionCopy,
		encodeJobFacts,
		encodeJobMetaCopy,
		sampleHostCards,
		selectedHost,
		onSelectHost,
		folderSampleHostHelpText,
		nextActionHeading,
		nextActionStatus,
		sampleActionSupportCopy,
		selectedHostLabel,
		selectedHostScheduleCopy,
		selectedHostCapacityCopy,
		selectedHostDetail,
		selectedHostSearchSummary,
		actionState,
		canRunPrimarySampleAction,
		onRunSample,
		confirmButtonLabel,
		canRunSample,
		sampleRunActive,
		canRetrySavedSampleDraft,
		retryableCalibrationRefreshBlockedByEmptyNote,
		retryableCalibrationNeedsRefresh,
		hasPendingProposal,
		pendingProposalNeedsRefresh,
		pendingProposalCanQueue,
		hasClearableTuningState,
		onClearTuningState
	}: {
		runReadinessHeading: string;
		runReadinessCopy: string;
		sampleQueueLabel: string;
		encodeQueueLabel: string;
		encodeJobStatus: string;
		encodeJobTone: string;
		encodeJobHeadline: string;
		encodeJobChipLabel: string;
		encodeJobDetail: string;
		encodeJobNextActionCopy: string;
		encodeJobFacts: EncodeJobFact[];
		encodeJobMetaCopy: string;
		sampleHostCards: SampleHostCard[];
		selectedHost: string;
		onSelectHost: (hostKey: string) => void;
		folderSampleHostHelpText: string;
		nextActionHeading: string;
		nextActionStatus: NextActionStatus;
		sampleActionSupportCopy: string;
		selectedHostLabel: string;
		selectedHostScheduleCopy: string | null;
		selectedHostCapacityCopy: string | null;
		selectedHostDetail: string | null;
		selectedHostSearchSummary: HostSearchSummary | null;
		actionState: string | null;
		canRunPrimarySampleAction: boolean;
		onRunSample: () => void;
		confirmButtonLabel: string;
		canRunSample: boolean;
		sampleRunActive: boolean;
		canRetrySavedSampleDraft: boolean;
		retryableCalibrationRefreshBlockedByEmptyNote: boolean;
		retryableCalibrationNeedsRefresh: boolean;
		hasPendingProposal: boolean;
		pendingProposalNeedsRefresh: boolean;
		pendingProposalCanQueue: boolean;
		hasClearableTuningState: boolean;
		onClearTuningState: () => void;
	} = $props();

	const selectedHostMeta = $derived.by(() => {
		const parts = [selectedHostScheduleCopy, selectedHostCapacityCopy].filter(Boolean);
		if (parts.length > 0) return parts.join(' · ');
		if (selectedHostDetail) return selectedHostDetail;
		return folderSampleHostHelpText || 'Choose a ready host before starting a sample.';
	});

	const hostPickerLeadCopy = $derived.by(() => {
		if (sampleRunActive) {
			return 'Host selection stays visible, but the active sample keeps the lane until it finishes.';
		}
		return 'Pick the workstation for the next representative pass. Full-folder encodes still follow the scheduled worker windows in Ops.';
	});

	const launchSupportCopy = $derived.by(() => {
		if (sampleRunActive) {
			return 'The sample lane is occupied right now, so this rail shifts into monitoring until the run finishes.';
		}
		if (!canRunSample) {
			return 'Start by choosing a ready host. Once the host is ready, use the bench to shape the next sample draft.';
		}
		if (canRetrySavedSampleDraft) {
			return 'A saved sample draft is already aligned with this host, so you can rerun it directly without going back through the bench.';
		}
		if (retryableCalibrationRefreshBlockedByEmptyNote) {
			return 'The stopped draft no longer matches the run target. Add a note so the bench can refresh it before you queue another sample.';
		}
		if (retryableCalibrationNeedsRefresh || pendingProposalNeedsRefresh) {
			return 'The current note or host changed, so refresh the bench draft before you queue the next run.';
		}
		if (hasPendingProposal && pendingProposalCanQueue) {
			return 'The current draft lines up with this host. Review it below, then run the representative sample when it looks right.';
		}
		return sampleActionSupportCopy;
	});

	const launchHeading = $derived.by(() => {
		if (sampleRunActive) return 'Sample in progress';
		if (!canRunSample) return 'Choose a ready host';
		if (canRetrySavedSampleDraft) {
			return selectedHostLabel ? `Run saved draft on ${selectedHostLabel}` : 'Run saved draft';
		}
		if (retryableCalibrationRefreshBlockedByEmptyNote) return 'Add a note';
		if (retryableCalibrationNeedsRefresh || pendingProposalNeedsRefresh) return 'Refresh draft';
		if (hasPendingProposal && pendingProposalCanQueue) {
			return selectedHostLabel ? `Run draft on ${selectedHostLabel}` : 'Run draft';
		}
		if (hasPendingProposal) return 'Revise draft';
		return nextActionHeading === 'Draft the next sample in bench chat'
			? 'Draft next sample'
			: nextActionHeading;
	});

	const runGateCopy = $derived.by(() => {
		if (!canRunSample) {
			return 'The sample button stays locked until the selected host is ready.';
		}
		if (sampleRunActive) {
			return 'Wait for the current sample to finish before starting another one.';
		}
		if (canRetrySavedSampleDraft) {
			return 'The saved draft is still valid for this host, so this button reruns it immediately.';
		}
		if (retryableCalibrationRefreshBlockedByEmptyNote) {
			return 'Add a note before refreshing the stopped draft. The bench cannot rebuild a new sample from an empty request.';
		}
		if (retryableCalibrationNeedsRefresh || pendingProposalNeedsRefresh) {
			return 'Refresh the bench draft first so the queued sample matches the latest note and host.';
		}
		if (!hasPendingProposal) {
			return 'Ask the bench for a draft first. This action only queues a ready sample plan.';
		}
		if (pendingProposalCanQueue) {
			return 'The draft is aligned with the selected host and can queue as soon as you confirm it.';
		}
		return 'Adjust the draft in the bench before queueing this sample.';
	});

	const showQueueWatch = $derived.by(() =>
		Boolean(encodeJobStatus && encodeJobStatus !== 'completed')
	);
</script>

<div class="control-deck">
	<div class="launch-console">
		<div class="launch-head">
			<div class="launch-heading-block">
				<p class="eyebrow-copy">Sample launch</p>
				<h3 class="run-card-title">{launchHeading}</h3>
				<p class="muted-copy launch-lede">{launchSupportCopy}</p>
			</div>
			<Pill label={nextActionStatus.label} variant={nextActionStatus.variant} />
		</div>

		<div class="launch-context-grid" aria-label="Current run context">
			<div class="launch-context-card">
				<p class="launch-context-label">Selected host</p>
				<p class="launch-context-value">{selectedHostLabel || 'Choose a ready host'}</p>
				<p class="muted-copy launch-context-meta">{selectedHostMeta}</p>
			</div>
			<div class="launch-context-card">
				<p class="launch-context-label">Run state</p>
				<p class="launch-context-value">{runReadinessHeading}</p>
				<p class="muted-copy launch-context-meta">{runReadinessCopy}</p>
			</div>
		</div>

		<div class="pill-row launch-status-pills">
			<Pill label={sampleQueueLabel} variant="neutral" wide />
			<Pill label={encodeQueueLabel} variant="ghost" wide />
		</div>

		<div class="host-picker-inline">
			<div class="section-copy-block">
				<p class="eyebrow-copy">Choose host</p>
				<p class="muted-copy host-section-copy">{hostPickerLeadCopy}</p>
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
						onclick={() => onSelectHost(hostCard.key)}
					>
						<span class="sample-host-badges">
							<span class={`sample-host-state ${hostCard.available ? 'ready' : 'unavailable'}`}
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
							{hostCapacityCopy(hostCard.runtime) || hostCard.detail || 'No runtime detail'}
						</span>
						{#if hostCard.searchSummary}
							<span class="muted-copy compact-host-meta tertiary"
								>{hostCard.searchSummary.label}</span
							>
						{/if}
					</button>
				{/each}
			</div>
			{#if !sampleHostCards.some((hostCard) => hostCard.key === selectedHost) && folderSampleHostHelpText}
				<p class="muted-copy host-selection-note">{folderSampleHostHelpText}</p>
			{/if}
		</div>

		{#if selectedHostSearchSummary}
			<p class="muted-copy host-search-callout">
				<span class="host-search-callout-label">{selectedHostSearchSummary.label}</span>
				{selectedHostSearchSummary.detail}
			</p>
		{/if}

		<div class="launch-action-footer">
			<div class="action-row primary-action-row compact-action-row single-primary-action-row">
				<Button
					loading={actionState === 'sample' || actionState === 'preview'}
					disabled={!canRunPrimarySampleAction}
					onclick={onRunSample}
				>
					{confirmButtonLabel}
				</Button>
			</div>
			<p class="inline-gate-copy sample-action-copy action-inline-note">
				<span class="eyebrow-copy">Run gate</span>
				{runGateCopy}
			</p>
		</div>
	</div>

	{#if showQueueWatch}
		<div class={`queue-watch-shell ${encodeJobTone}`.trim()}>
			<div class="queue-watch-head">
				<div>
					<p class="eyebrow-copy">Batch queue</p>
					<p class="queue-watch-title">{encodeJobHeadline}</p>
				</div>
				<span class={`folder-encode-chip ${encodeJobTone}`.trim()}>{encodeJobChipLabel}</span>
			</div>
			{#if encodeJobDetail}
				<p class="muted-copy queue-watch-detail">{encodeJobDetail}</p>
			{/if}
			{#if encodeJobNextActionCopy}
				<p class="inline-gate-copy queue-watch-next-step">
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
				<p class="muted-copy queue-watch-meta">{encodeJobMetaCopy}</p>
			{/if}
		</div>
	{/if}

	{#if hasClearableTuningState}
		<details class="danger-disclosure">
			<summary>
				<span>Advanced reset</span>
				<span class="summary-hint">Danger</span>
			</summary>
			<div class="danger-disclosure-body">
				<p class="inline-gate-copy destructive-action-copy">
					<span class="eyebrow-copy">Destructive</span>
					Removes the tuning thread, retained sample context, and sample artifacts for this folder only.
				</p>
				<div class="action-row utility-action-row">
					<Button
						variant="danger"
						loading={actionState === 'clear'}
						disabled={sampleRunActive}
						onclick={onClearTuningState}
					>
						Clear thread + sample artifacts
					</Button>
				</div>
			</div>
		</details>
	{/if}
</div>

<style>
	.control-deck {
		display: grid;
		gap: 0.45rem;
		padding: 0.35rem;
		border-radius: calc(var(--radius-lg) + 0.2rem);
		background: rgba(9, 14, 22, 0.74);
		box-shadow: none;
		border: 1px solid rgba(148, 163, 184, 0.14);
		color: inherit;
	}

	.control-deck :global(.eyebrow-copy) {
		color: rgba(125, 211, 252, 0.84);
	}

	.control-deck .muted-copy,
	.control-deck .compact-host-meta,
	.control-deck .inline-gate-copy {
		color: rgba(203, 213, 225, 0.74);
	}

	.launch-console,
	.queue-watch-shell,
	.danger-disclosure {
		background: rgba(15, 23, 42, 0.76);
		border: 1px solid rgba(148, 163, 184, 0.16);
		border-radius: 0;
		box-shadow: none;
	}

	.launch-console,
	.queue-watch-shell {
		display: grid;
		gap: 0.85rem;
		padding: 1rem 1.05rem;
	}

	.launch-head,
	.queue-watch-head {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		gap: 0.75rem;
		align-items: start;
	}

	.launch-heading-block,
	.section-copy-block {
		display: grid;
		gap: 0.28rem;
		min-width: 0;
	}

	.launch-lede,
	.host-section-copy,
	.queue-watch-detail,
	.queue-watch-next-step,
	.queue-watch-meta,
	.action-inline-note {
		margin: 0;
	}

	.run-card-title,
	.queue-watch-title,
	.launch-context-value,
	.sample-host-label,
	.host-search-callout-label,
	.folder-encode-fact strong {
		color: #f8fafc;
	}

	.run-card-title {
		margin: 0;
		font-size: 1.35rem;
		line-height: 1.15;
	}

	.queue-watch-title,
	.launch-context-value {
		margin: 0.08rem 0 0;
		font-size: 1rem;
		font-weight: 700;
		line-height: 1.2;
	}

	.launch-context-grid {
		display: grid;
		gap: 0.55rem;
	}

	.launch-context-card {
		display: grid;
		gap: 0.18rem;
		padding: 0.78rem 0.84rem;
		background: rgba(15, 20, 27, 0.92);
		border: 1px solid rgba(148, 163, 184, 0.14);
	}

	.launch-context-label,
	.folder-encode-fact p {
		margin: 0;
		font-size: 0.74rem;
		font-weight: 700;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: rgba(203, 213, 225, 0.72);
	}

	.launch-context-meta {
		margin: 0;
	}

	.launch-status-pills {
		padding-top: 0.05rem;
	}

	.host-picker-inline,
	.launch-action-footer {
		display: grid;
		gap: 0.7rem;
		padding-top: 0.8rem;
		border-top: 1px solid rgba(148, 163, 184, 0.14);
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
		padding: 0.85rem 0.9rem;
		border: 1px solid rgba(148, 163, 184, 0.16);
		background: rgba(15, 23, 42, 0.76);
		text-align: left;
		transition:
			transform 160ms ease,
			border-color 160ms ease,
			box-shadow 160ms ease,
			background 160ms ease;
	}

	.sample-host-card:hover:not(.disabled) {
		transform: translateY(-2px);
		border-color: rgba(56, 189, 248, 0.4);
	}

	.sample-host-card.preferred {
		border-color: rgba(56, 189, 248, 0.24);
	}

	.sample-host-card.selected {
		background: rgba(8, 47, 73, 0.8);
		border-color: rgba(56, 189, 248, 0.24);
		box-shadow: none;
	}

	.sample-host-card.disabled {
		opacity: 0.58;
		cursor: not-allowed;
	}

	.sample-host-badges {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: var(--space-2);
	}

	.sample-host-state,
	.sample-host-badge,
	.folder-encode-chip,
	.summary-hint {
		display: inline-flex;
		align-items: center;
		padding: 0.28rem 0.55rem;
		border-radius: 999px;
		font-size: 0.72rem;
		font-weight: 700;
		letter-spacing: 0.08em;
		text-transform: uppercase;
	}

	.sample-host-state.ready {
		background: rgba(20, 83, 45, 0.82);
		color: #dcfce7;
	}

	.sample-host-state.unavailable {
		background: rgba(71, 85, 105, 0.7);
		color: rgba(226, 232, 240, 0.76);
	}

	.sample-host-badge {
		background: rgba(8, 47, 73, 0.82);
		color: #dbeafe;
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
	}

	.compact-host-meta.tertiary {
		font-size: 0.77rem;
		font-weight: 700;
		color: #7dd3fc;
	}

	.host-search-callout {
		margin: -0.18rem 0 0;
		display: grid;
		gap: 0.12rem;
		font-size: 0.82rem;
		line-height: 1.42;
		padding: 0.78rem 0.84rem;
		border: 1px solid rgba(56, 189, 248, 0.18);
		background: rgba(8, 47, 73, 0.3);
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

	.utility-action-row {
		padding-top: 0.15rem;
	}

	.action-inline-note {
		padding: 0.15rem 0 0;
		border-radius: 0;
		background: transparent;
		border: 0;
		font-size: 0.86rem;
		line-height: 1.4;
	}

	.host-search-callout-label {
		display: block;
		margin-bottom: 0.18rem;
		font-size: 0.8rem;
		font-weight: 700;
		letter-spacing: 0.04em;
		text-transform: uppercase;
	}

	.queue-watch-shell.live {
		background: rgba(20, 83, 45, 0.46);
		border-color: rgba(74, 222, 128, 0.2);
	}

	.queue-watch-shell.queued {
		background: rgba(8, 47, 73, 0.38);
		border-color: rgba(56, 189, 248, 0.24);
	}

	.queue-watch-shell.warning {
		background: rgba(120, 53, 15, 0.42);
		border-color: rgba(251, 146, 60, 0.24);
	}

	.folder-encode-chip {
		background: rgba(148, 163, 184, 0.16);
		color: rgba(226, 232, 240, 0.78);
	}

	.folder-encode-chip.live {
		background: rgba(20, 83, 45, 0.82);
		color: #dcfce7;
	}

	.folder-encode-chip.queued {
		background: rgba(8, 47, 73, 0.82);
		color: #dbeafe;
	}

	.folder-encode-chip.warning {
		background: rgba(120, 53, 15, 0.82);
		color: #ffedd5;
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
		background: rgba(15, 20, 27, 0.92);
		border: 1px solid rgba(148, 163, 184, 0.14);
	}

	.folder-encode-fact strong {
		margin: 0;
	}

	.danger-disclosure {
		margin: 0;
	}

	.danger-disclosure summary {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.75rem;
		padding: 0.8rem 0.9rem;
		cursor: pointer;
		list-style: none;
	}

	.danger-disclosure summary::-webkit-details-marker {
		display: none;
	}

	.summary-hint {
		background: rgba(127, 29, 29, 0.72);
		color: #fee2e2;
	}

	.danger-disclosure-body {
		display: grid;
		gap: 0.75rem;
		padding: 0 0.9rem 0.9rem;
		border-top: 1px solid rgba(148, 163, 184, 0.14);
	}

	.destructive-action-copy {
		margin: 0;
		padding-top: 0.8rem;
	}

	.host-selection-note {
		margin: 0;
	}

	@media (min-width: 780px) {
		.launch-context-grid {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}

		.compact-host-grid {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}
	}
</style>
