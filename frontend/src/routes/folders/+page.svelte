<script lang="ts">
	import { onMount } from 'svelte';
	import { fetchJson } from '$lib/api/client';
	import type {
		DashboardFoldersPayload,
		DashboardSummaryPayload,
		HostsPayload
	} from '$lib/api/types';
	import HomeWorkbenchView from '$lib/components/workstation/HomeWorkbenchView.svelte';
	import RouteLoadingView from '$lib/components/workstation/RouteLoadingView.svelte';

	let dashboard = $state<DashboardSummaryPayload | null>(null);
	let foldersPayload = $state<DashboardFoldersPayload | null>(null);
	let hosts = $state<HostsPayload | null>(null);
	let loadError = $state('');

	onMount(async () => {
		try {
			const [dashboardPayload, foldersPayloadResult, hostsPayload] = await Promise.all([
				fetchJson<DashboardSummaryPayload>('/api/dashboard'),
				fetchJson<DashboardFoldersPayload>('/api/dashboard/folders'),
				fetchJson<HostsPayload>('/api/hosts?compact=1')
			]);
			dashboard = dashboardPayload;
			foldersPayload = foldersPayloadResult;
			hosts = hostsPayload;
		} catch (error) {
			loadError = error instanceof Error ? error.message : 'Folders route failed to load.';
		}
	});
</script>

<svelte:head>
	<title>Mediaforce Folders</title>
</svelte:head>

{#if dashboard && foldersPayload && hosts}
	<HomeWorkbenchView crumb="/folders" {dashboard} {foldersPayload} {hosts} mode="folders" />
{:else}
	<RouteLoadingView route="folders" subject="Folders" crumb="/folders" error={loadError} />
{/if}
