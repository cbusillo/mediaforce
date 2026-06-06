<script lang="ts">
	import { onMount } from 'svelte';
	import { fetchJson } from '$lib/api/client';
	import type { HostsPayload, SettingsPayload } from '$lib/api/types';
	import SettingsEditor from '$lib/components/settings/SettingsEditor.svelte';

	type SettledPayload<T> = { data: T; error: null } | { data: null; error: string };

	let settings = $state<SettingsPayload | null>(null);
	let hosts = $state<HostsPayload | null>(null);
	let loadError = $state<string | null>(null);

	function errorMessage(error: unknown): string {
		return error instanceof Error ? error.message : 'Request failed.';
	}

	async function settle<T>(promise: Promise<T>): Promise<SettledPayload<T>> {
		try {
			return { data: await promise, error: null };
		} catch (error) {
			return { data: null, error: errorMessage(error) };
		}
	}

	onMount(async () => {
		const [settingsPayload, hostsPayload] = await Promise.all([
			settle(fetchJson<SettingsPayload>('/api/settings?include_archive_cleanup=0')),
			settle(fetchJson<HostsPayload>('/api/hosts?compact=1'))
		]);
		settings = settingsPayload.data;
		hosts = hostsPayload.data;
		loadError = [settingsPayload.error, hostsPayload.error].filter(Boolean).join(' · ') || null;
	});
</script>

<svelte:head>
	<title>Mediaforce Settings</title>
</svelte:head>

<SettingsEditor {settings} {hosts} {loadError} />
