<script lang="ts">
	import { fetchJson } from '$lib/api/client';
	import {
		initialDashboard,
		initialFolderPayload,
		initialFoldersPayload,
		initialFolderStatusPayload
	} from '$lib/api/placeholders';
	import type {
		DashboardFoldersPayload,
		DashboardSummaryPayload,
		FolderPayload,
		FolderStatusPayload
	} from '$lib/api/types';
	import SeasonExperience from '$lib/components/season/SeasonExperience.svelte';
	import SeasonLibrary from '$lib/components/season/SeasonLibrary.svelte';

	let { data }: { data: { mode: 'directory' | 'studio'; prefix: string } } = $props();
	const mode = $derived(data.mode);
	const prefix = $derived(data.prefix);

	let dashboard = $state<DashboardSummaryPayload>(initialDashboard);
	let foldersPayload = $state<DashboardFoldersPayload>(initialFoldersPayload);
	let folder = $state<FolderPayload>(initialFolderPayload(''));
	let status = $state<FolderStatusPayload>(initialFolderStatusPayload(''));
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
			const [dashboardPayload, foldersPayloadResult] = await Promise.all([
				fetchJson<DashboardSummaryPayload>('/api/dashboard?preview_limit=0'),
				fetchJson<DashboardFoldersPayload>('/api/dashboard/folders')
			]);
			if (generation !== hydrationGeneration) return;
			dashboard = dashboardPayload;
			foldersPayload = foldersPayloadResult;
		} catch (error) {
			if (generation === hydrationGeneration) {
				loadError = error instanceof Error ? error.message : 'Unable to open the season library.';
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
			const [folderPayload, statusPayload] = await Promise.all([
				fetchJson<FolderPayload>(`/api/folders/${encodedPrefix}`),
				fetchJson<FolderStatusPayload>(`/api/folders/${encodedPrefix}/status`)
			]);
			if (generation !== hydrationGeneration) return;
			folder = folderPayload;
			status = statusPayload;
		} catch (error) {
			if (generation === hydrationGeneration) {
				loadError = error instanceof Error ? error.message : 'Unable to open this season.';
			}
		} finally {
			if (generation === hydrationGeneration) folderPending = false;
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
			void hydrateStudio(currentPrefix);
			const refreshTimer = window.setInterval(() => {
				if (!folderPending) void hydrateStudio(currentPrefix);
			}, 7000);
			return () => {
				window.clearInterval(refreshTimer);
				hydrationGeneration += 1;
			};
		} else {
			dashboard = initialDashboard;
			foldersPayload = initialFoldersPayload;
			void hydrateDirectory();
		}

		return () => {
			hydrationGeneration += 1;
		};
	});
</script>

<svelte:head>
	<title
		>{mode === 'studio'
			? `${prefix.split('/').at(-1)} · Mediaforce`
			: 'Make a TV season smaller · Mediaforce'}</title
	>
</svelte:head>

{#if mode === 'studio'}
	<SeasonExperience
		{folder}
		{status}
		{folderPending}
		loadError={loadError ?? undefined}
		onMutate={refreshStudio}
	/>
{:else}
	<SeasonLibrary {dashboard} {foldersPayload} {foldersPending} loadError={loadError ?? undefined} />
{/if}
