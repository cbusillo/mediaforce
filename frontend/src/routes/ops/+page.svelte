<script lang="ts">
	import OpsWorkstationView from '$lib/components/workstation/OpsWorkstationView.svelte';

	let { data } = $props();
</script>

<svelte:head>
	<title>Mediaforce Ops</title>
</svelte:head>

<OpsWorkstationView dashboard={data.dashboard} hosts={data.hosts} loadError={data.loadError} />
