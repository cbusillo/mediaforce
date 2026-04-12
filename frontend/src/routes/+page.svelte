<script lang="ts">
	import { browser } from '$app/environment';
	import type {
		DashboardScanJob,
		DashboardFoldersPayload,
		DashboardSummaryPayload,
		FolderCard as FolderCardData,
		HostsPayload
	} from '$lib/api/types';
	import { resolve } from '$app/paths';
	import { onDestroy, onMount } from 'svelte';
	import { fetchJson } from '$lib/api/client';
	import { folderLibraryKey, folderLibraryLabel } from '$lib/folder-display';
	import { formatGiB } from '$lib/format';
	import { hostsStatusPending } from '$lib/hosts/runtime';
	import { allQueueWorkersScheduledOffWindow, nextQueueWindowCopy } from '$lib/hosts/schedule';

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
	let folderSortKey = $state<FolderSortKey>('priority');
	let folderSortDirection = $state<FolderSortDirection>('asc');

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
			? 'Click an inactive pill to restore it, or All to reset.'
			: 'Click a pill to hide that library.'
	);
	const queueCapableHosts = $derived.by(
		() => hosts.hosts.filter((host) => host.capabilities.includes('encode_queue')).length
	);
	const reachableHosts = $derived.by(() => hosts.hosts.filter((host) => host.available).length);
	const sortedVisibleFolders = $derived.by(() => {
		const collator = new Intl.Collator('en-US', { sensitivity: 'base', numeric: true });
		const rankedFolders = visibleFolders.map((folder, index) => ({
			folder,
			index,
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

		return rankedFolders.map(({ folder }) => folder);
	});
	const activeWorkspaceFolder = $derived(sortedVisibleFolders[0] ?? null);
	const queueWatchJobs = $derived.by(() =>
		[...dashboard.encode_queue.running, ...dashboard.encode_queue.queued].slice(0, 5)
	);
	const recentQueueIssues = $derived.by(() =>
		[...(dashboard.encode_queue.recent ?? [])]
			.filter((job) => job.status !== 'completed')
			.slice(0, 3)
	);

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

	function folderSortAria(key: FolderSortKey): 'ascending' | 'descending' | 'none' {
		if (folderSortKey !== key) {
			return 'none';
		}
		return folderSortDirection === 'asc' ? 'ascending' : 'descending';
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
					Stop requested across the fleet.
				{:else if dashboard.encode_queue.state.is_paused}
					Queue is paused until you resume from Ops.
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
		</div>

		<div class="system-cell">
			<p class="system-label">Workers</p>
			<p class="system-value">{readyHosts} ready / {queueCapableHosts} queue-capable</p>
			<p class="system-detail">
				{reachableHosts} mounted now
				{#if queueCapableHosts === 0}
					. No queue-capable workers configured.
				{:else}
					.
				{/if}
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
					. {dashboard.archive_cleanup?.file_count} archived backups waiting in completed.
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
		<section class="station-card active-workspace" aria-label="Active workspace">
			<div class="section-head">
				<div>
					<p class="section-label">Active workspace</p>
					<h2 class="section-title">Home queue console</h2>
				</div>
				<div class="action-row">
					<a class="console-link" href={resolve('/ops')}>Open ops</a>
					<a class="console-link" href={resolve('/settings')}>Worker settings</a>
				</div>
			</div>

			{#if activeWorkspaceFolder}
				<div class="workspace-header">
					<div>
						<div class="workspace-badges">
							<span
								class="workspace-badge library"
								style={`--library-chip: ${libraryColors[folderLibraryKey(activeWorkspaceFolder.prefix)] ?? '#4b5563'};`}
							>
								{folderLibraryLabel(folderLibraryKey(activeWorkspaceFolder.prefix))}
							</span>
							<span class="workspace-badge neutral">{activeWorkspaceFolder.scope_label}</span>
							{#if activeWorkspaceFolder.review_badge_label}
								<span
									class={`workspace-badge review ${activeWorkspaceFolder.review_badge_tone ?? 'neutral'}`.trim()}
								>
									{activeWorkspaceFolder.review_badge_label}
								</span>
							{/if}
						</div>
						<h3 class="workspace-title">{activeWorkspaceFolder.title}</h3>
						<p class="workspace-subtitle">
							{activeWorkspaceFolder.subtitle || activeWorkspaceFolder.prefix}
						</p>
						<p class="workspace-summary">
							{activeWorkspaceFolder.pending_count} pending of {activeWorkspaceFolder.item_count} items.
							Projected reclaim {formatGiB(activeWorkspaceFolder.projected_reclaim_bytes, 1)}.
						</p>
					</div>

					<div class="workspace-actions">
						<a class="workspace-primary" href={resolve(`/folders/${activeWorkspaceFolder.prefix}`)}>
							Open folder studio
						</a>
						<a class="workspace-secondary" href={resolve('/completed')}>Open completed</a>
					</div>
				</div>

				<div class="fact-grid" aria-label="Active workspace facts">
					<div class="fact-cell highlight">
						<p class="fact-label">Projected reclaim</p>
						<p class="fact-value">{formatGiB(activeWorkspaceFolder.projected_reclaim_bytes, 1)}</p>
					</div>
					<div class="fact-cell">
						<p class="fact-label">Pending</p>
						<p class="fact-value">{activeWorkspaceFolder.pending_count}</p>
					</div>
					<div class="fact-cell">
						<p class="fact-label">Current size</p>
						<p class="fact-value">{formatGiB(activeWorkspaceFolder.total_size_bytes, 1)}</p>
					</div>
					<div class="fact-cell">
						<p class="fact-label">Average age</p>
						<p class="fact-value">{Math.round(activeWorkspaceFolder.average_age_days)} days</p>
					</div>
				</div>

				<div class="signal-grid">
					<div class="signal-panel">
						<p class="signal-label">Status mix</p>
						<p class="signal-value">{formatTopCounts(activeWorkspaceFolder.statuses, 4)}</p>
					</div>
					<div class="signal-panel">
						<p class="signal-label">Codec mix</p>
						<p class="signal-value">{formatTopCounts(activeWorkspaceFolder.video_codecs, 3)}</p>
					</div>
					<div class="signal-panel">
						<p class="signal-label">Metric readiness</p>
						<p class="signal-value">
							{metricsReady ? 'Full metric stack online' : dashboard.metric_status_copy}
						</p>
					</div>
				</div>
			{:else if catalogEmpty}
				<div class="empty-shell">
					<h3 class="workspace-title">No folders ranked yet</h3>
					<p class="workspace-summary">
						The catalog is still empty. Start a library refresh or check Settings before queueing
						work.
					</p>
				</div>
			{:else}
				<div class="empty-shell">
					<h3 class="workspace-title">Loading the workspace queue</h3>
					<p class="workspace-summary">
						Mediaforce is pulling the full folder payload for the active console view.
					</p>
				</div>
			{/if}
		</section>

		<div class="side-rail">
			<section class="station-card rail-card" aria-label="Queue watch">
				<div class="section-head compact">
					<div>
						<p class="section-label">Queue watch</p>
						<h2 class="section-title small">Live encode lanes</h2>
					</div>
					<a class="console-link" href={resolve('/ops')}>Queue controls</a>
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
										<p class="watch-title mono-copy">{job.prefix}</p>
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

			<section class="station-card rail-card" aria-label="Catalog watch">
				<div class="section-head compact">
					<div>
						<p class="section-label">Catalog watch</p>
						<h2 class="section-title small">Scan and backlog</h2>
					</div>
					<a class="console-link" href={resolve('/completed')}>Backups</a>
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
									<p class="watch-title mono-copy">{job.prefix}</p>
									<p class="watch-copy">{job.error || queueJobDetailCopy(job)}</p>
								</li>
							{/each}
						</ul>
					</div>
				{/if}
			</section>
		</div>
	</div>

	<section class="station-card filter-deck" aria-label="Library filters">
		<div class="section-head compact">
			<div>
				<p class="section-label">Queue filters</p>
				<h2 class="section-title small">Visible libraries</h2>
			</div>
			<p class="filter-hint">{filterHintCopy}</p>
		</div>

		<div class="filter-row">
			<button
				type="button"
				class:active={!libraryFiltersActive}
				class="filter-chip all-chip"
				onclick={enableAllLibraries}
			>
				All libraries
			</button>

			{#each folderLibraries as library (library.key)}
				<button
					type="button"
					class:active={!disabledLibraries.includes(library.key)}
					class="filter-chip"
					style={`--library-chip: ${libraryColors[library.key] ?? '#4b5563'};`}
					onclick={() => toggleLibraryFilter(library.key)}
				>
					<span class="filter-dot"></span>
					{library.label}
					<span class="filter-count">{library.count}</span>
				</button>
			{/each}
		</div>
	</section>

	<section class="station-card table-deck" aria-label="Folder work queue">
		<div class="section-head compact">
			<div>
				<p class="section-label">Work queue</p>
				<h2 class="section-title small">Ranked folders</h2>
			</div>
			<p class="queue-count">
				{sortedVisibleFolders.length} visible folders
				<span class="sort-summary">
					Sorted by {folderSortKey}
					{folderSortDirection === 'asc' ? 'ascending' : 'descending'}
				</span>
			</p>
		</div>

		{#if folderLoadState === 'loading' && folders.length === 0 && !catalogEmpty}
			<p class="muted-block">Loading the full folder queue from the active catalog snapshot.</p>
		{:else if folderLoadState === 'error'}
			<div class="table-message error-message">
				<p>Could not load the ranked folder queue.</p>
				<p>{folderLoadError}</p>
			</div>
		{:else if catalogEmpty}
			<div class="table-message">
				<p>No folders are available yet.</p>
				<p>Run a scan or confirm your library configuration in Settings.</p>
			</div>
		{:else if sortedVisibleFolders.length === 0}
			<div class="table-message">
				<p>All visible libraries are currently filtered out.</p>
				<p>Restore one library filter or press All libraries to resume the queue view.</p>
			</div>
		{:else}
			<div class="table-shell">
				<table>
					<thead>
						<tr>
							<th scope="col" aria-sort={folderSortAria('priority')}>
								<button
									type="button"
									class={`sort-header ${folderSortKey === 'priority' ? 'active' : ''}`.trim()}
									onclick={() => toggleFolderSort('priority')}
								>
									Priority
									<span class="sort-indicator"
										>{folderSortKey === 'priority'
											? folderSortDirection === 'asc'
												? '^'
												: 'v'
											: '-'}</span
									>
								</button>
							</th>
							<th scope="col" aria-sort={folderSortAria('title')}>
								<button
									type="button"
									class={`sort-header ${folderSortKey === 'title' ? 'active' : ''}`.trim()}
									onclick={() => toggleFolderSort('title')}
								>
									Folder
									<span class="sort-indicator"
										>{folderSortKey === 'title'
											? folderSortDirection === 'asc'
												? '^'
												: 'v'
											: '-'}</span
									>
								</button>
							</th>
							<th scope="col" aria-sort={folderSortAria('library')}>
								<button
									type="button"
									class={`sort-header ${folderSortKey === 'library' ? 'active' : ''}`.trim()}
									onclick={() => toggleFolderSort('library')}
								>
									Library
									<span class="sort-indicator"
										>{folderSortKey === 'library'
											? folderSortDirection === 'asc'
												? '^'
												: 'v'
											: '-'}</span
									>
								</button>
							</th>
							<th scope="col" aria-sort={folderSortAria('pending')}>
								<button
									type="button"
									class={`sort-header ${folderSortKey === 'pending' ? 'active' : ''}`.trim()}
									onclick={() => toggleFolderSort('pending')}
								>
									Pending
									<span class="sort-indicator"
										>{folderSortKey === 'pending'
											? folderSortDirection === 'asc'
												? '^'
												: 'v'
											: '-'}</span
									>
								</button>
							</th>
							<th scope="col" aria-sort={folderSortAria('reclaim')}>
								<button
									type="button"
									class={`sort-header ${folderSortKey === 'reclaim' ? 'active' : ''}`.trim()}
									onclick={() => toggleFolderSort('reclaim')}
								>
									Reclaim
									<span class="sort-indicator"
										>{folderSortKey === 'reclaim'
											? folderSortDirection === 'asc'
												? '^'
												: 'v'
											: '-'}</span
									>
								</button>
							</th>
							<th scope="col" aria-sort={folderSortAria('age')}>
								<button
									type="button"
									class={`sort-header ${folderSortKey === 'age' ? 'active' : ''}`.trim()}
									onclick={() => toggleFolderSort('age')}
								>
									Age
									<span class="sort-indicator"
										>{folderSortKey === 'age'
											? folderSortDirection === 'asc'
												? '^'
												: 'v'
											: '-'}</span
									>
								</button>
							</th>
							<th scope="col" aria-sort={folderSortAria('review')}>
								<button
									type="button"
									class={`sort-header ${folderSortKey === 'review' ? 'active' : ''}`.trim()}
									onclick={() => toggleFolderSort('review')}
								>
									Review
									<span class="sort-indicator"
										>{folderSortKey === 'review'
											? folderSortDirection === 'asc'
												? '^'
												: 'v'
											: '-'}</span
									>
								</button>
							</th>
							<th scope="col">Signal</th>
						</tr>
					</thead>
					<tbody>
						{#each sortedVisibleFolders as folder, index (folder.prefix)}
							<tr class:active-row={index === 0}>
								<td data-label="Priority">#{index + 1}</td>
								<td data-label="Folder">
									<a class="folder-link" href={resolve(`/folders/${folder.prefix}`)}>
										<span class="folder-title">{folder.title}</span>
										<span class="folder-meta">{folder.subtitle || folder.prefix}</span>
									</a>
								</td>
								<td data-label="Library">
									<span
										class="library-tag"
										style={`--library-chip: ${libraryColors[folderLibraryKey(folder.prefix)] ?? '#4b5563'};`}
									>
										<span class="filter-dot"></span>
										{folderLibraryLabel(folderLibraryKey(folder.prefix))}
									</span>
									<span class="table-subcopy">{folder.scope_label}</span>
								</td>
								<td data-label="Pending">
									<span class="table-value">{folder.pending_count} / {folder.item_count}</span>
									<span class="table-subcopy"
										>{Math.max(folder.item_count - folder.pending_count, 0)} complete</span
									>
								</td>
								<td data-label="Reclaim">
									<span class="table-value">{formatGiB(folder.projected_reclaim_bytes, 1)}</span>
									<span class="table-subcopy">{formatGiB(folder.total_size_bytes, 1)} on disk</span>
								</td>
								<td data-label="Age">
									<span class="table-value"
										>{Math.max(0, Math.round(folder.average_age_days))}d</span
									>
									<span class="table-subcopy">
										{folder.average_age_days > 0 ? 'Average item age' : 'Freshly ranked'}
									</span>
								</td>
								<td data-label="Review">
									<span class={`review-tag ${folder.review_badge_tone ?? 'neutral'}`.trim()}>
										{folder.review_badge_label || 'Ready to inspect'}
									</span>
									<span class="table-subcopy">{folder.scope_label}</span>
								</td>
								<td data-label="Signal">
									<span class="table-value">{formatTopCounts(folder.statuses, 3)}</span>
									<span class="table-subcopy"
										>{formatTopCounts(folder.video_codecs, 2, 'Codec mix pending')}</span
									>
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}
	</section>
</div>

<style>
	:global(html),
	:global(body) {
		background: #0b1014;
	}

	:global(body) {
		color: #e5edf6;
		background-image: none;
	}

	:global(body::after) {
		display: none;
	}

	:global(body::before) {
		inset: 0;
		background-image:
			linear-gradient(rgba(148, 163, 184, 0.05) 1px, transparent 1px),
			linear-gradient(90deg, rgba(148, 163, 184, 0.04) 1px, transparent 1px);
		background-size: 28px 28px;
		opacity: 0.32;
		mix-blend-mode: normal;
		mask-image: none;
	}

	:global(.page-shell) {
		width: min(1480px, calc(100vw - 1.75rem));
		gap: 0.95rem;
		padding-top: 0.7rem;
	}

	:global(.masthead),
	:global(.masthead.panel) {
		border: 1px solid rgba(148, 163, 184, 0.18) !important;
		box-shadow: 0 18px 34px rgba(2, 6, 23, 0.26) !important;
		backdrop-filter: blur(14px);
		background: rgba(10, 15, 21, 0.9) !important;
	}

	:global(.masthead)::before,
	:global(.masthead)::after {
		display: none !important;
	}

	:global(.masthead .brand-mark) {
		color: #7dd3fc;
	}

	:global(.masthead .brand-name) {
		font-family: 'Avenir Next', 'Segoe UI', sans-serif;
		font-size: clamp(1.1rem, 1.65vw, 1.45rem);
		letter-spacing: -0.02em;
		color: #f8fafc;
	}

	:global(.masthead .nav-row a) {
		border-radius: 0.55rem;
		border-color: rgba(148, 163, 184, 0.16);
		background: rgba(15, 23, 42, 0.72);
		color: rgba(226, 232, 240, 0.74);
		box-shadow: none;
	}

	:global(.masthead .nav-row a:hover),
	:global(.masthead .nav-row a.active) {
		transform: none;
		border-color: rgba(56, 189, 248, 0.42);
		background: rgba(8, 47, 73, 0.86);
		color: #f8fafc;
		box-shadow: none;
	}

	.workstation-screen {
		display: grid;
		gap: 1rem;
		padding: 0.25rem 0 1rem;
	}

	.system-strip,
	.console-grid,
	.filter-row,
	.section-head,
	.action-row,
	.workspace-actions,
	.watch-row,
	.filter-chip,
	.library-tag,
	.workspace-badges {
		display: flex;
	}

	.system-strip,
	.console-grid {
		gap: 1rem;
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
	.queue-cell::before,
	.active-workspace::before {
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

	.queue-cell.normal-state::before,
	.active-workspace::before {
		background: linear-gradient(90deg, rgba(56, 189, 248, 0.85), rgba(34, 197, 94, 0.22));
	}

	.system-label,
	.section-label,
	.alert-label,
	.fact-label,
	.signal-label {
		margin: 0;
		font-size: 0.72rem;
		font-weight: 800;
		letter-spacing: 0.16em;
		text-transform: uppercase;
		color: rgba(148, 163, 184, 0.88);
	}

	.system-value,
	.section-title,
	.workspace-title,
	.fact-value,
	.table-value {
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
	.workspace-summary,
	.workspace-subtitle,
	.watch-copy,
	.catalog-headline,
	.filter-hint,
	.queue-count,
	.table-subcopy,
	.muted-block,
	.table-message,
	.rail-summary,
	.rail-summary p {
		margin: 0;
		color: rgba(226, 232, 240, 0.74);
		line-height: 1.5;
	}

	.alert-strip,
	.section-head,
	.workspace-header,
	.fact-grid,
	.signal-grid,
	.side-rail,
	.filter-row {
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
	.console-link,
	.workspace-primary,
	.workspace-secondary,
	.filter-chip {
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
	.console-link,
	.workspace-primary,
	.workspace-secondary {
		display: inline-flex;
		padding: 0.72rem 0.95rem;
		border: 1px solid rgba(56, 189, 248, 0.22);
		background: rgba(15, 23, 42, 0.7);
		color: #e2e8f0;
	}

	.console-link:hover,
	.workspace-primary:hover,
	.workspace-secondary:hover,
	.alert-action:hover,
	.filter-chip:hover {
		transform: translateY(-1px);
		border-color: rgba(56, 189, 248, 0.5);
		background: rgba(30, 41, 59, 0.94);
	}

	.console-grid {
		display: grid;
		grid-template-columns: minmax(0, 1.5fr) minmax(25rem, 0.92fr);
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

	.section-head.compact .console-link {
		justify-self: start;
		width: fit-content;
	}

	.section-title {
		margin-top: 0.3rem;
		font-size: 1.25rem;
		font-weight: 700;
	}

	.section-title.small {
		font-size: 1rem;
	}

	.action-row,
	.workspace-actions,
	.workspace-badges,
	.filter-row {
		gap: 0.65rem;
		flex-wrap: wrap;
	}

	.workspace-header {
		grid-template-columns: minmax(0, 1fr) auto;
		gap: 1rem;
		align-items: start;
	}

	.workspace-title {
		margin-top: 0.7rem;
		font-size: clamp(1.18rem, 1.55vw, 1.55rem);
		font-weight: 700;
		line-height: 1.16;
	}

	.workspace-subtitle {
		margin-top: 0.55rem;
		font-size: 0.95rem;
	}

	.workspace-summary {
		margin-top: 0.8rem;
		max-width: 38rem;
	}

	.workspace-actions {
		justify-content: flex-end;
	}

	.workspace-primary {
		background: rgba(8, 47, 73, 0.92);
		border-color: rgba(56, 189, 248, 0.45);
	}

	.workspace-secondary {
		background: rgba(15, 23, 42, 0.72);
	}

	.workspace-badge,
	.review-tag,
	.watch-state,
	.library-tag {
		display: inline-flex;
		align-items: center;
		gap: 0.45rem;
		padding: 0.34rem 0.56rem;
		font-size: 0.72rem;
		font-weight: 800;
		letter-spacing: 0.04em;
		text-transform: uppercase;
	}

	.workspace-badge,
	.library-tag {
		border: 1px solid rgba(148, 163, 184, 0.28);
		color: #e2e8f0;
		background: rgba(30, 41, 59, 0.84);
	}

	.workspace-badge.library {
		border-color: color-mix(in srgb, var(--library-chip) 58%, rgba(148, 163, 184, 0.35));
	}

	.workspace-badge.library::before {
		content: '';
		width: 0.55rem;
		height: 0.55rem;
		border-radius: 999px;
		background: var(--library-chip);
	}

	.workspace-badge.review.ok,
	.review-tag.ok {
		background: rgba(20, 83, 45, 0.84);
		color: #dcfce7;
	}

	.workspace-badge.review.warning,
	.workspace-badge.review.warn,
	.review-tag.warning,
	.review-tag.warn {
		background: rgba(120, 53, 15, 0.84);
		color: #ffedd5;
	}

	.workspace-badge.review.attention,
	.review-tag.attention,
	.review-tag.neutral {
		background: rgba(8, 47, 73, 0.86);
		color: #dbeafe;
	}

	.fact-grid,
	.signal-grid {
		grid-template-columns: repeat(4, minmax(0, 1fr));
		gap: 0.75rem;
		margin-top: 1rem;
	}

	.signal-grid {
		grid-template-columns: repeat(3, minmax(0, 1fr));
		margin-top: 0.75rem;
	}

	.fact-cell,
	.signal-panel {
		padding: 0.85rem 0.9rem;
		border: 1px solid rgba(148, 163, 184, 0.18);
		background: rgba(15, 23, 42, 0.54);
	}

	.fact-cell.highlight {
		background: rgba(8, 47, 73, 0.82);
		border-color: rgba(56, 189, 248, 0.36);
	}

	.fact-value {
		margin-top: 0.45rem;
		font-size: 1.1rem;
		font-weight: 700;
	}

	.signal-value {
		margin: 0.45rem 0 0;
		color: rgba(241, 245, 249, 0.92);
		line-height: 1.5;
	}

	.side-rail {
		gap: 1rem;
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

	.empty-shell,
	.table-message,
	.muted-block {
		padding: 0.95rem 1rem;
		border: 1px solid rgba(148, 163, 184, 0.16);
		background: rgba(15, 23, 42, 0.46);
	}

	.filter-deck,
	.table-deck {
		padding-top: 1rem;
	}

	.filter-row {
		grid-template-columns: repeat(auto-fit, minmax(10rem, max-content));
		align-items: stretch;
	}

	.filter-chip {
		display: inline-flex;
		gap: 0.5rem;
		padding: 0.7rem 0.85rem;
		border: 1px solid rgba(148, 163, 184, 0.2);
		background: rgba(15, 23, 42, 0.62);
		color: rgba(226, 232, 240, 0.76);
	}

	.filter-chip.active {
		background: rgba(30, 41, 59, 0.88);
		color: #f8fafc;
		border-color: color-mix(in srgb, var(--library-chip, #38bdf8) 58%, rgba(148, 163, 184, 0.35));
	}

	.all-chip.active {
		border-color: rgba(56, 189, 248, 0.45);
	}

	.filter-dot {
		width: 0.55rem;
		height: 0.55rem;
		border-radius: 999px;
		background: var(--library-chip, #38bdf8);
		flex: 0 0 auto;
		align-self: center;
	}

	.filter-count {
		margin-left: auto;
		color: rgba(148, 163, 184, 0.8);
	}

	.table-shell {
		max-height: min(68vh, 58rem);
		overflow-x: auto;
		overflow-y: auto;
		border: 1px solid rgba(148, 163, 184, 0.16);
		background: rgba(9, 14, 22, 0.88);
	}

	table {
		width: 100%;
		border-collapse: collapse;
	}

	thead {
		background: rgba(15, 23, 42, 0.96);
	}

	thead th {
		position: sticky;
		top: 0;
		z-index: 2;
		background: rgba(15, 23, 42, 0.98);
	}

	th,
	td {
		padding: 0.85rem 0.9rem;
		text-align: left;
		vertical-align: top;
		border-bottom: 1px solid rgba(148, 163, 184, 0.14);
	}

	th {
		font-size: 0.72rem;
		font-weight: 800;
		letter-spacing: 0.12em;
		text-transform: uppercase;
		color: rgba(148, 163, 184, 0.88);
	}

	td {
		color: rgba(226, 232, 240, 0.86);
	}

	.sort-header {
		display: inline-flex;
		align-items: center;
		gap: 0.45rem;
		padding: 0;
		border: 0;
		background: transparent;
		color: inherit;
		font: inherit;
		font-weight: inherit;
		letter-spacing: inherit;
		text-transform: inherit;
	}

	.sort-header.active {
		color: #f8fafc;
	}

	.sort-indicator {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-width: 0.8rem;
		color: rgba(125, 211, 252, 0.9);
	}

	.sort-summary {
		display: block;
		margin-top: 0.15rem;
		font-size: 0.82rem;
		color: rgba(148, 163, 184, 0.84);
	}

	tbody tr {
		background: rgba(11, 15, 20, 0.76);
	}

	tbody tr:nth-child(even) {
		background: rgba(15, 23, 42, 0.82);
	}

	tbody tr.active-row {
		background: rgba(8, 47, 73, 0.34);
	}

	.folder-link {
		display: grid;
		gap: 0.18rem;
		color: inherit;
	}

	.folder-title {
		font-weight: 700;
		color: #f8fafc;
	}

	.folder-meta,
	.table-subcopy {
		display: block;
		margin-top: 0.18rem;
		font-size: 0.84rem;
	}

	.review-tag {
		justify-content: flex-start;
	}

	.table-value,
	.fact-value {
		font-family: 'SFMono-Regular', 'Menlo', monospace;
		letter-spacing: -0.01em;
	}

	.error-message {
		border-color: rgba(249, 115, 22, 0.3);
		background: rgba(67, 20, 7, 0.72);
	}

	@media (max-width: 1100px) {
		.system-strip,
		.fact-grid,
		.signal-grid {
			grid-template-columns: 1fr 1fr;
		}

		.workspace-header,
		.section-head {
			grid-template-columns: 1fr;
		}

		.workspace-actions {
			justify-content: flex-start;
		}
	}

	@media (max-width: 960px) {
		.console-grid {
			grid-template-columns: 1fr;
		}
	}

	@media (max-width: 720px) {
		.system-strip,
		.fact-grid,
		.signal-grid {
			grid-template-columns: 1fr;
		}

		.alert-strip {
			grid-template-columns: 1fr;
		}

		.filter-row {
			grid-template-columns: 1fr;
		}

		thead {
			display: none;
		}

		table,
		tbody,
		tr,
		td {
			display: block;
			width: 100%;
		}

		tr {
			padding: 0.4rem 0;
		}

		td {
			padding: 0.6rem 0.9rem;
		}

		td::before {
			content: attr(data-label);
			display: block;
			margin-bottom: 0.22rem;
			font-size: 0.68rem;
			font-weight: 800;
			letter-spacing: 0.12em;
			text-transform: uppercase;
			color: rgba(148, 163, 184, 0.8);
		}
	}
</style>
