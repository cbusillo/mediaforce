<script lang="ts">
	import type { WaitingReasons } from '$lib/season/experience';

	let { reasons }: { reasons: WaitingReasons } = $props();
</script>

{#if reasons.needsYou.length || reasons.waiting.length}
	<div class="waiting-reasons">
		{#if reasons.needsYou.length}
			<section class="waiting-reasons__group waiting-reasons__group--owner">
				<h2>Needs you</h2>
				<ul>
					{#each reasons.needsYou as group (group.reason)}
						<li><strong>{group.count}</strong> {group.label}</li>
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

	strong {
		font-variant-numeric: tabular-nums;
		font-weight: 600;
		margin-right: 4px;
	}
</style>
