<script lang="ts">
	import type { FolderCard as FolderCardData } from '$lib/api/types';
	import FolderCard from '$lib/components/FolderCard.svelte';
	import Panel from '$lib/components/Panel.svelte';
	import SectionHead from '$lib/components/SectionHead.svelte';
	import { folderLibraryKey, folderLibraryThemeStyle } from '$lib/folder-display';

	type FolderLibrary = { key: string; label: string; count: number };

	let {
		catalogScanActive,
		catalogScanLikelyStalled,
		catalogScanHeading,
		catalogScanProgressHeadline,
		catalogScanStatusCopy,
		catalogScanProgressFacts,
		folderLoadState,
		folders,
		folderLoadError,
		folderLibraries,
		disabledLibraries,
		onEnableAllLibraries,
		onToggleLibraryFilter,
		libraryColors,
		filterHintCopy,
		visibleFolders,
		catalogEmpty
	}: {
		catalogScanActive: boolean;
		catalogScanLikelyStalled: boolean;
		catalogScanHeading: string;
		catalogScanProgressHeadline: string;
		catalogScanStatusCopy: string;
		catalogScanProgressFacts: string[];
		folderLoadState: 'loading' | 'ready' | 'error';
		folders: FolderCardData[];
		folderLoadError: string | null;
		folderLibraries: FolderLibrary[];
		disabledLibraries: string[];
		onEnableAllLibraries: () => void;
		onToggleLibraryFilter: (libraryKey: string) => void;
		libraryColors: Record<string, string>;
		filterHintCopy: string;
		visibleFolders: FolderCardData[];
		catalogEmpty: boolean;
		} = $props();

		const libraryFiltersActive = $derived(disabledLibraries.length > 0);
		const folderRankingPending = $derived(folderLoadState === 'loading' && folders.length > 0);
		const totalFolderCount = $derived(folders.length);
		const visibleFolderCount = $derived(visibleFolders.length);
		const libraryControlSummary = $derived.by(() => {
			if (visibleFolderCount === 0 && totalFolderCount > 0 && libraryFiltersActive) {
				return 'No folders match current filters';
			}
			return `Showing ${visibleFolderCount} of ${totalFolderCount}`;
		});
</script>

<Panel variant="inset" class="folder-section" padding="1.2rem 1.3rem 1.4rem">
	<div class="section-stack">
		<div class="section-header-row">
				<SectionHead
					eyebrow="Folders"
					heading="Choose a folder to tune"
					lede="Start here. Folders stay sorted by estimated reclaim so the strongest space-saving bets rise first."
					size="compact"
				/>
					<div class="folder-header-side">
						<p class="section-kicker muted-copy">Open a folder to start representative-file tuning before you dive into fleet telemetry.</p>
						{#if folderRankingPending}
							<span class="section-status-chip" role="status" aria-live="polite" aria-atomic="true">Ranking details are still finishing</span>
						{/if}
					</div>
				</div>
			{#if catalogScanActive && !catalogScanLikelyStalled}
				<div class="catalog-refresh-inline" role="status" aria-live="polite" aria-atomic="true">
					<div class="catalog-refresh-inline-main">
						<span class="catalog-refresh-chip live">Library refresh live</span>
						<p class="muted-copy catalog-refresh-inline-copy">{catalogScanProgressHeadline}</p>
					</div>
					{#if catalogScanProgressFacts.length > 0}
						<div class="catalog-refresh-facts" aria-label="Library scan progress details">
							{#each catalogScanProgressFacts as fact (fact)}
								<span>{fact}</span>
							{/each}
						</div>
					{/if}
				</div>
			{:else if catalogScanLikelyStalled}
				<div
					class={`catalog-refresh-banner ${catalogScanLikelyStalled ? 'stalled' : 'live'}`.trim()}
					role="status"
				aria-live="polite"
			>
					<div class="catalog-refresh-header">
						<p class="eyebrow-copy">Library Refresh</p>
						<span class="catalog-refresh-chip stalled">No recent progress</span>
					</div>
					<p>{catalogScanHeading}</p>
					<p class="muted-copy">{catalogScanProgressHeadline}</p>
				<p class="muted-copy">{catalogScanStatusCopy}</p>
				{#if catalogScanProgressFacts.length > 0}
					<div class="catalog-refresh-facts" aria-label="Library scan progress details">
						{#each catalogScanProgressFacts as fact (fact)}
							<span>{fact}</span>
						{/each}
					</div>
				{/if}
			</div>
		{/if}
			{#if folderLoadState === 'error'}
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
						onclick={onEnableAllLibraries}
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
							onclick={() => onToggleLibraryFilter(library.key)}
						>
							<span>{library.label}</span>
							<span class="library-filter-count">{library.count}</span>
						</button>
					{/each}
				</div>
				<p class="filter-summary-inline muted-copy">
					<span class="filter-summary-status">{libraryControlSummary}</span>
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

<style>
	.section-stack {
		display: grid;
		gap: var(--space-3);
	}

		.section-header-row {
			display: flex;
			justify-content: space-between;
		gap: 1rem;
		align-items: start;
			flex-wrap: wrap;
		}

		.folder-header-side {
			display: grid;
			gap: 0.55rem;
			justify-items: start;
		}

		.section-status-chip {
			display: inline-flex;
			align-items: center;
			padding: 0.38rem 0.68rem;
			border-radius: var(--radius-pill);
			font-size: 0.78rem;
			font-weight: 700;
			background: rgba(23, 35, 31, 0.07);
			color: var(--ink-soft);
			border: 1px solid rgba(23, 35, 31, 0.08);
		}

		.folder-grid {
			display: grid;
			grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
			gap: var(--space-4);
		}

		.catalog-refresh-inline {
			display: flex;
			justify-content: space-between;
			gap: 0.9rem;
			align-items: center;
			flex-wrap: wrap;
			padding: 0.1rem 0;
		}

		.catalog-refresh-inline-main {
			display: flex;
			align-items: center;
			gap: 0.65rem;
			flex-wrap: wrap;
		}

		.catalog-refresh-inline-copy {
			margin: 0;
		}

	.catalog-refresh-banner {
		display: grid;
		gap: 0.38rem;
		padding: 0.78rem 0.88rem;
		border-radius: var(--radius-md);
		background: rgba(180, 83, 9, 0.08);
		border: 1px solid rgba(180, 83, 9, 0.16);
	}

	.catalog-refresh-banner.live {
		background: color-mix(in srgb, rgba(15, 118, 110, 0.12) 72%, white);
		border-color: color-mix(in srgb, rgba(15, 118, 110, 0.24) 82%, white);
	}

	.catalog-refresh-banner.stalled {
		background: rgba(180, 83, 9, 0.12);
		border-color: rgba(180, 83, 9, 0.24);
	}

	.catalog-refresh-header {
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: 0.8rem;
		flex-wrap: wrap;
	}

	.catalog-refresh-banner p {
		margin: 0;
	}

	.catalog-refresh-banner p:nth-child(2) {
		font-weight: 700;
		color: var(--accent-deep);
	}

	.catalog-refresh-chip {
		display: inline-flex;
		align-items: center;
		padding: 0.34rem 0.62rem;
		border-radius: var(--radius-pill);
		font-size: 0.76rem;
		font-weight: 700;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		border: 1px solid rgba(23, 35, 31, 0.12);
		background: rgba(255, 255, 255, 0.74);
		color: var(--ink-soft);
	}

	.catalog-refresh-chip.live {
		border-color: rgba(15, 118, 110, 0.22);
		background: rgba(15, 118, 110, 0.12);
		color: #0f5f59;
	}

	.catalog-refresh-chip.stalled {
		border-color: rgba(180, 83, 9, 0.22);
		background: rgba(180, 83, 9, 0.14);
		color: #8f450a;
	}

	.catalog-refresh-facts {
		display: flex;
		flex-wrap: wrap;
		gap: 0.55rem;
	}

	.catalog-refresh-facts span {
		display: inline-flex;
		align-items: center;
		padding: 0.34rem 0.56rem;
		border-radius: var(--radius-pill);
		font-size: 0.8rem;
		font-weight: 600;
		color: var(--ink-soft);
		background: rgba(255, 255, 255, 0.72);
		border: 1px solid rgba(23, 35, 31, 0.08);
	}

	.library-filter-strip {
		display: grid;
		gap: 0.85rem;
		padding: 0.95rem 1rem;
		border-radius: var(--radius-lg);
		background:
			linear-gradient(180deg, rgba(255, 255, 255, 0.74), rgba(255, 255, 255, 0.52)),
			radial-gradient(circle at top left, rgba(15, 118, 110, 0.08), transparent 46%);
		border: 1px solid rgba(23, 35, 31, 0.08);
	}

	.library-filter-row {
		display: flex;
		flex-wrap: wrap;
		gap: 0.65rem;
	}

	.library-filter-pill {
		display: inline-flex;
		align-items: center;
		gap: 0.55rem;
		padding: 0.52rem 0.8rem;
		border-radius: var(--radius-pill);
		border: 1px solid rgba(23, 35, 31, 0.1);
		background: rgba(255, 255, 255, 0.76);
		color: var(--ink);
		font: inherit;
		font-weight: 700;
		cursor: pointer;
	}

	.library-filter-pill.active {
		background: var(--library-surface, rgba(255, 255, 255, 0.76));
		border-color: var(--library-border, rgba(23, 35, 31, 0.1));
		color: var(--library-text, var(--ink));
		box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--library-base, #0f766e) 10%, white);
	}

	.library-filter-pill.inactive {
		opacity: 0.58;
		background: rgba(255, 255, 255, 0.76);
		border-color: rgba(23, 35, 31, 0.08);
		color: var(--ink-soft);
	}

	.all-pill.active {
		background: rgba(15, 118, 110, 0.1);
		border-color: rgba(15, 118, 110, 0.18);
		color: #0f5f59;
	}

	.library-filter-count {
		font-size: 0.82rem;
		font-weight: 600;
		color: var(--ink-soft);
	}

	.library-filter-pill.active .library-filter-count {
		color: color-mix(in srgb, var(--library-text, var(--ink)) 76%, white);
	}

	.all-pill.active .library-filter-count {
		color: rgba(15, 95, 89, 0.78);
	}

	.filter-summary-inline {
		margin: 0;
		display: flex;
		flex-wrap: wrap;
		gap: 0.45rem;
	}
</style>
