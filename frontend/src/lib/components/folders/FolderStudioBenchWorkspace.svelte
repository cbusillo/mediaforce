<script lang="ts">
	import { browser } from '$app/environment';
	import Button from '$lib/components/Button.svelte';
	import Pill from '$lib/components/Pill.svelte';
	import {
		buildCalibrationThreadScrollSignature,
		type ApprovedSeasonShortcut,
		type CalibrationThreadSession,
		type PendingSampleProposal,
		type PolicyWorkbenchRow,
		type PolicyWorkbenchSection,
		type ProposalSelfCheck,
		type WorkbenchStat
	} from '$lib/folders/studio';
	import { titleCase } from '$lib/format';

	type PillVariant = 'default' | 'ok' | 'warn' | 'neutral' | 'ghost';

	type PreviewSubmission = {
		note: string;
		hostKey: string;
		hostLabel: string;
	};

	type WorkbenchMemoryEntry = {
		title: string;
		summary: string;
		excerpt: string;
	};

	type WorkbenchToolbeltRow = {
		label: string;
		value: string;
	};

	const TRANSFORM_DEFAULTS_STORAGE_KEY = 'mediaforce:bench-transform-defaults';

	let {
		calibrationThreadSessions,
		calibrationThreadCountLabel,
		operatorRequestLabel,
		note,
		onNoteInput,
		onNoteKeydown,
		noteFieldLabel,
		noteFieldLede,
		notePlaceholder,
		approvedSeasonShortcut,
		approvedSeasonShortcutSummary,
		canUseApprovedSeasonShortcut,
		onPreviewApprovedSeasonDraft,
		canRequestBenchDraft,
		previewButtonLabel,
		onPreviewSampleDraft,
		actionState,
		noteSubmitHint,
		pendingProposal,
		pendingProposalNeedsRefresh,
		proposalDraftHeading,
		pendingProposalSignal,
		pendingOperatorRequestLabel,
		pendingProposalSelfCheckLabel,
		pendingProposalSelfCheckVariant,
		pendingProposalSelfCheck,
		proposalWorkbenchSections,
		previewSubmission,
		workbenchContextGoal,
		workbenchContextStats,
		workbenchMemoryEntries,
		workbenchToolbeltRows,
		proposalSteadyWorkbenchRows,
		pendingProposalTracePromptVersion,
		pendingProposalRawResponse,
		currentThreadSession,
		archivedThreadHeadline,
		archivedThreadDetail,
		threadHistorySummaryCopy
	}: {
		calibrationThreadSessions: CalibrationThreadSession[];
		calibrationThreadCountLabel: string;
		operatorRequestLabel: string;
		note: string;
		onNoteInput: (value: string) => void;
		onNoteKeydown: (event: KeyboardEvent) => void;
		noteFieldLabel: string;
		noteFieldLede: string;
		notePlaceholder: string;
		approvedSeasonShortcut: ApprovedSeasonShortcut | null;
		approvedSeasonShortcutSummary: string;
		canUseApprovedSeasonShortcut: boolean;
		onPreviewApprovedSeasonDraft: () => void;
		canRequestBenchDraft: boolean;
		previewButtonLabel: string;
		onPreviewSampleDraft: () => void;
		actionState: string | null;
		noteSubmitHint: string;
		pendingProposal: PendingSampleProposal | null;
		pendingProposalNeedsRefresh: boolean;
		proposalDraftHeading: string;
		pendingProposalSignal: string;
		pendingOperatorRequestLabel: string;
		pendingProposalSelfCheckLabel: string;
		pendingProposalSelfCheckVariant: PillVariant;
		pendingProposalSelfCheck: ProposalSelfCheck;
		proposalWorkbenchSections: PolicyWorkbenchSection[];
		previewSubmission: PreviewSubmission | null;
		workbenchContextGoal: string;
		workbenchContextStats: WorkbenchStat[];
		workbenchMemoryEntries: WorkbenchMemoryEntry[];
		workbenchToolbeltRows: WorkbenchToolbeltRow[];
		proposalSteadyWorkbenchRows: PolicyWorkbenchRow[];
		pendingProposalTracePromptVersion: string;
		pendingProposalRawResponse: string;
		currentThreadSession: CalibrationThreadSession | null;
		archivedThreadHeadline: string;
		archivedThreadDetail: string;
		threadHistorySummaryCopy: string;
	} = $props();

	let threadScrollViewport = $state<HTMLDivElement | null>(null);
	let lastAutoScrolledThreadSignature = $state('');
	let transformHeightDraft = $state('1080');
	let transformBlackBarDraft = $state('smart');
	let transformCropDraft = $state('');
	let transformDefaultsLoaded = $state(false);

	const transformRequestReady = $derived.by(() => {
		if (transformHeightDraft !== 'off') return true;
		if (transformBlackBarDraft === 'smart') return true;
		return transformBlackBarDraft === 'manual' && transformCropDraft.trim().length > 0;
	});

	const latestThreadSession = $derived(calibrationThreadSessions.at(-1) ?? null);
	const hasTypedRequest = $derived(note.trim().length > 0);
	const benchDraftActionLabel = $derived(
		hasTypedRequest ? 'Send request to bench' : previewButtonLabel
	);
	const benchDraftActionVariant = $derived(hasTypedRequest ? 'primary' : 'secondary');

	$effect(() => {
		if (!browser || transformDefaultsLoaded) return;
		try {
			const storedDefaults = JSON.parse(
				window.localStorage.getItem(TRANSFORM_DEFAULTS_STORAGE_KEY) || '{}'
			) as { height?: unknown; blackBars?: unknown };
			const storedHeight = String(storedDefaults.height ?? '').trim();
			const storedBlackBars = String(storedDefaults.blackBars ?? '').trim();
			if (['off', '2160', '1080', '720', '540'].includes(storedHeight)) {
				transformHeightDraft = storedHeight;
			}
			if (['off', 'smart', 'manual'].includes(storedBlackBars)) {
				transformBlackBarDraft = storedBlackBars;
			}
		} catch {
			// Ignore corrupt browser-local defaults and keep the workstation defaults.
		}
		transformDefaultsLoaded = true;
	});

	$effect(() => {
		if (!browser || !transformDefaultsLoaded) return;
		window.localStorage.setItem(
			TRANSFORM_DEFAULTS_STORAGE_KEY,
			JSON.stringify({ height: transformHeightDraft, blackBars: transformBlackBarDraft })
		);
	});

	$effect(() => {
		const latestSession = calibrationThreadSessions.at(-1);
		if (!latestSession) {
			lastAutoScrolledThreadSignature = '';
			return;
		}
		if (!threadScrollViewport) return;
		const latestSessionSignature = buildCalibrationThreadScrollSignature(
			latestSession,
			calibrationThreadSessions.length
		);
		if (latestSessionSignature === lastAutoScrolledThreadSignature) return;
		lastAutoScrolledThreadSignature = latestSessionSignature;
		queueMicrotask(() => {
			if (!threadScrollViewport) return;
			threadScrollViewport.scrollTop = threadScrollViewport.scrollHeight;
		});
	});

	function buildTransformRequestText(): string {
		const parts: string[] = [];
		if (transformHeightDraft !== 'off') {
			parts.push(`downsample to ${transformHeightDraft}p`);
		}
		if (transformBlackBarDraft === 'smart') {
			parts.push('use smart black-bar detection');
		}
		if (transformBlackBarDraft === 'manual') {
			const crop = transformCropDraft.trim();
			if (crop) parts.push(`use manual crop ${crop}`);
		}
		return parts.join(' and ');
	}

	function applyTransformRequestToNote(): void {
		const requestText = buildTransformRequestText();
		if (!requestText) return;
		const trimmedNote = note.trim();
		const sentence = `For this bench draft, ${requestText}.`;
		onNoteInput(trimmedNote ? `${trimmedNote}\n${sentence}` : sentence);
	}

	function handleNoteInput(event: Event): void {
		onNoteInput((event.currentTarget as HTMLTextAreaElement).value);
	}
</script>

<div class="bench-workspace-shell">
	<div class="bench-console-grid">
		<section class="field-block note-panel bench-compose-shell">
			<div class="bench-compose-head">
				<div class="bench-heading-row">
					<p class="eyebrow-copy">Bench</p>
					<div class="bench-compose-meta">
						<span>{calibrationThreadCountLabel}</span>
						{#if operatorRequestLabel}
							<span>{operatorRequestLabel}</span>
						{/if}
					</div>
				</div>
				{#if noteFieldLede}
					<p class="muted-copy note-lede">{noteFieldLede}</p>
				{/if}
				<div class="bench-panel-title-row">
					<h3 class="bench-panel-title">{noteFieldLabel}</h3>
				</div>
			</div>
			<div class="bench-compose-inline">
				<textarea
					bind:value={note}
					rows="2"
					oninput={handleNoteInput}
					onkeydown={onNoteKeydown}
					placeholder={notePlaceholder}
				></textarea>
				<div
					class="action-row note-composer-actions bench-compose-actions primary-bench-action-row"
				>
					<Button
						variant={benchDraftActionVariant}
						loading={actionState === 'preview'}
						disabled={!canRequestBenchDraft}
						onclick={onPreviewSampleDraft}
					>
						{benchDraftActionLabel}
					</Button>
					{#if approvedSeasonShortcut}
						<Button
							variant="ghost"
							loading={actionState === 'preview'}
							disabled={!canUseApprovedSeasonShortcut}
							onclick={onPreviewApprovedSeasonDraft}
						>
							Reuse season memory
						</Button>
					{/if}
				</div>
			</div>
			<details class="transform-request-disclosure" open>
				<summary>
					<span>Transform</span>
					<span>{transformRequestReady ? 'Ready' : 'Off'}</span>
				</summary>
				<div class="transform-request-console">
					<div class="transform-control-grid">
						<label class="transform-field">
							<span>Height cap</span>
							<select bind:value={transformHeightDraft} aria-label="Output height cap">
								<option value="off">Off</option>
								<option value="2160">2160p</option>
								<option value="1080">1080p</option>
								<option value="720">720p</option>
								<option value="540">540p</option>
							</select>
						</label>
						<div class="transform-field transform-mode-field">
							<span>Black bars</span>
							<div class="segmented-control" role="group" aria-label="Black bar handling">
								<button
									type="button"
									class:active={transformBlackBarDraft === 'off'}
									onclick={() => (transformBlackBarDraft = 'off')}>Off</button
								>
								<button
									type="button"
									class:active={transformBlackBarDraft === 'smart'}
									onclick={() => (transformBlackBarDraft = 'smart')}>Smart</button
								>
								<button
									type="button"
									class:active={transformBlackBarDraft === 'manual'}
									onclick={() => (transformBlackBarDraft = 'manual')}>Manual</button
								>
							</div>
						</div>
						<label class="transform-field transform-crop-field">
							<span>Crop</span>
							<input
								bind:value={transformCropDraft}
								aria-label="Manual crop"
								placeholder={transformBlackBarDraft === 'manual' ? '1920:800:0:140' : ''}
								disabled={transformBlackBarDraft !== 'manual'}
							/>
						</label>
					</div>
					<div class="transform-request-footer">
						<p class="muted-copy transform-request-preview">
							{buildTransformRequestText() || 'No transform request yet.'}
						</p>
						<Button
							variant="secondary"
							disabled={!transformRequestReady}
							onclick={applyTransformRequestToNote}>Append request</Button
						>
					</div>
				</div>
			</details>
			{#if approvedSeasonShortcut}
				<div class="bench-chat-context">
					<p class="inline-gate-copy note-review-copy approved-season-shortcut-copy">
						<span class="eyebrow-copy">Season memory</span>
						{approvedSeasonShortcut.count} approved {approvedSeasonShortcut.count === 1
							? 'season'
							: 'seasons'} saved for {approvedSeasonShortcut.root_label}. {approvedSeasonShortcutSummary}
					</p>
				</div>
			{/if}
			<div class="bench-compose-footer">
				<p class="muted-copy note-submit-hint">{noteSubmitHint}</p>
			</div>
		</section>

		<section class="bench-chat-shell bench-record-shell">
			{#if latestThreadSession}
				<div class="bench-history-shell">
					<div class="bench-history-head">
						<p class="eyebrow-copy">Bench thread</p>
						<span class="bench-history-meta">{calibrationThreadCountLabel}</span>
					</div>
					<div class="bench-log-latest">
						{#if latestThreadSession.note}
							<p class="thread-support latest-thread-note latest-note-inline">
								<span class="thread-role-inline">Request</span>
								{latestThreadSession.note}
							</p>
						{/if}
						<p class="thread-copy bench-log-headline">
							{latestThreadSession.summary || latestThreadSession.requestResponse}
						</p>
						{#if latestThreadSession.isCurrent && operatorRequestLabel}
							<div class="pill-row thread-chip-row compact-thread-chip-row">
								<Pill label={`Experiment ${operatorRequestLabel}`} variant="default" />
							</div>
						{/if}
					</div>
					<div
						bind:this={threadScrollViewport}
						class="calibration-thread-shell history-thread-shell thread-scroll-list bench-chat-log"
					>
						<div class="calibration-thread-list">
							{#each calibrationThreadSessions as session (session.key)}
								{#if session.note}
									<article class="thread-turn operator-turn">
										<p class="thread-role">You</p>
										<p class="thread-copy">{session.note}</p>
									</article>
								{/if}
								{#if session.summary || session.runSummary}
									<article class="thread-turn system-turn">
										<p class="thread-role">Bench</p>
										{#if session.requestResponse}
											<p class="thread-copy">{session.requestResponse}</p>
										{/if}
										{#if session.summary}
											<p class="thread-support">{session.summary}</p>
										{/if}
										{#if session.feasibilityNote}
											<p class="thread-support">{session.feasibilityNote}</p>
										{/if}
										{#if session.diagnosis}
											<p class="thread-support">{session.diagnosis}</p>
										{/if}
										{#if session.suggestedFollowUp}
											<p class="thread-support">
												<span class="eyebrow-copy">Suggested follow-up</span>
												{session.suggestedFollowUp}
											</p>
										{/if}
										{#if session.runSummary}
											<div class="thread-readout">
												<p class="eyebrow-copy">Last run readout</p>
												<p class="thread-copy">{session.runSummary}</p>
												{#if session.runNextStep}
													<p class="thread-support">{session.runNextStep}</p>
												{/if}
											</div>
										{/if}
									</article>
								{/if}
							{/each}
						</div>
					</div>
				</div>
			{:else}
				<div class="bench-chat-empty">
					<p class="thread-role">Bench</p>
					<p class="thread-copy">No bench read yet.</p>
				</div>
			{/if}
		</section>
	</div>

	{#if pendingProposal}
		<details class="bench-history-disclosure bench-draft-disclosure">
			<summary>Draft</summary>
			<div class:stale={pendingProposalNeedsRefresh} class="proposal-shell diagnosis-shell">
				<div class="proposal-head">
					<div class="section-copy-block">
						<p class="eyebrow-copy">Current draft</p>
						<h4 class="proposal-title">{proposalDraftHeading}</h4>
						{#if pendingProposalSignal}
							<p class="muted-copy">{pendingProposalSignal}</p>
						{/if}
					</div>
					<div class="pill-row proposal-pill-row">
						{#if pendingOperatorRequestLabel}
							<Pill label={`Heard ${pendingOperatorRequestLabel}`} variant="default" />
						{/if}
						{#if pendingProposal?.request_disposition}
							<Pill
								label={`Request ${titleCase(String(pendingProposal.request_disposition).replace(/_/g, ' '))}`}
								variant="ghost"
							/>
						{/if}
						{#if pendingProposalSelfCheckLabel}
							<Pill
								label={pendingProposalSelfCheckLabel}
								variant={pendingProposalSelfCheckVariant}
							/>
						{/if}
						{#if pendingProposal?.confidence}
							<Pill
								label={`Bench confidence ${titleCase(String(pendingProposal.confidence))}`}
								variant="ghost"
							/>
						{/if}
					</div>
				</div>

				{#if pendingProposal?.request_response || pendingProposal?.summary}
					<p class="thread-copy">{pendingProposal.request_response || pendingProposal.summary}</p>
				{/if}
				{#if pendingProposal?.request_response && pendingProposal.request_response !== pendingProposal.summary && pendingProposal?.summary}
					<p class="thread-support">{pendingProposal.summary}</p>
				{/if}
				{#if pendingProposal?.feasibility_note}
					<p class="thread-support">{pendingProposal.feasibility_note}</p>
				{/if}
				{#if pendingProposal?.diagnosis}
					<p class="thread-support">{pendingProposal.diagnosis}</p>
				{/if}
				{#if pendingProposalSelfCheck.summary}
					<p class="inline-gate-copy proposal-warning-copy">
						<span class="eyebrow-copy">Before you run it</span>
						{pendingProposalSelfCheck.summary}
					</p>
				{/if}
				{#if pendingProposalNeedsRefresh}
					<p class="inline-gate-copy proposal-warning-copy">
						<span class="eyebrow-copy">Refresh needed</span>
						This draft no longer matches the current note or selected host.
					</p>
				{/if}
				{#if previewSubmission}
					<p class="inline-gate-copy proposal-warning-copy">
						<span class="eyebrow-copy">Drafting now</span>
						Bench received {previewSubmission.note || 'your latest request'} and is preparing a fresh
						reply.
					</p>
				{/if}
				{#if pendingProposal?.suggested_follow_up}
					<p class="inline-gate-copy proposal-warning-copy">
						<span class="eyebrow-copy">Suggested follow-up</span>
						{pendingProposal.suggested_follow_up}
					</p>
				{/if}

				<details class="proposal-trace-shell diagnosis-details">
					<summary>Draft details</summary>
					<div class="proposal-workbench-grid diagnosis-disclosure-grid">
						{#if proposalWorkbenchSections.length}
							<section class="proposal-workbench-card">
								<p class="eyebrow-copy">Changed in this draft</p>
								{#each proposalWorkbenchSections as section (section.title)}
									<div class="proposal-change-section">
										<p class="proposal-section-title">{section.title}</p>
										<div class="proposal-change-grid workbench-change-grid">
											{#each section.rows as row (row.path)}
												<div class="proposal-change-card">
													<p class="eyebrow-copy">{row.label}</p>
													<p class="proposal-change-value">{row.draft}</p>
													<p class="proposal-change-previous">From {row.current}</p>
												</div>
											{/each}
										</div>
									</div>
								{/each}
							</section>
						{/if}

						<section class="proposal-workbench-card">
							<p class="eyebrow-copy">Run posture</p>
							{#if !proposalWorkbenchSections.length}
								<p class="muted-copy">This draft keeps the current policy.</p>
							{/if}
							{#if pendingProposalSelfCheck.issues?.length}
								<ul class="proposal-issue-list">
									{#each pendingProposalSelfCheck.issues ?? [] as issue (issue)}
										<li>{issue}</li>
									{/each}
								</ul>
							{/if}
						</section>

						<section class="proposal-workbench-card">
							<p class="eyebrow-copy">Bench saw</p>
							{#if workbenchContextGoal}
								<p class="muted-copy">{workbenchContextGoal}</p>
							{/if}
							{#if workbenchContextStats.length}
								<div class="proposal-context-grid">
									{#each workbenchContextStats as stat (stat.label)}
										<div class="proposal-context-card">
											<p class="eyebrow-copy">{stat.label}</p>
											<p class="proposal-change-value">{stat.value}</p>
											{#if stat.detail}
												<p class="muted-copy">{stat.detail}</p>
											{/if}
										</div>
									{/each}
								</div>
							{/if}
							{#if pendingProposal?.evidence_checked?.length}
								<div class="pill-row">
									{#each pendingProposal.evidence_checked ?? [] as evidence (evidence)}
										<Pill label={evidence} variant="neutral" />
									{/each}
								</div>
							{/if}
						</section>

						{#if workbenchMemoryEntries.length}
							<section class="proposal-workbench-card">
								<p class="eyebrow-copy">Retrieved memory</p>
								<div class="proposal-memory-shell">
									{#each workbenchMemoryEntries as entry (entry.title + entry.summary)}
										<div class="proposal-memory-card">
											<p class="proposal-memory-title">
												{entry.title || 'Prior approved tuning note'}
											</p>
											{#if entry.summary}
												<p class="muted-copy">{entry.summary}</p>
											{/if}
											{#if entry.excerpt}
												<p class="thread-support">{entry.excerpt}</p>
											{/if}
										</div>
									{/each}
								</div>
							</section>
						{/if}

						<section class="proposal-workbench-card proposal-trace-card-shell">
							<p class="eyebrow-copy">Advanced trace</p>
							{#if pendingProposalTracePromptVersion}
								<p class="muted-copy">
									<strong>Prompt version:</strong>
									{pendingProposalTracePromptVersion}
								</p>
							{/if}
							{#if workbenchToolbeltRows.length}
								{#each workbenchToolbeltRows as row (row.label)}
									<p class="muted-copy"><strong>{row.label}:</strong> {row.value}</p>
								{/each}
							{/if}
							{#if proposalSteadyWorkbenchRows.length}
								<p class="muted-copy"><strong>Unchanged settings</strong></p>
								{#each proposalSteadyWorkbenchRows as row (row.path)}
									<p class="muted-copy"><strong>{row.label}:</strong> {row.current}</p>
								{/each}
							{/if}
							{#if pendingProposalRawResponse}
								<pre class="proposal-trace-raw">{pendingProposalRawResponse}</pre>
							{/if}
						</section>
					</div>
				</details>
			</div>
		</details>
	{:else if currentThreadSession}
		<details class="bench-history-disclosure bench-draft-disclosure">
			<summary>Archive</summary>
			<div class="proposal-shell diagnosis-shell archived-diagnosis-shell">
				<p class="eyebrow-copy">Bench history</p>
				<h4 class="proposal-title">{calibrationThreadCountLabel} saved in the thread</h4>
				{#if archivedThreadHeadline}
					<p class="thread-copy">{archivedThreadHeadline}</p>
				{/if}
				{#if archivedThreadDetail}
					<p class="thread-support">{archivedThreadDetail}</p>
				{/if}
				<p class="thread-support">
					{threadHistorySummaryCopy} Scroll the thread above when you want to reopen earlier notes, compare
					bench replies, or rewrite the next draft request.
				</p>
			</div>
		</details>
	{/if}
</div>

<style>
	.bench-chat-shell,
	.archived-diagnosis-shell {
		display: grid;
		gap: var(--space-2);
		padding: 1rem 1.05rem;
		border-radius: 0;
		border: 1px solid rgba(148, 163, 184, 0.18);
		background: rgba(13, 18, 27, 0.96);
		box-shadow: none;
	}

	.bench-workspace-shell {
		display: grid;
		gap: 0.7rem;
		padding: 0;
		min-width: 0;
		border-radius: 0;
		background: transparent;
		box-shadow: none;
	}

	.bench-console-grid {
		display: grid;
		grid-template-columns: 1fr;
		gap: 0.9rem;
		align-items: start;
		min-width: 0;
	}

	.bench-workspace-shell .bench-chat-shell,
	.bench-workspace-shell .diagnosis-shell,
	.bench-workspace-shell .archived-diagnosis-shell {
		background: rgba(13, 18, 27, 0.96);
		border-color: rgba(148, 163, 184, 0.18);
		box-shadow: none;
	}

	.bench-chat-shell {
		gap: 0.7rem;
		padding: 0.9rem 0.95rem;
		background: transparent;
		border-top: 1px solid rgba(148, 163, 184, 0.16);
		border-left: 2px solid rgba(56, 189, 248, 0.14);
	}

	.bench-compose-shell {
		gap: 0.7rem;
		padding: 0.9rem 0.95rem;
		border-radius: 0;
		border: 0;
		border-left: 2px solid rgba(56, 189, 248, 0.18);
		background: transparent;
		box-shadow: none;
	}

	.bench-compose-head {
		display: grid;
		gap: 0.35rem;
	}

	.bench-heading-row,
	.bench-panel-title-row {
		display: flex;
		justify-content: space-between;
		gap: 0.6rem;
		align-items: baseline;
		flex-wrap: wrap;
		min-width: 0;
	}

	.bench-compose-inline {
		display: grid;
		gap: 0.55rem;
	}

	.bench-panel-title {
		margin: 0;
		font-size: 0.9rem;
		line-height: 1.25;
	}

	.bench-compose-meta {
		display: flex;
		gap: 0.45rem;
		flex-wrap: wrap;
		min-width: 0;
		font-size: 0.72rem;
		font-weight: 700;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: rgba(148, 163, 184, 0.74);
	}

	.bench-workspace-shell .bench-chat-shell {
		border-color: rgba(148, 163, 184, 0.18);
		background: rgba(13, 18, 27, 0.96);
	}

	.bench-chat-log {
		min-height: 18rem;
		max-height: clamp(22rem, 44vh, 34rem);
		padding: 0.72rem;
		background: rgba(5, 8, 14, 0.96);
		border-color: rgba(148, 163, 184, 0.18);
	}

	.bench-log-latest {
		display: grid;
		gap: 0.25rem;
		padding: 0.55rem 0.6rem;
		background: rgba(9, 15, 24, 0.78);
		border: 1px solid rgba(148, 163, 184, 0.14);
		border-left: 2px solid rgba(56, 189, 248, 0.22);
	}

	.latest-thread-note {
		margin: 0;
	}

	.thread-role-inline {
		font-size: 0.72rem;
		font-weight: 700;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: #7dd3fc;
	}

	.latest-note-inline {
		display: grid;
		gap: 0.16rem;
	}

	.bench-log-headline {
		margin: 0;
		font-size: 0.86rem;
		font-weight: 700;
		line-height: 1.35;
		color: #f8fafc;
	}

	.bench-log-latest .thread-copy,
	.bench-log-latest .thread-support,
	.latest-thread-note {
		display: -webkit-box;
		-webkit-box-orient: vertical;
		overflow: hidden;
	}

	.latest-thread-note {
		line-clamp: 2;
		-webkit-line-clamp: 2;
	}

	.bench-log-latest .thread-copy {
		line-clamp: 2;
		-webkit-line-clamp: 2;
	}

	.bench-log-latest .thread-support {
		line-clamp: 1;
		-webkit-line-clamp: 1;
	}

	.bench-history-disclosure {
		display: grid;
		gap: 0.55rem;
	}

	.bench-history-shell {
		display: grid;
		gap: 0.65rem;
		min-width: 0;
	}

	.bench-history-head {
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: 0.75rem;
		padding-bottom: 0.35rem;
		border-bottom: 1px solid rgba(148, 163, 184, 0.14);
		min-width: 0;
	}

	.bench-history-meta {
		font-size: 0.7rem;
		font-weight: 800;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: rgba(148, 163, 184, 0.72);
	}

	.bench-history-disclosure summary {
		cursor: pointer;
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: 0.55rem;
		padding: 0.35rem 0;
		border-top: 1px solid rgba(148, 163, 184, 0.14);
		font-size: 0.68rem;
		font-weight: 800;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: rgba(148, 163, 184, 0.78);
		min-width: 0;
	}

	.bench-history-disclosure summary::after {
		content: '+';
		font-size: 0.95rem;
		line-height: 1;
	}

	.bench-history-disclosure[open] summary::after {
		content: '-';
	}

	.bench-draft-disclosure {
		padding: 0 0.95rem;
		border-left: 2px solid rgba(148, 163, 184, 0.14);
	}

	.bench-chat-empty {
		display: grid;
		gap: 0.35rem;
		align-content: center;
		min-height: 12rem;
		padding: 0.35rem 0.2rem;
	}

	.bench-chat-context {
		display: grid;
		gap: 0.55rem;
	}

	.transform-request-console {
		display: grid;
		gap: 0.65rem;
		padding: 0.7rem;
		min-width: 0;
		border-radius: 0;
		border: 1px solid rgba(148, 163, 184, 0.18);
		background: rgba(8, 12, 18, 0.96);
	}

	.transform-request-footer {
		display: grid;
		grid-template-columns: minmax(0, 1fr);
		gap: 0.55rem;
		min-width: 0;
	}

	.transform-control-grid {
		display: grid;
		grid-template-columns: minmax(0, 1fr);
		gap: 0.65rem;
		align-items: end;
		min-width: 0;
	}

	.transform-field {
		display: grid;
		gap: 0.35rem;
		min-width: 0;
		font-size: 0.76rem;
		font-weight: 700;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: rgba(203, 213, 225, 0.8);
	}

	.transform-field select,
	.transform-field input {
		width: 100%;
		min-height: 2.35rem;
		padding: 0.5rem 0.65rem;
		border-radius: 0;
		border: 1px solid rgba(148, 163, 184, 0.22);
		background: rgba(5, 8, 14, 0.96);
		color: #f8fafc;
		font: inherit;
		letter-spacing: 0;
		text-transform: none;
		box-sizing: border-box;
	}

	.transform-field input:disabled {
		color: rgba(148, 163, 184, 0.52);
		background: rgba(10, 15, 21, 0.94);
	}

	.segmented-control {
		display: grid;
		grid-template-columns: repeat(3, minmax(0, 1fr));
		min-height: 2.35rem;
		padding: 0.16rem;
		min-width: 0;
		border-radius: 0;
		border: 1px solid rgba(148, 163, 184, 0.22);
		background: rgba(5, 8, 14, 0.96);
	}

	.segmented-control button {
		min-width: 0;
		border: 0;
		border-radius: calc(var(--radius-md) - 0.28rem);
		background: transparent;
		color: rgba(203, 213, 225, 0.78);
		font-size: 0.78rem;
		font-weight: 800;
		letter-spacing: 0;
		cursor: pointer;
	}

	.segmented-control button.active {
		background: rgba(9, 33, 49, 0.96);
		color: #e0f2fe;
		box-shadow: inset 0 0 0 1px rgba(56, 189, 248, 0.24);
	}

	.transform-request-preview {
		margin: 0;
		min-width: 0;
		color: rgba(203, 213, 225, 0.78);
		overflow-wrap: anywhere;
	}

	.bench-workspace-shell .diagnosis-shell {
		background: rgba(13, 18, 27, 0.96);
	}

	.calibration-thread-shell {
		display: grid;
		gap: 0.65rem;
		padding: 0.72rem 0.78rem;
		min-width: 0;
		border-radius: 0;
		border: 1px solid rgba(148, 163, 184, 0.18);
		background: rgba(8, 12, 18, 0.96);
	}

	.history-thread-shell {
		padding: 0.72rem;
	}

	.calibration-thread-list {
		display: grid;
		gap: 0.65rem;
	}

	.thread-scroll-list {
		overflow: auto;
		padding-right: 0.35rem;
	}

	.thread-turn {
		display: grid;
		gap: 0.3rem;
		width: 100%;
		max-width: 100%;
		min-width: 0;
		padding: 0.72rem 0.78rem;
		border-radius: 0;
		border: 1px solid rgba(148, 163, 184, 0.14);
		box-shadow: none;
		box-sizing: border-box;
	}

	.operator-turn {
		justify-self: stretch;
		background: rgba(8, 47, 73, 0.72);
		border-color: rgba(56, 189, 248, 0.22);
		border-left: 3px solid rgba(56, 189, 248, 0.44);
	}

	.system-turn {
		justify-self: stretch;
		background: rgba(15, 23, 42, 0.8);
		border-color: rgba(148, 163, 184, 0.16);
		border-left: 3px solid rgba(251, 146, 60, 0.24);
	}

	.operator-turn .thread-copy {
		color: #e0f2fe;
	}

	.system-turn .thread-copy {
		color: #f8fafc;
	}

	.system-turn .thread-support {
		color: rgba(203, 213, 225, 0.72);
	}

	.thread-role {
		margin: 0;
		font-size: 0.73rem;
		font-weight: 700;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--accent-deep);
	}

	.thread-copy {
		margin: 0;
		font-size: 0.88rem;
		line-height: 1.4;
		overflow-wrap: anywhere;
	}

	.thread-support {
		margin: 0;
		font-size: 0.8rem;
		line-height: 1.38;
		color: var(--ink-soft);
		overflow-wrap: anywhere;
	}

	.thread-readout {
		display: grid;
		gap: 0.22rem;
		margin-top: 0.1rem;
		padding-top: 0.55rem;
		border-top: 1px solid rgba(23, 35, 31, 0.08);
	}

	.thread-chip-row {
		padding-top: 0.1rem;
	}

	.proposal-shell {
		display: grid;
		gap: 0.65rem;
		padding: 0.2rem 0 0.6rem;
		border-radius: 0;
		border: 0;
		background: transparent;
	}

	.diagnosis-shell {
		gap: 0.75rem;
	}

	.diagnosis-details {
		padding-top: 0.2rem;
	}

	.diagnosis-disclosure-grid {
		padding-top: 0.7rem;
	}

	.proposal-shell.stale {
		border-color: rgba(180, 83, 9, 0.24);
		background:
			linear-gradient(180deg, rgba(255, 251, 235, 0.94), rgba(255, 251, 235, 0.82)),
			rgba(180, 83, 9, 0.04);
	}

	.proposal-head {
		display: flex;
		gap: 0.65rem;
		justify-content: space-between;
		align-items: start;
		flex-wrap: wrap;
		min-width: 0;
	}

	.proposal-title {
		margin: 0;
		font-size: 0.94rem;
		line-height: 1.3;
	}

	.proposal-pill-row {
		justify-content: flex-end;
	}

	.proposal-change-grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
		gap: 0.7rem;
	}

	.proposal-change-card {
		display: grid;
		gap: 0.18rem;
		padding: 0.72rem 0.76rem;
		border-radius: 0;
		background: rgba(8, 12, 18, 0.96);
		border: 1px solid rgba(148, 163, 184, 0.14);
	}

	.proposal-change-value {
		margin: 0;
		font-size: 0.98rem;
		font-weight: 700;
		line-height: 1.3;
		overflow-wrap: anywhere;
	}

	.proposal-change-previous {
		margin: 0;
		font-size: 0.83rem;
		line-height: 1.4;
		color: var(--ink-soft);
		overflow-wrap: anywhere;
	}

	.proposal-warning-copy {
		padding: 0.75rem 0.8rem;
		border-radius: calc(var(--radius-md) - 0.18rem);
		background: rgba(15, 118, 110, 0.06);
		border: 1px solid rgba(15, 118, 110, 0.1);
	}

	.proposal-workbench-grid {
		display: grid;
		gap: 0.8rem;
		grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
		min-width: 0;
	}

	.proposal-workbench-card {
		display: grid;
		gap: 0.65rem;
		min-width: 0;
		padding: 0.78rem;
		border-radius: 0;
		background: rgba(8, 12, 18, 0.96);
		border: 1px solid rgba(148, 163, 184, 0.14);
		align-content: start;
	}

	.proposal-workbench-card .muted-copy {
		min-width: 0;
		max-width: 100%;
		overflow-wrap: anywhere;
		word-break: break-word;
	}

	.proposal-trace-card-shell {
		grid-column: 1 / -1;
	}

	.proposal-context-grid {
		display: grid;
		gap: 0.65rem;
	}

	.proposal-context-card {
		display: grid;
		gap: 0.22rem;
		min-width: 0;
		padding: 0.7rem 0.74rem;
		border-radius: 0;
		background: rgba(13, 18, 27, 0.96);
		border: 1px solid rgba(148, 163, 184, 0.14);
	}

	.workbench-change-grid {
		grid-template-columns: 1fr;
	}

	.proposal-change-section {
		display: grid;
		gap: 0.5rem;
	}

	.proposal-section-title,
	.proposal-memory-title {
		margin: 0;
		font-size: 0.9rem;
		font-weight: 700;
		line-height: 1.35;
		overflow-wrap: anywhere;
	}

	.proposal-memory-shell {
		display: grid;
		gap: 0.55rem;
	}

	.proposal-memory-card {
		display: grid;
		gap: 0.22rem;
		padding: 0.7rem 0.74rem;
		border-radius: 0;
		background: rgba(13, 18, 27, 0.96);
		border: 1px solid rgba(148, 163, 184, 0.14);
	}

	.proposal-trace-shell {
		display: grid;
		gap: 0.7rem;
		padding-top: 0.1rem;
		min-width: 0;
	}

	.proposal-trace-shell summary {
		cursor: pointer;
		display: flex;
		justify-content: space-between;
		align-items: center;
		padding: 0.68rem 0.74rem;
		border-radius: 0;
		background: rgba(8, 12, 18, 0.96);
		border: 1px solid rgba(148, 163, 184, 0.14);
		font-size: 0.78rem;
		font-weight: 700;
		letter-spacing: 0.05em;
		text-transform: uppercase;
		color: var(--accent-deep);
	}

	.proposal-trace-shell summary::after {
		content: '+';
		font-size: 1rem;
		font-weight: 700;
		line-height: 1;
		color: var(--ink-soft);
	}

	.proposal-trace-shell[open] summary {
		background: rgba(9, 33, 49, 0.96);
	}

	.proposal-trace-shell[open] summary::after {
		content: '-';
	}

	.proposal-trace-raw {
		display: block;
		width: 100%;
		margin: 0;
		padding: 0.72rem;
		min-width: 0;
		max-width: 100%;
		border-radius: 0;
		background: rgba(5, 8, 14, 0.96);
		border: 1px solid rgba(148, 163, 184, 0.14);
		font-size: 0.76rem;
		line-height: 1.45;
		white-space: pre-wrap;
		word-break: break-word;
		overflow-wrap: anywhere;
		overflow-x: auto;
		box-sizing: border-box;
	}

	.proposal-issue-list {
		margin: 0;
		padding-left: 1.15rem;
		display: grid;
		gap: 0.32rem;
		color: var(--ink-soft);
		font-size: 0.88rem;
		line-height: 1.45;
	}

	.note-panel {
		padding: 0.9rem 0.95rem;
	}

	.note-lede {
		max-width: 42ch;
		overflow-wrap: anywhere;
	}

	.bench-compose-footer {
		display: grid;
		gap: 0.75rem;
	}

	.note-submit-hint {
		max-width: 36ch;
		font-size: 0.78rem;
		line-height: 1.35;
	}

	.field-block {
		display: grid;
		gap: var(--space-2);
	}

	textarea {
		width: 100%;
		padding: 0.62rem 0.7rem;
		border-radius: 0;
		border: 1px solid rgba(148, 163, 184, 0.12);
		background: rgba(5, 8, 14, 0.68);
		color: var(--ink);
		min-height: 4.6rem;
		resize: vertical;
	}

	textarea::placeholder {
		color: rgba(148, 163, 184, 0.42);
		font-style: italic;
	}

	.bench-compose-shell textarea {
		min-height: 5rem;
	}

	.action-row {
		display: flex;
		gap: var(--space-2);
		flex-wrap: wrap;
		align-items: start;
	}

	.inline-gate-copy {
		display: grid;
		gap: 0.15rem;
		font-size: 0.9rem;
		line-height: 1.45;
		color: var(--ink-soft);
		overflow-wrap: anywhere;
	}

	.bench-compose-actions :global(button) {
		min-height: 2rem;
		padding: 0.42rem 0.7rem;
		font-size: 0.84rem;
	}

	.bench-compose-actions {
		display: grid;
		grid-template-columns: minmax(0, 1fr);
		gap: 0.45rem;
	}

	.bench-compose-actions :global(button) {
		width: 100%;
	}

	.primary-bench-action-row {
		align-items: center;
	}

	.primary-bench-action-row :global(button:first-child) {
		flex: 1 1 auto;
	}

	.section-copy-block {
		display: grid;
		gap: 0.3rem;
		min-width: 0;
	}

	.bench-compose-shell :global(.eyebrow-copy),
	.transform-request-disclosure summary {
		color: rgba(148, 163, 184, 0.74);
	}

	.bench-compose-meta {
		display: flex;
		gap: 0.45rem;
		flex-wrap: wrap;
		font-size: 0.74rem;
		color: rgba(203, 213, 225, 0.76);
	}

	.transform-request-disclosure summary {
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: 0.55rem;
		min-width: 0;
	}

	.transform-request-disclosure summary span:last-child,
	.bench-history-disclosure summary::after,
	.proposal-trace-shell summary::after {
		flex: 0 0 auto;
	}

	.transform-request-disclosure summary span:first-child,
	.proposal-trace-shell summary {
		overflow-wrap: anywhere;
	}

	.pill-row {
		display: flex;
		gap: var(--space-2);
		flex-wrap: wrap;
	}

	@media (max-width: 720px) {
		.bench-compose-actions :global(button) {
			width: 100%;
		}
	}
</style>
