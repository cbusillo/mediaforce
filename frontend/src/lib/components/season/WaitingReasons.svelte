<script lang="ts">
	import { formatDecimalFileSize, type WaitingReasons } from '$lib/season/experience';

	let {
		reasons,
		scope = '',
		busy = false,
		anchorId = undefined,
		onSizeDecision
	}: {
		reasons: WaitingReasons;
		scope?: string;
		busy?: boolean;
		/** An id other parts of the page link to, such as the season's episode list. */
		anchorId?: string;
		onSizeDecision?: (jobId: string, allow: boolean) => void;
	} = $props();

	function fileName(relPath: string): string {
		return relPath.split('/').at(-1) ?? relPath;
	}
</script>

{#if reasons.needsYou.length || reasons.waiting.length || reasons.yourChoices.length}
	<div class="waiting-reasons" id={anchorId}>
		{#if scope}<p class="waiting-reasons__scope">For {scope}</p>{/if}
		{#if reasons.needsYou.length}
			<section class="waiting-reasons__group waiting-reasons__group--owner">
				<h2>Needs you</h2>
				<ul>
					{#each reasons.needsYou as group (group.reason)}
						<li>
							<strong>{group.count}</strong>
							{group.label}
							{#if onSizeDecision && group.size_questions?.length}
								<ul class="size-questions">
									{#each group.size_questions as question (question.job_id)}
										<li class="size-question">
											<span class="size-question__file">{fileName(question.rel_path)}</span>
											<span class="size-question__detail">
												Size goal {formatDecimalFileSize(question.goal_bytes)}. Keeping the picture
												quality needs {formatDecimalFileSize(question.smallest_quality_safe_bytes)}.
											</span>
											<span class="size-question__actions">
												<button
													type="button"
													disabled={busy}
													onclick={() => onSizeDecision(question.job_id, true)}
												>
													Allow {formatDecimalFileSize(question.smallest_quality_safe_bytes)} for this
													file
												</button>
												<button
													type="button"
													class="quiet"
													disabled={busy}
													onclick={() => onSizeDecision(question.job_id, false)}
												>
													Keep the original
												</button>
											</span>
										</li>
									{/each}
								</ul>
							{/if}
						</li>
					{/each}
				</ul>
			</section>
		{/if}
		{#if reasons.waiting.length}
			<section class="waiting-reasons__group">
				<h2>Waiting</h2>
				<ul>
					{#each reasons.waiting as group (group.reason)}
						<li><strong>{group.count}</strong> {group.label}</li>
					{/each}
				</ul>
			</section>
		{/if}
		{#if reasons.yourChoices.length}
			<section class="waiting-reasons__group">
				<h2>Your choices</h2>
				<ul>
					{#each reasons.yourChoices as group (group.reason)}
						<li><strong>{group.count}</strong> {group.label}</li>
					{/each}
				</ul>
			</section>
		{/if}
	</div>
{/if}

<style>
	.waiting-reasons {
		display: flex;
		flex-wrap: wrap;
		gap: 12px 32px;
		margin-top: 22px;
		max-width: 620px;
	}

	.waiting-reasons__scope {
		color: var(--muted);
		flex-basis: 100%;
		font-size: 12px;
		margin: 0;
	}

	.waiting-reasons__group {
		border-left: 2px solid var(--line);
		min-width: 0;
		padding-left: 12px;
	}

	.waiting-reasons__group--owner {
		border-left-color: #b1625f;
	}

	h2 {
		color: var(--muted);
		font-size: 11px;
		font-weight: 600;
		letter-spacing: 0.08em;
		margin: 0 0 6px;
		text-transform: uppercase;
	}

	ul {
		display: grid;
		gap: 3px;
		list-style: none;
		margin: 0;
		padding: 0;
	}

	li {
		font-size: 13px;
		overflow-wrap: anywhere;
	}

	.size-questions {
		gap: 10px;
		margin: 8px 0 4px;
	}

	.size-question {
		display: grid;
		gap: 3px;
	}

	.size-question__file {
		font-family: var(--mf-font-mono);
		font-size: 12px;
	}

	.size-question__detail {
		color: var(--muted);
	}

	.size-question__actions {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
		margin-top: 3px;
	}

	.size-question__actions button {
		background: transparent;
		border: 1px solid var(--line);
		border-radius: 999px;
		color: inherit;
		cursor: pointer;
		font: inherit;
		font-size: 12px;
		font-weight: 650;
		min-height: 30px;
		padding: 0 12px;
	}

	.size-question__actions button:first-child {
		background: #232620;
		border-color: #232620;
		color: #f7f1e7;
	}

	.size-question__actions button:disabled {
		cursor: not-allowed;
		opacity: 0.45;
	}

	.size-question__actions button:focus-visible {
		outline: 3px solid #4b8060;
		outline-offset: 2px;
	}

	strong {
		font-variant-numeric: tabular-nums;
		font-weight: 600;
		margin-right: 4px;
	}
</style>
