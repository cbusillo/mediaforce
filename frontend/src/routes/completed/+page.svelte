<script lang="ts">
	import { onMount } from 'svelte';
	import { fetchJson } from '$lib/api/client';
	import type { CompletedPayload } from '$lib/api/types';
	import CompletedWorkstationView from '$lib/components/workstation/CompletedWorkstationView.svelte';
	import RouteLoadingView from '$lib/components/workstation/RouteLoadingView.svelte';

	let completed = $state<CompletedPayload | null>(null);
	let loadError = $state('');

	onMount(async () => {
		try {
			completed = await fetchJson<CompletedPayload>('/api/completed');
		} catch (error) {
			loadError = error instanceof Error ? error.message : 'Completed route failed to load.';
		}
	});
</script>

<svelte:head>
	<title>Mediaforce Completed</title>
</svelte:head>

{#if completed}
	<CompletedWorkstationView {completed} loadError={null} />
{:else}
	<RouteLoadingView route="completed" subject="Completed" crumb="/completed" error={loadError} />
{/if}
