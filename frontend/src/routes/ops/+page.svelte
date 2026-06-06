<script lang="ts">
	import { onMount } from 'svelte';
	import { fetchJson } from '$lib/api/client';
	import { initialDashboard, initialHosts } from '$lib/api/placeholders';
	import type { DashboardSummaryPayload, HostsPayload } from '$lib/api/types';
	import OpsWorkstationView from '$lib/components/workstation/OpsWorkstationView.svelte';

	let dashboard = $state<DashboardSummaryPayload | null>(initialDashboard);
	let hosts = $state<HostsPayload | null>(initialHosts);
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

<OpsWorkstationView {dashboard} {hosts} loadError={loadError || null} />
