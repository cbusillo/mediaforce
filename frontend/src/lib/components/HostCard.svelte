<script lang="ts">
	import type { HostRuntime } from '$lib/api/types';
	import Panel from '$lib/components/Panel.svelte';
	import Pill from '$lib/components/Pill.svelte';
	import { titleCase } from '$lib/format';

	let { host }: { host: HostRuntime } = $props();

	const stateVariant = $derived(host.available ? 'ok' : 'warn');
</script>

<Panel class="host-card" padding="1.1rem 1.15rem 1.2rem">
	<div class="head-row">
		<div>
			<h3>{host.label}</h3>
			<p class="muted-copy host-key">{host.key}</p>
		</div>
		<Pill label={host.available ? 'Ready' : 'Attention'} variant={stateVariant} />
	</div>

	<div class="pill-row">
		<Pill label={host.schedule_profile_label} variant="neutral" />
		<Pill label={`P${host.priority} · ${host.max_parallel_encodes}x`} variant="ghost" />
		<Pill label={`${host.active_encode_count} running`} variant="ghost" />
	</div>

	{#if host.message}
		<p class="muted-copy">{host.message}</p>
	{/if}

	{#if host.schedule_detail}
		<p class="muted-copy">{host.schedule_detail}</p>
	{/if}

	{#if host.capabilities.length}
		<div class="pill-row">
			{#each host.capabilities as capability (capability)}
				<Pill label={titleCase(capability)} variant="neutral" />
			{/each}
		</div>
	{/if}

	{#if host.issues.length}
		<ul class="muted-copy issues">
			{#each host.issues as issue (issue)}
				<li>{issue}</li>
			{/each}
		</ul>
	{/if}
</Panel>

<style>
	:global(.host-card) {
		display: grid;
		gap: var(--space-3);
	}

	.head-row {
		display: flex;
		justify-content: space-between;
		gap: var(--space-3);
		align-items: start;
		flex-wrap: wrap;
	}

	h3 {
		font-size: 1.3rem;
		font-weight: 700;
	}

	.host-key {
		font-size: 0.84rem;
	}

	.pill-row {
		display: flex;
		gap: var(--space-2);
		flex-wrap: wrap;
	}

	.issues {
		margin: 0;
	}
</style>
