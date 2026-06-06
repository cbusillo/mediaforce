<script lang="ts">
	import { onMount } from 'svelte';
	import { fetchJson } from '$lib/api/client';
	import { initialDashboard, initialFoldersPayload, initialHosts } from '$lib/api/placeholders';
	import type {
		DashboardFoldersPayload,
		DashboardSummaryPayload,
		HostsPayload
	} from '$lib/api/types';
	import HomeWorkbenchView from '$lib/components/workstation/HomeWorkbenchView.svelte';

	let dashboard = $state<DashboardSummaryPayload>(initialDashboard);
	let foldersPayload = $state<DashboardFoldersPayload>(initialFoldersPayload);
	let hosts = $state<HostsPayload>(initialHosts);
	let loadError = $state('');
	let foldersPending = $state(true);

	onMount(async () => {
		try {
			const [dashboardPayload, foldersPayloadResult, hostsPayload] = await Promise.all([
				fetchJson<DashboardSummaryPayload>('/api/dashboard?preview_limit=0'),
				fetchJson<DashboardFoldersPayload>('/api/dashboard/folders'),
				fetchJson<HostsPayload>('/api/hosts?compact=1')
			]);
			foldersPayload = foldersPayloadResult;
			dashboard = { ...dashboardPayload, folders_preview: foldersPayloadResult.folders };
			hosts = hostsPayload;
		} catch (error) {
			loadError = error instanceof Error ? error.message : 'Folders route failed to load.';
		} finally {
			foldersPending = false;
		}
	});
</script>

<svelte:head>
	<title>Mediaforce Folders</title>
</svelte:head>

<HomeWorkbenchView
	crumb="/folders"
	{dashboard}
	{foldersPayload}
	{hosts}
	{foldersPending}
	{loadError}
	mode="folders"
/>
