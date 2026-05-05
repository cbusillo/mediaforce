<script lang="ts">
	import SettingsEditor from '$lib/components/settings/SettingsEditor.svelte';

	let { data } = $props();
</script>

<svelte:head>
	<title>Mediaforce Settings</title>
</svelte:head>

<SettingsEditor settings={data.settings} hosts={data.hosts} loadError={data.loadError} />
