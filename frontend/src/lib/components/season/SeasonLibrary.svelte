<script lang="ts">
	import { resolve } from '$app/paths';
	import { onMount } from 'svelte';
	import { postJson } from '$lib/api/client';
	import type {
		DashboardFoldersPayload,
		DashboardSummaryPayload,
		FolderCard,
		LifecycleState,
		SeasonLifecycleState
	} from '$lib/api/types';
	import {
		folderHref,
		formatFileSize,
		librarySeasonState,
		seasonIdentity,
		seasonNumberLabel
	} from '$lib/season/experience';
	import {
		buildShowCards,
		compareSeasonCards,
		filterShowCards,
		olderSeasonLibraryAction,
		savingsPercent,
		seasonsByShow,
		sortShowCards,
		tvLibraryStateGroup,
		type LibrarySort,
		type TvLibraryStateKey
	} from '$lib/season/library';
	import LibraryLayout from '$lib/components/workstation/LibraryLayout.svelte';
	import StateBadge from '$lib/components/workstation/StateBadge.svelte';
	import { summarizeWorkStates } from '$lib/components/workstation/library-layout';
	import type {
		LibraryMetric,
		LibraryNotice,
		LibraryTone,
		LibraryWorkSegment
	} from '$lib/components/workstation/library-layout';

	type LifecycleMode = 'auto' | 'on' | 'off';

	let {
		dashboard,
		foldersPayload,
		foldersPending = false,
		detailsPending = false,
		loadError = '',
		detailsError = '',
		onLifecycleSaved = () => undefined
	}: {
		dashboard: DashboardSummaryPayload;
		foldersPayload: DashboardFoldersPayload;
		foldersPending?: boolean;
		detailsPending?: boolean;
		loadError?: string;
		detailsError?: string;
		onLifecycleSaved?: (lifecycle: LifecycleState) => void;
	} = $props();

	let query = $state('');
	let stateFilter = $state<'all' | TvLibraryStateKey>('all');
	let sortMode = $state<LibrarySort>('size');
	let selectedShowPrefix = $state('');
	let detailExpanded = $state(true);
	let pendingLifecycleMode = $state<{ prefix: string; mode: LifecycleMode } | null>(null);
	let policySavingPrefix = $state('');
	let policySavingTitle = $state('');
	let policyError = $state('');

	onMount(() => {
		if (window.matchMedia('(max-width: 760px)').matches) detailExpanded = false;
	});

	const seasonCards = $derived(
		foldersPayload.folders.filter(
			(card) => card.scope_label.toLowerCase() === 'season' || /\/season\s+\d+$/i.test(card.prefix)
		)
	);
	const groupedSeasons = $derived(seasonsByShow(seasonCards));
	const showCards = $derived(buildShowCards(foldersPayload.series_folders ?? [], seasonCards));
	const searchMatchedShows = $derived(filterShowCards(showCards, groupedSeasons, query));
	const filteredShows = $derived(
		sortShowCards(
			searchMatchedShows.filter(
				(show) => stateFilter === 'all' || showLibraryStateGroup(show.prefix).key === stateFilter
			),
			groupedSeasons,
			sortMode
		)
	);
	const selectedShow = $derived(
		filteredShows.find((show) => show.prefix === selectedShowPrefix) ?? filteredShows[0]
	);
	const selectedShowIndex = $derived(
		selectedShow ? filteredShows.findIndex((show) => show.prefix === selectedShow.prefix) : -1
	);
	const selectedSeasons = $derived(
		selectedShow
			? [...(groupedSeasons.get(selectedShow.prefix) ?? [])].sort(compareSeasonCards)
			: []
	);
	const selectedSeasonPreview = $derived.by(() => {
		if (selectedSeasons.length <= 4) return selectedSeasons;
		return [...selectedSeasons.slice(0, 2), ...selectedSeasons.slice(-2)];
	});
	const hiddenSelectedSeasonCount = $derived(
		Math.max(0, selectedSeasons.length - selectedSeasonPreview.length)
	);
	const selectedLifecycle = $derived(selectedShow?.lifecycle ?? null);
	const olderSeasonAction = $derived(
		selectedLifecycle ? olderSeasonLibraryAction(selectedLifecycle) : null
	);
	const displayedLifecycleMode = $derived<LifecycleMode>(
		pendingLifecycleMode?.prefix === selectedShow?.prefix
			? pendingLifecycleMode.mode
			: (selectedLifecycle?.policy_mode ?? 'auto')
	);
	const policySaving = $derived(Boolean(policySavingPrefix));
	const lifecycleAvailable = $derived(selectedLifecycle !== null);
	const eligibleEpisodeCount = $derived(selectedLifecycle?.eligible_candidate_count ?? 0);
	const heldEpisodeCount = $derived(selectedLifecycle?.held_candidate_count ?? 0);
	const selectedShowActionAvailable = $derived(
		Boolean(
			olderSeasonAction ||
			(selectedShow &&
				selectedSeasons.length > 1 &&
				(!lifecycleAvailable || eligibleEpisodeCount > 0))
		)
	);
	const librarySize = $derived(
		seasonCards.reduce((total, card) => total + Math.max(0, card.total_size_bytes), 0)
	);
	const librarySavings = $derived(
		seasonCards.reduce((total, card) => total + Math.max(0, card.projected_reclaim_bytes), 0)
	);
	const librarySavingsReady = $derived(
		!detailsPending && seasonCards.every((card) => !card.details_loading)
	);
	const libraryOutput = $derived(Math.max(0, librarySize - librarySavings));
	const libraryMetrics = $derived<LibraryMetric[]>([
		{
			value: `${seasonCards.length}`,
			label: seasonCards.length === 1 ? 'Season' : 'Seasons',
			detail: countLabel(showCards.length, 'show')
		},
		{ value: librarySizeLabel(librarySize), label: 'Current size' },
		{
			value: librarySavingsReady ? librarySizeLabel(libraryOutput) : '…',
			label: 'Estimated output',
			detail: librarySavingsReady ? 'Approximate' : undefined,
			pending: !librarySavingsReady
		},
		{
			value: librarySavingsReady ? librarySizeLabel(librarySavings) : '…',
			label: 'Estimated space saved',
			detail: librarySavingsReady ? 'Approximate' : undefined,
			pending: !librarySavingsReady
		}
	]);
	const libraryWorkSegments = $derived<LibraryWorkSegment[]>(
		summarizeWorkStates(showCards, (show) => showLibraryStateGroup(show.prefix))
	);
	const libraryNotices = $derived.by<LibraryNotice[]>(() => {
		const notices: LibraryNotice[] = [];
		if (loadError)
			notices.push({ title: 'TV Library could not open', detail: loadError, tone: 'fail' });
		if (detailsError) {
			notices.push({
				title: 'TV Library is ready',
				detail: `${detailsError} Names, episode counts, and current sizes are still available.`
			});
		}
		return notices;
	});

	$effect(() => {
		const available = filteredShows;
		if (available.length === 0) {
			selectedShowPrefix = '';
			return;
		}
		if (!available.some((show) => show.prefix === selectedShowPrefix)) {
			selectedShowPrefix = available[0].prefix;
		}
	});

	function seasonCount(showPrefix: string): number {
		return groupedSeasons.get(showPrefix)?.length ?? 0;
	}

	function countLabel(count: number, singular: string): string {
		return `${count} ${count === 1 ? singular : `${singular}s`}`;
	}

	function fullyHeld(card: FolderCard): boolean {
		return Boolean(
			card.lifecycle &&
			card.lifecycle.held_candidate_count > 0 &&
			card.lifecycle.eligible_candidate_count === 0
		);
	}

	function librarySizeLabel(bytes: number): string {
		if (bytes >= 1024 ** 4) {
			return `${(bytes / 1024 ** 4).toLocaleString('en-US', { maximumFractionDigits: 1 })} TB`;
		}
		return formatFileSize(bytes);
	}

	function normalizeTone(tone: string): LibraryTone {
		if (tone === 'attention' || tone === 'fail') return 'fail';
		if (tone === 'success' || tone === 'ready') return 'ready';
		if (tone === 'active') return 'active';
		if (tone === 'wait') return 'wait';
		return 'idle';
	}

	function showLibraryStateGroup(showPrefix: string) {
		const states = (groupedSeasons.get(showPrefix) ?? []).map((season) =>
			librarySeasonState(season, dashboard)
		);
		return tvLibraryStateGroup(states);
	}

	function selectWorkSegment(key: string) {
		query = '';
		stateFilter = stateFilter === key ? 'all' : (key as TvLibraryStateKey);
	}

	function clearFilters() {
		query = '';
		stateFilter = 'all';
	}

	function encodePrefix(prefix: string): string {
		return prefix
			.split('/')
			.map((segment) => encodeURIComponent(segment))
			.join('/');
	}

	function providerStateCopy(): string {
		const lifecycle = selectedLifecycle;
		if (!lifecycle)
			return detailsPending ? 'Checking lifecycle policy' : 'Lifecycle policy unavailable';
		if (lifecycle.provider_state === 'active') return lifecycle.provider_status || 'Active series';
		if (lifecycle.provider_state === 'ended') return lifecycle.provider_status || 'Ended series';
		if (lifecycle.provider_state === 'stale') return 'Cached status is stale';
		return 'Series status unknown';
	}

	function lifecycleModeCopy(): string {
		switch (displayedLifecycleMode) {
			case 'on':
				return 'On always protects the highest numbered season. For an active series, this matches Auto.';
			case 'off':
				return 'Off skips current-season protection. Recent additions can still be held.';
			default:
				return 'Auto uses series status. Active, unknown, or stale status protects the current season.';
		}
	}

	function seasonHoldCopy(season: SeasonLifecycleState | undefined): string {
		if (!season?.held_candidate_count) return '';
		const reasonCodes = new Set(season.hold_reasons.map((reason) => reason.code));
		if (reasonCodes.has('current_season') && reasonCodes.has('recent_acquisition')) {
			return 'Current + recent · held';
		}
		if (reasonCodes.has('current_season')) return 'Current · held';
		if (reasonCodes.has('recent_acquisition')) return 'Recent · held';
		return `${season.held_candidate_count} held`;
	}

	async function saveLifecycleMode(event: Event) {
		if (!selectedShow || policySaving) return;
		const show = selectedShow;
		const mode = (event.currentTarget as HTMLSelectElement).value as LifecycleMode;
		pendingLifecycleMode = { prefix: show.prefix, mode };
		policySavingPrefix = show.prefix;
		policySavingTitle = show.title;
		policyError = '';
		try {
			const response = await postJson<{
				ok: boolean;
				message?: string;
				lifecycle?: LifecycleState;
			}>(`/api/folders/${encodePrefix(show.prefix)}/series-lifecycle`, { mode });
			if (!response.ok) throw new Error(response.message || 'Lifecycle policy could not be saved.');
			if (!response.lifecycle) throw new Error('Saved lifecycle policy was not returned.');
			onLifecycleSaved(response.lifecycle);
		} catch (error) {
			policyError = `${show.title}: ${
				error instanceof Error ? error.message : 'Lifecycle policy could not be saved.'
			}`;
		} finally {
			if (pendingLifecycleMode?.prefix === show.prefix) pendingLifecycleMode = null;
			if (policySavingPrefix === show.prefix) {
				policySavingPrefix = '';
				policySavingTitle = '';
			}
		}
	}

	function selectShow(prefix: string, revealInspector = false) {
		selectedShowPrefix = prefix;
		detailExpanded = revealInspector && !window.matchMedia('(max-width: 760px)').matches;
	}

	function toggleShowDetail(prefix: string) {
		if (selectedShow?.prefix === prefix) detailExpanded = !detailExpanded;
		else {
			selectedShowPrefix = prefix;
			detailExpanded = true;
		}
		requestAnimationFrame(() => {
			document
				.querySelector<HTMLElement>(`[data-tv-show-row="${CSS.escape(prefix)}"]`)
				?.scrollIntoView({ block: 'nearest' });
		});
	}
</script>

<svelte:head>
	<title>TV Library · Mediaforce</title>
	<meta
		name="description"
		content="Choose a TV season or show, make one test, compare it, and then make the rest smaller."
	/>
</svelte:head>

<LibraryLayout
	active="tv"
	title="TV Library"
	metrics={libraryMetrics}
	workSegments={libraryWorkSegments}
	activeWorkSegment={stateFilter === 'all' ? '' : stateFilter}
	onWorkSegmentSelect={selectWorkSegment}
	notices={libraryNotices}
	toolbarSummary={`${filteredShows.length.toLocaleString('en-US')} of ${showCards.length.toLocaleString('en-US')} shows`}
	loading={foldersPending || detailsPending}
>
	{#snippet toolbar()}
		<label class="search-field">
			<span class="sr-only">Find a show or season</span>
			<input bind:value={query} type="search" placeholder="Find a show or season" />
		</label>
		<label>
			<span>State</span>
			<select bind:value={stateFilter} aria-label="Filter shows by state">
				<option value="all">All states</option>
				<option value="attention">Needs attention</option>
				<option value="processing">In progress</option>
				<option value="ready">Ready to act on</option>
				<option value="idle">No active work</option>
			</select>
		</label>
		<label class="sort-field">
			<span>Sort</span>
			<select bind:value={sortMode} aria-label="Sort shows">
				<option value="savings" disabled={!librarySavingsReady}>
					Most estimated space saved{librarySavingsReady ? '' : ' (estimating)'}
				</option>
				<option value="size">Largest library</option>
				<option value="seasons">Most seasons</option>
				<option value="name">Name A–Z</option>
			</select>
		</label>
	{/snippet}

	{#if foldersPending && !seasonCards.length}
		<div class="loading-state" role="status" aria-live="polite">
			<span></span><span></span><span></span>
			<p>Loading TV library…</p>
		</div>
	{:else if !filteredShows.length}
		<div class="empty-state">
			{#if query.trim() || stateFilter !== 'all'}
				<h3>No TV shows match these filters.</h3>
				<p>Try another title, season, or workflow state.</p>
				<button class="secondary-button" type="button" onclick={clearFilters}>Clear filters</button>
			{:else}
				<h3>No TV shows or seasons found.</h3>
				<p>Choose a TV folder in Settings, then scan it to see shows and seasons here.</p>
				<a class="primary-button" href={resolve('/settings')}>Open Settings</a>
			{/if}
		</div>
	{:else}
		<div class="library-register">
			<div class="show-list__head library-register__header" aria-hidden="true">
				<span>Show</span><span>Seasons</span><span>Current size</span><span>Est. saved</span><span
					>State</span
				>
			</div>
			<div class="library-browser library-workbench">
				<div class="show-list library-index">
					{#each filteredShows as show, showIndex (show.prefix)}
						{@const showState = showLibraryStateGroup(show.prefix)}
						<div class="ledger-row">
							<button
								type="button"
								class="show-row"
								class:selected={show.prefix === selectedShow?.prefix}
								data-tv-show-row={show.prefix}
								data-library-state={showState.key}
								aria-pressed={show.prefix === selectedShow?.prefix}
								onclick={() => selectShow(show.prefix, true)}
							>
								<span class="show-copy">
									<strong>{show.title}</strong>
									<small>{countLabel(show.item_count, 'episode')}</small>
								</span>
								<span class="show-count">{seasonCount(show.prefix)}</span>
								<span class="show-size">{formatFileSize(show.total_size_bytes)}</span>
								<span class="show-savings">
									{#if show.details_loading && show.projected_reclaim_bytes <= 0}
										<strong class="metric-pending">…</strong>
										<small>{detailsPending ? 'estimating' : 'estimate unavailable'}</small>
									{:else if fullyHeld(show)}
										<strong>Held</strong>
										<small>no eligible savings</small>
									{:else}
										<strong>~{formatFileSize(show.projected_reclaim_bytes)}</strong>
									{/if}
								</span>
								<StateBadge
									tone={showState.tone}
									label={showState.label}
									compact
									quiet={showState.tone === 'ready' && show.prefix !== selectedShow?.prefix}
								/>
							</button>
							<button
								class="row-inspect"
								type="button"
								aria-controls={`tv-show-detail-${showIndex}`}
								aria-expanded={show.prefix === selectedShow?.prefix && detailExpanded}
								aria-label={`${show.prefix === selectedShow?.prefix && detailExpanded ? 'Close' : 'Inspect'} ${show.title}`}
								onclick={() => toggleShowDetail(show.prefix)}
							>
								<span aria-hidden="true"
									>{show.prefix === selectedShow?.prefix && detailExpanded
										? 'Close'
										: 'Inspect'}</span
								>
							</button>
						</div>
					{/each}
				</div>

				{#if selectedShow && detailExpanded}
					<div
						id={`tv-show-detail-${selectedShowIndex}`}
						class="season-list library-inspector"
						class:season-list--has-action={selectedShowActionAvailable}
						style:grid-row={selectedShowIndex + 2}
						aria-live="polite"
					>
						<div class="season-list__heading">
							<div class="season-list__title">
								<span class="eyebrow">Selected show</span>
								<strong>{selectedShow?.title}</strong>
								<small>
									{countLabel(selectedSeasons.length, 'season')} · {countLabel(
										selectedShow?.item_count ?? 0,
										'episode'
									)}
								</small>
							</div>
							{#if selectedShow}
								<button
									class="detail-collapse"
									type="button"
									onclick={() => (detailExpanded = false)}
								>
									Collapse ↑
								</button>
								<div class="show-summary">
									<span><strong>{formatFileSize(selectedShow.total_size_bytes)}</strong> now</span>
									{#if selectedShow.details_loading && selectedShow.projected_reclaim_bytes <= 0}
										<span class="show-summary__savings"
											><strong class="metric-pending">…</strong>
											{detailsPending ? 'estimating space saved' : 'estimate unavailable'}</span
										>
									{:else if fullyHeld(selectedShow)}
										<span class="show-summary__savings"
											><strong>Held</strong> · no eligible savings</span
										>
									{:else}
										<span class="show-summary__savings"
											><strong>~{formatFileSize(selectedShow.projected_reclaim_bytes)}</strong>
											estimated saved · {savingsPercent(selectedShow)}%</span
										>
									{/if}
								</div>
								<div class="show-policy">
									<label>
										<span>Current-season policy for this show</span>
										{#key selectedShow.prefix}
											<select
												value={displayedLifecycleMode}
												onchange={saveLifecycleMode}
												disabled={policySaving || !lifecycleAvailable}
												aria-describedby="current-season-policy-help"
											>
												<option value="auto">Auto · use series status</option>
												<option value="on">On · protect current season</option>
												<option value="off">Off · no current-season hold</option>
											</select>
										{/key}
										<small id="current-season-policy-help">
											{policySaving
												? `Saving current-season policy for ${policySavingTitle}…`
												: lifecycleModeCopy()}
										</small>
									</label>
									<div>
										<strong>{providerStateCopy()}</strong>
										{#if lifecycleAvailable}
											<span>{eligibleEpisodeCount} eligible · {heldEpisodeCount} held</span>
										{:else}
											<span
												>{detailsPending
													? 'Checking eligibility…'
													: 'Eligibility unavailable'}</span
											>
										{/if}
									</div>
								</div>
								{#if policyError}<p class="policy-error">{policyError}</p>{/if}
							{/if}
						</div>

						{#if selectedShow && olderSeasonAction}
							<div class="show-action show-action--override">
								<div>
									<strong>Process older seasons with one setup</strong>
									<span>
										Include {olderSeasonAction.episodeCount}
										{olderSeasonAction.episodeCount === 1 ? 'episode' : 'episodes'} across
										{olderSeasonAction.seasonCount} older
										{olderSeasonAction.seasonCount === 1 ? 'season' : 'seasons'}.
										{olderSeasonAction.latestSeasonLabel} stays original, and the current-season policy
										does not change. Recent-acquisition holds are bypassed only after confirmation.
									</span>
								</div>
								<a class="primary-button" href={resolve(folderHref(selectedShow.prefix))}
									>Open in Studio</a
								>
							</div>
						{/if}

						{#if selectedShow && !olderSeasonAction && selectedSeasons.length > 1 && (!lifecycleAvailable || eligibleEpisodeCount > 0)}
							<div class="show-action">
								<div>
									<strong>Use one setup for eligible seasons</strong>
									{#if lifecycleAvailable}
										<span
											>Approve one representative test, then make {eligibleEpisodeCount} eligible episodes
											with that choice. {heldEpisodeCount} held episodes stay original.</span
										>
									{:else}
										<span>Mediaforce is checking which seasons the lifecycle policy allows.</span>
									{/if}
								</div>
								{#if lifecycleAvailable && eligibleEpisodeCount > 0}
									<a class="primary-button" href={resolve(folderHref(selectedShow.prefix))}
										>Open in Studio</a
									>
								{:else}
									<button class="primary-button primary-button--disabled" type="button" disabled>
										{lifecycleAvailable ? 'No eligible seasons' : 'Checking eligibility'}
									</button>
								{/if}
							</div>
						{/if}

						{#each selectedSeasonPreview as season, previewIndex (season.prefix)}
							{@const state = librarySeasonState(season, dashboard)}
							{@const seasonName = seasonIdentity(season.prefix).season}
							{@const seasonLifecycle = season.lifecycle?.seasons?.[0]}
							<a
								class="season-row"
								class:season-row--mobile-optional={hiddenSelectedSeasonCount > 0 &&
									(previewIndex === 1 || previewIndex === 2)}
								href={resolve(folderHref(season.prefix))}
							>
								<span class="season-number">{seasonNumberLabel(seasonName)}</span>
								<span class="season-copy">
									<strong>{seasonName}</strong>
									<small>
										{countLabel(season.item_count, 'episode')} · {formatFileSize(
											season.total_size_bytes
										)} now
									</small>
								</span>
								<span class="season-savings">
									{#if season.details_loading}
										<strong class="metric-pending">…</strong>
										<small
											>{detailsPending ? 'estimating space saved' : 'estimate unavailable'}</small
										>
									{:else if seasonLifecycle?.held_candidate_count && !seasonLifecycle.eligible_candidate_count}
										<strong>Held</strong>
										<small>stays original</small>
									{:else}
										<strong>~{formatFileSize(season.projected_reclaim_bytes)}</strong>
										<small>estimated saved · {savingsPercent(season)}%</small>
									{/if}
								</span>
								{#if seasonLifecycle?.held_candidate_count}
									<span
										class="season-state"
										title={seasonLifecycle.hold_reasons
											.map((reason) => `${reason.label}: ${reason.detail}`)
											.join(' ')}
									>
										<StateBadge tone="wait" label={seasonHoldCopy(seasonLifecycle)} compact />
									</span>
								{:else if state.key !== 'needs_test'}
									<span class="season-state">
										<StateBadge tone={normalizeTone(state.tone)} label={state.label} compact />
									</span>
								{/if}
								<svg viewBox="0 0 20 20" aria-hidden="true"><path d="m7 4.5 5.5 5.5L7 15.5" /></svg>
							</a>
						{/each}

						{#if selectedShow && hiddenSelectedSeasonCount > 0}
							<div class="season-list__more">
								<span class="season-list__more-desktop"
									>{hiddenSelectedSeasonCount} more seasons are available.</span
								>
								<span class="season-list__more-mobile"
									>{selectedSeasons.length - 2} more seasons are available.</span
								>
								<a href={resolve(folderHref(selectedShow.prefix))}
									>View all {selectedSeasons.length} seasons in Studio →</a
								>
							</div>
						{/if}

						{#if !selectedSeasons.length}
							<div class="season-list__empty">No seasons are available for this show.</div>
						{/if}
					</div>
				{/if}
			</div>
		</div>
	{/if}
</LibraryLayout>

<style>
	:global(html) {
		background: var(--mf-bg-base);
	}

	.eyebrow {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-semibold);
		letter-spacing: 0.08em;
		text-transform: uppercase;
	}

	.library-browser {
		display: grid;
		grid-auto-flow: row;
		grid-template-columns: minmax(0, 1fr);
		min-width: 0;
	}

	.show-list {
		display: contents;
	}

	.show-list__head,
	.show-row {
		display: grid;
		grid-template-columns: minmax(320px, 1fr) 88px 128px 128px 180px;
	}

	.show-list__head {
		background: var(--mf-bg-panel);
		border-bottom: 1px solid var(--mf-line-strong);
		color: var(--mf-fg-tertiary);
		font-size: 10px;
		font-weight: var(--mf-weight-semibold);
		letter-spacing: 0.1em;
		padding: var(--mf-space-4) var(--mf-space-6);
		position: sticky;
		text-transform: uppercase;
		top: 0;
		z-index: 2;
	}

	.ledger-row {
		position: relative;
	}

	.show-row {
		align-items: center;
		border: 0;
		border-bottom: 1px solid var(--mf-line-muted);
		border-radius: 0;
		color: var(--mf-fg-secondary);
		gap: var(--mf-space-5);
		min-height: 56px;
		padding: var(--mf-space-4) var(--mf-space-6);
		text-align: left;
		width: 100%;
	}

	.show-row:hover {
		background: var(--mf-bg-panel-2);
		color: var(--mf-fg-primary);
	}

	.show-row.selected {
		background: var(--mf-bg-panel-2);
		box-shadow: inset 3px 0 var(--mf-active-fg);
		color: var(--mf-fg-primary);
	}

	.show-copy {
		display: grid;
		flex: 1;
		gap: 1px;
		min-width: 0;
	}

	.show-copy strong {
		font-size: 14px;
		font-weight: var(--mf-weight-semibold);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.show-copy small {
		color: var(--mf-fg-tertiary);
		font-size: 12px;
	}

	.show-count,
	.show-size {
		font-size: 12px;
		font-variant-numeric: tabular-nums;
		text-align: right;
	}

	.show-savings,
	.season-savings {
		display: grid;
		flex: 0 0 auto;
		gap: 1px;
		justify-items: end;
	}

	.show-savings {
		min-width: 70px;
	}

	.season-savings {
		min-width: 104px;
	}

	.show-savings strong,
	.season-savings strong {
		color: var(--mf-fg-primary);
		font-size: var(--mf-text-xs);
		font-variant-numeric: tabular-nums;
		font-weight: 600;
	}

	.show-savings small,
	.season-savings small {
		color: var(--mf-fg-tertiary);
		font-size: 10px;
		white-space: nowrap;
	}

	.show-row.selected .show-savings strong {
		color: var(--mf-active-fg);
	}

	.show-row :global(.state-badge) {
		max-width: 100%;
		white-space: nowrap;
	}

	.row-inspect {
		display: none;
	}

	.metric-pending,
	.show-summary__savings .metric-pending {
		color: var(--mf-fg-tertiary);
		font-variant-numeric: normal;
	}

	.season-row > svg {
		fill: none;
		height: 17px;
		stroke: currentColor;
		stroke-linecap: round;
		stroke-linejoin: round;
		stroke-width: 1.5;
		width: 17px;
	}

	.season-list {
		display: grid;
		gap: var(--mf-space-6) var(--mf-space-7);
		grid-column: 1;
		grid-template-columns: minmax(260px, 0.8fr) minmax(0, 1.2fr);
		padding: var(--mf-space-7) var(--mf-space-6) var(--mf-space-8);
		box-shadow:
			inset 3px 0 var(--mf-active-fg),
			0 10px 24px color-mix(in srgb, var(--mf-fg-primary) 8%, transparent);
	}

	.season-list--has-action {
		grid-template-columns: minmax(260px, 0.8fr) minmax(0, 1.2fr);
	}

	.season-list__heading {
		align-items: start;
		display: grid;
		gap: var(--mf-space-7);
		grid-column: 1 / -1;
		grid-template-columns: minmax(260px, 0.8fr) minmax(0, 1.2fr);
		position: relative;
	}

	.season-list--has-action .season-list__heading {
		grid-template-columns: minmax(260px, 0.8fr) minmax(0, 1.2fr);
	}

	.season-list__title {
		display: grid;
		gap: 2px;
		grid-column: 1;
		grid-row: 1 / span 2;
	}

	.season-list__heading span,
	.season-list__heading small {
		color: var(--mf-fg-tertiary);
		font-size: 12px;
	}

	.season-list__heading strong {
		font-size: 22px;
		font-weight: var(--mf-weight-semibold);
	}

	.show-summary {
		display: grid;
		gap: 2px;
		grid-column: 2;
		grid-row: 1;
		justify-items: start;
		min-width: 138px;
		text-align: left;
	}

	.show-summary span {
		color: var(--mf-fg-tertiary);
		font-size: 11px;
	}

	.show-summary strong {
		font-size: 12px;
		font-variant-numeric: tabular-nums;
	}

	.show-summary__savings,
	.show-summary__savings strong {
		color: var(--mf-ready-fg);
	}

	.show-policy {
		align-items: start;
		display: flex;
		gap: var(--mf-space-5);
		grid-column: 2;
		grid-row: 2;
	}

	.detail-collapse {
		background: transparent;
		border: 0;
		border-radius: var(--mf-radius-2);
		color: var(--mf-fg-secondary);
		font-size: var(--mf-text-xs);
		padding: var(--mf-space-2) var(--mf-space-3);
		position: absolute;
		right: 0;
		top: 0;
	}

	.show-policy label,
	.show-policy > div {
		display: grid;
		gap: var(--mf-space-1);
	}

	.show-policy label {
		min-width: 0;
	}

	.show-policy label small {
		color: var(--mf-fg-tertiary);
		font-size: 11px;
		line-height: 1.35;
		max-width: 360px;
	}

	.show-policy select {
		background: var(--mf-bg-panel-2);
		border: 1px solid var(--mf-line-strong);
		border-radius: var(--mf-radius-1);
		color: var(--mf-fg-primary);
		font: inherit;
		min-height: 30px;
		padding: 3px 28px 3px 8px;
		width: 100%;
	}

	.show-policy strong {
		font-size: 12px;
	}

	.show-policy span {
		font-size: 11px;
	}

	.policy-error {
		color: var(--mf-fail-fg);
		font-size: 12px;
		margin: 0;
	}

	.show-action {
		align-self: start;
		align-items: center;
		border-top: 1px solid var(--mf-line-strong);
		display: grid;
		gap: var(--mf-space-6);
		grid-column: 1 / -1;
		grid-row: auto;
		grid-template-columns: minmax(0, 1fr) auto;
		margin: 0;
		padding: var(--mf-space-6) 0 0;
	}

	.show-action > div {
		display: grid;
		gap: 2px;
	}

	.show-action strong {
		color: var(--mf-ready-fg);
		font-size: 13px;
	}

	.show-action span {
		color: var(--mf-fg-secondary);
		font-size: 12px;
	}

	.show-action--override {
		border-left-color: var(--mf-line-strong);
	}

	.show-action--override strong {
		color: var(--mf-wait-fg);
	}

	.show-action .primary-button {
		flex: 0 0 auto;
		justify-self: start;
		min-height: 34px;
		white-space: nowrap;
	}

	.season-row {
		align-items: center;
		border-top: 1px solid var(--mf-line-muted);
		color: var(--mf-fg-primary);
		display: flex;
		gap: var(--mf-space-5);
		grid-column: 1 / -1;
		min-height: 56px;
		padding: var(--mf-space-4) var(--mf-space-2);
		text-decoration: none;
	}

	.season-row:hover {
		background: var(--mf-bg-panel-2);
		color: var(--mf-active-fg);
	}

	.season-number {
		align-items: center;
		background: var(--mf-bg-raised);
		border-radius: 8px;
		color: var(--mf-fg-secondary);
		display: inline-flex;
		font-size: 13px;
		font-weight: 600;
		height: 38px;
		justify-content: center;
		min-width: 38px;
	}

	.season-copy {
		display: grid;
		flex: 1;
		gap: 2px;
	}

	.season-copy small {
		color: var(--mf-fg-tertiary);
		font-size: 12px;
	}

	.primary-button,
	.secondary-button {
		align-items: center;
		border-radius: var(--mf-radius-2);
		display: inline-flex;
		font-size: 14px;
		font-weight: 600;
		justify-content: center;
		min-height: 38px;
		padding: 0 15px;
		text-decoration: none;
	}

	.primary-button {
		background: var(--mf-active-solid);
		color: var(--mf-active-contrast);
	}

	.primary-button:hover {
		background: var(--mf-active-solid-hi);
		color: var(--mf-active-contrast);
	}

	.primary-button--disabled {
		cursor: default;
		opacity: 0.62;
	}

	.secondary-button {
		background: var(--mf-bg-panel);
		border: 1px solid var(--mf-line-strong);
		color: var(--mf-fg-primary);
	}

	.loading-state,
	.empty-state {
		align-items: center;
		display: flex;
		flex-direction: column;
		gap: 9px;
		justify-content: center;
		min-height: 360px;
		padding: 32px;
		text-align: center;
	}

	.loading-state {
		flex-direction: row;
	}

	.loading-state span {
		animation: pulse 1.1s ease-in-out infinite;
		background: var(--mf-active-fg);
		border-radius: 50%;
		height: 6px;
		width: 6px;
	}

	.loading-state span:nth-child(2) {
		animation-delay: 120ms;
	}

	.loading-state span:nth-child(3) {
		animation-delay: 240ms;
	}

	.empty-state h3 {
		font-size: 17px;
	}

	.empty-state p,
	.season-list__empty {
		grid-column: 1 / -1;
		color: var(--mf-fg-secondary);
		font-size: 14px;
	}

	.season-list__more {
		align-items: center;
		border-top: 1px solid var(--mf-line-strong);
		color: var(--mf-fg-secondary);
		display: flex;
		font-size: var(--mf-text-xs);
		gap: var(--mf-space-5);
		grid-column: 1 / -1;
		justify-content: space-between;
		padding: var(--mf-space-5) var(--mf-space-2) 0;
	}

	.season-list__more a {
		color: var(--mf-active-fg);
		font-weight: var(--mf-weight-semibold);
		text-decoration: none;
		white-space: nowrap;
	}

	.season-list__more-mobile {
		display: none;
	}

	.season-list__empty {
		border-top: 1px solid var(--mf-line-muted);
		padding: 28px 4px;
	}

	.sr-only {
		clip: rect(0, 0, 0, 0);
		clip-path: inset(50%);
		height: 1px;
		overflow: hidden;
		position: absolute;
		white-space: nowrap;
		width: 1px;
	}

	button:focus-visible,
	a:focus-visible,
	input:focus-visible,
	select:focus-visible {
		box-shadow: var(--mf-ring-focus);
		outline: none;
	}

	@keyframes pulse {
		0%,
		100% {
			opacity: 0.3;
			transform: translateY(1px);
		}
		50% {
			opacity: 1;
			transform: translateY(-1px);
		}
	}

	@media (min-width: 1101px) {
		.show-policy {
			border-top: 1px solid var(--mf-line-muted);
			justify-content: space-between;
			padding: 10px 0 0;
			width: 100%;
		}

		.show-policy label {
			flex: 1;
		}

		.season-row {
			display: grid;
			grid-template-columns: 38px minmax(180px, 1fr) minmax(140px, auto) auto 17px;
		}

		.season-number {
			grid-column: 1;
			grid-row: 1;
		}

		.season-copy {
			grid-column: 2;
			grid-row: 1;
			min-width: 0;
		}

		.season-savings {
			grid-column: 3;
			grid-row: 1;
			justify-items: start;
			min-width: 0;
		}

		.season-state {
			grid-column: 4;
			grid-row: 1;
		}

		.season-row > svg {
			grid-column: 5;
			grid-row: 1;
			justify-self: end;
		}
	}

	@media (max-width: 1100px) {
		.show-list__head,
		.show-row {
			grid-template-columns: minmax(260px, 1fr) 80px 118px 178px;
		}

		.show-list__head > :nth-child(4),
		.show-row > :nth-child(4) {
			display: none;
		}

		.season-list,
		.season-list.season-list--has-action {
			grid-template-columns: minmax(210px, 0.75fr) minmax(320px, 1.25fr);
		}

		.season-list__heading,
		.season-list--has-action .season-list__heading {
			grid-template-columns: minmax(210px, 0.75fr) minmax(320px, 1.25fr);
		}

		.show-action {
			grid-column: 1 / -1;
			grid-row: auto;
		}
	}

	@media (max-width: 760px) {
		.show-list__head {
			border: 0;
			height: 0;
			overflow: hidden;
			padding: 0;
			visibility: hidden;
		}

		.show-row {
			grid-template-columns: minmax(0, 1fr) auto;
			min-height: 68px;
			padding-right: 88px;
		}

		.show-count,
		.show-size,
		.show-savings {
			display: none;
		}

		.season-list,
		.season-list.season-list--has-action {
			grid-template-columns: minmax(0, 1fr);
			padding: var(--mf-space-6) var(--mf-space-5) var(--mf-space-7);
		}

		.season-list__heading,
		.season-list--has-action .season-list__heading {
			grid-template-columns: minmax(0, 1fr);
		}

		.season-list__title,
		.show-summary,
		.show-policy,
		.season-row,
		.season-list__empty,
		.season-list__more,
		.show-action {
			grid-column: 1;
			grid-row: auto;
		}

		.show-policy {
			border-top: 1px solid var(--mf-line-muted);
			justify-content: space-between;
			padding: 10px 0 0;
			width: 100%;
		}

		.show-action {
			align-items: stretch;
			grid-template-columns: minmax(0, 1fr);
		}

		.show-action .primary-button {
			width: 100%;
		}

		.row-inspect {
			background: var(--mf-bg-panel);
			border: 1px solid var(--mf-line-strong);
			border-radius: var(--mf-radius-2);
			bottom: var(--mf-space-3);
			color: var(--mf-fg-secondary);
			display: inline-flex;
			font-size: 11px;
			min-height: 26px;
			padding: 0 var(--mf-space-2);
			position: absolute;
			right: var(--mf-space-3);
		}

		.season-list__more {
			align-items: flex-start;
			flex-direction: column;
			gap: var(--mf-space-2);
		}

		.season-list__more a {
			white-space: normal;
		}

		.season-row--mobile-optional,
		.season-list__more-desktop {
			display: none;
		}

		.season-list__more-mobile {
			display: inline;
		}
	}

	@media (max-width: 440px) {
		.primary-button {
			width: 100%;
		}

		.show-summary {
			justify-items: start;
			min-width: 0;
			text-align: left;
		}

		.season-row {
			align-items: flex-start;
			display: grid;
			gap: 9px 12px;
			grid-template-columns: 38px minmax(0, 1fr) 17px;
		}

		.season-number {
			grid-column: 1;
			grid-row: 1 / span 3;
		}

		.season-copy {
			grid-column: 2;
			grid-row: 1;
			min-width: 0;
		}

		.season-savings {
			grid-column: 2;
			grid-row: 2;
			justify-items: start;
			margin-left: 0;
		}

		.season-state {
			grid-column: 2;
			grid-row: 3;
			margin-left: 0;
			max-width: none;
		}

		.season-row > svg {
			align-self: center;
			grid-column: 3;
			grid-row: 1 / span 3;
			justify-self: end;
		}

		.season-row.season-row--mobile-optional {
			display: none;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.loading-state span {
			animation: none;
		}
	}
</style>
