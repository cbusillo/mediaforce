<script lang="ts">
	import { browser } from '$app/environment';
	import type {
		DashboardFoldersPayload,
		DashboardSummaryPayload,
		FolderCard as FolderCardData,
		HostsPayload
	} from '$lib/api/types';
	import { goto, invalidateAll } from '$app/navigation';
	import { resolve } from '$app/paths';
	import { onDestroy, onMount } from 'svelte';
	import { fetchJson, postJson } from '$lib/api/client';
	import {
		folderLibraryKey,
		folderLibraryLabel,
		folderLibraryThemeStyle
	} from '$lib/folder-display';
	import Button from '$lib/components/Button.svelte';
	import FolderCard from '$lib/components/FolderCard.svelte';
	import { formatGiB, hostSettingsAnchor } from '$lib/format';
	import HeroCard from '$lib/components/HeroCard.svelte';
	import HostCard from '$lib/components/HostCard.svelte';
	import Panel from '$lib/components/Panel.svelte';
	import Pill from '$lib/components/Pill.svelte';
	import SectionHead from '$lib/components/SectionHead.svelte';
	import { toasts } from '$lib/stores/toasts';

	let {
		data
	}: {
		data: {
			dashboard: DashboardSummaryPayload;
			hosts: HostsPayload;
		};
	} = $props();

	const initialDashboard = (() => data.dashboard)();
	const dashboard = $derived(data.dashboard);
	const hosts = $derived(data.hosts);
	let folders = $state<FolderCardData[]>(initialDashboard.folders_preview);
	let catalogEmpty = $state(initialDashboard.catalog_empty);
	let folderLoadState = $state<'loading' | 'ready' | 'error'>(
		initialDashboard.catalog_empty ? 'ready' : 'loading'
	);
	let folderLoadError = $state<string | null>(null);
	let activeFolderRequest = 0;
	let requestedFolderCacheKey = $state<string | null>(null);
	let pendingStoredDisabledLibraries = $state<string[] | null>(null);
	let folderLoadController: AbortController | null = null;
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
	const encodeQueueHasWork = $derived.by(
		() => dashboard.encode_queue.running_count > 0 || dashboard.encode_queue.queued_count > 0
	);
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
	const catalogScanStatus = $derived(String(dashboard.scan_job?.status ?? 'idle'));
	const catalogScanActive = $derived(
		catalogScanStatus === 'queued' || catalogScanStatus === 'running'
	);
	const catalogScanHeading = $derived(
		catalogScanStatus === 'running' ? 'Refreshing libraries now' : 'Library refresh queued'
	);

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
	<HeroCard>
		{#snippet copy()}
			<SectionHead
				eyebrow="Current Strategy"
				heading={`${folders.length} folders ready for tuning with ${formatGiB(totalEstimatedSavings, 0)} on the table.`}
				lede="Start with the biggest reclaim, validate with a sample, then send the full folder only when the draft looks right."
				size="display"
			/>
		{/snippet}

		{#snippet meta()}
			<div class="hero-support-column">
				<div class="hero-stat-list" aria-label="Strategy summary">
					{#each heroFacts as fact (fact.label)}
						<div class="hero-stat-row">
							<p class="eyebrow-copy">{fact.label}</p>
							<p class="hero-stat-value">{fact.value}</p>
						</div>
					{/each}
				</div>
				<div class="pill-column">
					{#if metricsReady}
						<Pill label="All metrics ready" variant="ok" wide />
					{:else}
						<Pill
							label={`VMAF ${dashboard.metric_support.vmaf ? 'ready' : 'missing'}`}
							variant={dashboard.metric_support.vmaf ? 'ok' : 'warn'}
						/>
						<Pill
							label={`XPSNR ${dashboard.metric_support.xpsnr ? 'ready' : 'missing'}`}
							variant={dashboard.metric_support.xpsnr ? 'ok' : 'warn'}
						/>
						<Pill
							label={`SSIM ${dashboard.metric_support.ssim ? 'ready' : 'missing'}`}
							variant={dashboard.metric_support.ssim ? 'ok' : 'warn'}
						/>
					{/if}
				</div>
			</div>
		{/snippet}

		{#snippet aside()}
			<p>{dashboard.metric_status_copy}</p>
		{/snippet}
	</HeroCard>

	<div class="queue-grid">
		{#each queueCards as card, index (card.eyebrow)}
			<Panel variant={index === 1 ? 'accent' : 'default'}>
				<div class="panel-stack">
					<SectionHead
						eyebrow={card.eyebrow}
						heading={card.heading}
						lede={card.lede}
						size="compact"
					/>
					{#if index === 0}
						{#if pendingReviewCount > 0}
							<div class="queue-pill-row">
								<span class="queue-pill attention">Pending review: {pendingReviewCount}</span>
							</div>
						{/if}
					{:else}
						<div class="queue-pill-row">
							<span class={`queue-pill ${encodeQueueStatus.tone}`.trim()}
								>{encodeQueueStatus.label}</span
							>
							{#if dashboard.encode_queue.state.stop_requested}
								<span class="queue-pill attention">Stop requested</span>
							{/if}
							<a class="queue-link-pill" href="#remote-hosts">
								Workers ready: {readyHosts}
							</a>
							{#if readyHosts < encodeCapableHosts}
								<span class="queue-pill neutral">{reachableHosts} mounted</span>
							{/if}
						</div>
						<div class="action-row">
							{#if dashboard.encode_queue.state.is_paused}
								<Button
									variant="primary"
									loading={queueAction === 'resume'}
									onclick={() => runQueueAction('resume')}>Resume Queue</Button
								>
							{:else}
								<Button
									variant="primary"
									loading={queueAction === 'pause'}
									onclick={() => runQueueAction('pause')}>Pause Queue</Button
								>
							{/if}
							<Button
								variant="danger"
								loading={queueAction === 'stop'}
								disabled={!encodeQueueHasWork || dashboard.encode_queue.state.stop_requested}
								onclick={() => runQueueAction('stop')}>Stop + Clean</Button
							>
						</div>
					{/if}
				</div>
			</Panel>
		{/each}
	</div>

	<Panel variant="inset" class="folder-section" padding="1.2rem 1.3rem 1.4rem">
		<div class="section-stack">
			<div class="section-header-row">
				<SectionHead
					eyebrow="Remote Hosts"
					heading="Where encodes can run"
					lede="Worker availability stays close to the queue controls so scheduling decisions are easy to read."
					size="compact"
				/>
				<div class="section-header-tools">
					<span class={`section-summary-chip ${readyHosts > 0 ? 'active' : ''}`.trim()}
						>{readyHosts} ready</span
					>
					<a class="section-action-link" href={resolve('/settings')}> Manage in Settings </a>
				</div>
			</div>
			<div id="remote-hosts" class="host-grid">
				{#each rankedHosts as host (host.key)}
					<HostCard {host} onSettingsClick={() => openHostSettings(host.key)} />
				{/each}
			</div>
		</div>
	</Panel>

	<Panel variant="inset" class="folder-section" padding="1.2rem 1.3rem 1.4rem">
		<div class="section-stack">
			<div class="section-header-row">
				<SectionHead
					eyebrow="Folders"
					heading="Candidate folders"
					lede="Sorted by estimated reclaim so the strongest space-saving bets stay first."
					size="compact"
				/>
				<div class="folder-header-side">
					<p class="section-kicker muted-copy">
						Open a folder to start representative-file tuning.
					</p>
				</div>
			</div>
			{#if catalogScanActive}
				<div class="catalog-refresh-banner" role="status" aria-live="polite">
					<p class="eyebrow-copy">Library Refresh</p>
					<p>{catalogScanHeading}</p>
					<p class="muted-copy">
						New or changed library roots are being rescanned before this recommendation list
						updates.
					</p>
				</div>
			{/if}
			{#if folderLoadState === 'loading' && folders.length > 0}
				<div class="catalog-refresh-banner" role="status" aria-live="polite">
					<p class="eyebrow-copy">Folders</p>
					<p>Folders are clickable now.</p>
					<p class="muted-copy">
						Detailed ranking and age data are still finishing in the background.
					</p>
				</div>
			{:else if folderLoadState === 'error'}
				<div class="catalog-refresh-banner" role="alert">
					<p class="eyebrow-copy">Folders</p>
					<p>Folder list failed to load.</p>
					<p class="muted-copy">{folderLoadError ?? 'Unknown folder loading error.'}</p>
				</div>
			{/if}
			{#if folderLibraries.length > 1}
				<div class="library-filter-strip" aria-label="Library filters">
					<div class="library-filter-row">
						<button
							type="button"
							class={`library-filter-pill all-pill ${!libraryFiltersActive ? 'active' : ''}`.trim()}
							aria-pressed={!libraryFiltersActive}
							onclick={enableAllLibraries}
						>
							<span>All</span>
							<span class="library-filter-count">{folders.length}</span>
						</button>
						{#each folderLibraries as library (library.key)}
							<button
								type="button"
								class={`library-filter-pill ${disabledLibraries.includes(library.key) ? 'inactive' : 'active'}`.trim()}
								aria-pressed={!disabledLibraries.includes(library.key)}
								style={folderLibraryThemeStyle(libraryColors[library.key])}
								onclick={() => toggleLibraryFilter(library.key)}
							>
								<span>{library.label}</span>
								<span class="library-filter-count">{library.count}</span>
							</button>
						{/each}
					</div>
					<p class="filter-summary-inline muted-copy">
						<span class="filter-summary-status">{folderFilterSummary}</span>
						<span class="filter-summary-separator" aria-hidden="true">·</span>
						<span class="filter-summary-hint">{filterHintCopy}</span>
					</p>
				</div>
			{/if}
			{#if visibleFolders.length > 0}
				<div class="folder-grid">
					{#each visibleFolders as folder (folder.prefix)}
						<FolderCard {folder} libraryColor={libraryColors[folderLibraryKey(folder.prefix)]} />
					{/each}
				</div>
			{/if}
			{#if !visibleFolders.length && !catalogEmpty}
				<div class="catalog-refresh-banner" role="status">
					<p class="eyebrow-copy">Library Filter</p>
					<p>No candidate folders are visible.</p>
					<p class="muted-copy">Turn a library back on to restore matching folders.</p>
				</div>
			{/if}
			{#if catalogEmpty}
				<div class="catalog-refresh-banner" role="status">
					<p class="eyebrow-copy">Folders</p>
					<p>No candidate folders are available yet.</p>
					<p class="muted-copy">A library scan or new media changes may still be in flight.</p>
				</div>
			{/if}
		</div>
	</Panel>
</div>

<style>
	.page-stack,
	.panel-stack,
	.section-stack {
		display: grid;
		gap: var(--space-3);
	}

	.queue-grid {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: var(--space-4);
	}

	.folder-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
		gap: var(--space-4);
	}

	.catalog-refresh-banner {
		display: grid;
		gap: 0.24rem;
		padding: 0.78rem 0.88rem;
		border-radius: var(--radius-md);
		background: rgba(180, 83, 9, 0.08);
		border: 1px solid rgba(180, 83, 9, 0.16);
	}

	.catalog-refresh-banner p {
		margin: 0;
	}

	.catalog-refresh-banner p:nth-child(2) {
		font-weight: 700;
		color: var(--accent-deep);
	}

	.host-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
		gap: var(--space-4);
	}

	.pill-column {
		display: grid;
		gap: var(--space-2);
		align-content: start;
	}

	.hero-support-column {
		display: grid;
		gap: var(--space-3);
		align-content: start;
	}

	.hero-stat-list {
		display: grid;
		gap: 0.65rem;
	}

	.hero-stat-row {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
		gap: 0.8rem;
		padding: 0.6rem 0.7rem;
		border-radius: var(--radius-md);
		background: rgba(255, 255, 255, 0.52);
		border: 1px solid rgba(23, 35, 31, 0.08);
	}

	.queue-pill-row {
		display: flex;
		flex-wrap: wrap;
		gap: 0.65rem;
		align-items: center;
	}

	.queue-pill,
	.queue-link-pill,
	.section-summary-chip {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		padding: 0.48rem 0.82rem;
		border-radius: var(--radius-pill);
		font-size: 0.84rem;
		font-weight: 700;
		line-height: 1;
		border: 1px solid rgba(23, 35, 31, 0.1);
		background: rgba(255, 255, 255, 0.78);
		color: var(--ink-soft);
	}

	.queue-pill.ok,
	.section-summary-chip.active {
		background: rgba(47, 107, 62, 0.12);
		border-color: rgba(47, 107, 62, 0.18);
		color: var(--ok);
	}

	.queue-pill.attention {
		background: rgba(180, 83, 9, 0.12);
		border-color: rgba(180, 83, 9, 0.18);
		color: #9a5b00;
	}

	.queue-pill.neutral {
		background: rgba(255, 255, 255, 0.76);
		border-color: rgba(23, 35, 31, 0.12);
		color: var(--ink-soft);
	}

	.queue-link-pill,
	.section-action-link {
		text-decoration: none;
		transition:
			transform 150ms ease,
			border-color 150ms ease,
			background-color 150ms ease,
			color 150ms ease;
	}

	.queue-link-pill {
		background: rgba(15, 118, 110, 0.09);
		border-color: rgba(15, 118, 110, 0.18);
		color: var(--accent-deep);
	}

	.queue-link-pill:hover,
	.section-action-link:hover {
		transform: translateY(-1px);
	}

	.section-header-tools {
		display: flex;
		align-items: center;
		justify-content: flex-end;
		gap: 0.7rem;
		flex-wrap: wrap;
	}

	.section-action-link {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		padding: 0.48rem 0.82rem;
		border-radius: var(--radius-pill);
		border: 1px solid rgba(23, 35, 31, 0.1);
		background: rgba(255, 255, 255, 0.72);
		color: var(--ink-soft);
		font-size: 0.84rem;
		font-weight: 700;
	}

	.hero-stat-row :global(.eyebrow-copy) {
		font-size: 0.72rem;
	}

	.hero-stat-value {
		font-size: 1.02rem;
		font-weight: 700;
		line-height: 1.2;
		text-align: right;
	}

	.action-row {
		display: flex;
		gap: var(--space-2);
		flex-wrap: wrap;
	}

	.section-header-row {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		gap: var(--space-3);
		align-items: end;
		padding-bottom: 0.95rem;
		border-bottom: 1px solid rgba(23, 35, 31, 0.08);
	}

	.section-kicker {
		max-width: 18rem;
		font-size: 0.88rem;
		line-height: 1.45;
		text-align: right;
	}

	.folder-header-side {
		display: grid;
		gap: 0.2rem;
		justify-items: end;
	}

	.library-filter-strip {
		display: grid;
		gap: 0.55rem;
		justify-items: start;
	}

	.library-filter-row {
		display: inline-flex;
		flex-wrap: wrap;
		gap: 0.45rem;
		padding: 0.36rem;
		width: fit-content;
		max-width: 100%;
		border-radius: calc(var(--radius-pill) + 0.45rem);
		background: rgba(255, 255, 255, 0.56);
		border: 1px solid rgba(23, 35, 31, 0.08);
		justify-content: flex-start;
		box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.45);
	}

	.library-filter-pill {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: 0.42rem;
		padding: 0.48rem 0.8rem;
		border-radius: var(--radius-pill);
		border: 1px solid var(--library-border, rgba(23, 35, 31, 0.12));
		background: var(--library-surface, rgba(23, 35, 31, 0.06));
		color: var(--library-text, var(--ink-soft));
		font-size: 0.8rem;
		font-weight: 700;
		line-height: 1;
		cursor: pointer;
		transition:
			transform 150ms ease,
			scale 150ms ease,
			background-color 150ms ease,
			border-color 150ms ease,
			color 150ms ease,
			box-shadow 150ms ease;
		box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.34);
	}

	.library-filter-pill:hover {
		transform: translateY(-1px);
		border-color: color-mix(in srgb, var(--library-base, var(--accent-deep)) 35%, white);
	}

	.library-filter-pill.inactive {
		opacity: 0.62;
		border-style: dashed;
		box-shadow: none;
	}

	.library-filter-pill.inactive:hover {
		opacity: 0.8;
	}

	.library-filter-pill:active {
		scale: 0.98;
	}

	.library-filter-pill.active {
		background: var(--library-base, var(--accent-deep));
		border-color: color-mix(in srgb, var(--library-base, var(--accent-deep)) 85%, white);
		box-shadow: 0 10px 24px var(--library-glow, rgba(15, 118, 110, 0.16));
		color: #f8fcfb;
	}

	.library-filter-pill.all-pill {
		background: rgba(23, 35, 31, 0.04);
		border-color: rgba(23, 35, 31, 0.1);
		color: var(--ink-soft);
	}

	.library-filter-pill.all-pill:hover {
		border-color: rgba(23, 35, 31, 0.18);
	}

	.library-filter-pill.all-pill.active {
		background: var(--accent-deep);
		border-color: rgba(15, 118, 110, 0.22);
		box-shadow: 0 10px 24px rgba(15, 118, 110, 0.16);
		color: #f8fcfb;
	}

	.library-filter-count {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-width: 1.45rem;
		height: 1.45rem;
		padding: 0 0.35rem;
		border-radius: 999px;
		background: rgba(255, 255, 255, 0.72);
		color: var(--library-text, var(--ink));
		font-size: 0.72rem;
		font-weight: 800;
	}

	.library-filter-pill.active .library-filter-count {
		background: rgba(255, 255, 255, 0.18);
		color: #f8fcfb;
	}

	.filter-summary-inline {
		margin: 0;
		font-size: 0.82rem;
		line-height: 1.45;
		display: flex;
		flex-wrap: wrap;
		justify-content: flex-start;
		gap: 0.35rem 0.6rem;
		max-width: 38rem;
	}

	.filter-summary-status {
		color: var(--ink-soft);
	}

	.filter-summary-separator {
		color: rgba(23, 35, 31, 0.32);
	}

	.filter-summary-hint {
		color: rgba(74, 91, 86, 0.82);
	}

	@media (max-width: 860px) {
		.queue-grid {
			grid-template-columns: 1fr;
		}

		.section-header-row {
			grid-template-columns: 1fr;
			align-items: start;
		}

		.section-header-tools {
			justify-content: flex-start;
		}

		.folder-header-side {
			justify-items: start;
		}

		.section-kicker {
			max-width: none;
			text-align: left;
		}

		.filter-summary-inline,
		.library-filter-row {
			text-align: left;
			justify-content: flex-start;
		}

		.filter-summary-inline {
			max-width: none;
			padding-top: 0;
		}
	}

	@media (max-width: 720px) {
		.folder-grid,
		.host-grid {
			grid-template-columns: 1fr;
		}
	}
</style>
