<script lang="ts">
	import { browser } from '$app/environment';
	import type {
		DashboardScanJob,
		DashboardFoldersPayload,
		DashboardSummaryPayload,
		FolderCard as FolderCardData,
		HostsPayload
	} from '$lib/api/types';
	import { goto, invalidateAll } from '$app/navigation';
	import { resolve } from '$app/paths';
	import { onDestroy, onMount } from 'svelte';
	import { fetchJson, postJson } from '$lib/api/client';
	import { folderLibraryKey, folderLibraryLabel } from '$lib/folder-display';
	import { formatGiB, hostSettingsAnchor } from '$lib/format';
	import DashboardFolderGrid from '$lib/components/dashboard/DashboardFolderGrid.svelte';
	import DashboardHero from '$lib/components/dashboard/DashboardHero.svelte';
	import DashboardHostGrid from '$lib/components/dashboard/DashboardHostGrid.svelte';
	import DashboardQueues from '$lib/components/dashboard/DashboardQueues.svelte';
	import { toasts } from '$lib/stores/toasts';

	let {
		data
	}: {
		data: {
			dashboard: DashboardSummaryPayload;
			hosts: HostsPayload;
		};
	} = $props();

	let dashboardOverride = $state<DashboardSummaryPayload | null>(null);
	const dashboard = $derived(dashboardOverride ?? data.dashboard);
	const hosts = $derived(data.hosts);
	let folders = $state<FolderCardData[]>([]);
	let catalogEmpty = $state(false);
	let folderLoadState = $state<'loading' | 'ready' | 'error'>('loading');
	let folderLoadError = $state<string | null>(null);
	let activeFolderRequest = 0;
	let requestedFolderCacheKey = $state<string | null>(null);
	let pendingStoredDisabledLibraries = $state<string[] | null>(null);
	let folderLoadController: AbortController | null = null;
	let dashboardRefreshController: AbortController | null = null;
	let dashboardRefreshError = $state<string | null>(null);
	let clockNow = $state(Date.now());
	const libraryColors = $derived(dashboard.library_colors ?? {});
	const LIBRARY_FILTER_STORAGE_KEY = 'mediaforce.dashboard.disabledLibraries';
	const totalEstimatedSavings = $derived.by(() =>
		folders.reduce((total, folder) => total + folder.estimated_savings_bytes, 0)
	);
	const foldersPending = $derived.by(() =>
		folders.reduce((total, folder) => total + folder.pending_count, 0)
	);
	const encodeCapableHosts = $derived.by(
		() => hosts.hosts.filter((host) => host.capabilities.includes('encode_queue')).length
	);
	const readyHosts = $derived.by(() => hosts.hosts.filter((host) => host.queue_active).length);
	const rankedHosts = $derived.by(() =>
		[...hosts.hosts].sort(
			(left, right) =>
				Number(right.priority) - Number(left.priority) || left.label.localeCompare(right.label)
		)
	);
	const reachableHosts = $derived.by(() => hosts.hosts.filter((host) => host.available).length);
	const pendingReviewCount = $derived.by(
		() =>
			dashboard.calibration_queue.sample.pending_review_count +
			dashboard.calibration_queue.full.pending_review_count
	);
	const calibrationQueueHasWork = $derived.by(
		() =>
			dashboard.calibration_queue.sample.running_count +
				dashboard.calibration_queue.sample.queued_count +
				dashboard.calibration_queue.full.running_count +
				dashboard.calibration_queue.full.queued_count >
			0
	);
	const encodeRunningJobs = $derived(dashboard.encode_queue.running ?? []);
	const encodeQueueEtaCopy = $derived(dashboard.encode_queue.telemetry?.eta_copy ?? null);
	const encodeQueueStatus = $derived.by(() => {
		if (dashboard.encode_queue.state.stop_requested) {
			return { label: 'Stopping', tone: 'attention' as const };
		}

		if (dashboard.encode_queue.state.is_paused) {
			return { label: 'Paused', tone: 'neutral' as const };
		}

		if (dashboard.encode_queue.running_count > 0) {
			return { label: 'Running', tone: 'ok' as const };
		}

		if (dashboard.encode_queue.queued_count > 0) {
			return { label: 'Queued', tone: 'attention' as const };
		}

		return { label: 'Idle', tone: 'neutral' as const };
	});
	const heroFacts = $derived.by(() => [
		{ label: 'Top folders', value: String(folders.length) },
		{ label: 'Pending items', value: String(foldersPending) },
		{ label: 'Potential reclaim', value: formatGiB(totalEstimatedSavings, 1) }
	]);
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
		return 'New or changed library roots are being rescanned before this recommendation list updates.';
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

	const folderLibraries = $derived.by(() => {
		const counts: Record<string, { key: string; label: string; count: number }> = {};

		for (const folder of folders) {
			const key = folderLibraryKey(folder.prefix);
			const existing = counts[key];
			if (existing) {
				existing.count += 1;
				continue;
			}
			counts[key] = { key, label: folderLibraryLabel(key), count: 1 };
		}

		return Object.values(counts).sort((left, right) => left.label.localeCompare(right.label));
	});

	let disabledLibraries = $state<string[]>([]);
	let libraryFiltersHydrated = $state(false);

	const visibleFolders = $derived.by(() =>
		folders.filter((folder) => !disabledLibraries.includes(folderLibraryKey(folder.prefix)))
	);
	const libraryFiltersActive = $derived(disabledLibraries.length > 0);
	const availableLibraryKeys = $derived.by(() => folderLibraries.map((library) => library.key));
	const filterHintCopy = $derived(
		libraryFiltersActive
			? 'Click an inactive pill to restore it, or All to reset.'
			: 'Click a pill to hide that library.'
	);
	const folderFilterSummary = $derived.by(() => {
		if (!libraryFiltersActive) {
			return `Showing all ${folders.length} candidate folders.`;
		}

		if (!visibleFolders.length) {
			return 'No folders match the selected libraries.';
		}

		return `Showing ${visibleFolders.length} of ${folders.length} candidate folders.`;
	});

	const queueCards = $derived.by(() => [
		{
			eyebrow: 'Calibration',
			heading: `${dashboard.calibration_queue.sample.running_count + dashboard.calibration_queue.full.running_count} running · ${dashboard.calibration_queue.sample.queued_count + dashboard.calibration_queue.full.queued_count} queued`,
			lede: 'Sample calibrations and proof encodes stay separate so you can tune before committing to a full folder run.'
		},
		{
			eyebrow: 'Encode Queue',
			heading: `${dashboard.encode_queue.running_count} running · ${dashboard.encode_queue.queued_count} queued`,
			lede: 'Queue health, worker availability, and controls for whole-folder runs.'
		}
	]);

	let queueAction = $state<string | null>(null);
	let calibrationQueueAction = $state<string | null>(null);

	async function runQueueAction(action: 'pause' | 'resume' | 'stop') {
		if (
			action === 'stop' &&
			browser &&
			!window.confirm('Stop active encodes and clear the queue? This cannot be undone.')
		) {
			return;
		}

		queueAction = action;
		try {
			const response = await postJson<{ message: string }>(`/api/encode-queue/${action}`, {});
			toasts.success('Queue updated', response.message);
			await invalidateAll();
		} catch (error) {
			toasts.error(
				'Queue update failed',
				error instanceof Error ? error.message : 'Unexpected queue error'
			);
		} finally {
			queueAction = null;
		}
	}

	async function stopCalibrationQueue() {
		if (
			browser &&
			!window.confirm(
				'Stop active calibrations and clear queued calibration jobs? This cannot be undone.'
			)
		) {
			return;
		}

		calibrationQueueAction = 'stop';
		try {
			const response = await postJson<{ message: string }>(`/api/calibration-queue/stop`, {});
			toasts.success('Calibration queue updated', response.message);
			await invalidateAll();
		} catch (error) {
			toasts.error(
				'Calibration queue update failed',
				error instanceof Error ? error.message : 'Unexpected calibration queue error'
			);
		} finally {
			calibrationQueueAction = null;
		}
	}

	async function openHostSettings(hostKey: string) {
		await goto(resolve('/settings'));
		window.location.hash = hostSettingsAnchor(hostKey);
	}

	function toggleLibraryFilter(libraryKey: string) {
		disabledLibraries = disabledLibraries.includes(libraryKey)
			? disabledLibraries.filter((value) => value !== libraryKey)
			: [...disabledLibraries, libraryKey];
	}

	function enableAllLibraries() {
		disabledLibraries = [];
	}

	function parseIsoDate(value: string | null | undefined): Date | null {
		if (!value) {
			return null;
		}
		const parsed = new Date(value);
		return Number.isNaN(parsed.getTime()) ? null : parsed;
	}

	function formatCount(value: number): string {
		return new Intl.NumberFormat('en-US').format(value);
	}

	function formatRelativeTime(value: string | null | undefined, now: number): string {
		const parsed = parseIsoDate(value);
		if (!parsed) {
			return 'just now';
		}
		const seconds = Math.max(0, Math.round((now - parsed.getTime()) / 1000));
		if (seconds < 10) {
			return 'moments ago';
		}
		if (seconds < 60) {
			return `${seconds}s ago`;
		}
		const minutes = Math.round(seconds / 60);
		if (minutes < 60) {
			return `${minutes}m ago`;
		}
		const hours = Math.round(minutes / 60);
		return `${hours}h ago`;
	}

	async function loadDashboardSummary() {
		dashboardRefreshController?.abort();
		dashboardRefreshController = new AbortController();
		try {
			dashboardOverride = await fetchJson<DashboardSummaryPayload>(`/api/dashboard`, fetch, {
				signal: dashboardRefreshController.signal
			});
			dashboardRefreshError = null;
			clockNow = Date.now();
			if (dashboardRefreshController.signal.aborted) {
				return;
			}
			dashboardRefreshController = null;
		} catch (error) {
			if (error instanceof DOMException && error.name === 'AbortError') {
				return;
			}
			dashboardRefreshError =
				error instanceof Error ? error.message : 'Unexpected dashboard refresh error';
			dashboardRefreshController = null;
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

	onMount(() => {
		if (!browser) {
			return;
		}

		try {
			const storedValue = window.localStorage.getItem(LIBRARY_FILTER_STORAGE_KEY);
			if (!storedValue) {
				pendingStoredDisabledLibraries = [];
				return;
			}

			const parsed = JSON.parse(storedValue);
			if (!Array.isArray(parsed)) {
				pendingStoredDisabledLibraries = [];
				return;
			}

			pendingStoredDisabledLibraries = parsed
				.filter((value): value is string => typeof value === 'string')
				.filter((value, index, values) => values.indexOf(value) === index);
		} catch {
			pendingStoredDisabledLibraries = [];
		}
	});

	onDestroy(() => {
		folderLoadController?.abort();
		dashboardRefreshController?.abort();
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
		if (dashboardOverride !== null) {
			return;
		}

		folders = data.dashboard.folders_preview;
		catalogEmpty = data.dashboard.catalog_empty;
		folderLoadState = data.dashboard.catalog_empty ? 'ready' : 'loading';
	});

	$effect(() => {
		if (!browser || libraryFiltersHydrated || (!folders.length && !catalogEmpty)) {
			return;
		}

		const knownLibraries = new Set(availableLibraryKeys);
		disabledLibraries = (pendingStoredDisabledLibraries ?? []).filter((value) =>
			knownLibraries.has(value)
		);
		libraryFiltersHydrated = true;
	});

	$effect(() => {
		if (!browser || !libraryFiltersHydrated || (!folders.length && !catalogEmpty)) {
			return;
		}

		const knownLibraries = new Set(availableLibraryKeys);
		const normalizedDisabledLibraries = disabledLibraries.filter((value) =>
			knownLibraries.has(value)
		);

		if (normalizedDisabledLibraries.length !== disabledLibraries.length) {
			disabledLibraries = normalizedDisabledLibraries;
			return;
		}

		if (!normalizedDisabledLibraries.length) {
			window.localStorage.removeItem(LIBRARY_FILTER_STORAGE_KEY);
			return;
		}

		window.localStorage.setItem(
			LIBRARY_FILTER_STORAGE_KEY,
			JSON.stringify(normalizedDisabledLibraries)
		);
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
	<title>Dashboard · Mediaforce</title>
</svelte:head>

<div class="page-stack">
	<DashboardHero
		foldersCount={folders.length}
		{totalEstimatedSavings}
		{heroFacts}
		{metricsReady}
		metricSupport={dashboard.metric_support}
		metricStatusCopy={dashboard.metric_status_copy}
	/>

	<DashboardQueues
		{queueCards}
		{pendingReviewCount}
		{calibrationQueueAction}
		{calibrationQueueHasWork}
		{stopCalibrationQueue}
		{encodeQueueStatus}
		{queueAction}
		{dashboard}
		{readyHosts}
		{encodeCapableHosts}
		{reachableHosts}
		{runQueueAction}
		{encodeQueueEtaCopy}
		{encodeRunningJobs}
	/>

	<DashboardHostGrid {rankedHosts} {readyHosts} onOpenHostSettings={openHostSettings} />

	<DashboardFolderGrid
		{catalogScanActive}
		{catalogScanLikelyStalled}
		{catalogScanHeading}
		{catalogScanProgressHeadline}
		{catalogScanStatusCopy}
		{catalogScanProgressFacts}
		{folderLoadState}
		{folders}
		{folderLoadError}
		{folderLibraries}
		{disabledLibraries}
		onEnableAllLibraries={enableAllLibraries}
		onToggleLibraryFilter={toggleLibraryFilter}
		{libraryColors}
		{folderFilterSummary}
		{filterHintCopy}
		{visibleFolders}
		{catalogEmpty}
	/>
</div>

<style>
	.page-stack {
		display: grid;
		gap: var(--space-3);
	}
</style>
