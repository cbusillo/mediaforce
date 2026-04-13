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
	import { resolve } from '$app/paths';
	import { onDestroy, onMount } from 'svelte';
	import { fetchJson, postJson } from '$lib/api/client';
	import HomeQueueContext from '$lib/components/home/HomeQueueContext.svelte';
	import HomeQueueTable from '$lib/components/home/HomeQueueTable.svelte';
	import { folderLibraryKey, folderLibraryLabel, folderRoutePrefix } from '$lib/folder-display';
	import { formatGiB } from '$lib/format';
	import { hostsStatusPending } from '$lib/hosts/runtime';
	import { allQueueWorkersScheduledOffWindow, nextQueueWindowCopy } from '$lib/hosts/schedule';
	import { toasts } from '$lib/stores/toasts';

	type FolderSortKey = 'priority' | 'title' | 'library' | 'pending' | 'reclaim' | 'age' | 'review';
	type FolderSortDirection = 'asc' | 'desc';

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
	const sortedVisibleFolderRows = $derived.by(() => {
		const collator = new Intl.Collator('en-US', { sensitivity: 'base', numeric: true });
		const rankedFolders = visibleFolders.map((folder, index) => ({
			folder,
			index,
			priority: index + 1,
			libraryLabel: folderLibraryLabel(folderLibraryKey(folder.prefix)),
			reviewLabel: String(folder.review_badge_label ?? '')
		}));

		rankedFolders.sort((left, right) => {
			const direction = folderSortDirection === 'asc' ? 1 : -1;
			let comparison = 0;

			switch (folderSortKey) {
				case 'priority':
					comparison = left.index - right.index;
					break;
				case 'title':
					comparison = collator.compare(left.folder.title, right.folder.title);
					break;
				case 'library':
					comparison = collator.compare(left.libraryLabel, right.libraryLabel);
					break;
				case 'pending':
					comparison = left.folder.pending_count - right.folder.pending_count;
					break;
				case 'reclaim':
					comparison = left.folder.projected_reclaim_bytes - right.folder.projected_reclaim_bytes;
					break;
				case 'age':
					comparison = left.folder.average_age_days - right.folder.average_age_days;
					break;
				case 'review':
					comparison = collator.compare(left.reviewLabel, right.reviewLabel);
					break;
			}

			if (comparison === 0) {
				comparison = left.index - right.index;
			}

			return comparison * direction;
		});

		return rankedFolders;
	});
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
	const folderLookup = $derived.by(() => {
		const lookup: Record<string, FolderCardData> = {};
		for (const folder of [...dashboard.folders_preview, ...folders]) {
			if (!lookup[folder.prefix]) {
				lookup[folder.prefix] = folder;
			}
		}
		return lookup;
	});
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

	function defaultFolderSortDirection(key: FolderSortKey): FolderSortDirection {
		switch (key) {
			case 'priority':
			case 'title':
			case 'library':
			case 'review':
				return 'asc';
			case 'pending':
			case 'reclaim':
			case 'age':
				return 'desc';
		}
	}

	function toggleFolderSort(key: FolderSortKey) {
		if (folderSortKey === key) {
			folderSortDirection = folderSortDirection === 'asc' ? 'desc' : 'asc';
			return;
		}

		folderSortKey = key;
		folderSortDirection = defaultFolderSortDirection(key);
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

	function formatTopCounts(
		mapping: Record<string, number> | null | undefined,
		limit = 3,
		emptyCopy = 'No signal yet'
	): string {
		if (!mapping) {
			return emptyCopy;
		}
		const entries = Object.entries(mapping)
			.filter(([, value]) => Number(value) > 0)
			.sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]))
			.slice(0, limit);

		if (!entries.length) {
			return emptyCopy;
		}

		return entries.map(([key, value]) => `${value} ${key}`).join(' · ');
	}

	function folderTitleForPrefix(prefix: string): string {
		return folderLookup[prefix]?.title || prefix.split('/').filter(Boolean).at(-1) || prefix;
	}

	function folderSubtitleForPrefix(prefix: string): string {
		return folderLookup[prefix]?.subtitle || prefix;
	}

	function queueJobDetailCopy(
		job: DashboardSummaryPayload['encode_queue']['running'][number]
	): string {
		const progressState = String(job.progress?.progress_state ?? '').trim();
		if (progressState) {
			return progressState;
		}
		const telemetrySummary = String(job.telemetry_summary ?? '').trim();
		if (telemetrySummary) {
			return telemetrySummary;
		}
		const schedulerCopy = String(job.scheduler_status_copy ?? '').trim();
		if (schedulerCopy) {
			return schedulerCopy;
		}
		const attemptSummary = String(job.attempt_summary ?? '').trim();
		if (attemptSummary) {
			return attemptSummary;
		}
		return 'Waiting for the next queue event.';
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
	<section class="system-strip" aria-label="Fleet system state">
		<div
			class={`system-cell queue-cell ${dashboard.encode_queue.state.stop_requested || dashboard.encode_queue.state.is_paused ? 'warning-state' : 'normal-state'}`.trim()}
		>
			<p class="system-label">Queue state</p>
			<p class="system-value">{fleetSnapshotLabel}</p>
			<p class="system-detail">
				{#if dashboard.encode_queue.state.stop_requested}
					Stop was requested across the fleet. Resume here when you are ready to restart queue work.
				{:else if dashboard.encode_queue.state.is_paused}
					Queue is paused. Resume it here or open Ops for full queue controls.
				{:else if queueWorkersScheduledOffWindow}
					{nextWorkerWindow
						? `Waiting for the next worker window at ${nextWorkerWindow}.`
						: 'All queue workers are currently scheduled off-window.'}
				{:else}
					{dashboard.encode_queue.telemetry?.eta_copy
						? `Estimated queue finish in ${dashboard.encode_queue.telemetry.eta_copy}.`
						: 'Queue is ready for the next operator decision.'}
				{/if}
			</p>
			{#if dashboard.encode_queue.state.stop_requested || dashboard.encode_queue.state.is_paused}
				<div class="queue-cell-actions">
					<button
						type="button"
						class="system-action"
						onclick={resumeQueueFromHome}
						disabled={actionState !== null}
					>
						{actionState === 'resume-queue' ? 'Resuming queue...' : 'Resume queue'}
					</button>
					<a class="console-link" href={resolve('/ops')}>Open ops</a>
				</div>
			{/if}
		</div>

		<div class="system-cell">
			<p class="system-label">Workers</p>
			<p class="system-value">{readyHosts} ready / {queueCapableHosts} queue-capable</p>
			<p class="system-detail">
				{queueCapableHosts === 0
					? `${reachableHosts} mounted now. No queue-capable workers configured.`
					: `${reachableHosts} mounted now.`}
			</p>
		</div>

		<div class="system-cell">
			<p class="system-label">Catalog</p>
			<p class="system-value">{catalogScanActive ? catalogScanHeading : 'Catalog standing by'}</p>
			<p class="system-detail">
				{catalogScanActive
					? catalogScanProgressHeadline
					: 'Folder ranking is ready for the next pick.'}
			</p>
		</div>

		<div class="system-cell accent-cell">
			<p class="system-label">Recovery</p>
			<p class="system-value">{formatGiB(totalProjectedReclaim, 1)} visible reclaim</p>
			<p class="system-detail">
				{foldersPending} pending items across {visibleFolders.length} visible folders
				{#if Number(dashboard.archive_cleanup?.file_count ?? 0) > 0}
					. {dashboard.archive_cleanup?.file_count} completed backups are ready for cleanup.
				{:else}
					.
				{/if}
			</p>
		</div>
	</section>

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
				{formatTopCounts}
			/>
		</div>

		<div class="side-rail">
			<HomeQueueContext
				{activeWorkspaceFolder}
				{libraryColors}
				{metricsReady}
				metricStatusCopy={dashboard.metric_status_copy}
				{catalogEmpty}
				{activeWorkspaceQueued}
				queueActionDisabled={actionState !== null}
				queueActionPending={actionState === 'queue-folder'}
				{queueActiveWorkspaceFolder}
				{formatTopCounts}
			/>

			<section class="station-card rail-card control-card" aria-label="Console controls">
				<div class="section-head compact">
					<div>
						<p class="section-label">Console controls</p>
						<h2 class="section-title small">Global actions</h2>
					</div>
				</div>

				<div class="rail-actions">
					<a class="console-link" href={resolve('/ops')}>Open ops</a>
					<a class="console-link" href={resolve('/settings')}>Settings</a>
					<a class="console-link" href={resolve('/completed')}>Completed</a>
				</div>
			</section>

			<section class="station-card rail-card" aria-label="Queue monitor">
				<div class="section-head compact">
					<div>
						<p class="section-label">Queue monitor</p>
						<h2 class="section-title small">Active encodes</h2>
					</div>
				</div>

				<div class="rail-summary">
					<p>{fleetSnapshotLabel}</p>
					<p>{pendingReviewCount} pending review</p>
				</div>

				{#if queueWatchJobs.length > 0}
					<ul class="watch-list">
						{#each queueWatchJobs as job (job.job_id)}
							<li>
								<div class="watch-row">
									<div>
										<p class="watch-title">{folderTitleForPrefix(job.prefix)}</p>
										<p class="watch-path mono-copy">{folderSubtitleForPrefix(job.prefix)}</p>
										<p class="watch-copy">{queueJobDetailCopy(job)}</p>
									</div>
									<span class="watch-state">{job.status}</span>
								</div>
							</li>
						{/each}
					</ul>
				{:else}
					<p class="muted-block">
						No encode jobs are active. The next folder pick will define the queue.
					</p>
				{/if}
			</section>

			<section class="station-card rail-card" aria-label="Catalog status">
				<div class="section-head compact">
					<div>
						<p class="section-label">Catalog status</p>
						<h2 class="section-title small">Scan and cleanup</h2>
					</div>
				</div>

				<p class="catalog-headline">{catalogScanStatusCopy}</p>
				{#if catalogScanProgressFacts.length > 0}
					<ul class="fact-list">
						{#each catalogScanProgressFacts as fact (fact)}
							<li>{fact}</li>
						{/each}
					</ul>
				{/if}

				<div class="rail-summary stack">
					<p>
						Metrics: {metricsReady ? 'VMAF, XPSNR, and SSIM online' : dashboard.metric_status_copy}
					</p>
					<p>
						Archive root: <span class="mono-copy"
							>{dashboard.archive_cleanup?.archive_root || 'not set'}</span
						>
					</p>
				</div>

				{#if recentQueueIssues.length > 0}
					<div class="issue-block">
						<p class="signal-label">Recent blockers</p>
						<ul class="watch-list compact-list">
							{#each recentQueueIssues as job (job.job_id)}
								<li>
									<p class="watch-title">{folderTitleForPrefix(job.prefix)}</p>
									<p class="watch-path mono-copy">{folderSubtitleForPrefix(job.prefix)}</p>
									<p class="watch-copy">{job.error || queueJobDetailCopy(job)}</p>
								</li>
							{/each}
						</ul>
					</div>
				{/if}
			</section>
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
		content: '';
		position: fixed;
		inset: 0;
		z-index: -1;
		pointer-events: none;
		background-image:
			linear-gradient(rgba(148, 163, 184, 0.05) 1px, transparent 1px),
			linear-gradient(90deg, rgba(148, 163, 184, 0.04) 1px, transparent 1px);
		background-size: 28px 28px;
		opacity: 0.32;
	}

	.system-strip,
	.console-grid,
	.section-head,
	.watch-row {
		display: flex;
	}

	.system-strip,
	.console-grid {
		gap: 1rem;
	}

	.main-column {
		display: grid;
		gap: 1rem;
		align-content: start;
		min-width: 0;
	}

	.system-strip {
		display: grid;
		grid-template-columns: repeat(4, minmax(0, 1fr));
	}

	.system-cell,
	.station-card,
	.alert-strip {
		position: relative;
		border: 1px solid rgba(148, 163, 184, 0.18);
		background: rgba(15, 20, 27, 0.94);
		box-shadow: 0 18px 38px rgba(2, 6, 23, 0.2);
		overflow: hidden;
	}

	.system-cell {
		padding: 1rem 1.1rem;
		min-height: 8.5rem;
	}

	.accent-cell {
		background: rgba(13, 33, 42, 0.94);
	}

	.alert-strip::before,
	.queue-cell::before {
		content: '';
		position: absolute;
		inset: 0 0 auto;
		height: 2px;
		background: linear-gradient(90deg, rgba(56, 189, 248, 0.75), rgba(34, 197, 94, 0.2));
	}

	.queue-cell.warning-state {
		border-color: rgba(249, 115, 22, 0.3);
		background: rgba(58, 26, 13, 0.94);
	}

	.queue-cell.warning-state::before {
		background: linear-gradient(90deg, rgba(251, 146, 60, 0.95), rgba(248, 113, 113, 0.3));
	}

	.queue-cell.normal-state::before {
		background: linear-gradient(90deg, rgba(56, 189, 248, 0.85), rgba(34, 197, 94, 0.22));
	}

	.system-label,
	.section-label,
	.alert-label,
	.signal-label {
		margin: 0;
		font-size: 0.72rem;
		font-weight: 800;
		letter-spacing: 0.16em;
		text-transform: uppercase;
		color: rgba(148, 163, 184, 0.88);
	}

	.system-value,
	.section-title {
		margin: 0;
		color: #f8fafc;
	}

	.system-value {
		margin-top: 0.4rem;
		font-size: 1.2rem;
		font-weight: 700;
		line-height: 1.25;
	}

	.system-detail,
	.alert-copy,
	.watch-copy,
	.catalog-headline,
	.rail-summary,
	.rail-summary p {
		margin: 0;
		color: rgba(226, 232, 240, 0.74);
		line-height: 1.5;
	}

	.alert-strip,
	.section-head,
	.side-rail {
		display: grid;
	}

	.alert-strip {
		grid-template-columns: minmax(0, 1fr) auto;
		gap: 1rem;
		align-items: center;
		padding: 1rem 1.1rem;
		background: linear-gradient(180deg, rgba(42, 22, 14, 0.96), rgba(69, 26, 3, 0.92));
	}

	.alert-action,
	.system-action,
	.console-link {
		align-items: center;
		justify-content: center;
		font-weight: 700;
		transition:
			border-color 150ms ease,
			background-color 150ms ease,
			color 150ms ease,
			transform 150ms ease;
	}

	.alert-action,
	.system-action,
	.console-link {
		display: inline-flex;
		padding: 0.72rem 0.95rem;
		border: 1px solid rgba(56, 189, 248, 0.22);
		background: rgba(15, 23, 42, 0.7);
		color: #e2e8f0;
	}

	.console-link:hover,
	.system-action:hover,
	.alert-action:hover {
		transform: translateY(-1px);
		border-color: rgba(56, 189, 248, 0.5);
		background: rgba(30, 41, 59, 0.94);
	}

	.system-action {
		background: rgba(154, 52, 18, 0.84);
		border-color: rgba(251, 146, 60, 0.5);
		color: #fff7ed;
	}

	.system-action:hover {
		background: rgba(194, 65, 12, 0.9);
		border-color: rgba(253, 186, 116, 0.65);
	}

	.system-action:disabled {
		opacity: 0.62;
		cursor: default;
		transform: none;
	}

	.console-grid {
		display: grid;
		grid-template-columns: minmax(0, 1.8fr) minmax(22rem, 0.88fr);
		align-items: start;
	}

	.station-card {
		padding: 1.1rem;
	}

	.section-head {
		grid-template-columns: minmax(0, 1fr) auto;
		gap: 1rem;
		align-items: start;
		margin-bottom: 1rem;
	}

	.section-head.compact {
		margin-bottom: 0.85rem;
	}

	.section-title {
		margin-top: 0.3rem;
		font-size: 1.25rem;
		font-weight: 700;
	}

	.section-title.small {
		font-size: 1rem;
	}

	.queue-cell-actions,
	.rail-actions {
		display: flex;
		gap: 0.65rem;
		flex-wrap: wrap;
	}

	.queue-cell-actions {
		margin-top: 0.85rem;
	}

	.watch-state {
		display: inline-flex;
		align-items: center;
		gap: 0.45rem;
		padding: 0.34rem 0.56rem;
		font-size: 0.72rem;
		font-weight: 800;
		letter-spacing: 0.04em;
		text-transform: uppercase;
	}

	.side-rail {
		gap: 1rem;
	}

	.control-card {
		padding-bottom: 1rem;
	}

	@media (min-width: 961px) {
		.side-rail {
			position: sticky;
			top: 5.9rem;
		}
	}

	.rail-summary {
		display: flex;
		gap: 0.75rem;
		justify-content: space-between;
		padding: 0.75rem 0.85rem;
		border: 1px solid rgba(148, 163, 184, 0.16);
		background: rgba(15, 23, 42, 0.64);
	}

	.rail-summary.stack {
		display: grid;
		gap: 0.45rem;
		justify-content: stretch;
	}

	.watch-list,
	.fact-list {
		margin: 0.9rem 0 0;
		padding: 0;
		list-style: none;
	}

	.watch-list {
		display: grid;
		gap: 0.7rem;
	}

	.watch-row {
		justify-content: space-between;
		gap: 0.75rem;
		padding: 0.75rem 0.85rem;
		border: 1px solid rgba(148, 163, 184, 0.16);
		background: rgba(15, 23, 42, 0.6);
	}

	.watch-title {
		margin: 0;
		color: #f8fafc;
		font-size: 0.8rem;
	}

	.watch-copy {
		margin-top: 0.25rem;
		font-size: 0.88rem;
	}

	.watch-path {
		margin-top: 0.18rem;
		font-size: 0.76rem;
		color: rgba(148, 163, 184, 0.82);
	}

	.watch-state {
		align-self: start;
		border: 1px solid rgba(56, 189, 248, 0.22);
		background: rgba(30, 41, 59, 0.88);
		color: #cbd5e1;
	}

	.fact-list {
		display: grid;
		gap: 0.45rem;
		padding-left: 1rem;
		color: rgba(226, 232, 240, 0.74);
	}

	.issue-block {
		margin-top: 1rem;
	}

	.compact-list {
		margin-top: 0.55rem;
	}

	.empty-shell {
		padding: 0.95rem 1rem;
		border: 1px solid rgba(148, 163, 184, 0.16);
		background: rgba(15, 23, 42, 0.46);
	}

	@media (max-width: 1100px) {
		.system-strip {
			grid-template-columns: 1fr 1fr;
		}

		.section-head {
			grid-template-columns: 1fr;
		}
	}

	@media (max-width: 960px) {
		.console-grid {
			grid-template-columns: 1fr;
		}
	}

	@media (max-width: 720px) {
		.system-strip {
			grid-template-columns: 1fr;
		}

		.alert-strip {
			grid-template-columns: 1fr;
		}
	}
</style>
