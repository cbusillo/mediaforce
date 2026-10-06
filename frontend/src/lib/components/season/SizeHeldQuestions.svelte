<script lang="ts">
	import type { StagedIntegrityRecord } from '$lib/api/types';
	import { formatDecimalFileSize } from '$lib/season/experience';

	let {
		records,
		busy = false,
		onDecision
	}: {
		records: StagedIntegrityRecord[];
		busy?: boolean;
		onDecision: (libraryItemId: number, keep: boolean) => void;
	} = $props();

	function fileName(relPath: string | null): string {
		return (relPath ?? '').split('/').at(-1) || 'This file';
	}
</script>

{#if records.length > 0}
	<section id="season-size-held" class="size-held" aria-label="Files that need you">
		<h3>Needs you</h3>
		<ul>
			{#each records as record (record.item_id)}
				<li>
					<span class="size-held__file">{fileName(record.rel_path)}</span>
					<span class="size-held__detail">
						{#if record.remake?.reason === 'final_size'}
							Missed its approved size goal.
						{:else if record.remake?.reason === 'settings_history'}
							Its approved settings were not recorded.
						{:else}
							Came out at {formatDecimalFileSize(record.size_prediction?.actual_bytes)}; its sample
							predicted {formatDecimalFileSize(record.size_prediction?.predicted_bytes)}.
						{/if}
						Making it again removes only this compressed copy and queues this file. The original stays.
						{#if record.remake?.blocked_reason}
							{record.remake.blocked_reason}
						{/if}
					</span>
					<span class="size-held__actions">
						{#if record.disposition === 'size_held'}
							<button
								type="button"
								disabled={busy}
								onclick={() => onDecision(record.item_id as number, true)}
							>
								Keep this file
							</button>
						{/if}
						<button
							type="button"
							disabled={busy || Boolean(record.remake?.blocked_reason)}
							onclick={() => onDecision(record.item_id as number, false)}
						>
							Make again
						</button>
					</span>
				</li>
			{/each}
		</ul>
	</section>
{/if}

<style>
	.size-held {
		border-left: 2px solid #b1625f;
		display: grid;
		gap: 6px;
		padding-left: 12px;
	}

	.size-held h3 {
		color: var(--mf-fg-tertiary);
		font-size: 11px;
		font-weight: 600;
		letter-spacing: 0.08em;
		margin: 0;
		text-transform: uppercase;
	}

	.size-held ul {
		display: grid;
		gap: 10px;
		list-style: none;
		margin: 0;
		padding: 0;
	}

	.size-held li {
		display: grid;
		font-size: 13px;
		gap: 3px;
	}

	.size-held__file {
		font-family: var(--mf-font-mono);
		font-size: 12px;
		overflow-wrap: anywhere;
	}

	.size-held__detail {
		color: var(--mf-fg-tertiary);
	}

	.size-held__actions {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
		margin-top: 3px;
	}

	.size-held__actions button {
		background: transparent;
		border: 1px solid var(--mf-line-muted);
		border-radius: 999px;
		color: inherit;
		cursor: pointer;
		font: inherit;
		font-size: 12px;
		font-weight: 650;
		min-height: 30px;
		padding: 0 12px;
	}

	.size-held__actions button:first-child {
		background: #232620;
		border-color: #232620;
		color: #f7f1e7;
	}

	.size-held__actions button:disabled {
		cursor: not-allowed;
		opacity: 0.45;
	}

	.size-held__actions button:focus-visible {
		outline: 3px solid #4b8060;
		outline-offset: 2px;
	}
</style>
