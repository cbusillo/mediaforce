<script lang="ts">
	import Button from '$lib/components/Button.svelte';
	import { compactScheduleCopy, type SampleHostCard } from '$lib/folders/studio';

	type EncodeJobFact = {
		label: string;
		value: string;
	};

	let {
		encodeJobStatus,
		encodeJobTone,
		encodeJobHeadline,
		encodeJobChipLabel,
		encodeJobDetail,
		encodeJobNextActionCopy,
		encodeJobFacts,
		encodeJobMetaCopy,
		reviewGateStatus,
		sampleHostCards,
		selectedHost,
		onSelectHost,
		folderSampleHostHelpText,
		nextActionHeading,
		selectedHostLabel,
		actionState,
		canRunPrimarySampleAction,
		onRunSample,
		onReviseDraft,
		onApproveDraft,
		approvalButtonDisabled,
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
		encodeJobStatus: string;
		encodeJobTone: string;
		encodeJobHeadline: string;
		encodeJobChipLabel: string;
		encodeJobDetail: string;
		encodeJobNextActionCopy: string;
		encodeJobFacts: EncodeJobFact[];
		encodeJobMetaCopy: string;
		reviewGateStatus: string;
		sampleHostCards: SampleHostCard[];
		selectedHost: string;
		onSelectHost: (hostKey: string) => void;
		folderSampleHostHelpText: string;
		nextActionHeading: string;
		selectedHostLabel: string;
		actionState: string | null;
		canRunPrimarySampleAction: boolean;
		onRunSample: () => void;
		onReviseDraft: () => void;
		onApproveDraft: () => void;
		approvalButtonDisabled: boolean;
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

	const launchHeading = $derived.by(() => {
		if (reviewGateStatus === 'accepted') return 'Monitor folder encode';
		if (reviewGateStatus === 'needs_approval') return 'Approve or revise draft';
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
		if (reviewGateStatus === 'accepted') {
			return 'The draft is already approved. Monitor encode progress here or in Ops.';
		}
		if (reviewGateStatus === 'needs_approval') {
			return 'Approve queues the full folder encode. Revise returns this draft to the bench.';
		}
		if (!canRunSample) {
			return 'Host not ready.';
		}
		if (sampleRunActive) {
			return 'Wait for the current sample to finish.';
		}
		if (canRetrySavedSampleDraft) {
			return 'The saved draft is still valid for this host.';
		}
		if (retryableCalibrationRefreshBlockedByEmptyNote) {
			return 'Add a note before refreshing the stopped draft.';
		}
		if (retryableCalibrationNeedsRefresh || pendingProposalNeedsRefresh) {
			return 'Refresh draft to match the current note and host.';
		}
		if (!hasPendingProposal) {
			return 'Ask the bench for a draft first.';
		}
		if (pendingProposalCanQueue) {
			return 'This draft can queue on the selected host.';
		}
		return 'Adjust the draft in the bench before queueing this sample.';
	});

	const showQueueWatch = $derived.by(() =>
		Boolean(encodeJobStatus && encodeJobStatus !== 'completed')
	);

	const isApprovalDecision = $derived(reviewGateStatus === 'needs_approval');

	const queueSummaryCopy = $derived.by(() => {
		const parts = [encodeJobHeadline, encodeJobChipLabel].filter(Boolean);
		return parts.join(' · ');
	});
</script>

<div class="control-deck">
	<section class="launch-console deck-block">
		<div class="launch-head">
			<div class="launch-heading-block">
				<p class="eyebrow-copy">{isApprovalDecision ? 'Decision' : 'Run'}</p>
				<h3 class="run-card-title">{launchHeading}</h3>
			</div>
		</div>

		{#if !isApprovalDecision}
			<div class="host-picker-inline">
				<div class="section-copy-block host-picker-copy">
					<p class="eyebrow-copy">Host</p>
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
							<span class="sample-host-primary">
								<span class="sample-host-label">{hostCard.label}</span>
								<span class="sample-host-badges">
									<span class={`sample-host-state ${hostCard.available ? 'ready' : 'unavailable'}`}
										>{hostCard.available ? 'Ready' : 'Unavailable'}</span
									>
								</span>
							</span>
							{#if compactScheduleCopy(hostCard.runtime)}
								<span class="muted-copy compact-host-meta"
									>{compactScheduleCopy(hostCard.runtime)}</span
								>
							{/if}
						</button>
					{/each}
				</div>
				{#if !sampleHostCards.some((hostCard) => hostCard.key === selectedHost) && folderSampleHostHelpText}
					<p class="muted-copy host-selection-note">{folderSampleHostHelpText}</p>
				{/if}
			</div>
		{/if}

		<div class="launch-action-footer">
			<div
				class="action-row primary-action-row compact-action-row single-primary-action-row"
				class:approval-action-row={isApprovalDecision}
			>
				{#if isApprovalDecision}
					<Button
						variant="approve"
						loading={actionState === 'save'}
						disabled={approvalButtonDisabled}
						onclick={onApproveDraft}
					>
						Approve and queue encode
					</Button>
					<Button variant="secondary" onclick={onReviseDraft}>Request changes</Button>
				{:else}
					<Button
						loading={actionState === 'sample' || actionState === 'preview'}
						disabled={!canRunPrimarySampleAction}
						onclick={onRunSample}
					>
						{confirmButtonLabel}
					</Button>
				{/if}
			</div>
			<p class="inline-gate-copy sample-action-copy action-inline-note">
				<span class="eyebrow-copy">Status</span>
				{runGateCopy}
			</p>
		</div>
	</section>

	{#if showQueueWatch}
		<details class="queue-watch-disclosure">
			<summary>
				<span>Queue</span>
				<span>{queueSummaryCopy}</span>
			</summary>
			<div class={`queue-watch-shell deck-block compact-queue-watch ${encodeJobTone}`.trim()}>
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
		</details>
	{/if}

	{#if hasClearableTuningState}
		<details class="danger-disclosure" class:approval-recovery-disclosure={isApprovalDecision}>
			<summary>
				<span>{isApprovalDecision ? 'Recovery reset' : 'Advanced reset'}</span>
				<span class="summary-hint">{isApprovalDecision ? 'Advanced' : 'Danger'}</span>
			</summary>
			<div class="danger-disclosure-body">
				<p class="inline-gate-copy destructive-action-copy">
					<span class="eyebrow-copy">Destructive</span>
					{isApprovalDecision
						? 'Use only if stale tuning artifacts are blocking this approval. This removes the tuning thread, retained sample context, and sample artifacts for this folder only.'
						: 'Removes the tuning thread, retained sample context, and sample artifacts for this folder only.'}
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
		gap: 0.7rem;
		padding: 0;
		border-radius: 0;
		background: transparent;
		box-shadow: none;
		border: 0;
		color: inherit;
	}

	.deck-block {
		display: grid;
		gap: 0.75rem;
		padding: 0.9rem 0.95rem;
		background: transparent;
		border: 0;
		border-top: 1px solid rgba(148, 163, 184, 0.16);
		border-left: 2px solid rgba(56, 189, 248, 0.12);
		border-radius: 0;
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
		background: transparent;
		border: 0;
		border-radius: 0;
		box-shadow: none;
	}

	.launch-console,
	.queue-watch-shell {
		display: grid;
		gap: 0.6rem;
		padding: 0.9rem 0.95rem;
	}

	.launch-head,
	.queue-watch-head {
		display: grid;
		grid-template-columns: minmax(0, 1fr);
		gap: 0.6rem;
		align-items: start;
	}

	.launch-heading-block,
	.section-copy-block {
		display: grid;
		gap: 0.28rem;
		min-width: 0;
	}

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
		font-size: 1rem;
		line-height: 1.15;
	}

	.queue-watch-title,
	.launch-context-value {
		margin: 0.08rem 0 0;
		font-size: 0.92rem;
		font-weight: 700;
		line-height: 1.2;
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

	.host-picker-inline,
	.launch-action-footer {
		display: grid;
		gap: 0.45rem;
		padding-top: 0.7rem;
		border-top: 1px solid rgba(148, 163, 184, 0.14);
	}

	.host-picker-copy {
		gap: 0.18rem;
	}

	.sample-host-grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
		gap: 0.35rem;
		justify-content: start;
		min-width: 0;
	}

	.compact-host-grid {
		grid-template-columns: 1fr;
		gap: 0.5rem;
	}

	.sample-host-card {
		display: grid;
		gap: 0.14rem;
		width: 100%;
		min-width: 0;
		padding: 0.45rem 0;
		border: 0;
		border-top: 1px solid rgba(148, 163, 184, 0.12);
		background: transparent;
		text-align: left;
		box-sizing: border-box;
		transition:
			transform 160ms ease,
			border-color 160ms ease,
			box-shadow 160ms ease,
			background 160ms ease;
	}

	.sample-host-card:hover:not(.disabled) {
		transform: none;
		border-color: rgba(56, 189, 248, 0.4);
	}

	.sample-host-card.preferred {
		border-color: rgba(56, 189, 248, 0.18);
	}

	.sample-host-card.selected {
		background: rgba(8, 47, 73, 0.18);
		border-color: rgba(56, 189, 248, 0.28);
		border-left: 2px solid rgba(56, 189, 248, 0.42);
		padding-left: 0.55rem;
		box-shadow: none;
	}

	.sample-host-card.disabled {
		opacity: 0.58;
		cursor: not-allowed;
	}

	.sample-host-primary {
		display: flex;
		justify-content: space-between;
		gap: 0.55rem;
		align-items: start;
		flex-wrap: wrap;
		min-width: 0;
	}

	.sample-host-badges {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: 0.2rem;
	}

	.sample-host-state,
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

	.sample-host-label {
		font-size: 0.92rem;
		font-weight: 700;
		line-height: 1.2;
		overflow-wrap: anywhere;
	}

	.compact-host-meta {
		font-size: 0.72rem;
		line-height: 1.35;
		overflow-wrap: anywhere;
	}

	.action-row {
		display: flex;
		gap: var(--space-2);
		flex-wrap: wrap;
		align-items: start;
	}

	.primary-action-row :global(button) {
		min-width: 100%;
	}

	.compact-action-row :global(button) {
		flex: 1 1 12rem;
		min-width: 0;
	}

	.single-primary-action-row :global(button) {
		flex-basis: 100%;
	}

	.approval-action-row :global(button) {
		flex: 1 1 100%;
		min-width: 0;
	}

	.single-primary-action-row.approval-action-row :global(button) {
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
		font-size: 0.8rem;
		line-height: 1.35;
	}

	.compact-queue-watch {
		gap: 0.65rem;
	}

	.queue-watch-disclosure {
		display: grid;
		gap: 0.45rem;
	}

	.queue-watch-disclosure summary {
		cursor: pointer;
		display: flex;
		justify-content: space-between;
		gap: 0.5rem;
		align-items: start;
		flex-wrap: wrap;
		padding: 0.35rem 0;
		border-top: 1px solid rgba(148, 163, 184, 0.14);
		font-size: 0.68rem;
		font-weight: 800;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: rgba(148, 163, 184, 0.78);
		min-width: 0;
	}

	.queue-watch-disclosure summary span:last-child {
		text-align: right;
		overflow-wrap: anywhere;
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

	.queue-watch-shell.blocked {
		background: rgba(62, 15, 17, 0.96);
		border-color: rgba(248, 113, 113, 0.34);
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

	.folder-encode-chip.blocked {
		background: rgba(62, 15, 17, 0.82);
		color: #fee2e2;
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
		background: rgba(15, 20, 27, 0.62);
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
		align-items: start;
		justify-content: space-between;
		gap: 0.75rem;
		flex-wrap: wrap;
		padding: 0.8rem 0.9rem;
		cursor: pointer;
		list-style: none;
		min-width: 0;
	}

	.approval-recovery-disclosure summary {
		padding: 0.35rem 0;
		color: rgba(203, 213, 225, 0.78);
		font-size: 0.82rem;
	}

	.danger-disclosure summary::-webkit-details-marker {
		display: none;
	}

	.summary-hint {
		background: rgba(127, 29, 29, 0.72);
		color: #fee2e2;
	}

	.approval-recovery-disclosure .summary-hint {
		background: rgba(30, 41, 59, 0.72);
		color: rgba(226, 232, 240, 0.78);
	}

	.danger-disclosure-body {
		display: grid;
		gap: 0.75rem;
		padding: 0 0.9rem 0.9rem;
		border-top: 1px solid rgba(148, 163, 184, 0.14);
	}

	.danger-disclosure:not([open]) .danger-disclosure-body {
		display: none;
	}

	.destructive-action-copy {
		margin: 0;
		padding-top: 0.8rem;
	}

	.host-selection-note {
		margin: 0;
	}
</style>
