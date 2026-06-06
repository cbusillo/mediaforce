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
			const [dashboardPayload, hostsPayload] = await Promise.all([
				fetchJson<DashboardSummaryPayload>('/api/dashboard'),
				fetchJson<HostsPayload>('/api/hosts?compact=1')
			]);
			dashboard = dashboardPayload;
			hosts = hostsPayload;
			foldersPayload = {
				folders: dashboardPayload.folders_preview,
				folder_cache_key: dashboardPayload.folder_cache_key,
				catalog_empty: dashboardPayload.catalog_empty
			};
		} catch (error) {
			loadError = error instanceof Error ? error.message : 'Work route failed to load.';
		}
	});
</script>

<svelte:head>
	<title>Mediaforce Work</title>
</svelte:head>

{#if dashboard && foldersPayload && hosts}
	<HomeWorkbenchView {dashboard} {foldersPayload} {hosts} />
{:else}
	<RouteLoadingView route="queue" subject="Work" crumb="/" error={loadError} />
{/if}
