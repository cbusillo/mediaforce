<script lang="ts">
	import { fetchJson } from '$lib/api/client';
	import {
		initialDashboard,
		initialFolderPayload,
		initialFoldersPayload,
		initialFolderStatusPayload,
		initialHosts
	} from '$lib/api/placeholders';
	import type {
		DashboardFoldersPayload,
		DashboardSummaryPayload,
		FolderPayload,
		FolderStatusPayload,
		HostsPayload
	} from '$lib/api/types';
	import FolderStudioView from '$lib/components/workstation/FolderStudioView.svelte';
	import HomeWorkbenchView from '$lib/components/workstation/HomeWorkbenchView.svelte';

	let { data }: { data: { mode: 'directory' | 'studio'; prefix: string } } = $props();
	const mode = $derived(data.mode);
	const prefix = $derived(data.prefix);

	let dashboard = $state<DashboardSummaryPayload>(initialDashboard);
	let foldersPayload = $state<DashboardFoldersPayload>(initialFoldersPayload);
	let folder = $state<FolderPayload>(initialFolderPayload(''));
	let status = $state<FolderStatusPayload>(initialFolderStatusPayload(''));
	let hosts = $state<HostsPayload>(initialHosts);
	let foldersPending = $state(false);
	let folderPending = $state(false);
	let loadError = $state<string | null>(null);
	let hydrationGeneration = 0;

	function encodePrefix(prefix: string): string {
		return prefix
			.split('/')
			.map((segment) => encodeURIComponent(segment))
			.join('/');
	}

	async function hydrateDirectory() {
		const generation = ++hydrationGeneration;
		foldersPending = true;
		loadError = null;
		try {
			const [dashboardPayload, foldersPayloadResult, hostsPayload] = await Promise.all([
				fetchJson<DashboardSummaryPayload>('/api/dashboard?preview_limit=0'),
				fetchJson<DashboardFoldersPayload>('/api/dashboard/folders'),
				fetchJson<HostsPayload>('/api/hosts?compact=1')
			]);
			if (generation !== hydrationGeneration) return;
			dashboard = dashboardPayload;
			foldersPayload = foldersPayloadResult;
			hosts = hostsPayload;
		} catch (error) {
			if (generation === hydrationGeneration) {
				loadError = error instanceof Error ? error.message : 'Unable to load folder index.';
			}
		} finally {
			if (generation === hydrationGeneration) foldersPending = false;
		}
	}

	async function hydrateStudio(currentPrefix: string) {
		const generation = ++hydrationGeneration;
		folderPending = true;
		loadError = null;
		const encodedPrefix = encodePrefix(currentPrefix);
		try {
			const [folderPayload, statusPayload, hostsPayload] = await Promise.all([
				fetchJson<FolderPayload>(`/api/folders/${encodedPrefix}`),
				fetchJson<FolderStatusPayload>(`/api/folders/${encodedPrefix}/status`),
				fetchJson<HostsPayload>('/api/hosts?compact=1')
			]);
			if (generation !== hydrationGeneration) return;
			folder = folderPayload;
			status = statusPayload;
			hosts = hostsPayload;
			folderPending = false;
		} catch (error) {
			if (generation === hydrationGeneration) {
				loadError = error instanceof Error ? error.message : 'Unable to load folder state.';
			}
		}
	}

	async function refreshStudio() {
		await hydrateStudio(prefix);
	}

	$effect(() => {
		const currentMode = mode;
		const currentPrefix = prefix;
		loadError = null;

		if (currentMode === 'studio') {
			folder = initialFolderPayload(currentPrefix);
			status = initialFolderStatusPayload(currentPrefix);
			hosts = initialHosts;
			void hydrateStudio(currentPrefix);
		} else {
			dashboard = initialDashboard;
			foldersPayload = initialFoldersPayload;
			hosts = initialHosts;
			void hydrateDirectory();
		}

		return () => {
			hydrationGeneration += 1;
		};
	});
</script>

<svelte:head>
	<title>{mode === 'studio' ? `${prefix} · Mediaforce Folder Studio` : 'Mediaforce Folders'}</title>
</svelte:head>

{#if mode === 'studio'}
	<FolderStudioView
		{folder}
		{status}
		{hosts}
		{folderPending}
		loadError={loadError ?? undefined}
		onMutate={refreshStudio}
	/>
{:else}
	<HomeWorkbenchView
		crumb="/folders"
		{dashboard}
		{foldersPayload}
		{hosts}
		{foldersPending}
		loadError={loadError ?? undefined}
		mode="folders"
	/>
{/if}
