<script lang="ts">
	import { onMount } from 'svelte';

	import { fetchJson } from '$lib/api/client';
	import { initialDashboard, initialFoldersPayload } from '$lib/api/placeholders';
	import type { DashboardFoldersPayload, DashboardSummaryPayload } from '$lib/api/types';
	import { mergeFolderPayloads } from '$lib/season/library';
	import SeasonLibrary from './SeasonLibrary.svelte';

	let dashboard = $state<DashboardSummaryPayload>(initialDashboard);
	let structurePayload = $state<DashboardFoldersPayload>(initialFoldersPayload);
	let detailsPayload = $state<DashboardFoldersPayload>(initialFoldersPayload);
	let foldersPayload = $state<DashboardFoldersPayload>(initialFoldersPayload);
	let structureLoadError = $state('');
	let detailsLoadError = $state('');
	let structurePending = $state(true);
	let detailsPending = $state(true);
	let disposed = false;
	const loadError = $derived(
		!foldersPayload.folders.length && !structurePending && !detailsPending
			? structureLoadError || detailsLoadError
			: ''
	);

	onMount(() => {
		disposed = false;
		let refreshTimer: number | undefined;

		void hydrateDashboard().then(() => {
			if (disposed) return;
			refreshTimer = window.setInterval(() => void refreshDashboard(), 7000);
		});
		void hydrateStructure().finally(() => {
			if (!disposed) void hydrateDetails();
		});

		return () => {
			disposed = true;
			if (refreshTimer) window.clearInterval(refreshTimer);
		};
	});

	async function hydrateDashboard() {
		try {
			const dashboardPayload = await fetchJson<DashboardSummaryPayload>(
				'/api/dashboard?preview_limit=0'
			);
			if (disposed) return;
			dashboard = { ...dashboardPayload, folders_preview: foldersPayload.folders };
		} catch {
			// Library structure is independently useful when live queue status is unavailable.
		} finally {
			if (!disposed) {
				syncFolders();
			}
		}
	}

	async function refreshDashboard() {
		try {
			const dashboardPayload = await fetchJson<DashboardSummaryPayload>(
				'/api/dashboard?preview_limit=0'
			);
			if (disposed) return;
			dashboard = { ...dashboardPayload, folders_preview: foldersPayload.folders };
		} catch {
			// Keep the last useful view when a background refresh briefly fails.
		}
	}

	async function hydrateStructure() {
		try {
			const payload = await fetchJson<DashboardFoldersPayload>('/api/dashboard/library');
			if (disposed) return;
			structurePayload = payload;
			structureLoadError = '';
			syncFolders();
		} catch (error) {
			if (disposed) return;
			structureLoadError =
				error instanceof Error ? error.message : 'The season list could not open.';
		} finally {
			if (!disposed) {
				structurePending = false;
				syncFolders();
			}
		}
	}

	async function hydrateDetails() {
		try {
			const payload = await fetchJson<DashboardFoldersPayload>('/api/dashboard/library/details');
			if (disposed) return;
			detailsPayload = payload;
			detailsLoadError = '';
			syncFolders();
		} catch (error) {
			if (disposed) return;
			detailsLoadError =
				error instanceof Error ? error.message : 'Savings and status details could not load.';
		} finally {
			if (!disposed) detailsPending = false;
		}
	}

	function syncFolders() {
		if (structurePending) return;
		foldersPayload = mergeFolderPayloads(structurePayload, detailsPayload);
		dashboard = { ...dashboard, folders_preview: foldersPayload.folders };
	}
</script>

<SeasonLibrary
	{dashboard}
	{foldersPayload}
	foldersPending={structurePending}
	{detailsPending}
	{loadError}
	detailsError={foldersPayload.folders.length ? detailsLoadError : ''}
/>
