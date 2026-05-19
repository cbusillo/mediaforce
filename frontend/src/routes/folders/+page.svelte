<script lang="ts">
	import HomeWorkbenchView from '$lib/components/workstation/HomeWorkbenchView.svelte';

	let { data } = $props();
</script>

<svelte:head>
	<title>Mediaforce Folders</title>
</svelte:head>

<HomeWorkbenchView
	crumb="/folders"
	dashboard={data.dashboard}
	foldersPayload={data.foldersPayload}
	hosts={data.hosts}
	mode="folders"
/>
