<script lang="ts">
	import type { HostRuntime } from '$lib/api/types';
	import Panel from '$lib/components/Panel.svelte';
	import { titleCase } from '$lib/format';

	let { host }: { host: HostRuntime } = $props();

	type StateTone = 'ok' | 'warn' | 'hold' | 'neutral';

	const stateChip = $derived.by(() => {
		if (!host.available) {
			return { label: 'Attention', tone: 'warn' as StateTone };
		}

		if (host.queue_active) {
			return { label: 'Ready', tone: 'ok' as StateTone };
		}

		if (host.schedule_open === false) {
			return { label: 'Window Closed', tone: 'hold' as StateTone };
		}

		if (host.active_reason === 'parallel encode slots are full') {
			return { label: 'Busy', tone: 'neutral' as StateTone };
		}

		if (host.active_reason === 'encode queue capability disabled') {
			return { label: 'Disabled', tone: 'neutral' as StateTone };
		}

		return { label: 'Standby', tone: 'neutral' as StateTone };
	});

	const supportingDetails = $derived.by(() => {
		const details: string[] = [];

		if (host.schedule_detail) {
			details.push(`Schedule: ${host.schedule_detail}`);
		}

		if (host.capabilities.length) {
			details.push(
				`Capabilities: ${host.capabilities.map((capability) => titleCase(capability)).join(' · ')}`
			);
		}

		return details;
	});

	const runtimeFacts = $derived.by(() => {
		const facts = [host.schedule_profile_label, `P${host.priority}`];

		facts.push(`${host.max_parallel_encodes} lane${host.max_parallel_encodes === 1 ? '' : 's'}`);
		facts.push(host.active_encode_count > 0 ? `${host.active_encode_count} running` : 'Idle');

		return facts;
	});
</script>

<Panel class={`host-card ${stateChip.tone}`.trim()} padding="0.85rem 0.95rem 0.95rem">
	<div class="head-row">
		<div class="title-block">
			<h3>{host.label}</h3>
			<p class="muted-copy host-key">{host.key}</p>
		</div>
		<span class={`state-chip ${stateChip.tone}`.trim()}>
			<span class="state-dot"></span>
			{stateChip.label}
		</span>
	</div>

	<div class="meta-strip">
		{#each runtimeFacts as fact (fact)}
			<span>{fact}</span>
		{/each}
	</div>

	{#if host.message}
		<div class="detail-block">
			<p class="eyebrow-copy">Status</p>
			<p class="muted-copy">{host.message}</p>
		</div>
	{/if}

	{#if supportingDetails.length}
		<div class="supporting-strip muted-copy" aria-label="Host details">
			{#each supportingDetails as detail (detail)}
				<span>{detail}</span>
			{/each}
		</div>
	{/if}

	{#if host.issues.length}
		<div class="issues-box">
			<p class="eyebrow-copy">Attention</p>
			<ul class="muted-copy issues">
				{#each host.issues as issue (issue)}
					<li>{issue}</li>
				{/each}
			</ul>
		</div>
	{/if}
</Panel>

<style>
	:global(.host-card) {
		display: grid;
		gap: 0.7rem;
	}

	:global(.host-card)::before {
		content: '';
		position: absolute;
		inset: 0 auto 0 0;
		width: 4px;
		background: rgba(23, 35, 31, 0.1);
		pointer-events: none;
	}

	:global(.host-card.ok)::before {
		background: linear-gradient(180deg, rgba(47, 107, 62, 0.9), rgba(47, 107, 62, 0.28));
	}

	:global(.host-card.warn)::before {
		background: linear-gradient(180deg, rgba(194, 65, 12, 0.9), rgba(194, 65, 12, 0.28));
	}

	:global(.host-card.hold)::before {
		background: linear-gradient(180deg, rgba(180, 83, 9, 0.85), rgba(180, 83, 9, 0.24));
	}

	:global(.host-card.neutral)::before {
		background: linear-gradient(180deg, rgba(82, 101, 94, 0.62), rgba(82, 101, 94, 0.18));
	}

	.head-row {
		display: flex;
		justify-content: space-between;
		gap: var(--space-2);
		align-items: start;
		flex-wrap: wrap;
	}

	.title-block {
		min-width: 0;
		flex: 1 1 180px;
	}

	h3 {
		font-size: 1.12rem;
		font-weight: 700;
		line-height: 1.1;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.host-key {
		font-size: 0.84rem;
	}

	.state-chip {
		display: inline-flex;
		align-items: center;
		gap: 0.45rem;
		padding: 0.28rem 0.62rem;
		border-radius: var(--radius-pill);
		font-size: 0.8rem;
		font-weight: 700;
		line-height: 1;
		white-space: nowrap;
		background: rgba(15, 118, 110, 0.08);
		color: var(--accent-deep);
		border: 1px solid rgba(15, 118, 110, 0.14);
	}

	.state-chip.ok {
		background: rgba(47, 107, 62, 0.1);
		color: var(--ok);
		border-color: rgba(47, 107, 62, 0.18);
	}

	.state-chip.warn {
		background: rgba(194, 65, 12, 0.1);
		color: var(--warn);
		border-color: rgba(194, 65, 12, 0.18);
	}

	.state-chip.hold {
		background: rgba(180, 83, 9, 0.1);
		color: #9a5b00;
		border-color: rgba(180, 83, 9, 0.18);
	}

	.state-chip.neutral {
		background: rgba(255, 255, 255, 0.76);
		color: var(--ink-soft);
		border-color: rgba(23, 35, 31, 0.12);
	}

	.state-dot {
		width: 0.45rem;
		height: 0.45rem;
		border-radius: 999px;
		background: currentColor;
		flex: 0 0 auto;
	}

	.meta-strip {
		display: flex;
		flex-wrap: wrap;
		gap: 0.45rem;
		font-size: 0.79rem;
		line-height: 1.35;
	}

	.meta-strip span {
		display: inline-flex;
		align-items: center;
		padding: 0.34rem 0.62rem;
		border-radius: 999px;
		background: rgba(255, 255, 255, 0.68);
		border: 1px solid rgba(23, 35, 31, 0.08);
		color: var(--ink-soft);
	}

	.detail-block {
		display: grid;
		gap: 0.2rem;
		padding: 0.65rem 0.8rem;
		border-radius: var(--radius-md);
		background: rgba(255, 255, 255, 0.5);
		border: 1px solid rgba(23, 35, 31, 0.06);
	}

	.supporting-strip {
		display: flex;
		flex-wrap: wrap;
		gap: 0.5rem;
		font-size: 0.8rem;
		line-height: 1.4;
	}

	.supporting-strip span {
		display: inline-flex;
		align-items: center;
		padding: 0.18rem 0;
	}

	.issues-box {
		display: grid;
		gap: 0.35rem;
		padding: 0.75rem 0.85rem;
		border-radius: var(--radius-md);
		background: rgba(194, 65, 12, 0.06);
		border-left: 3px solid rgba(194, 65, 12, 0.7);
	}

	.issues {
		margin: 0;
		padding-left: 1rem;
		line-height: 1.45;
	}
</style>
