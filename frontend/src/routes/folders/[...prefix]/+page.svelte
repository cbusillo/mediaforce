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
	import SeasonExperience from '$lib/components/season/SeasonExperience.svelte';
	import SeasonLibrary from '$lib/components/season/SeasonLibrary.svelte';
	import MovieStudioView from '$lib/components/workstation/MovieStudioView.svelte';
	import OtherStudioView from '$lib/components/workstation/OtherStudioView.svelte';

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
	let statusPending = $state(false);
	let folderHydrated = $state(false);
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
		if (currentPrefix !== prefix) return;
		const generation = ++hydrationGeneration;
		folderPending = true;
		loadError = null;
		const encodedPrefix = encodePrefix(currentPrefix);
		try {
			const [folderPayload, statusPayload, hostsPayload] = await Promise.all([
				fetchJson<FolderPayload>(`/api/folders/${encodedPrefix}`),
				fetchJson<FolderStatusPayload>(`/api/folders/${encodedPrefix}/status`),
				fetchJson<HostsPayload>('/api/hosts?compact=1').catch(() => initialHosts)
			]);
			if (generation !== hydrationGeneration || currentPrefix !== prefix) return;
			folder = folderPayload;
			status = statusPayload;
			hosts = hostsPayload;
		} catch (error) {
			if (generation === hydrationGeneration) {
				loadError = error instanceof Error ? error.message : 'Unable to open this media scope.';
			}
		} finally {
			if (generation === hydrationGeneration) {
				folderPending = false;
				folderHydrated = true;
			}
		}
	}

	async function refreshStudio() {
		await hydrateStudio(prefix);
	}

	async function refreshStudioStatus(currentPrefix: string) {
		if (currentPrefix !== prefix || folderPending || statusPending || !status.polling_active)
			return;
		const generation = hydrationGeneration;
		statusPending = true;
		try {
			const encodedPrefix = encodePrefix(currentPrefix);
			const nextStatus = await fetchJson<FolderStatusPayload>(
				`/api/folders/${encodedPrefix}/status`
			);
			if (generation !== hydrationGeneration || currentPrefix !== prefix) return;
			const finishedWork = status.polling_active && !nextStatus.polling_active;
			status = nextStatus;
			if (finishedWork) await hydrateStudio(currentPrefix);
		} catch {
			// Keep the last useful progress state when a background poll briefly fails.
		} finally {
			statusPending = false;
		}
	}

	$effect(() => {
		const currentMode = mode;
		const currentPrefix = prefix;
		loadError = null;

		if (currentMode === 'studio') {
			folderHydrated = false;
			folder = initialFolderPayload(currentPrefix);
			status = initialFolderStatusPayload(currentPrefix);
			hosts = initialHosts;
			void hydrateStudio(currentPrefix);
			const refreshTimer = window.setInterval(() => {
				void refreshStudioStatus(currentPrefix);
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
			? `${prefix.split('/').at(-1)} · ${folder.pending ? 'Studio' : folder.media_scope.domain === 'movie' ? 'Movie Studio' : folder.media_scope.domain === 'other' ? 'Other Studio' : 'Mediaforce'}`
			: 'Make a TV season smaller · Mediaforce'}</title
	>
</svelte:head>

{#if mode === 'studio'}
	{#if !folderHydrated && (folder.pending || folderPending)}
		<div class="studio-loading" role="status">
			<span aria-hidden="true"></span>
			<strong>Opening Studio</strong>
			<p>Reading scope, membership, workflow, and worker readiness.</p>
		</div>
	{:else if folder.pending}
		<div class="studio-loading" role="alert">
			<strong>Studio could not open</strong>
			<p>{loadError ?? 'The media scope is unavailable.'}</p>
		</div>
	{:else if folder.media_scope.domain === 'movie'}
		<MovieStudioView
			{folder}
			{status}
			{hosts}
			{folderPending}
			loadError={loadError ?? undefined}
			onMutate={refreshStudio}
		/>
	{:else if folder.media_scope.domain === 'other'}
		<OtherStudioView
			{folder}
			{status}
			{hosts}
			{folderPending}
			loadError={loadError ?? undefined}
			onMutate={refreshStudio}
		/>
	{:else}
		<SeasonExperience
			{folder}
			{status}
			{folderPending}
			loadError={loadError ?? undefined}
			onMutate={refreshStudio}
		/>
	{/if}
{:else}
	<SeasonLibrary {dashboard} {foldersPayload} {foldersPending} loadError={loadError ?? undefined} />
{/if}

<style>
	.studio-loading {
		align-items: center;
		display: grid;
		gap: 8px;
		justify-items: center;
		min-height: calc(100vh - 120px);
		padding: 36px;
		text-align: center;
	}

	.studio-loading span {
		animation: studio-pulse 900ms ease-in-out infinite;
		background: var(--mf-active-fg);
		height: 9px;
		width: 9px;
	}

	.studio-loading p {
		color: var(--mf-fg-secondary);
		font-size: var(--mf-text-sm);
		margin: 0;
	}

	@keyframes studio-pulse {
		50% {
			opacity: 0.25;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.studio-loading span {
			animation: none;
		}
	}
</style>
