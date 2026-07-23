<script lang="ts">
	import { onMount } from 'svelte';
	import { fetchJson } from '$lib/api/client';
	import type { HostsPayload, SettingsPayload } from '$lib/api/types';
	import SettingsEditor from '$lib/components/settings/SettingsEditor.svelte';
	import { hostsStatusPending } from '$lib/hosts/runtime';

	type SettledPayload<T> = { data: T; error: null } | { data: null; error: string };

	let settings = $state<SettingsPayload | null>(null);
	let hosts = $state<HostsPayload | null>(null);
	let loadError = $state<string | null>(null);
	const HOST_STATUS_POLL_INTERVAL_MS = 1_500;
	const HOST_STATUS_POLL_LIMIT = 40;

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

	onMount(() => {
		let disposed = false;
		let hostPollTimer: number | undefined;

		async function refreshHosts(pollCount: number) {
			const result = await settle(fetchJson<HostsPayload>('/api/hosts?compact=1'));
			if (disposed) return;
			if (result.data) hosts = result.data;
			if (result.error && !hosts) loadError = result.error;
			if (result.data && hostsStatusPending(result.data) && pollCount < HOST_STATUS_POLL_LIMIT) {
				hostPollTimer = window.setTimeout(
					() => void refreshHosts(pollCount + 1),
					HOST_STATUS_POLL_INTERVAL_MS
				);
			}
		}

		async function loadSettings() {
			const [settingsPayload, hostsPayload] = await Promise.all([
				settle(fetchJson<SettingsPayload>('/api/settings?include_archive_cleanup=0')),
				settle(fetchJson<HostsPayload>('/api/hosts?compact=1'))
			]);
			if (disposed) return;
			settings = settingsPayload.data;
			hosts = hostsPayload.data;
			loadError = [settingsPayload.error, hostsPayload.error].filter(Boolean).join(' · ') || null;
			if (hostsPayload.data && hostsStatusPending(hostsPayload.data)) {
				hostPollTimer = window.setTimeout(() => void refreshHosts(1), HOST_STATUS_POLL_INTERVAL_MS);
			}
		}

		void loadSettings();
		return () => {
			disposed = true;
			if (hostPollTimer !== undefined) window.clearTimeout(hostPollTimer);
		};
	});
</script>

<svelte:head>
	<title>Settings · Mediaforce</title>
</svelte:head>

<SettingsEditor {settings} {hosts} {loadError} />
