<script lang="ts">
	import { resolve } from '$app/paths';
	import type { HostRuntime } from '$lib/api/types';
	import HostCard from '$lib/components/HostCard.svelte';
	import Panel from '$lib/components/Panel.svelte';
	import SectionHead from '$lib/components/SectionHead.svelte';

	let {
		rankedHosts,
		readyHosts,
		onOpenHostSettings
	}: {
		rankedHosts: HostRuntime[];
		readyHosts: number;
		onOpenHostSettings: (hostKey: string) => Promise<void>;
	} = $props();
</script>

<Panel variant="inset" class="folder-section" padding="1.2rem 1.3rem 1.4rem">
	<div class="section-stack">
		<div class="section-header-row">
			<SectionHead
				eyebrow="Remote Hosts"
				heading="Where encodes can run"
				lede="See which workers are ready now, scheduled for later, or need attention."
				size="compact"
			/>
			<div class="section-header-tools">
				<span class={`section-summary-chip ${readyHosts > 0 ? 'active' : ''}`.trim()}
					>{readyHosts} ready</span
				>
				<a class="section-action-link" href={resolve('/settings')}> Edit in Settings </a>
			</div>
		</div>
		<div id="remote-hosts" class="host-grid">
			{#each rankedHosts as host (host.key)}
				<HostCard {host} onSettingsClick={() => onOpenHostSettings(host.key)} />
			{/each}
		</div>
	</div>
</Panel>

<style>
	.section-stack {
		display: grid;
		gap: var(--space-3);
	}

	.section-header-row {
		display: flex;
		justify-content: space-between;
		gap: 1rem;
		align-items: start;
		flex-wrap: wrap;
	}

	.section-header-tools {
		display: flex;
		gap: 0.8rem;
		align-items: center;
		flex-wrap: wrap;
	}

	.section-summary-chip {
		display: inline-flex;
		align-items: center;
		gap: 0.35rem;
		padding: 0.42rem 0.72rem;
		border-radius: var(--radius-pill);
		font-size: 0.78rem;
		font-weight: 700;
		letter-spacing: 0.03em;
		text-transform: uppercase;
		background: rgba(23, 35, 31, 0.08);
		color: var(--ink-soft);
	}

	.section-summary-chip.active {
		background: rgba(15, 118, 110, 0.14);
		color: #0f5f59;
	}

	.section-action-link {
		font-weight: 700;
		color: var(--accent-deep);
		text-decoration: none;
	}

	.host-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
		gap: var(--space-4);
	}
</style>
