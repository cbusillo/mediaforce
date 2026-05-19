<script lang="ts">
	import FolderStudioView from '$lib/components/workstation/FolderStudioView.svelte';
	import HomeWorkbenchView from '$lib/components/workstation/HomeWorkbenchView.svelte';

	let { data } = $props();
</script>

<svelte:head>
	<title
		>{data.mode === 'studio'
			? `${data.folder.prefix} · Mediaforce Folder Studio`
			: 'Mediaforce Folders'}</title
	>
</svelte:head>

{#if data.mode === 'studio'}
	<FolderStudioView folder={data.folder} status={data.status} hosts={data.hosts} />
{:else}
	<HomeWorkbenchView
		crumb="/folders"
		dashboard={data.dashboard}
		foldersPayload={data.foldersPayload}
		hosts={data.hosts}
		mode="folders"
	/>
{/if}
