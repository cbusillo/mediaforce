<script lang="ts">
	import { onMount } from 'svelte';
	import { fetchJson } from '$lib/api/client';
	import type { DashboardSummaryPayload, HostsPayload } from '$lib/api/types';
	import OpsWorkstationView from '$lib/components/workstation/OpsWorkstationView.svelte';
	import RouteLoadingView from '$lib/components/workstation/RouteLoadingView.svelte';

	let dashboard = $state<DashboardSummaryPayload | null>(null);
	let hosts = $state<HostsPayload | null>(null);
	let loadError = $state('');

	onMount(async () => {
		try {
			const [dashboardPayload, hostsPayload] = await Promise.all([
				fetchJson<DashboardSummaryPayload>('/api/dashboard?preview_limit=0'),
				fetchJson<HostsPayload>('/api/hosts?compact=1')
			]);
			dashboard = dashboardPayload;
			hosts = hostsPayload;
		} catch (error) {
			loadError = error instanceof Error ? error.message : 'Ops route failed to load.';
		}
	});
</script>

<svelte:head>
	<title>Mediaforce Ops</title>
</svelte:head>

{#if dashboard && hosts}
	<OpsWorkstationView {dashboard} {hosts} loadError={null} />
{:else}
	<RouteLoadingView route="ops" subject="Ops" crumb="/ops" error={loadError} />
{/if}
