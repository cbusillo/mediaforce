<script lang="ts">
	import { browser } from '$app/environment';
	import '$lib/design/workstation-shell.css';
	import type {
		DashboardScanJob,
		DashboardFoldersPayload,
		DashboardSummaryPayload,
		FolderCard as FolderCardData,
		HostsPayload
	} from '$lib/api/types';
	import { onDestroy, onMount } from 'svelte';
	import { fetchJson, postJson } from '$lib/api/client';
	import HomeQueueRail from '$lib/components/home/HomeQueueRail.svelte';
	import HomeSystemStrip from '$lib/components/home/HomeSystemStrip.svelte';
	import HomeQueueTable from '$lib/components/home/HomeQueueTable.svelte';
	import { folderLibraryKey, folderRoutePrefix } from '$lib/folder-display';
	import { formatGiB } from '$lib/format';
	import { hostsStatusPending } from '$lib/hosts/runtime';
	import { allQueueWorkersScheduledOffWindow, nextQueueWindowCopy } from '$lib/hosts/schedule';
	import { toasts } from '$lib/stores/toasts';
	import type { FolderSortDirection, FolderSortKey } from '$lib/components/home/home-queue-display';
	import {
		buildFolderLookup,
		defaultFolderSortDirection,
		deriveFolderLibraries,
		formatCount,
		formatRelativeTime,
		formatTopCounts,
		parseIsoDate,
		rankVisibleFolders
	} from '$lib/components/home/home-queue-display';

	const EMPTY_DASHBOARD: DashboardSummaryPayload = {
		folders_preview: [],
		library_colors: {},
		scan_job: null,
		calibration_queue: {
			sample: {
				running: [],
				queued: [],
				pending_review: [],
				running_count: 0,
				queued_count: 0,
				pending_review_count: 0
			},
			full: {
				running: [],
				queued: [],
				pending_review: [],
				running_count: 0,
				queued_count: 0,
				pending_review_count: 0
			},
			active_count: 0
		},
		encode_queue: {
			running_count: 0,
			queued_count: 0,
			running: [],
			queued: [],
			telemetry: undefined,
			state: { is_paused: false, stop_requested: false }
		},
		archive_cleanup: {
			archive_root: '',
			file_count: 0,
			total_size_bytes: 0,
			has_cleanup: false
		},
		catalog_empty: true,
		folder_cache_key: '',
		metric_support: { vmaf: false, xpsnr: false, ssim: false },
		metric_status_copy: ''
	};
	const EMPTY_HOSTS: HostsPayload = { compact: true, hosts: [] };

	let dashboardOverride = $state<DashboardSummaryPayload | null>(null);
	let hostsPayload = $state<HostsPayload | null>(null);
	let dashboardLoadError = $state<string | null>(null);
	let hostsLoadError = $state<string | null>(null);
	const dashboard = $derived(dashboardOverride ?? EMPTY_DASHBOARD);
	const hosts = $derived(hostsPayload ?? EMPTY_HOSTS);
	let folders = $state<FolderCardData[]>([]);
	let catalogEmpty = $state(false);
	let folderLoadState = $state<'loading' | 'ready' | 'error'>('loading');
	let folderLoadError = $state<string | null>(null);
	let activeFolderRequest = 0;
	let requestedFolderCacheKey = $state<string | null>(null);
	let pendingStoredDisabledLibraries = $state<string[] | null>(null);
	let libraryFilterStorageLoaded = $state(false);
	let folderLoadController: AbortController | null = null;
	let dashboardRefreshController: AbortController | null = null;
	let hostsSummaryController: AbortController | null = null;
	let hostsSummaryRetryTimer: number | null = null;
	let dashboardRefreshError = $state<string | null>(null);
	let activeDashboardRefreshRequest = 0;
	let activeHostsSummaryRequest = 0;
	let clockNow = $state(Date.now());
	const landingLoadIssues = $derived.by(() => {
		const issues: string[] = [];
		if (dashboardLoadError) {
			issues.push(`Dashboard summary failed to load: ${dashboardLoadError}`);
		}
		if (hostsLoadError) {
			issues.push(`Host snapshot failed to load: ${hostsLoadError}`);
		}
		return issues;
	});
	const libraryColors = $derived(dashboard.library_colors ?? {});
	const LIBRARY_FILTER_STORAGE_KEY = 'mediaforce.dashboard.disabledLibraries';
	const readyHosts = $derived.by(() => hosts.hosts.filter((host) => host.queue_active).length);
	const pendingReviewCount = $derived.by(
		() =>
			dashboard.calibration_queue.sample.pending_review_count +
			dashboard.calibration_queue.full.pending_review_count
	);
	const queueWorkersScheduledOffWindow = $derived.by(() =>
		allQueueWorkersScheduledOffWindow(hosts.hosts)
	);
	const nextWorkerWindow = $derived.by(() => nextQueueWindowCopy(hosts.hosts, new Date(clockNow)));
	const fleetSnapshotLabel = $derived.by(() => {
		if (dashboard.encode_queue.running_count > 0) {
			return `${dashboard.encode_queue.running_count} encodes running`;
		}
		if (dashboard.encode_queue.queued_count > 0) {
			return `${dashboard.encode_queue.queued_count} folder${dashboard.encode_queue.queued_count === 1 ? '' : 's'} queued`;
		}
		return 'Fleet idle';
	});
	const metricsReady = $derived(
		dashboard.metric_support.vmaf && dashboard.metric_support.xpsnr && dashboard.metric_support.ssim
	);
	const catalogScanJob = $derived(dashboard.scan_job as DashboardScanJob | null);
	const catalogScanStatus = $derived(String(catalogScanJob?.status ?? 'idle'));
	const catalogScanActive = $derived(
		catalogScanStatus === 'queued' || catalogScanStatus === 'running'
	);
	const catalogScanStats = $derived(
		catalogScanJob?.stats ?? { items_seen: 0, updated_paths: 0, unchanged: 0 }
	);
	const catalogScanLastProgressAt = $derived(
		catalogScanJob?.last_progress_at ?? catalogScanJob?.started_at ?? null
	);
	const catalogScanSecondsSinceProgress = $derived.by(() => {
		const progressAt = parseIsoDate(catalogScanLastProgressAt);
		if (!progressAt) {
			return null;
		}
		return Math.max(0, Math.round((clockNow - progressAt.getTime()) / 1000));
	});
	const catalogScanLikelyStalled = $derived.by(
		() => catalogScanStatus === 'running' && (catalogScanSecondsSinceProgress ?? 0) >= 90
	);
	const catalogScanHeading = $derived(
		catalogScanStatus === 'running' ? 'Refreshing libraries now' : 'Library refresh queued'
	);
	const catalogScanProgressHeadline = $derived.by(() => {
		if (catalogScanStatus === 'queued') {
			return 'The next library refresh is queued and will begin shortly.';
		}
		if (catalogScanStats.items_seen > 0) {
			return `Scanned ${formatCount(catalogScanStats.items_seen)} items so far.`;
		}
		return 'Scan worker is active and cataloging the library now.';
	});
	const catalogScanStatusCopy = $derived.by(() => {
		if (dashboardRefreshError) {
			return `Dashboard updates paused: ${dashboardRefreshError}`;
		}
		if (catalogScanLikelyStalled) {
			return 'Still marked running, but no new progress has been recorded recently. This can happen on a slow mount or if the scan is hung.';
		}
		if (catalogScanActive) {
			return 'New or changed library roots are being rescanned before this recommendation list updates.';
		}
		return 'No library scan is active. Ranked folders reflect the latest completed catalog pass.';
	});
	const catalogScanProgressFacts = $derived.by(() => {
		const facts: string[] = [];
		if (catalogScanStats.updated_paths > 0) {
			facts.push(`${formatCount(catalogScanStats.updated_paths)} new or changed`);
		}
		if (catalogScanStats.unchanged > 0) {
			facts.push(`${formatCount(catalogScanStats.unchanged)} unchanged`);
		}
		if (catalogScanLastProgressAt) {
			facts.push(`Last progress ${formatRelativeTime(catalogScanLastProgressAt, clockNow)}`);
		}
		return facts;
	});

	const folderLibraries = $derived.by(() => deriveFolderLibraries(folders));

	let disabledLibraries = $state<string[]>([]);
	let libraryFiltersHydrated = $state(false);
	let folderSortKey = $state<FolderSortKey>('priority');
	let folderSortDirection = $state<FolderSortDirection>('asc');
	const fullFolderLibrariesReady = $derived(catalogEmpty || folderLoadState === 'ready');

	const visibleFolders = $derived.by(() =>
		folders.filter((folder) => !disabledLibraries.includes(folderLibraryKey(folder.prefix)))
	);
	const totalProjectedReclaim = $derived.by(() =>
		visibleFolders.reduce((total, folder) => total + folder.projected_reclaim_bytes, 0)
	);
	const foldersPending = $derived.by(() =>
		visibleFolders.reduce((total, folder) => total + folder.pending_count, 0)
	);
	const libraryFiltersActive = $derived(disabledLibraries.length > 0);
	const availableLibraryKeys = $derived.by(() => folderLibraries.map((library) => library.key));
	const filterHintCopy = $derived(
		libraryFiltersActive
			? 'Show a hidden library again, or use Show all to reset the queue view.'
			: 'Click a library to hide it from the queue.'
	);
	const queueCapableHosts = $derived.by(
		() => hosts.hosts.filter((host) => host.capabilities.includes('encode_queue')).length
	);
	const reachableHosts = $derived.by(() => hosts.hosts.filter((host) => host.available).length);
	const queueStateTone = $derived.by(() => {
		if (dashboard.encode_queue.state.stop_requested || dashboard.encode_queue.state.is_paused) {
			return 'warning-state';
		}
		if (dashboard.encode_queue.queued_count > 0 && readyHosts === 0) {
			return 'schedule-state';
		}
		if (queueWorkersScheduledOffWindow && dashboard.encode_queue.queued_count > 0) {
			return 'schedule-state';
		}
		return 'normal-state';
	});
	const workerStateTone = $derived.by(() => {
		if (queueCapableHosts === 0) {
			return 'warning-state';
		}
		if (readyHosts === 0 && dashboard.encode_queue.queued_count > 0) {
			return 'schedule-state';
		}
		return 'normal-state';
	});
	const recoveryDetailCopy = $derived.by(() => {
		const cleanupCount = Number(dashboard.archive_cleanup?.file_count ?? 0);
		const cleanupCopy =
			cleanupCount > 0
				? `${cleanupCount} completed backups are ready for cleanup.`
				: 'No completed backups are waiting for cleanup.';
		return `${foldersPending} pending items across ${visibleFolders.length} visible folders. ${cleanupCopy}`;
	});
	const sortedVisibleFolderRows = $derived.by(() =>
		rankVisibleFolders(visibleFolders, folderSortKey, folderSortDirection)
	);
	const sortedVisibleFolders = $derived.by(() =>
		sortedVisibleFolderRows.map(({ folder }) => folder)
	);
	const activeWorkspaceFolder = $derived(sortedVisibleFolderRows[0]?.folder ?? null);
	const queueWatchJobs = $derived.by(() =>
		[...dashboard.encode_queue.running, ...dashboard.encode_queue.queued].slice(0, 5)
	);
	const queuedFolderPrefixes = $derived.by(
		() =>
			new Set(
				[...dashboard.encode_queue.running, ...dashboard.encode_queue.queued].map(
					(job) => job.prefix
				)
			)
	);
	const recentQueueIssues = $derived.by(() =>
		[...(dashboard.encode_queue.recent ?? [])]
			.filter((job) => job.status !== 'completed')
			.slice(0, 3)
	);
	const folderLookup = $derived.by(() => buildFolderLookup(dashboard.folders_preview, folders));
	const activeWorkspaceQueued = $derived.by(() =>
		activeWorkspaceFolder ? queuedFolderPrefixes.has(activeWorkspaceFolder.prefix) : false
	);
	let actionState = $state<null | 'resume-queue' | 'queue-folder'>(null);

	function toggleLibraryFilter(libraryKey: string) {
		disabledLibraries = disabledLibraries.includes(libraryKey)
			? disabledLibraries.filter((value) => value !== libraryKey)
			: [...disabledLibraries, libraryKey];
	}

	function enableAllLibraries() {
		disabledLibraries = [];
	}

	function toggleFolderSort(key: FolderSortKey) {
		if (folderSortKey === key) {
			folderSortDirection = folderSortDirection === 'asc' ? 'desc' : 'asc';
			return;
		}

		folderSortKey = key;
		folderSortDirection = defaultFolderSortDirection(key);
	}

	async function refreshHomeData() {
		await Promise.all([loadDashboardSummary(), loadHostsSummary()]);
	}

	async function resumeQueueFromHome() {
		actionState = 'resume-queue';
		try {
			const response = await postJson<{ message?: string }>('/api/encode-queue/resume', {});
			toasts.success('Queue resumed', response.message ?? 'Resumed the encode queue.');
			await refreshHomeData();
		} catch (error) {
			toasts.error(
				'Could not resume queue',
				error instanceof Error ? error.message : 'Unexpected queue resume error'
			);
		} finally {
			actionState = null;
		}
	}

	async function queueActiveWorkspaceFolder() {
		if (
			!activeWorkspaceFolder ||
			activeWorkspaceQueued ||
			activeWorkspaceFolder.pending_count <= 0
		) {
			return;
		}

		actionState = 'queue-folder';
		try {
			const encodedPrefix = folderRoutePrefix(activeWorkspaceFolder.prefix);
			const response = await postJson<{ message?: string; action?: string }>(
				`/api/folders/${encodedPrefix}/queue-encode`,
				{ notes: '', bypass_schedule: false }
			);
			toasts.success(
				response.action === 'recovered' ? 'Failed files recovered' : 'Folder queued',
				response.message ?? 'Queued the current folder.'
			);
			await refreshHomeData();
		} catch (error) {
			toasts.error(
				'Could not queue folder',
				error instanceof Error ? error.message : 'Unexpected queue error'
			);
		} finally {
			actionState = null;
		}
	}

	async function loadDashboardSummary() {
		dashboardRefreshController?.abort();
		dashboardRefreshController = new AbortController();
		const requestId = ++activeDashboardRefreshRequest;
		try {
			const nextDashboard = await fetchJson<DashboardSummaryPayload>(`/api/dashboard`, fetch, {
				signal: dashboardRefreshController.signal
			});
			if (requestId !== activeDashboardRefreshRequest) {
				return;
			}
			dashboardOverride = nextDashboard;
			dashboardRefreshError = null;
			dashboardLoadError = null;
			clockNow = Date.now();
			dashboardRefreshController = null;
		} catch (error) {
			if (requestId !== activeDashboardRefreshRequest) {
				return;
			}
			if (error instanceof DOMException && error.name === 'AbortError') {
				return;
			}
			dashboardRefreshError =
				error instanceof Error ? error.message : 'Unexpected dashboard refresh error';
			dashboardLoadError = dashboardRefreshError;
			dashboardRefreshController = null;
		} finally {
			if (requestId === activeDashboardRefreshRequest) {
				dashboardRefreshController = null;
			}
		}
	}

	async function loadHostsSummary() {
		hostsSummaryController?.abort();
		hostsSummaryController = new AbortController();
		const requestId = ++activeHostsSummaryRequest;
		try {
			const nextHostsPayload = await fetchJson<HostsPayload>('/api/hosts?compact=1', fetch, {
				signal: hostsSummaryController.signal
			});
			if (requestId !== activeHostsSummaryRequest) {
				return;
			}
			hostsPayload = nextHostsPayload;
			hostsLoadError = null;
			if (browser && hostsStatusPending(nextHostsPayload)) {
				hostsSummaryRetryTimer ??= window.setTimeout(() => {
					hostsSummaryRetryTimer = null;
					void loadHostsSummary();
				}, 1000);
			} else if (hostsSummaryRetryTimer !== null) {
				clearTimeout(hostsSummaryRetryTimer);
				hostsSummaryRetryTimer = null;
			}
		} catch (error) {
			if (requestId !== activeHostsSummaryRequest) {
				return;
			}
			if (error instanceof DOMException && error.name === 'AbortError') {
				return;
			}
			hostsLoadError = error instanceof Error ? error.message : 'Unexpected host loading error';
		} finally {
			if (requestId === activeHostsSummaryRequest) {
				hostsSummaryController = null;
			}
		}
	}

	async function loadFolders(cacheKey: string) {
		folderLoadController?.abort();
		folderLoadController = new AbortController();
		const requestId = ++activeFolderRequest;
		folderLoadState = 'loading';
		folderLoadError = null;

		try {
			const response = await fetchJson<DashboardFoldersPayload>(
				`/api/dashboard/folders?cache=${encodeURIComponent(cacheKey)}`,
				fetch,
				{ signal: folderLoadController.signal }
			);
			if (requestId !== activeFolderRequest) {
				return;
			}
			folders = response.folders;
			catalogEmpty = response.catalog_empty;
			folderLoadState = 'ready';
			folderLoadController = null;
		} catch (error) {
			if (requestId !== activeFolderRequest) {
				return;
			}
			if (error instanceof DOMException && error.name === 'AbortError') {
				return;
			}
			folderLoadState = 'error';
			folderLoadError = error instanceof Error ? error.message : 'Unexpected folder loading error';
			folderLoadController = null;
		}
	}

	function retryLandingLoads() {
		void loadDashboardSummary();
		void loadHostsSummary();
		if (dashboard.folder_cache_key) {
			requestedFolderCacheKey = null;
			void loadFolders(dashboard.folder_cache_key);
		}
	}

	onMount(() => {
		if (!browser) {
			return;
		}

		void loadDashboardSummary();
		void loadHostsSummary();

		try {
			const storedValue = window.localStorage.getItem(LIBRARY_FILTER_STORAGE_KEY);
			if (!storedValue) {
				pendingStoredDisabledLibraries = [];
				libraryFilterStorageLoaded = true;
				return;
			}

			const parsed = JSON.parse(storedValue);
			if (!Array.isArray(parsed)) {
				pendingStoredDisabledLibraries = [];
				libraryFilterStorageLoaded = true;
				return;
			}

			pendingStoredDisabledLibraries = parsed
				.filter((value): value is string => typeof value === 'string')
				.filter((value, index, values) => values.indexOf(value) === index);
		} catch {
			pendingStoredDisabledLibraries = [];
		} finally {
			libraryFilterStorageLoaded = true;
		}
	});

	onDestroy(() => {
		folderLoadController?.abort();
		dashboardRefreshController?.abort();
		hostsSummaryController?.abort();
		if (hostsSummaryRetryTimer !== null) {
			clearTimeout(hostsSummaryRetryTimer);
			hostsSummaryRetryTimer = null;
		}
	});

	$effect(() => {
		if (!browser || !catalogScanActive) {
			return;
		}

		clockNow = Date.now();
		void loadDashboardSummary();
		const refreshHandle = window.setInterval(() => {
			void loadDashboardSummary();
		}, 4000);
		const clockHandle = window.setInterval(() => {
			clockNow = Date.now();
		}, 1000);

		return () => {
			window.clearInterval(refreshHandle);
			window.clearInterval(clockHandle);
		};
	});

	$effect(() => {
		if (
			!browser ||
			!libraryFilterStorageLoaded ||
			libraryFiltersHydrated ||
			(!folders.length && !catalogEmpty)
		) {
			return;
		}

		disabledLibraries = [...(pendingStoredDisabledLibraries ?? [])];
		libraryFiltersHydrated = true;
	});

	$effect(() => {
		if (!browser || !libraryFiltersHydrated) {
			return;
		}

		const nextDisabledLibraries = fullFolderLibrariesReady
			? disabledLibraries.filter((value) => new Set(availableLibraryKeys).has(value))
			: disabledLibraries;

		if (nextDisabledLibraries.length !== disabledLibraries.length) {
			disabledLibraries = nextDisabledLibraries;
			return;
		}

		if (!nextDisabledLibraries.length) {
			window.localStorage.removeItem(LIBRARY_FILTER_STORAGE_KEY);
			return;
		}

		window.localStorage.setItem(LIBRARY_FILTER_STORAGE_KEY, JSON.stringify(nextDisabledLibraries));
	});

	$effect(() => {
		if (!browser) {
			return;
		}

		const nextCacheKey = dashboard.folder_cache_key;
		if (!nextCacheKey || nextCacheKey === requestedFolderCacheKey) {
			return;
		}

		requestedFolderCacheKey = nextCacheKey;
		folders = dashboard.folders_preview;
		catalogEmpty = dashboard.catalog_empty;
		folderLoadState = dashboard.catalog_empty ? 'ready' : 'loading';
		void loadFolders(nextCacheKey);
	});
</script>

<svelte:head>
	<title>Folders · Mediaforce</title>
</svelte:head>

<div class="workstation-screen">
	<HomeSystemStrip
		{queueStateTone}
		{workerStateTone}
		{fleetSnapshotLabel}
		stopRequested={dashboard.encode_queue.state.stop_requested}
		queuePaused={dashboard.encode_queue.state.is_paused}
		{queueWorkersScheduledOffWindow}
		nextWorkerWindow={nextWorkerWindow ?? null}
		etaCopy={dashboard.encode_queue.telemetry?.eta_copy}
		{actionState}
		{resumeQueueFromHome}
		{readyHosts}
		{queueCapableHosts}
		{reachableHosts}
		{catalogScanActive}
		{catalogScanHeading}
		{catalogScanProgressHeadline}
		totalProjectedReclaimCopy={formatGiB(totalProjectedReclaim, 1)}
		{recoveryDetailCopy}
	/>

	{#if landingLoadIssues.length > 0}
		<section class="alert-strip" aria-label="Runtime issues">
			<div>
				<p class="alert-label">Runtime issue</p>
				{#each landingLoadIssues as issue (issue)}
					<p class="alert-copy">{issue}</p>
				{/each}
			</div>
			<button type="button" class="alert-action" onclick={retryLandingLoads}>Retry load</button>
		</section>
	{/if}

	<div class="console-grid">
		<div class="main-column">
			<HomeQueueTable
				{folderLibraries}
				{libraryColors}
				{disabledLibraries}
				{libraryFiltersActive}
				{filterHintCopy}
				{folderLoadState}
				{folderLoadError}
				showInitialLoadingMessage={folderLoadState === 'loading' &&
					folders.length === 0 &&
					!catalogEmpty}
				{catalogEmpty}
				{sortedVisibleFolders}
				{sortedVisibleFolderRows}
				activeWorkspacePrefix={activeWorkspaceFolder?.prefix ?? null}
				{folderSortKey}
				{folderSortDirection}
				{enableAllLibraries}
				{toggleLibraryFilter}
				{toggleFolderSort}
				{activeWorkspaceFolder}
				{activeWorkspaceQueued}
				queueActionDisabled={actionState !== null}
				queueActionPending={actionState === 'queue-folder'}
				{queueActiveWorkspaceFolder}
				{formatTopCounts}
			/>
		</div>

		<div class="side-column">
			<div class="side-column-sticky">
				<HomeQueueRail
					{folderLookup}
					{queueWatchJobs}
					{recentQueueIssues}
					{fleetSnapshotLabel}
					{pendingReviewCount}
					{catalogScanStatusCopy}
					{catalogScanProgressFacts}
					{metricsReady}
					metricStatusCopy={dashboard.metric_status_copy}
					archiveRoot={dashboard.archive_cleanup?.archive_root || ''}
				/>
			</div>
		</div>
	</div>
</div>

<style>
	.workstation-screen {
		display: grid;
		gap: 1rem;
		padding: 0.25rem 0 1rem;
		position: relative;
		isolation: isolate;
		z-index: 0;
	}

	.workstation-screen::before {
		content: '';
		position: fixed;
		inset: 0;
		z-index: -2;
		pointer-events: none;
		background: #0b1014;
	}

	.workstation-screen::after {
		display: none;
	}

	.main-column {
		display: grid;
		gap: 1rem;
		align-content: start;
		min-width: 0;
		overflow: hidden;
	}

	.side-column {
		display: grid;
		gap: 1rem;
		align-content: start;
		min-width: 0;
	}

	.side-column-sticky {
		display: flex;
		flex-direction: column;
		gap: 1rem;
		align-content: start;
		min-width: 0;
	}

	@media (min-width: 961px) {
		.side-column-sticky {
			position: sticky;
			top: 5.9rem;
			max-height: calc(100dvh - 5.9rem - 1rem);
			overflow-y: auto;
			overflow-x: clip;
			scrollbar-gutter: stable;
			padding-right: 0.15rem;
			z-index: 5;
		}
	}

	.console-grid > :global(*),
	.main-column > :global(*) {
		min-width: 0;
	}

	.alert-strip {
		position: relative;
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		gap: 1rem;
		align-items: center;
		padding: 1rem 1.1rem;
		border: 1px solid rgba(148, 163, 184, 0.18);
		background: rgba(55, 24, 11, 0.96);
		box-shadow: 0 18px 38px rgba(2, 6, 23, 0.2);
		overflow: hidden;
	}

	.alert-strip::before {
		content: '';
		position: absolute;
		inset: 0 0 auto;
		height: 2px;
		background: rgba(56, 189, 248, 0.85);
	}

	.alert-label {
		margin: 0;
		font-size: 0.72rem;
		font-weight: 800;
		letter-spacing: 0.16em;
		text-transform: uppercase;
		color: rgba(148, 163, 184, 0.88);
	}

	.alert-copy {
		margin: 0;
		color: rgba(226, 232, 240, 0.74);
		line-height: 1.5;
	}

	.alert-action {
		align-items: center;
		justify-content: center;
		font-weight: 700;
		transition:
			border-color 150ms ease,
			background-color 150ms ease,
			color 150ms ease,
			transform 150ms ease;
	}

	.alert-action {
		display: inline-flex;
		padding: 0.72rem 0.95rem;
		border: 1px solid rgba(56, 189, 248, 0.22);
		background: rgba(15, 23, 42, 0.7);
		color: #e2e8f0;
	}

	.alert-action:hover {
		transform: translateY(-1px);
		border-color: rgba(56, 189, 248, 0.5);
		background: rgba(30, 41, 59, 0.94);
	}

	.console-grid {
		display: grid;
		gap: 1rem;
		grid-template-columns: minmax(0, 1.95fr) minmax(18.5rem, 0.68fr);
		align-items: start;
	}

	@media (max-width: 1180px) {
		.console-grid {
			grid-template-columns: 1fr;
		}

		.side-column-sticky {
			position: static;
			max-height: none;
			overflow: visible;
			padding-right: 0;
		}
	}

	@media (max-width: 720px) {
		.alert-strip {
			grid-template-columns: 1fr;
		}
	}
</style>
