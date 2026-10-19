<script lang="ts">
	import type { MotionHeldFile, MotionKeptFile } from '$lib/api/types';
	import { motionHoldReasonLabel } from '$lib/season/experience';

	let {
		held = [],
		kept = [],
		busy = false,
		finishes = 'season',
		onKeep,
		onUndo
	}: {
		held?: MotionHeldFile[];
		kept?: MotionKeptFile[];
		busy?: boolean;
		/** What keeping them lets finish, named in the intro. */
		finishes?: 'season' | 'seasons' | 'episode';
		onKeep: (libraryItemIds: number[]) => void;
		onUndo: (libraryItemIds: number[]) => void;
	} = $props();

	function fileName(relPath: string): string {
		return relPath.split('/').at(-1) || relPath;
	}

	const finishTarget = $derived(
		finishes === 'seasons' ? 'their seasons' : finishes === 'episode' ? 'it' : 'the season'
	);
</script>

{#if held.length > 0 || kept.length > 0}
	<div class="motion-keep">
		{#if held.length > 0}
			<section class="motion-keep__group" aria-label="Held for their motion pattern">
				<h3>Held for their motion pattern</h3>
				<p>
					Mediaforce can’t convert {held.length === 1 ? 'this episode' : 'these episodes'} safely. Keep
					{held.length === 1 ? 'it' : 'them'} original to finish {finishTarget}. You can undo this.
				</p>
				{#if held.length > 1}
					<div class="motion-keep__bulk">
						<button
							type="button"
							disabled={busy}
							onclick={() => onKeep(held.map((file) => file.library_item_id))}
						>
							Keep all {held.length} original
						</button>
					</div>
				{/if}
				<ul>
					{#each held as file (file.library_item_id)}
						<li>
							<span class="motion-keep__file">{fileName(file.rel_path)}</span>
							<span class="motion-keep__reason">{motionHoldReasonLabel(file.reason)}</span>
							<button
								type="button"
								disabled={busy}
								aria-label={`Keep ${fileName(file.rel_path)} original`}
								onclick={() => onKeep([file.library_item_id])}
							>
								Keep original
							</button>
						</li>
					{/each}
				</ul>
			</section>
		{/if}
		{#if kept.length > 0}
			<section class="motion-keep__group" aria-label="Kept original">
				<h3>Kept original</h3>
				<p>
					You chose to keep {kept.length === 1 ? 'this episode' : 'these episodes'} as {kept.length ===
					1
						? 'it is'
						: 'they are'}. {kept.length === 1 ? 'It no longer counts' : 'They no longer count'} as work.
				</p>
				{#if kept.length > 1}
					<div class="motion-keep__bulk">
						<button
							type="button"
							disabled={busy}
							onclick={() => onUndo(kept.map((file) => file.library_item_id))}
						>
							Undo all {kept.length}
						</button>
					</div>
				{/if}
				<ul>
					{#each kept as file (file.library_item_id)}
						<li>
							<span class="motion-keep__file">{fileName(file.rel_path)}</span>
							<span class="motion-keep__reason">{motionHoldReasonLabel(file.reason)}</span>
							<button
								type="button"
								disabled={busy}
								aria-label={`Undo keeping ${fileName(file.rel_path)} original`}
								onclick={() => onUndo([file.library_item_id])}
							>
								Undo
							</button>
						</li>
					{/each}
				</ul>
			</section>
		{/if}
	</div>
{/if}

<style>
	.motion-keep {
		display: grid;
		gap: 14px;
		text-align: left;
	}

	.motion-keep__group {
		border-left: 2px solid var(--mf-line-muted);
		display: grid;
		gap: 6px;
		padding-left: 12px;
	}

	.motion-keep h3 {
		color: var(--mf-fg-tertiary);
		font-size: 11px;
		font-weight: 600;
		letter-spacing: 0.08em;
		margin: 0;
		text-transform: uppercase;
	}

	.motion-keep p {
		font-size: 13px;
		margin: 0;
	}

	.motion-keep ul {
		display: grid;
		gap: 8px;
		list-style: none;
		margin: 0;
		padding: 0;
	}

	.motion-keep li {
		align-items: center;
		display: flex;
		flex-wrap: wrap;
		font-size: 13px;
		gap: 4px 10px;
	}

	.motion-keep__file {
		font-family: var(--mf-font-mono), monospace;
		font-size: 12px;
		overflow-wrap: anywhere;
	}

	.motion-keep__reason {
		color: var(--mf-fg-tertiary);
	}

	.motion-keep__bulk {
		display: flex;
	}

	.motion-keep button {
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

	.motion-keep button:disabled {
		cursor: not-allowed;
		opacity: 0.45;
	}

	.motion-keep button:focus-visible {
		outline: 3px solid #4b8060;
		outline-offset: 2px;
	}
</style>
