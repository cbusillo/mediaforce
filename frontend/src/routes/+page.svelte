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
			const [dashboardPayload, hostsPayload] = await Promise.all([
				fetchJson<DashboardSummaryPayload>('/api/dashboard?preview_limit=0'),
				fetchJson<HostsPayload>('/api/hosts?compact=1')
			]);
			dashboard = dashboardPayload;
			hosts = hostsPayload;
			foldersPayload = {
				folders: [],
				folder_cache_key: dashboardPayload.folder_cache_key,
				catalog_empty: false
			};
			foldersPending = true;
			void hydrateFolders(dashboardPayload);
		} catch (error) {
			loadError = error instanceof Error ? error.message : 'Work route failed to load.';
			foldersPending = false;
		}
	});

	async function hydrateFolders(dashboardPayload: DashboardSummaryPayload) {
		try {
			const hydratedFolders = await fetchJson<DashboardFoldersPayload>('/api/dashboard/folders');
			foldersPayload = hydratedFolders;
			dashboard = { ...dashboardPayload, folders_preview: hydratedFolders.folders };
		} catch (error) {
			loadError = error instanceof Error ? error.message : 'Work folders failed to load.';
		} finally {
			foldersPending = false;
		}
	}
</script>

<svelte:head>
	<title>Mediaforce Work</title>
</svelte:head>

{#if dashboard && foldersPayload && hosts}
	<HomeWorkbenchView {dashboard} {foldersPayload} {hosts} {foldersPending} {loadError} />
{/if}
