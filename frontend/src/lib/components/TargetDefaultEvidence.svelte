<script lang="ts">
	import type {
		TargetDefaultEvidence,
		TargetDefaultEvidenceConfidence,
		TargetDefaultEvidenceScope,
		TargetDefaultReportPayload
	} from '$lib/api/types';
	import { formatDecimalFileSize } from '$lib/season/experience';

	let {
		evidence,
		currentTargetBytes = null,
		durationSeconds = null
	}: {
		evidence?: TargetDefaultEvidence | null;
		currentTargetBytes?: number | null;
		durationSeconds?: number | null;
	} = $props();

	const report = $derived(
		evidence?.schema_version === 1 &&
			evidence.status === 'available' &&
			validReport(evidence.report)
			? evidence.report
			: null
	);
	const proposalBytes = $derived(
		report &&
			typeof report.proposed_bytes_per_45_minutes === 'number' &&
			Number.isFinite(report.proposed_bytes_per_45_minutes) &&
			report.proposed_bytes_per_45_minutes > 0
			? report.proposed_bytes_per_45_minutes
			: null
	);
	const exactItemBytes = $derived(
		report && proposalBytes !== null
			? exactItemEstimate(proposalBytes, report.reference_runtime_seconds, durationSeconds)
			: null
	);
	const selectedScopeReport = $derived(
		report ? (report.scopes.find((scope) => scope.scope === report.proposed_scope) ?? null) : null
	);
	const unavailableReason = $derived(
		friendlyReason(evidence?.reason ?? evidence?.report?.fallback_reason ?? null)
	);

	function validReport(
		value: TargetDefaultReportPayload | null
	): value is TargetDefaultReportPayload {
		return Boolean(
			value &&
			value.schema_version === 1 &&
			value.source === 'operator_visual_boundaries' &&
			value.mode === 'review_only' &&
			value.reference_runtime_seconds === 2700 &&
			Array.isArray(value.scopes) &&
			value.proposed_scope &&
			typeof value.proposed_bytes_per_45_minutes === 'number' &&
			Number.isFinite(value.proposed_bytes_per_45_minutes) &&
			value.proposed_bytes_per_45_minutes > 0 &&
			value.scopes.some((scope) => scope.scope === value.proposed_scope)
		);
	}

	function exactItemEstimate(
		bytesPer45Minutes: number,
		referenceRuntimeSeconds: number,
		duration: number | null
	): number | null {
		if (typeof duration !== 'number' || !Number.isFinite(duration) || duration <= 0) return null;
		const estimate = (bytesPer45Minutes * duration) / referenceRuntimeSeconds;
		return Number.isFinite(estimate) && estimate > 0 ? estimate : null;
	}

	function scopeLabel(scope: TargetDefaultEvidenceScope): string {
		if (scope === 'item') return 'Exact item';
		if (scope === 'folder') return 'Same folder';
		return 'Content profile';
	}

	function confidenceLabel(value: TargetDefaultEvidenceConfidence): string {
		if (value === 'high') return 'High';
		if (value === 'moderate') return 'Moderate';
		if (value === 'limited') return 'Limited';
		return 'None';
	}

	function runtimeLabel(seconds: number): string {
		const minutes = Math.round(seconds / 60);
		if (minutes < 60) return `${minutes} min runtime-derived estimate`;
		const hours = Math.floor(minutes / 60);
		const remaining = minutes % 60;
		return remaining
			? `${hours} hr ${remaining} min runtime-derived estimate`
			: `${hours} hr runtime-derived estimate`;
	}

	function friendlyReason(reason: string | null): string {
		const normalized = String(reason ?? '')
			.trim()
			.toLowerCase();
		if (!normalized) return 'No review-only target proposal is available for this item yet.';
		if (normalized.includes('missing') || normalized.includes('context')) {
			return 'This item does not have enough matching review context for a suggestion.';
		}
		if (normalized.includes('conflict')) {
			return 'The reviewed size boundaries conflict, so the current target stays in place.';
		}
		if (normalized.includes('quality')) {
			return 'The available reviews do not meet the threshold for a suggestion.';
		}
		if (normalized.includes('spread') || normalized.includes('unstable')) {
			return 'The reviewed sizes vary too much for a stable suggestion.';
		}
		if (normalized.includes('insufficient') || normalized.includes('supported')) {
			return 'There are not enough compatible repeated reviews for a suggestion.';
		}
		return 'No review-only target proposal is available for this item yet.';
	}
</script>

<div
	class:target-evidence--unavailable={!report || !selectedScopeReport || proposalBytes === null}
	class="target-evidence"
	data-target-default-evidence={report && selectedScopeReport && proposalBytes !== null
		? 'available'
		: 'unavailable'}
>
	<details>
		<summary>
			<strong
				>{report && selectedScopeReport && proposalBytes !== null
					? 'Suggested sample target'
					: 'Target-size evidence'}</strong
			>
			<span
				>{report && selectedScopeReport && proposalBytes !== null
					? 'Target-size evidence · review-only'
					: 'Unavailable'}</span
			>
		</summary>
		{#if report && selectedScopeReport && proposalBytes !== null}
			<div class="target-evidence__body">
				<div class="target-evidence__lead">
					<span>Suggested size</span>
					<strong>{formatDecimalFileSize(proposalBytes)} per 45 minutes</strong>
				</div>
				{#if exactItemBytes !== null}
					<div class="target-evidence__row">
						<span>For this item</span>
						<strong>{formatDecimalFileSize(exactItemBytes)}</strong>
						<small>{runtimeLabel(durationSeconds as number)}</small>
					</div>
				{/if}
				<div class="target-evidence__facts">
					<div>
						<span>Scope</span><strong
							>{scopeLabel(report.proposed_scope as TargetDefaultEvidenceScope)}</strong
						>
					</div>
					<div>
						<span>Evidence</span>
						<strong
							>{selectedScopeReport.approved_source_count} files · {selectedScopeReport.approved_artifact_count}
							reviews</strong
						>
						{#if selectedScopeReport.rejected_source_count > 0}
							<small>{selectedScopeReport.rejected_source_count} rejected files reviewed</small>
						{/if}
					</div>
					<div>
						<span>Evidence confidence</span><strong
							>{confidenceLabel(selectedScopeReport.confidence)}</strong
						>
					</div>
				</div>
				<p class="target-evidence__note">
					Confidence reflects evidence coverage, not a quality guarantee.
				</p>
				<p class="target-evidence__note">
					Based on sample reviews; production acceptance has not been verified.
				</p>
				<p class="target-evidence__current">
					{#if typeof currentTargetBytes === 'number' && Number.isFinite(currentTargetBytes) && currentTargetBytes > 0}
						Current target: {formatDecimalFileSize(currentTargetBytes)}. This suggestion does not
						change it.
					{:else}
						This suggestion does not change the current target.
					{/if}
				</p>
			</div>
		{:else}
			<p class="target-evidence__unavailable">{unavailableReason}</p>
		{/if}
	</details>
</div>

<style>
	.target-evidence {
		border-top: 1px solid var(--mf-line-muted);
		margin-top: 10px;
		padding-top: 10px;
	}
	.target-evidence--unavailable {
		color: var(--mf-fg-primary);
		font-size: 12px;
	}
	details {
		background: var(--mf-bg-panel-2);
		border: 1px solid var(--mf-line-muted);
		border-radius: var(--mf-radius-2);
	}
	summary {
		align-items: center;
		cursor: pointer;
		display: flex;
		justify-content: space-between;
		min-height: 34px;
		padding: 0 10px;
	}
	summary strong,
	.target-evidence__lead strong,
	.target-evidence__row strong,
	.target-evidence__facts strong {
		color: var(--mf-fg-primary);
		font-size: 11px;
	}
	summary span,
	.target-evidence__body span,
	.target-evidence__body small {
		color: var(--mf-fg-tertiary);
		font-size: 10px;
	}
	summary span {
		font-family: var(--mf-font-mono), monospace;
	}
	.target-evidence__body {
		border-top: 1px solid var(--mf-line-muted);
		display: grid;
		gap: 8px;
		padding: 10px 12px 11px;
	}
	.target-evidence__lead,
	.target-evidence__row,
	.target-evidence__facts > div {
		display: grid;
		gap: 3px;
	}
	.target-evidence__lead strong {
		font-size: 15px;
	}
	.target-evidence__row {
		border-top: 1px solid var(--mf-line-muted);
		padding-top: 8px;
	}
	.target-evidence__facts {
		display: grid;
		gap: 8px;
		grid-template-columns: repeat(3, minmax(0, 1fr));
	}
	.target-evidence__note,
	.target-evidence__current,
	.target-evidence__unavailable {
		color: var(--mf-fg-secondary);
		font-size: 11px;
		line-height: 1.4;
		margin: 0;
	}
	.target-evidence__current {
		color: var(--mf-fg-primary);
	}
	.target-evidence__unavailable {
		margin: 6px 0 0;
	}
	@media (max-width: 560px) {
		.target-evidence__facts {
			grid-template-columns: 1fr;
		}
	}
</style>
