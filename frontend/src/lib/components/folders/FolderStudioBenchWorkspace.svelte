<script lang="ts">
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

	let {
		calibrationThreadSessions,
		calibrationThreadCountLabel,
		operatorRequestLabel,
		runVerdictOutcomeCopy,
		runVerdictOutcomeVariant,
		note,
		onNoteInput,
		onNoteKeydown,
		noteFieldLabel,
		noteFieldLede,
		notePlaceholder,
		reviewConversationCopy,
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
		runVerdictOutcomeCopy: string;
		runVerdictOutcomeVariant: PillVariant;
		note: string;
		onNoteInput: (value: string) => void;
		onNoteKeydown: (event: KeyboardEvent) => void;
		noteFieldLabel: string;
		noteFieldLede: string;
		notePlaceholder: string;
		reviewConversationCopy: string | null;
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
</script>

<div class="bench-workspace-shell">
	<div class="bench-console-grid">
		<section class="field-block note-panel bench-compose-shell">
			<div class="bench-compose-head">
				<div class="section-copy-block">
					<p class="eyebrow-copy">Bench direction</p>
					<h3 class="bench-panel-title">{noteFieldLabel}</h3>
					<p class="muted-copy note-lede">{noteFieldLede}</p>
				</div>
				<div class="pill-row bench-compose-pills">
					<Pill label={calibrationThreadCountLabel} variant="ghost" />
					{#if operatorRequestLabel}
						<Pill label={`Current experiment ${operatorRequestLabel}`} variant="neutral" />
					{/if}
				</div>
			</div>
			<textarea
				value={note}
				rows="7"
				oninput={(event) => onNoteInput((event.currentTarget as HTMLTextAreaElement).value)}
				onkeydown={onNoteKeydown}
				placeholder={notePlaceholder}
			></textarea>
			{#if reviewConversationCopy || approvedSeasonShortcut}
				<div class="bench-chat-context">
					{#if reviewConversationCopy}
						<p class="inline-gate-copy note-review-copy">
							<span class="eyebrow-copy">Bench context</span>
							{reviewConversationCopy}
						</p>
					{/if}
					{#if approvedSeasonShortcut}
						<p class="inline-gate-copy note-review-copy approved-season-shortcut-copy">
							<span class="eyebrow-copy">Approved season memory</span>
							{approvedSeasonShortcut.count} approved {approvedSeasonShortcut.count === 1
								? 'season is'
								: 'seasons are'} already saved for {approvedSeasonShortcut.root_label}. Use the
							shortcut to ask the bench to match {approvedSeasonShortcutSummary}.
						</p>
					{/if}
				</div>
			{/if}
			<div class="bench-compose-footer">
				<p class="muted-copy note-submit-hint">{noteSubmitHint}</p>
				<div class="action-row note-composer-actions bench-compose-actions">
					{#if approvedSeasonShortcut}
						<Button
							variant="ghost"
							loading={actionState === 'preview'}
							disabled={!canUseApprovedSeasonShortcut}
							onclick={onPreviewApprovedSeasonDraft}
						>
							Reuse approved seasons
						</Button>
					{/if}
					<Button
						loading={actionState === 'preview'}
						disabled={!canRequestBenchDraft}
						onclick={onPreviewSampleDraft}
					>
						{previewButtonLabel}
					</Button>
				</div>
			</div>
		</section>

		<section class="bench-chat-shell bench-record-shell">
			<div class="thread-inline-head bench-chat-head">
				<div class="section-copy-block">
					<p class="eyebrow-copy">Bench record</p>
					<p class="muted-copy thread-history-copy">
						The live request-and-reply thread stays visible here while you shape the next draft.
					</p>
				</div>
				<span class="thread-history-meta">{calibrationThreadCountLabel} · newest at bottom</span>
			</div>
			<div
				bind:this={threadScrollViewport}
				class="calibration-thread-shell history-thread-shell thread-scroll-list bench-chat-log"
			>
				{#if calibrationThreadSessions.length}
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
									<div class="pill-row thread-chip-row">
										{#if session.requestDisposition}
											<Pill
												label={`Request ${titleCase(String(session.requestDisposition).replace(/_/g, ' '))}`}
												variant="ghost"
											/>
										{/if}
										{#if session.confidence}
											<Pill
												label={`Tuner confidence ${titleCase(String(session.confidence))}`}
												variant="ghost"
											/>
										{/if}
										{#if session.isCurrent && operatorRequestLabel}
											<Pill label={`Experiment ${operatorRequestLabel}`} variant="default" />
										{/if}
										{#if session.isCurrent && runVerdictOutcomeCopy}
											<Pill label={runVerdictOutcomeCopy} variant={runVerdictOutcomeVariant} />
										{/if}
										{#if session.isCurrent && session.runConfidence}
											<Pill
												label={`Run confidence ${titleCase(String(session.runConfidence))}`}
												variant="ghost"
											/>
										{/if}
									</div>
								</article>
							{/if}
						{/each}
					</div>
				{:else}
					<div class="bench-chat-empty">
						<p class="thread-role">Bench</p>
						<p class="thread-copy">Draft the first request to start the bench record.</p>
						<p class="thread-support">
							Your latest request, the bench reply, and any pending draft state will stay visible
							here.
						</p>
					</div>
				{/if}
			</div>
		</section>
	</div>

	{#if pendingProposal}
		<div class:stale={pendingProposalNeedsRefresh} class="proposal-shell diagnosis-shell">
			<div class="proposal-head">
				<div class="section-copy-block">
					<p class="eyebrow-copy">Current bench draft</p>
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
						<Pill label={pendingProposalSelfCheckLabel} variant={pendingProposalSelfCheckVariant} />
					{/if}
					{#if pendingProposal?.confidence}
						<Pill
							label={`Bench confidence ${titleCase(String(pendingProposal.confidence))}`}
							variant="ghost"
						/>
					{/if}
				</div>
			</div>

			<div class="diagnosis-grid">
				<section class="proposal-workbench-card diagnosis-copy-card">
					<p class="eyebrow-copy">Latest read</p>
					{#if pendingProposal?.request_response || pendingProposal?.summary}
						<p class="thread-copy">
							{pendingProposal.request_response || pendingProposal.summary}
						</p>
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
					{#if pendingProposal?.suggested_follow_up}
						<p class="inline-gate-copy proposal-warning-copy">
							<span class="eyebrow-copy">Suggested follow-up</span>
							{pendingProposal.suggested_follow_up}
						</p>
					{/if}
					{#if previewSubmission}
						<p class="inline-gate-copy proposal-warning-copy">
							<span class="eyebrow-copy">Drafting now</span>
							Bench received {previewSubmission.note || 'your latest request'} and is preparing a fresh
							reply.
						</p>
					{/if}
				</section>

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
						<p class="muted-copy">
							This draft keeps the current policy. Review the evidence below before deciding whether
							to ask again.
						</p>
					{/if}
					{#if pendingProposalSelfCheck.summary}
						<p class="inline-gate-copy proposal-warning-copy">
							<span class="eyebrow-copy">Before you run it</span>
							{pendingProposalSelfCheck.summary}
						</p>
					{/if}
					{#if pendingProposalSelfCheck.issues?.length}
						<ul class="proposal-issue-list">
							{#each pendingProposalSelfCheck.issues ?? [] as issue (issue)}
								<li>{issue}</li>
							{/each}
						</ul>
					{/if}
					{#if pendingProposalNeedsRefresh}
						<p class="inline-gate-copy proposal-warning-copy">
							<span class="eyebrow-copy">Refresh needed</span>
							This draft no longer matches the current note or selected host.
						</p>
					{/if}
				</section>
			</div>

			<details class="proposal-trace-shell diagnosis-details">
				<summary>Bench context and trace</summary>
				<div class="proposal-workbench-grid diagnosis-disclosure-grid">
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
	{:else if currentThreadSession}
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
	{/if}
</div>

<style>
	.bench-chat-shell,
	.archived-diagnosis-shell {
		display: grid;
		gap: var(--space-2);
		padding: 1rem 1.05rem;
		border-radius: var(--radius-lg);
		border: 1px solid rgba(23, 35, 31, 0.08);
		background: rgba(255, 255, 255, 0.7);
		box-shadow: 0 10px 28px rgba(23, 35, 31, 0.04);
	}

	.bench-workspace-shell {
		display: grid;
		gap: 0.9rem;
		padding: 0.35rem;
		min-width: 0;
		border-radius: calc(var(--radius-lg) + 0.2rem);
		background:
			linear-gradient(180deg, rgba(255, 252, 246, 0.96), rgba(252, 249, 242, 0.92)),
			radial-gradient(circle at top left, rgba(242, 220, 182, 0.2), transparent 34%);
		box-shadow: 0 24px 46px rgba(62, 43, 24, 0.09);
	}

	.bench-console-grid {
		display: grid;
		grid-template-columns: minmax(0, 0.96fr) minmax(0, 1.04fr);
		gap: 0.9rem;
		align-items: start;
	}

	.bench-workspace-shell .bench-chat-shell,
	.bench-workspace-shell .diagnosis-shell,
	.bench-workspace-shell .archived-diagnosis-shell {
		background: rgba(255, 255, 255, 0.72);
		border-color: rgba(104, 87, 61, 0.08);
		box-shadow: none;
	}

	.bench-chat-shell {
		gap: 0.9rem;
		padding: 1rem 1.05rem;
		background:
			linear-gradient(180deg, rgba(255, 255, 255, 0.8), rgba(255, 251, 245, 0.72)),
			radial-gradient(circle at top left, rgba(221, 199, 160, 0.16), transparent 36%);
	}

	.bench-compose-shell {
		gap: 0.85rem;
		padding: 1rem 1.05rem;
		border-radius: var(--radius-lg);
		border: 1px solid rgba(15, 118, 110, 0.16);
		background:
			linear-gradient(180deg, rgba(244, 252, 249, 0.94), rgba(255, 255, 255, 0.88)),
			radial-gradient(circle at bottom right, rgba(15, 118, 110, 0.1), transparent 36%);
		box-shadow: none;
	}

	.bench-compose-head {
		display: grid;
		gap: 0.75rem;
	}

	.bench-panel-title {
		margin: 0;
		font-size: 1.12rem;
		line-height: 1.25;
	}

	.bench-compose-pills {
		justify-content: flex-start;
	}

	.bench-workspace-shell .bench-chat-shell {
		border-color: rgba(164, 115, 46, 0.12);
		background:
			linear-gradient(180deg, rgba(255, 255, 255, 0.8), rgba(255, 251, 245, 0.72)),
			radial-gradient(circle at top left, rgba(221, 199, 160, 0.16), transparent 36%);
	}

	.bench-chat-head {
		align-items: center;
	}

	.bench-chat-log {
		max-height: 30rem;
		padding: 0.95rem;
		background:
			linear-gradient(180deg, rgba(255, 255, 255, 0.94), rgba(251, 247, 240, 0.9)),
			rgba(221, 199, 160, 0.08);
		border-color: rgba(164, 115, 46, 0.12);
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

	.bench-workspace-shell .diagnosis-shell {
		background:
			linear-gradient(180deg, rgba(255, 250, 240, 0.94), rgba(255, 255, 255, 0.84)),
			radial-gradient(circle at top right, rgba(221, 199, 160, 0.18), transparent 38%);
	}

	.calibration-thread-shell {
		display: grid;
		gap: 0.85rem;
		padding: 0.95rem 1rem;
		min-width: 0;
		border-radius: var(--radius-md);
		border: 1px solid rgba(15, 118, 110, 0.12);
		background:
			linear-gradient(180deg, rgba(255, 255, 255, 0.9), rgba(255, 255, 255, 0.76)),
			rgba(15, 118, 110, 0.04);
	}

	.history-thread-shell {
		padding: 0.85rem;
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
		gap: 0.32rem;
		max-width: min(100%, 43rem);
		min-width: 0;
		padding: 0.85rem 0.95rem;
		border-radius: 0;
		border: 1px solid rgba(148, 163, 184, 0.14);
		box-shadow: none;
	}

	.operator-turn {
		justify-self: end;
		background: rgba(8, 47, 73, 0.84);
		border-color: rgba(56, 189, 248, 0.24);
	}

	.system-turn {
		justify-self: start;
		background: rgba(15, 23, 42, 0.8);
		border-color: rgba(148, 163, 184, 0.16);
		border-left: 4px solid rgba(251, 146, 60, 0.28);
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
		font-size: 0.98rem;
		line-height: 1.45;
		overflow-wrap: anywhere;
	}

	.thread-support {
		margin: 0;
		font-size: 0.88rem;
		line-height: 1.45;
		color: var(--ink-soft);
		overflow-wrap: anywhere;
	}

	.thread-readout {
		display: grid;
		gap: 0.22rem;
		padding-top: 0.55rem;
		border-top: 1px solid rgba(23, 35, 31, 0.08);
	}

	.thread-chip-row {
		padding-top: 0.1rem;
	}

	.proposal-shell {
		display: grid;
		gap: 0.8rem;
		padding: 0.95rem 1rem;
		border-radius: var(--radius-md);
		border: 1px solid rgba(15, 118, 110, 0.16);
		background:
			linear-gradient(180deg, rgba(255, 255, 255, 0.94), rgba(255, 255, 255, 0.8)),
			rgba(15, 118, 110, 0.06);
	}

	.diagnosis-shell {
		gap: 1rem;
	}

	.diagnosis-grid {
		display: grid;
		gap: 0.8rem;
		grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
	}

	.diagnosis-copy-card {
		grid-column: span 2;
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
		gap: var(--space-3);
		justify-content: space-between;
		align-items: start;
		flex-wrap: wrap;
		min-width: 0;
	}

	.proposal-title {
		margin: 0;
		font-size: 1.08rem;
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
		padding: 0.82rem 0.85rem;
		border-radius: var(--radius-md);
		background: rgba(247, 246, 241, 0.92);
		border: 1px solid rgba(23, 35, 31, 0.07);
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
		padding: 0.9rem;
		border-radius: var(--radius-md);
		background: rgba(255, 255, 255, 0.78);
		border: 1px solid rgba(23, 35, 31, 0.08);
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
		padding: 0.75rem 0.8rem;
		border-radius: calc(var(--radius-md) - 0.15rem);
		background: rgba(247, 246, 241, 0.88);
		border: 1px solid rgba(23, 35, 31, 0.06);
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
		padding: 0.75rem 0.8rem;
		border-radius: calc(var(--radius-md) - 0.15rem);
		background: rgba(247, 246, 241, 0.88);
		border: 1px solid rgba(23, 35, 31, 0.06);
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
		padding: 0.8rem 0.95rem;
		border-radius: calc(var(--radius-md) - 0.12rem);
		background: rgba(255, 255, 255, 0.72);
		border: 1px solid rgba(23, 35, 31, 0.08);
		font-size: 0.84rem;
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
		background: rgba(247, 244, 237, 0.92);
	}

	.proposal-trace-shell[open] summary::after {
		content: '-';
	}

	.proposal-trace-raw {
		display: block;
		width: 100%;
		margin: 0;
		padding: 0.85rem;
		min-width: 0;
		max-width: 100%;
		border-radius: calc(var(--radius-md) - 0.15rem);
		background: rgba(23, 35, 31, 0.06);
		border: 1px solid rgba(23, 35, 31, 0.08);
		font-size: 0.82rem;
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
		padding: 1rem 1.05rem;
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
		max-width: 48ch;
		font-size: 0.88rem;
		line-height: 1.45;
	}

	.field-block {
		display: grid;
		gap: var(--space-2);
	}

	textarea {
		width: 100%;
		padding: 0.9rem 1rem;
		border-radius: var(--radius-md);
		border: 1px solid rgba(23, 35, 31, 0.12);
		background: var(--surface-2);
		color: var(--ink);
		min-height: 8rem;
		resize: vertical;
	}

	.bench-compose-shell textarea {
		min-height: 14rem;
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

	.thread-inline-head {
		display: flex;
		justify-content: space-between;
		gap: 1rem;
		align-items: start;
		min-width: 0;
	}

	.thread-history-meta {
		font-size: 0.8rem;
		color: var(--ink-soft);
		overflow-wrap: anywhere;
	}

	.thread-history-copy {
		margin-top: -0.15rem;
		overflow-wrap: anywhere;
	}

	.section-copy-block {
		display: grid;
		gap: 0.3rem;
		min-width: 0;
	}

	.pill-row {
		display: flex;
		gap: var(--space-2);
		flex-wrap: wrap;
	}

	@media (max-width: 900px) {
		.bench-console-grid {
			grid-template-columns: 1fr;
		}

		.diagnosis-copy-card {
			grid-column: auto;
		}
	}

	@media (max-width: 720px) {
		.thread-inline-head {
			flex-direction: column;
			align-items: start;
		}

		.bench-compose-actions :global(button) {
			width: 100%;
		}
	}
</style>
