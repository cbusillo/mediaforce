<script lang="ts">
	import { onMount } from 'svelte';
	import { fetchJson } from '$lib/api/client';
	import type { ArchiveCleanupSummary, CompletedPayload } from '$lib/api/types';
	import CompletedWorkstationView from '$lib/components/workstation/CompletedWorkstationView.svelte';

	let completed = $state<CompletedPayload | null>(null);
	let loadError = $state('');
	let archiveRefreshGeneration = 0;

	async function fetchArchiveCleanup(): Promise<ArchiveCleanupSummary> {
		return fetchJson<ArchiveCleanupSummary>('/api/archive-cleanup');
	}

	function updateCompleted(nextCompleted: CompletedPayload) {
		completed = nextCompleted;
		archiveRefreshGeneration += 1;
	}

	async function refreshArchiveCleanup() {
		const generation = ++archiveRefreshGeneration;
		try {
			const archiveCleanup = await fetchArchiveCleanup();
			if (generation !== archiveRefreshGeneration || !completed) return;
			completed = { ...completed, archive_cleanup: archiveCleanup };
		} catch {
			// Keep the cheap completed payload visible if the deeper archive scan fails.
		}
	}

	async function hydrateCompleted() {
		try {
			updateCompleted(await fetchJson<CompletedPayload>('/api/completed'));
			void refreshArchiveCleanup();
		} catch (error) {
			loadError = error instanceof Error ? error.message : 'Completed route failed to load.';
		}
	}

	onMount(() => {
		void hydrateCompleted();
		return () => {
			archiveRefreshGeneration += 1;
		};
	});
</script>

<svelte:head>
	<title>Finished media · Mediaforce</title>
</svelte:head>

<CompletedWorkstationView
	{completed}
	loadError={loadError || null}
	onArchiveRefresh={fetchArchiveCleanup}
	onCompletedUpdate={updateCompleted}
/>
