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
</script>

<div class="control-deck">
	<div class="run-readiness-card compact-status-card">
		<p class="eyebrow-copy">Run environment</p>
		<h3 class="run-card-title">{runReadinessHeading}</h3>
		<p class="muted-copy">{runReadinessCopy}</p>
		<div class="pill-row run-status-pills">
			<Pill label={sampleQueueLabel} variant="neutral" wide />
			<Pill label={encodeQueueLabel} variant="ghost" wide />
		</div>
		{#if encodeJobStatus && encodeJobStatus !== 'completed'}
			<div class={`folder-encode-card ${encodeJobTone}`.trim()}>
				<div class="folder-encode-header">
					<div>
						<p class="eyebrow-copy">Folder encode</p>
						<p class="folder-encode-title">{encodeJobHeadline}</p>
					</div>
					<span class={`folder-encode-chip ${encodeJobTone}`.trim()}>{encodeJobChipLabel}</span>
				</div>
				{#if encodeJobDetail}
					<p class="muted-copy folder-encode-detail">{encodeJobDetail}</p>
				{/if}
				{#if encodeJobNextActionCopy}
					<p class="inline-gate-copy folder-encode-next-step">
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
					<p class="muted-copy folder-encode-meta">{encodeJobMetaCopy}</p>
				{/if}
			</div>
		{/if}
	</div>

	<div class="host-picker-shell">
		<div class="section-copy-block">
			<p class="eyebrow-copy">Host picker</p>
			<p class="muted-copy host-section-copy">
				Pick the machine for this representative pass. The chosen host stays visible in the action
				bar below.
			</p>
			<p class="muted-copy host-section-copy host-schedule-note">
				Representative samples can run now. Full-folder encodes still wait for the worker windows
				you see in Ops.
			</p>
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
						<span class="muted-copy compact-host-meta">{compactScheduleCopy(hostCard.runtime)}</span
						>
					{/if}
					<span class="muted-copy compact-host-meta secondary">
						{hostCapacityCopy(hostCard.runtime) || hostCard.detail || 'No runtime detail'}
					</span>
					{#if hostCard.searchSummary}
						<span class="muted-copy compact-host-meta tertiary">{hostCard.searchSummary.label}</span
						>
					{/if}
				</button>
			{/each}
		</div>
		{#if !sampleHostCards.some((hostCard) => hostCard.key === selectedHost) && folderSampleHostHelpText}
			<p class="muted-copy host-selection-note">{folderSampleHostHelpText}</p>
		{/if}
	</div>

	<div class="run-setup-card action-card">
		<p class="eyebrow-copy">Next action</p>
		<div class="action-card-head">
			<h3 class="run-card-title">{nextActionHeading}</h3>
			<Pill label={nextActionStatus.label} variant={nextActionStatus.variant} />
		</div>
		<p class="muted-copy">{sampleActionSupportCopy}</p>
		<p class="selected-host-inline">
			<span class="selected-host-inline-value">{selectedHostLabel || 'Choose a host above'}</span>
			{#if selectedHostScheduleCopy || selectedHostCapacityCopy}
				<span class="muted-copy selected-host-inline-meta">
					{[selectedHostScheduleCopy, selectedHostCapacityCopy].filter(Boolean).join(' · ')}
				</span>
			{:else if selectedHostDetail}
				<span class="muted-copy selected-host-inline-meta">{selectedHostDetail}</span>
			{/if}
		</p>
		{#if selectedHostSearchSummary}
			<p class="muted-copy host-search-callout">
				<span class="host-search-callout-label">{selectedHostSearchSummary.label}</span>
				{selectedHostSearchSummary.detail}
			</p>
		{/if}
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
			{#if !canRunSample}
				The sample button stays locked until the host is ready.
			{:else if sampleRunActive}
				Wait for the current sample to finish before starting another one.
			{:else if canRetrySavedSampleDraft}
				The last bench draft is already saved for this host, so this button reruns that test encode
				directly.
			{:else if retryableCalibrationRefreshBlockedByEmptyNote}
				Add a note before refreshing this stopped sample draft. The tuner cannot build a new draft
				from an empty request.
			{:else if retryableCalibrationNeedsRefresh}
				The saved sample draft no longer matches the current note or host. Refresh it from the bench
				chat below before queueing the next run.
			{:else if !hasPendingProposal}
				Use the bench chat below to draft the next sample. This button only queues a ready draft.
			{:else if pendingProposalNeedsRefresh}
				The note or host changed. Refresh the draft from the bench chat below so the queued sample
				matches the latest request.
			{:else if pendingProposalCanQueue}
				The current draft is aligned with the selected host and can queue when you confirm it.
			{:else}
				Adjust the draft or ask the bench again before queueing the sample.
			{/if}
		</p>
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
</div>

<style>
	.control-deck {
		display: grid;
		gap: 0;
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
	.control-deck .selected-host-inline-meta,
	.control-deck .compact-host-meta,
	.control-deck .inline-gate-copy {
		color: rgba(203, 213, 225, 0.74);
	}

	.control-deck .run-readiness-card,
	.control-deck .host-picker-shell,
	.control-deck .action-card {
		background: rgba(15, 23, 42, 0.76);
		box-shadow: none;
		border: 1px solid rgba(148, 163, 184, 0.16);
		border-radius: 0;
	}

	.control-deck .host-picker-shell,
	.control-deck .action-card {
		margin-top: 0.35rem;
	}

	.control-deck .sample-host-card {
		background: rgba(15, 23, 42, 0.76);
		border-color: rgba(148, 163, 184, 0.16);
		color: #f8fafc;
	}

	.control-deck .sample-host-card.selected {
		background: rgba(8, 47, 73, 0.8);
		border-color: rgba(56, 189, 248, 0.24);
		box-shadow: none;
	}

	.control-deck .sample-host-state.ready {
		background: rgba(20, 83, 45, 0.82);
		color: #dcfce7;
	}

	.control-deck .sample-host-badge {
		background: rgba(8, 47, 73, 0.82);
		color: #dbeafe;
	}

	.control-deck .selected-host-inline-value,
	.control-deck .run-card-title,
	.control-deck .sample-host-label,
	.host-search-callout-label,
	.folder-encode-fact strong,
	.folder-encode-title {
		color: #f8fafc;
	}

	.host-picker-shell {
		display: grid;
		gap: var(--space-2);
		padding: 1rem 1.05rem;
	}

	.compact-status-card,
	.action-card {
		display: grid;
		gap: 0.75rem;
		padding: 1.1rem 1.15rem;
	}

	.section-copy-block {
		display: grid;
		gap: 0.3rem;
		min-width: 0;
	}

	.host-section-copy {
		max-width: 62ch;
	}

	.host-schedule-note {
		margin-top: -0.12rem;
	}

	.run-card-title {
		margin: 0;
		font-size: 1.35rem;
		line-height: 1.15;
	}

	.run-status-pills {
		padding-top: 0.15rem;
	}

	.folder-encode-card {
		display: grid;
		gap: 0.75rem;
		padding: 0.9rem 0.95rem;
		border: 1px solid rgba(148, 163, 184, 0.16);
		background: rgba(15, 20, 27, 0.92);
	}

	.folder-encode-card.live {
		background: rgba(20, 83, 45, 0.68);
		border-color: rgba(74, 222, 128, 0.2);
	}

	.folder-encode-card.queued {
		background: rgba(8, 47, 73, 0.8);
		border-color: rgba(56, 189, 248, 0.24);
	}

	.folder-encode-card.warning {
		background: rgba(120, 53, 15, 0.7);
		border-color: rgba(251, 146, 60, 0.24);
	}

	.folder-encode-header {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		gap: 0.75rem;
		align-items: start;
	}

	.folder-encode-title {
		margin: 0.1rem 0 0;
		font-size: 1rem;
		font-weight: 700;
		line-height: 1.2;
	}

	.folder-encode-chip {
		display: inline-flex;
		align-items: center;
		padding: 0.28rem 0.55rem;
		border-radius: 999px;
		font-size: 0.72rem;
		font-weight: 700;
		letter-spacing: 0.08em;
		text-transform: uppercase;
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

	.folder-encode-detail,
	.folder-encode-next-step,
	.folder-encode-meta {
		margin: 0;
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

	.folder-encode-fact p,
	.folder-encode-fact strong {
		margin: 0;
	}

	.folder-encode-fact p {
		font-size: 0.74rem;
		font-weight: 700;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: rgba(203, 213, 225, 0.72);
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
		border-color: rgba(56, 189, 248, 0.4);
	}

	.sample-host-card.disabled {
		opacity: 0.7;
		cursor: not-allowed;
	}

	.sample-host-badges {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		flex-wrap: wrap;
	}

	.sample-host-state {
		display: inline-flex;
		align-items: center;
		padding: 0.25rem 0.55rem;
		border-radius: 999px;
		font-size: 0.72rem;
		font-weight: 700;
		letter-spacing: 0.08em;
		text-transform: uppercase;
	}

	.sample-host-state.unavailable {
		background: rgba(148, 163, 184, 0.16);
		color: rgba(226, 232, 240, 0.78);
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

	.sample-host-badge {
		display: inline-flex;
		align-items: center;
		padding: 0.25rem 0.5rem;
		border-radius: 999px;
		font-size: 0.72rem;
		font-weight: 700;
		letter-spacing: 0.08em;
		text-transform: uppercase;
	}

	.host-selection-note {
		margin-top: -0.1rem;
	}

	.selected-host-inline {
		display: grid;
		margin: 0;
		gap: 0.2rem;
	}

	.selected-host-inline-value {
		font-size: 1rem;
		font-weight: 700;
		line-height: 1.35;
	}

	.selected-host-inline-meta {
		max-width: 44ch;
	}

	.host-search-callout {
		margin: -0.18rem 0 0;
		display: grid;
		gap: 0.12rem;
		font-size: 0.82rem;
		line-height: 1.42;
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

	.action-card-head {
		display: flex;
		justify-content: space-between;
		gap: 0.8rem;
		align-items: start;
		flex-wrap: wrap;
	}

	.action-inline-note {
		padding: 0.15rem 0 0;
		border-radius: 0;
		background: transparent;
		border: 0;
		font-size: 0.86rem;
		line-height: 1.4;
	}

	.utility-action-row {
		padding-top: 0.15rem;
	}

	.destructive-action-copy {
		padding-top: 0;
	}

	.danger-disclosure {
		display: grid;
		gap: 0.75rem;
		padding: 0.9rem 0.95rem;
		border: 1px solid rgba(251, 146, 60, 0.24);
		background: rgba(67, 20, 7, 0.62);
	}

	.danger-disclosure summary {
		cursor: pointer;
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto auto;
		gap: 0.8rem;
		align-items: center;
		font-size: 0.9rem;
		font-weight: 700;
		color: #f8fafc;
	}

	.danger-disclosure summary::after {
		content: '+';
		font-size: 1rem;
		font-weight: 700;
		line-height: 1;
		color: rgba(203, 213, 225, 0.72);
	}

	.danger-disclosure[open] summary::after {
		content: '-';
	}

	.danger-disclosure-body {
		display: grid;
		gap: 0.7rem;
		padding-top: 0.1rem;
	}

	.summary-hint {
		font-size: 0.82rem;
		font-weight: 700;
		color: rgba(203, 213, 225, 0.72);
	}

	@media (max-width: 720px) {
		.sample-host-grid {
			grid-template-columns: 1fr;
		}

		.danger-disclosure summary {
			grid-template-columns: 1fr;
		}
	}
</style>
