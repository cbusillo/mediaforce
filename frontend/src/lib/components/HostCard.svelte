<script lang="ts">
	import type { HostRuntime } from '$lib/api/types';
	import Panel from '$lib/components/Panel.svelte';
	import Pill from '$lib/components/Pill.svelte';
	import { titleCase } from '$lib/format';

	let { host }: { host: HostRuntime } = $props();

	const stateVariant = $derived(host.available ? 'ok' : 'warn');
</script>

<Panel class="host-card" padding="0.9rem 1rem 1rem">
	<div class="head-row">
		<div class="title-block">
			<h3>{host.label}</h3>
			<p class="muted-copy host-key">{host.key}</p>
		</div>
		<span class={`state-chip ${stateVariant}`.trim()}>
			<span class="state-dot"></span>
			{host.available ? 'Ready' : 'Attention'}
		</span>
	</div>

	<div class="meta-strip">
		<span>{host.schedule_profile_label}</span>
		<span>P{host.priority}</span>
		<span>{host.max_parallel_encodes}x encodes</span>
		<span>{host.active_encode_count} running</span>
	</div>

	{#if host.message}
		<div class="detail-block">
			<p class="eyebrow-copy">Status</p>
			<p class="muted-copy">{host.message}</p>
		</div>
	{/if}

	{#if host.schedule_detail}
		<div class="detail-block">
			<p class="eyebrow-copy">Schedule</p>
			<p class="muted-copy">{host.schedule_detail}</p>
		</div>
	{/if}

	{#if host.capabilities.length}
		<div class="pill-row">
			{#each host.capabilities as capability (capability)}
				<Pill label={titleCase(capability)} variant="neutral" />
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
		gap: var(--space-2);
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
		gap: 0.55rem;
		color: var(--ink-soft);
		font-size: 0.86rem;
		line-height: 1.35;
	}

	.meta-strip span:not(:last-child)::after {
		content: '·';
		margin-left: 0.55rem;
		color: rgba(23, 35, 31, 0.3);
	}

	.detail-block {
		display: grid;
		gap: 0.2rem;
		padding: 0.75rem 0.85rem;
		border-radius: var(--radius-md);
		background: rgba(255, 255, 255, 0.5);
		border: 1px solid rgba(23, 35, 31, 0.06);
	}

	.pill-row {
		display: flex;
		gap: 0.45rem;
		flex-wrap: wrap;
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
