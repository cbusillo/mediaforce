<script lang="ts">
	import CompletedWorkstationView from '$lib/components/workstation/CompletedWorkstationView.svelte';

	let { data } = $props();
</script>

<svelte:head>
	<title>Mediaforce Completed</title>
</svelte:head>

<CompletedWorkstationView completed={data.completed} loadError={data.loadError} />
