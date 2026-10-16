<script lang="ts">
	import { onMount } from 'svelte';

	import { fetchJson } from '$lib/api/client';
	import { initialFoldersPayload } from '$lib/api/placeholders';
	import type {
		DashboardFoldersPayload,
		DashboardSummaryPayload,
		LifecycleState
	} from '$lib/api/types';
	import {
		applySeriesLifecycle,
		libraryJobsPollTracker,
		mergeFolderPayloads
	} from '$lib/season/library';
	import SeasonLibrary from './SeasonLibrary.svelte';

	let structurePayload = $state<DashboardFoldersPayload>(initialFoldersPayload);
	let detailsPayload = $state<DashboardFoldersPayload>(initialFoldersPayload);
	let foldersPayload = $state<DashboardFoldersPayload>(initialFoldersPayload);
	let structureLoadError = $state('');
	let detailsLoadError = $state('');
	let structurePending = $state(true);
	let detailsPending = $state(true);
	let disposed = false;
	let detailsRefreshing = false;
	const jobsPoll = libraryJobsPollTracker();
	const loadError = $derived(
		!foldersPayload.folders.length && !structurePending && !detailsPending
			? structureLoadError || detailsLoadError
			: ''
	);

	onMount(() => {
		disposed = false;
		let refreshTimer: number | undefined;
		void readJobs().then((dashboard) => {
			if (dashboard) jobsPoll.reset(dashboard);
		});
		void hydrateStructure()
			.finally(() => (disposed ? undefined : hydrateDetails()))
			.finally(() => {
				if (!disposed) refreshTimer = window.setInterval(() => void refreshOnJobChange(), 7000);
			});

		return () => {
			disposed = true;
			if (refreshTimer) window.clearInterval(refreshTimer);
		};
	});

	async function readJobs(): Promise<DashboardSummaryPayload | null> {
		try {
			return await fetchJson<DashboardSummaryPayload>('/api/dashboard?preview_limit=0');
		} catch {
			// The cards stay as last read when the job summary is briefly unavailable.
			return null;
		}
	}

	/** Each card shows its server state, so read the cards again only when a job changed. */
	async function refreshOnJobChange() {
		const dashboard = await readJobs();
		if (disposed || !dashboard || !jobsPoll.changed(dashboard) || detailsRefreshing) return;
		detailsRefreshing = true;
		try {
			await hydrateDetails();
		} finally {
			detailsRefreshing = false;
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

	function applySavedLifecycle(lifecycle: LifecycleState) {
		structurePayload = applySeriesLifecycle(structurePayload, lifecycle);
		detailsPayload = applySeriesLifecycle(detailsPayload, lifecycle);
		syncFolders();
	}

	function syncFolders() {
		if (structurePending) return;
		foldersPayload = mergeFolderPayloads(structurePayload, detailsPayload);
	}
</script>

<SeasonLibrary
	{foldersPayload}
	foldersPending={structurePending}
	{detailsPending}
	{loadError}
	detailsError={foldersPayload.folders.length ? detailsLoadError : ''}
	onLifecycleSaved={applySavedLifecycle}
/>
