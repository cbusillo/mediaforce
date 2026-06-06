<script lang="ts">
	import { onMount } from 'svelte';
	import { fetchJson } from '$lib/api/client';
	import type { CompletedPayload } from '$lib/api/types';
	import CompletedWorkstationView from '$lib/components/workstation/CompletedWorkstationView.svelte';

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

<CompletedWorkstationView {completed} loadError={loadError || null} />
