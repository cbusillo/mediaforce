<script lang="ts">
	import {
		stagedIntegrityDispositionCopy,
		type SeasonPromotionIntegrity
	} from '$lib/season/experience';

	let {
		integrity,
		tone
	}: {
		integrity: SeasonPromotionIntegrity;
		tone: 'blocked' | 'ready';
	} = $props();

	function recordPath(relPath: string | null, stagingPath: string | null): string {
		return relPath || stagingPath || 'Unidentified staged file';
	}
</script>

<div class:integrity-panel--blocked={tone === 'blocked'} class="integrity-panel">
	<div class="integrity-summary" aria-label="Season replacement readiness" role="group">
		<div>
			<span>Ready to replace</span>
			<strong>{integrity.readyCount}</strong>
		</div>
		<div>
			<span>Already in place</span>
			<strong>{integrity.alreadyPlacedCount}</strong>
		</div>
		<div>
			<span>File blockers</span>
			<strong>{integrity.unresolvedCount}</strong>
		</div>
	</div>

	{#if integrity.blockers.length > 0}
		<div class="integrity-blockers" aria-label="Season blockers" role="group">
			{#each integrity.blockers as blocker, index (`${blocker.code}-${index}`)}
				<div class="integrity-blocker">
					<strong>{blocker.count} × {blocker.label}</strong>
					<span>{blocker.nextAction}</span>
				</div>
			{/each}
		</div>
	{/if}

	{#if integrity.records.length > 0}
		<details class="integrity-inventory" open={tone === 'blocked'}>
			<summary>
				<span
					>{integrity.totalCount === 1
						? 'Browse the season file'
						: `Browse all ${integrity.totalCount} season files`}</span
				>
				<small
					>{integrity.reportComplete
						? 'Complete inventory'
						: 'Inventory needs another check'}</small
				>
			</summary>
			<div class="integrity-table-wrap">
				<table>
					<thead>
						<tr>
							<th>File</th>
							<th>State</th>
							<th>What happens next</th>
						</tr>
					</thead>
					<tbody>
						{#each integrity.records as record, index (`${record.disposition}-${record.item_id ?? 'file'}-${record.staging_path ?? index}`)}
							{@const copy = stagedIntegrityDispositionCopy(record.disposition)}
							<tr>
								<td><code>{recordPath(record.rel_path, record.staging_path)}</code></td>
								<td><strong>{copy.label}</strong></td>
								<td>
									<span>{record.detail}</span>
									<small>{copy.nextAction}</small>
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		</details>
	{/if}
</div>

<style>
	.integrity-panel {
		display: grid;
		gap: 10px;
		max-width: 860px;
		width: 100%;
	}

	.integrity-summary {
		background: var(--mf-bg-panel-2);
		border: 1px solid var(--mf-line-muted);
		display: grid;
		grid-template-columns: repeat(3, minmax(0, 1fr));
	}

	.integrity-summary div {
		border-right: 1px solid var(--mf-line-muted);
		display: grid;
		gap: 3px;
		padding: 10px 12px;
	}

	.integrity-summary div:last-child {
		border-right: 0;
	}

	.integrity-summary span,
	.integrity-inventory small,
	.integrity-blocker span,
	td small {
		color: var(--mf-fg-tertiary);
		font-size: 11px;
	}

	.integrity-summary strong {
		font-size: 18px;
	}

	.integrity-panel--blocked .integrity-summary div:last-child strong {
		color: var(--mf-fail-fg);
	}

	.integrity-blockers {
		border: 1px solid var(--mf-fail-line);
		display: grid;
	}

	.integrity-blocker {
		align-items: baseline;
		background: var(--mf-fail-bg);
		border-bottom: 1px solid var(--mf-fail-line);
		display: grid;
		gap: 8px;
		grid-template-columns: minmax(170px, 0.7fr) minmax(240px, 1.3fr);
		padding: 9px 11px;
	}

	.integrity-blocker:last-child {
		border-bottom: 0;
	}

	.integrity-blocker strong {
		color: var(--mf-fail-fg);
		font-size: 12px;
	}

	.integrity-inventory {
		border: 1px solid var(--mf-line-muted);
	}

	.integrity-inventory summary {
		align-items: center;
		background: var(--mf-bg-panel-2);
		cursor: pointer;
		display: flex;
		font-size: 12px;
		font-weight: 650;
		justify-content: space-between;
		padding: 10px 12px;
	}

	.integrity-table-wrap {
		overflow-x: auto;
	}

	table {
		border-collapse: collapse;
		font-size: 11px;
		width: 100%;
	}

	th,
	td {
		border-bottom: 1px solid var(--mf-line-muted);
		padding: 8px 10px;
		text-align: left;
		vertical-align: top;
	}

	th {
		background: var(--mf-bg-panel);
		color: var(--mf-fg-tertiary);
		position: sticky;
		top: 0;
		z-index: 1;
	}

	td:first-child {
		max-width: 320px;
	}

	code {
		color: var(--mf-fg-secondary);
		font-family: var(--mf-font-mono);
		font-size: 10px;
		overflow-wrap: anywhere;
	}

	td span,
	td small {
		display: block;
	}

	td small {
		margin-top: 3px;
	}

	@media (max-width: 640px) {
		.integrity-summary {
			grid-template-columns: 1fr;
		}

		.integrity-summary div,
		.integrity-summary div:last-child {
			border-bottom: 1px solid var(--mf-line-muted);
			border-right: 0;
		}

		.integrity-summary div:last-child {
			border-bottom: 0;
		}

		.integrity-blocker {
			grid-template-columns: 1fr;
		}

		.integrity-inventory summary {
			align-items: flex-start;
			gap: 4px;
			flex-direction: column;
		}

		table,
		tbody,
		tr,
		td {
			display: block;
		}

		thead {
			display: none;
		}

		tr {
			border-bottom: 1px solid var(--mf-line-muted);
			padding: 8px 10px;
		}

		td {
			border: 0;
			max-width: none;
			padding: 3px 0;
		}
	}
</style>
