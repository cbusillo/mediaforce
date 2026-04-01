<script lang="ts">
	import type { FolderPayload, FolderStatusPayload, HostsPayload } from '$lib/api/types';
	import FolderStudioView from '$lib/components/folders/FolderStudioView.svelte';

	let {
		data
	}: {
		data: {
			folder: FolderPayload;
			status: FolderStatusPayload;
			hosts: HostsPayload;
		};
	} = $props();
</script>

<FolderStudioView {data} />
