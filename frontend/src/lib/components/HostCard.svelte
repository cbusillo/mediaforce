<script lang="ts">
	import type { HostRuntime } from '$lib/api/types';
	import Panel from '$lib/components/Panel.svelte';
	import { titleCase } from '$lib/format';

	let {
		host,
		onSettingsClick = null
	}: {
		host: HostRuntime;
		onSettingsClick?: (() => void) | null;
	} = $props();

	type StateTone = 'ok' | 'warn' | 'hold' | 'neutral';

	const hostTone = $derived.by(() => {
		if (!host.available) {
			return 'warn' as StateTone;
		}

		if (host.queue_active) {
			return 'ok' as StateTone;
		}

		if (host.schedule_open === false) {
			return 'hold' as StateTone;
		}

		if (host.active_reason === 'parallel encode slots are full') {
			return 'neutral' as StateTone;
		}

		if (host.active_reason === 'encode queue capability disabled') {
			return 'warn' as StateTone;
		}

		return 'neutral' as StateTone;
	});

	const runtimeFacts = $derived.by(() => {
		const facts = [host.schedule_profile_label, `P${host.priority}`];

		if (host.schedule_detail) {
			facts.push(host.schedule_detail);
		}

		facts.push(`${host.max_parallel_encodes} lane${host.max_parallel_encodes === 1 ? '' : 's'}`);

		if (host.active_encode_count > 0) {
			facts.push(`${host.active_encode_count} running`);
		}

		return facts;
	});

	const capabilityFacts = $derived.by(() =>
		host.capabilities.map((capability) => titleCase(capability))
	);

	const attentionCard = $derived.by(() => {
		if (!host.available) {
			return {
				message: host.message,
				detail: host.issues[0] ?? null,
				linkLabel: onSettingsClick ? 'Open host settings' : null
			};
		}

		if (host.active_reason === 'encode queue capability disabled') {
			return {
				message: 'Encode queue capability disabled',
				detail: onSettingsClick ? 'Enable queue support for this worker in Settings.' : null,
				linkLabel: onSettingsClick ? 'Open host settings' : null
			};
		}

		return null;
	});
</script>

<Panel class={`host-card ${hostTone}`.trim()} padding="0.8rem 0.9rem 0.92rem">
	<div class="head-row">
		<div class="title-block">
			<h3>{host.label}</h3>
			<p class="muted-copy host-key">{host.key}</p>
		</div>
		{#if attentionCard}
			<span class="state-chip warn">
				<span class="state-dot"></span>
				Attention
			</span>
		{/if}
	</div>

	<div class="meta-strip">
		{#each runtimeFacts as fact (fact)}
			<span>{fact}</span>
		{/each}
	</div>

	{#if capabilityFacts.length}
		<div class="supporting-strip" aria-label="Host capabilities">
			{#each capabilityFacts as capability (capability)}
				<span>{capability}</span>
			{/each}
		</div>
	{/if}

	{#if attentionCard}
		<div class="issues-box">
			<p>{attentionCard.message}</p>
			{#if attentionCard.detail}
				<p class="muted-copy">{attentionCard.detail}</p>
			{/if}
			{#if onSettingsClick && attentionCard.linkLabel}
				<button type="button" class="attention-link" onclick={onSettingsClick}>
					{attentionCard.linkLabel}
				</button>
			{/if}
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

	:global(.host-card.ok) {
		background:
			linear-gradient(180deg, rgba(244, 251, 246, 0.98), rgba(255, 252, 246, 0.98)),
			var(--surface-1);
	}

	:global(.host-card.warn)::before {
		background: linear-gradient(180deg, rgba(194, 65, 12, 0.9), rgba(194, 65, 12, 0.28));
	}

	:global(.host-card.warn) {
		background:
			linear-gradient(180deg, rgba(255, 247, 242, 0.98), rgba(255, 252, 246, 0.98)),
			var(--surface-1);
	}

	:global(.host-card.hold)::before {
		background: linear-gradient(180deg, rgba(180, 83, 9, 0.85), rgba(180, 83, 9, 0.24));
	}

	:global(.host-card.hold) {
		background:
			linear-gradient(180deg, rgba(255, 249, 239, 0.98), rgba(255, 252, 246, 0.98)),
			var(--surface-1);
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
		font-size: 1.08rem;
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
		padding: 0.28rem 0.58rem;
		border-radius: var(--radius-pill);
		font-size: 0.76rem;
		font-weight: 700;
		line-height: 1;
		white-space: nowrap;
		background: rgba(15, 118, 110, 0.08);
		color: var(--accent-deep);
		border: 1px solid rgba(15, 118, 110, 0.14);
	}

	.state-chip.warn {
		background: rgba(194, 65, 12, 0.1);
		color: var(--warn);
		border-color: rgba(194, 65, 12, 0.18);
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
		gap: 0.38rem;
		font-size: 0.76rem;
		line-height: 1.35;
	}

	.meta-strip span {
		display: inline-flex;
		align-items: center;
		padding: 0.32rem 0.56rem;
		border-radius: 999px;
		background: rgba(255, 255, 255, 0.68);
		border: 1px solid rgba(23, 35, 31, 0.08);
		color: var(--ink-soft);
	}

	.supporting-strip {
		display: flex;
		flex-wrap: wrap;
		gap: 0.42rem;
		font-size: 0.76rem;
		line-height: 1.4;
	}

	.supporting-strip span {
		display: inline-flex;
		align-items: center;
		padding: 0.34rem 0.58rem;
		border-radius: 999px;
		background: rgba(15, 118, 110, 0.08);
		border: 1px solid rgba(15, 118, 110, 0.12);
		color: var(--accent-deep);
	}

	.issues-box {
		display: grid;
		gap: 0.32rem;
		padding: 0.72rem 0.8rem;
		border-radius: var(--radius-md);
		background: rgba(194, 65, 12, 0.06);
		border: 1px solid rgba(194, 65, 12, 0.14);
	}

	.issues-box p:first-child {
		margin: 0;
		font-weight: 700;
		color: var(--warn);
	}

	.issues-box p {
		margin: 0;
	}

	.attention-link {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: fit-content;
		margin-top: 0.15rem;
		padding: 0.42rem 0.68rem;
		border-radius: 999px;
		border: 1px solid rgba(194, 65, 12, 0.16);
		background: rgba(255, 255, 255, 0.72);
		color: var(--warn);
		font-size: 0.76rem;
		font-weight: 700;
		text-decoration: none;
		cursor: pointer;
		transition:
			transform 150ms ease,
			background-color 150ms ease,
			border-color 150ms ease;
	}

	.attention-link:hover {
		transform: translateY(-1px);
		background: rgba(255, 255, 255, 0.9);
	}
</style>
