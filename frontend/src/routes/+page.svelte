<script lang="ts">
	import HomeWorkbenchView from '$lib/components/workstation/HomeWorkbenchView.svelte';

	let { data } = $props();
</script>

<svelte:head>
	<title>Mediaforce Work</title>
</svelte:head>

<HomeWorkbenchView
	dashboard={data.dashboard}
	foldersPayload={data.foldersPayload}
	hosts={data.hosts}
/>
