<script lang="ts">
	import { onMount } from 'svelte';

	import { fetchJson } from '$lib/api/client';
	import { initialDashboard, initialHosts } from '$lib/api/placeholders';
	import type { DashboardSummaryPayload, HostsPayload } from '$lib/api/types';
	import OpsWorkstationView from '$lib/components/workstation/OpsWorkstationView.svelte';

	let dashboard = $state<DashboardSummaryPayload | null>(initialDashboard);
	let hosts = $state<HostsPayload | null>(initialHosts);
	let loadError = $state('');
	let refreshGeneration = 0;

	async function refreshOpsData() {
		const generation = ++refreshGeneration;
		try {
			const [dashboardPayload, hostsPayload] = await Promise.all([
				fetchJson<DashboardSummaryPayload>('/api/dashboard?preview_limit=0'),
				fetchJson<HostsPayload>('/api/hosts?compact=1')
			]);
			if (generation !== refreshGeneration) return;
			dashboard = dashboardPayload;
			hosts = hostsPayload;
			loadError = '';
		} catch (error) {
			if (generation !== refreshGeneration) return;
			loadError = error instanceof Error ? error.message : 'Ops route failed to load.';
			throw error instanceof Error ? error : new Error(loadError);
		}
	}

	onMount(() => {
		void refreshOpsData().catch(() => undefined);
		return () => {
			refreshGeneration += 1;
		};
	});
</script>

<svelte:head>
	<title>Activity · Mediaforce</title>
</svelte:head>

<OpsWorkstationView {dashboard} {hosts} loadError={loadError || null} onRefresh={refreshOpsData} />
