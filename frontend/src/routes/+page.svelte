<script lang="ts">
	import { onMount } from 'svelte';
	import { fetchJson } from '$lib/api/client';
	import type {
		DashboardFoldersPayload,
		DashboardSummaryPayload,
		HostsPayload
	} from '$lib/api/types';
	import HomeWorkbenchView from '$lib/components/workstation/HomeWorkbenchView.svelte';

	const emptyQueueLane = {
		running: [],
		queued: [],
		pending_review: [],
		running_count: 0,
		queued_count: 0,
		pending_review_count: 0
	};

	const initialDashboard: DashboardSummaryPayload = {
		folders_preview: [],
		library_colors: {},
		scan_job: null,
		calibration_queue: {
			sample: emptyQueueLane,
			full: emptyQueueLane,
			active_count: 0
		},
		encode_queue: {
			running_count: 0,
			queued_count: 0,
			running: [],
			queued: [],
			state: { is_paused: false, stop_requested: false, scheduler_summary: 'loading' }
		},
		catalog_empty: false,
		folder_cache_key: 'loading',
		metric_support: { vmaf: false, xpsnr: false, ssim: false },
		metric_status_copy: 'loading'
	};

	const initialFoldersPayload: DashboardFoldersPayload = {
		folders: [],
		folder_cache_key: 'loading',
		catalog_empty: false
	};

	const initialHosts: HostsPayload = { compact: true, hosts: [] };

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
			const hydratedFolders = await fetchJson<DashboardFoldersPayload>(
				'/api/dashboard/folders?include_series=0'
			);
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
