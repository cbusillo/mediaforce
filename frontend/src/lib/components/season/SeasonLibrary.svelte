<script lang="ts">
	import { resolve } from '$app/paths';
	import { invalidateAll } from '$app/navigation';
	import { postJson } from '$lib/api/client';
	import type {
		DashboardFoldersPayload,
		DashboardSummaryPayload,
		FolderCard,
		SeasonLifecycleState
	} from '$lib/api/types';
	import {
		activeSeasonCards,
		folderHref,
		formatFileSize,
		librarySeasonState,
		seasonIdentity,
		seasonNumberLabel
	} from '$lib/season/experience';
	import {
		buildShowCards,
		filterShowCards,
		savingsPercent,
		seasonsByShow,
		sortShowCards,
		type LibrarySort
	} from '$lib/season/library';
	import LibraryModeNav from '$lib/components/workstation/LibraryModeNav.svelte';

	type LifecycleMode = 'auto' | 'on' | 'off';

	let {
		dashboard,
		foldersPayload,
		foldersPending = false,
		detailsPending = false,
		loadError = '',
		detailsError = ''
	}: {
		dashboard: DashboardSummaryPayload;
		foldersPayload: DashboardFoldersPayload;
		foldersPending?: boolean;
		detailsPending?: boolean;
		loadError?: string;
		detailsError?: string;
	} = $props();

	let query = $state('');
	let sortMode = $state<LibrarySort>('size');
	let selectedShowPrefix = $state('');
	let showListElement: HTMLElement | undefined = $state();
	let pendingLifecycleMode = $state<{ prefix: string; mode: LifecycleMode } | null>(null);
	let policySavingPrefix = $state('');
	let policySavingTitle = $state('');
	let policyError = $state('');

	const seasonCards = $derived(
		foldersPayload.folders.filter(
			(card) => card.scope_label.toLowerCase() === 'season' || /\/season\s+\d+$/i.test(card.prefix)
		)
	);
	const groupedSeasons = $derived(seasonsByShow(seasonCards));
	const showCards = $derived(buildShowCards(foldersPayload.series_folders ?? [], seasonCards));
	const filteredShows = $derived(
		sortShowCards(filterShowCards(showCards, groupedSeasons, query), groupedSeasons, sortMode)
	);
	const selectedShow = $derived(
		filteredShows.find((show) => show.prefix === selectedShowPrefix) ?? filteredShows[0]
	);
	const selectedSeasons = $derived(
		selectedShow
			? [...(groupedSeasons.get(selectedShow.prefix) ?? [])].sort(compareSeasonCards)
			: []
	);
	const selectedLifecycle = $derived(selectedShow?.lifecycle ?? null);
	const displayedLifecycleMode = $derived<LifecycleMode>(
		pendingLifecycleMode?.prefix === selectedShow?.prefix
			? pendingLifecycleMode.mode
			: (selectedLifecycle?.policy_mode ?? 'auto')
	);
	const policySaving = $derived(Boolean(policySavingPrefix));
	const lifecycleAvailable = $derived(selectedLifecycle !== null);
	const eligibleEpisodeCount = $derived(selectedLifecycle?.eligible_candidate_count ?? 0);
	const heldEpisodeCount = $derived(selectedLifecycle?.held_candidate_count ?? 0);
	const activeCards = $derived(activeSeasonCards(seasonCards, dashboard).slice(0, 4));
	const resumeCard = $derived(activeCards[0] ?? null);
	const librarySize = $derived(
		seasonCards.reduce((total, card) => total + Math.max(0, card.total_size_bytes), 0)
	);
	const librarySavings = $derived(
		seasonCards.reduce((total, card) => total + Math.max(0, card.projected_reclaim_bytes), 0)
	);
	const librarySavingsReady = $derived(
		!detailsPending && seasonCards.every((card) => !card.details_loading)
	);

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

	$effect(() => {
		const selectedPrefix = selectedShowPrefix;
		const currentSort = sortMode;
		const currentQuery = query;
		queueMicrotask(() => {
			if (
				selectedPrefix !== selectedShowPrefix ||
				currentSort !== sortMode ||
				currentQuery !== query
			)
				return;
			const selectedButton = showListElement?.querySelector<HTMLElement>('button.selected');
			if (!selectedButton || !showListElement) return;
			const listBounds = showListElement.getBoundingClientRect();
			const selectedBounds = selectedButton.getBoundingClientRect();
			if (selectedBounds.top < listBounds.top) {
				showListElement.scrollTop += selectedBounds.top - listBounds.top;
			} else if (selectedBounds.bottom > listBounds.bottom) {
				showListElement.scrollTop += selectedBounds.bottom - listBounds.bottom;
			}
		});
	});

	function compareSeasonCards(left: FolderCard, right: FolderCard): number {
		const leftNumber = Number(seasonNumberLabel(seasonIdentity(left.prefix).season));
		const rightNumber = Number(seasonNumberLabel(seasonIdentity(right.prefix).season));
		if (Number.isFinite(leftNumber) && Number.isFinite(rightNumber))
			return leftNumber - rightNumber;
		return left.title.localeCompare(right.title);
	}

	function seasonCount(showPrefix: string): number {
		return groupedSeasons.get(showPrefix)?.length ?? 0;
	}

	function showInitial(title: string): string {
		return title.trim().slice(0, 1).toUpperCase() || 'T';
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
			const response = await postJson<{ ok: boolean; message?: string }>(
				`/api/folders/${encodePrefix(show.prefix)}/series-lifecycle`,
				{ mode }
			);
			if (!response.ok) throw new Error(response.message || 'Lifecycle policy could not be saved.');
			await invalidateAll();
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
</script>

<svelte:head>
	<title>Your TV library · Mediaforce</title>
	<meta
		name="description"
		content="Choose a TV season or show, make one test, compare it, and then make the rest smaller."
	/>
</svelte:head>

<main class="season-library">
	<LibraryModeNav active="tv" />
	<header class="page-heading">
		<div>
			<h1>Your library</h1>
			<p>Choose a season, or use one good setup for every season in a show.</p>
		</div>
		<div
			class="library-total"
			role="group"
			aria-label={librarySavingsReady
				? `${seasonCards.length} seasons, ${librarySizeLabel(librarySize)} stored, about ${librarySizeLabel(librarySavings)} available to save`
				: `${seasonCards.length} seasons, ${librarySizeLabel(librarySize)} stored, savings calculating`}
		>
			<div><strong>{seasonCards.length}</strong><span>seasons</span></div>
			<div><strong>{librarySizeLabel(librarySize)}</strong><span>stored</span></div>
			<div class="library-total__savings">
				{#if librarySavingsReady}
					<strong>~{librarySizeLabel(librarySavings)}</strong><span>space to save</span>
				{:else}
					<strong class="metric-pending">…</strong><span>calculating savings</span>
				{/if}
			</div>
		</div>
	</header>

	{#if loadError}
		<div class="notice notice--problem" role="alert">
			<strong>Your library could not be opened.</strong>
			<span>{loadError}</span>
		</div>
	{/if}
	{#if detailsError}
		<div class="notice notice--detail" role="status">
			<strong>Your library is ready.</strong>
			<span>{detailsError} Names, episode counts, and current sizes are still available.</span>
		</div>
	{/if}

	{#if resumeCard}
		{@const resumeIdentity = seasonIdentity(resumeCard.card.prefix)}
		<section class="resume-card" aria-labelledby="resume-heading">
			<div class="resume-card__copy">
				<span class="status-chip" data-tone={resumeCard.state.tone}>{resumeCard.state.label}</span>
				<h2 id="resume-heading">{resumeIdentity.show} · {resumeIdentity.season}</h2>
				<p>{resumeCard.state.detail}</p>
				<span class="resume-card__meta">
					{resumeCard.card.item_count} episodes · {formatFileSize(resumeCard.card.total_size_bytes)}
					{#if resumeCard.card.details_loading}
						· savings calculating
					{:else if fullyHeld(resumeCard.card)}
						· protected · stays original
					{:else}
						· about {formatFileSize(resumeCard.card.projected_reclaim_bytes)} to save
					{/if}
				</span>
			</div>
			<a class="primary-button" href={resolve(folderHref(resumeCard.card.prefix))}>Open season</a>
		</section>
	{/if}

	<section class="library-card" aria-labelledby="choose-season-heading" aria-busy={detailsPending}>
		<div class="library-card__heading">
			<div>
				<h2 id="choose-season-heading">Browse your library</h2>
				<p>Compare the biggest opportunities, then open one season or the whole show.</p>
			</div>
			<div class="library-controls">
				<label class="search-field">
					<svg viewBox="0 0 20 20" aria-hidden="true">
						<circle cx="8.5" cy="8.5" r="5.5" /><path d="m12.5 12.5 4 4" />
					</svg>
					<span class="sr-only">Find a show or season</span>
					<input bind:value={query} type="search" placeholder="Find a show or season" />
				</label>
				<label class="sort-field">
					<span>Sort</span>
					<select bind:value={sortMode} aria-label="Sort shows">
						<option value="savings" disabled={!librarySavingsReady}>
							Most space to save{librarySavingsReady ? '' : ' (calculating)'}
						</option>
						<option value="size">Largest library</option>
						<option value="seasons">Most seasons</option>
						<option value="name">Name A–Z</option>
					</select>
				</label>
			</div>
		</div>

		{#if foldersPending && !seasonCards.length}
			<div class="loading-state" role="status" aria-live="polite">
				<span></span><span></span><span></span>
				<p>Opening your library…</p>
			</div>
		{:else if !filteredShows.length}
			<div class="empty-state">
				{#if query.trim()}
					<h3>No seasons match “{query}”</h3>
					<p>Try a show title or season number.</p>
					<button class="secondary-button" type="button" onclick={() => (query = '')}
						>Clear search</button
					>
				{:else}
					<h3>Point Mediaforce at your TV folder to get started.</h3>
					<p>Your shows and seasons will appear here.</p>
					<a class="primary-button" href={resolve('/settings')}>Choose folders</a>
				{/if}
			</div>
		{:else}
			<div class="library-browser">
				<label class="mobile-show-picker">
					<span>Show</span>
					<select bind:value={selectedShowPrefix}>
						{#each filteredShows as show (show.prefix)}
							<option value={show.prefix}>{show.title}</option>
						{/each}
					</select>
				</label>
				<nav class="show-list" aria-label="TV shows" bind:this={showListElement}>
					{#each filteredShows as show (show.prefix)}
						<button
							type="button"
							class:selected={show.prefix === selectedShow?.prefix}
							aria-pressed={show.prefix === selectedShow?.prefix}
							onclick={() => (selectedShowPrefix = show.prefix)}
						>
							<span class="show-initial" aria-hidden="true">{showInitial(show.title)}</span>
							<span class="show-copy">
								<strong>{show.title}</strong>
								<small
									>{seasonCount(show.prefix)}
									{seasonCount(show.prefix) === 1 ? 'season' : 'seasons'} · {formatFileSize(
										show.total_size_bytes
									)}</small
								>
							</span>
							<span class="show-savings">
								{#if show.details_loading && show.projected_reclaim_bytes <= 0}
									<strong class="metric-pending">…</strong>
									<small>{detailsPending ? 'calculating' : 'not estimated'}</small>
								{:else if fullyHeld(show)}
									<strong>Held</strong>
									<small>no eligible savings</small>
								{:else}
									<strong>~{formatFileSize(show.projected_reclaim_bytes)}</strong>
									<small>{show.details_loading ? 'partial estimate' : 'to save'}</small>
								{/if}
							</span>
						</button>
					{/each}
				</nav>

				<div class="season-list" aria-live="polite">
					<div class="season-list__heading">
						<div class="season-list__title">
							<span>Selected show</span>
							<strong>{selectedShow?.title}</strong>
							<small
								>{selectedSeasons.length} seasons · {selectedShow?.item_count ?? 0} episodes</small
							>
						</div>
						{#if selectedShow}
							<div class="show-summary">
								<span><strong>{formatFileSize(selectedShow.total_size_bytes)}</strong> now</span>
								{#if selectedShow.details_loading && selectedShow.projected_reclaim_bytes <= 0}
									<span class="show-summary__savings"
										><strong class="metric-pending">…</strong>
										{detailsPending ? 'calculating savings' : 'savings not estimated'}</span
									>
								{:else if fullyHeld(selectedShow)}
									<span class="show-summary__savings"
										><strong>Held</strong> · no eligible savings</span
									>
								{:else}
									<span class="show-summary__savings"
										><strong>~{formatFileSize(selectedShow.projected_reclaim_bytes)}</strong> to
										save · {savingsPercent(selectedShow)}%</span
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
											>{detailsPending ? 'Checking eligibility…' : 'Eligibility unavailable'}</span
										>
									{/if}
								</div>
							</div>
							{#if policyError}<p class="policy-error">{policyError}</p>{/if}
						{/if}
					</div>

					{#if selectedShow && selectedSeasons.length > 1}
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
									>Set up eligible seasons</a
								>
							{:else}
								<button class="primary-button primary-button--disabled" type="button" disabled>
									{lifecycleAvailable ? 'No eligible seasons' : 'Checking eligibility'}
								</button>
							{/if}
						</div>
					{/if}

					{#each selectedSeasons as season (season.prefix)}
						{@const state = librarySeasonState(season, dashboard)}
						{@const seasonName = seasonIdentity(season.prefix).season}
						{@const seasonLifecycle = season.lifecycle?.seasons?.[0]}
						<a class="season-row" href={resolve(folderHref(season.prefix))}>
							<span class="season-number">{seasonNumberLabel(seasonName)}</span>
							<span class="season-copy">
								<strong>{seasonName}</strong>
								<small
									>{season.item_count} episodes · {formatFileSize(season.total_size_bytes)} now</small
								>
							</span>
							<span class="season-savings">
								{#if season.details_loading}
									<strong class="metric-pending">…</strong>
									<small>{detailsPending ? 'calculating savings' : 'not estimated'}</small>
								{:else if seasonLifecycle?.held_candidate_count && !seasonLifecycle.eligible_candidate_count}
									<strong>Held</strong>
									<small>stays original</small>
								{:else}
									<strong>~{formatFileSize(season.projected_reclaim_bytes)}</strong>
									<small>to save · {savingsPercent(season)}%</small>
								{/if}
							</span>
							{#if seasonLifecycle?.held_candidate_count}
								<span
									class="status-chip"
									data-tone="attention"
									title={seasonLifecycle.hold_reasons
										.map((reason) => `${reason.label}: ${reason.detail}`)
										.join(' ')}>{seasonHoldCopy(seasonLifecycle)}</span
								>
							{:else if state.key !== 'needs_test'}
								<span class="status-chip" data-tone={state.tone}>{state.label}</span>
							{/if}
							<svg viewBox="0 0 20 20" aria-hidden="true"><path d="m7 4.5 5.5 5.5L7 15.5" /></svg>
						</a>
					{/each}

					{#if !selectedSeasons.length}
						<div class="season-list__empty">No seasons are available for this show.</div>
					{/if}
				</div>
			</div>
		{/if}
	</section>
</main>

<style>
	:global(html) {
		background: var(--mf-bg-base);
	}

	.season-library {
		margin: 0 auto;
		max-width: 1088px;
		padding: 34px 24px 64px;
	}

	.page-heading {
		align-items: flex-end;
		display: flex;
		gap: 24px;
		justify-content: space-between;
		margin-bottom: 24px;
	}

	h1 {
		font-size: clamp(22px, 2.5vw, 28px);
		font-weight: 600;
		letter-spacing: -0.02em;
		line-height: 1.25;
	}

	.page-heading p,
	.library-card__heading p {
		color: var(--mf-fg-secondary);
		font-size: 14px;
		margin-top: 5px;
	}

	.library-total {
		border: 1px solid var(--mf-line-muted);
		display: grid;
		grid-template-columns: repeat(3, auto);
		white-space: nowrap;
	}

	.library-total div {
		display: grid;
		gap: 1px;
		padding: 8px 13px;
	}

	.library-total div + div {
		border-left: 1px solid var(--mf-line-muted);
	}

	.library-total strong {
		color: var(--mf-fg-primary);
		font-size: 14px;
		font-variant-numeric: tabular-nums;
	}

	.library-total span {
		color: var(--mf-fg-tertiary);
		font-size: 10px;
		font-weight: 600;
		letter-spacing: 0.04em;
		text-transform: uppercase;
	}

	.library-total__savings strong {
		color: var(--mf-ready-fg);
	}

	.library-total__savings {
		min-width: 118px;
	}

	.notice {
		border: 1px solid var(--mf-fail-line);
		border-radius: var(--mf-radius-3);
		display: grid;
		gap: 2px;
		margin-bottom: 18px;
		padding: 14px 16px;
	}

	.notice--problem {
		background: var(--mf-fail-bg);
		color: var(--mf-fail-fg);
	}

	.notice--detail {
		background: var(--mf-idle-bg);
		border-color: var(--mf-idle-line);
		color: var(--mf-idle-fg);
	}

	.resume-card {
		align-items: center;
		background: var(--mf-active-bg);
		border: 1px solid var(--mf-active-line);
		border-left-width: 3px;
		border-radius: var(--mf-radius-3);
		box-shadow: var(--mf-shadow-popover);
		display: flex;
		gap: 24px;
		justify-content: space-between;
		margin-bottom: 24px;
		padding: 18px 20px;
	}

	.resume-card__copy {
		display: grid;
		gap: 4px;
	}

	.resume-card h2 {
		font-size: 18px;
		margin-top: 4px;
	}

	.resume-card p {
		color: var(--mf-fg-secondary);
		font-size: 14px;
	}

	.resume-card__meta {
		color: var(--mf-fg-tertiary);
		font-size: 12px;
	}

	.library-card {
		background: var(--mf-bg-panel);
		border: 1px solid var(--mf-line);
		border-radius: var(--mf-radius-3);
		box-shadow: var(--mf-shadow-popover);
		overflow: hidden;
	}

	.library-card__heading {
		align-items: center;
		border-bottom: 1px solid var(--mf-line-muted);
		display: flex;
		gap: 20px;
		justify-content: space-between;
		padding: 18px 20px;
	}

	.library-card__heading h2 {
		font-size: 17px;
	}

	.library-controls {
		align-items: end;
		display: flex;
		gap: 10px;
		justify-content: flex-end;
		max-width: 570px;
		width: 100%;
	}

	.search-field {
		align-items: center;
		background: var(--mf-bg-input);
		border: 1px solid var(--mf-line);
		border-radius: var(--mf-radius-2);
		display: flex;
		max-width: 310px;
		padding: 0 11px;
		width: 100%;
	}

	.search-field:focus-within {
		border-color: var(--mf-active-fg);
		box-shadow: var(--mf-ring-focus);
	}

	.search-field svg {
		fill: none;
		height: 17px;
		stroke: var(--mf-fg-tertiary);
		stroke-linecap: round;
		stroke-width: 1.5;
		width: 17px;
	}

	.search-field input {
		background: transparent;
		border: 0;
		color: var(--mf-fg-primary);
		font-size: 14px;
		height: 38px;
		min-width: 0;
		outline: 0;
		padding: 0 0 0 9px;
		width: 100%;
	}

	.sort-field {
		display: grid;
		gap: 4px;
		min-width: 190px;
	}

	.sort-field > span,
	.mobile-show-picker > span {
		color: var(--mf-fg-tertiary);
		font-size: 10px;
		font-weight: 600;
		letter-spacing: 0.05em;
		text-transform: uppercase;
	}

	.sort-field select,
	.mobile-show-picker select {
		background: var(--mf-bg-input);
		border: 1px solid var(--mf-line);
		border-radius: var(--mf-radius-2);
		color: var(--mf-fg-primary);
		height: 38px;
		padding: 0 30px 0 10px;
	}

	.library-browser {
		display: grid;
		grid-template-columns: minmax(220px, 0.78fr) minmax(0, 1.6fr);
		height: clamp(460px, calc(100vh - 300px), 680px);
		min-height: 0;
	}

	.mobile-show-picker {
		display: none;
	}

	.show-list {
		background: var(--mf-bg-panel-2);
		border-right: 1px solid var(--mf-line-muted);
		height: 100%;
		overflow-y: auto;
		padding: 8px;
		scrollbar-gutter: stable;
	}

	.show-list button {
		align-items: center;
		border: 1px solid transparent;
		border-radius: var(--mf-radius-2);
		color: var(--mf-fg-secondary);
		display: flex;
		gap: 10px;
		min-height: 54px;
		padding: 8px 9px;
		text-align: left;
		width: 100%;
	}

	.show-list button:hover {
		background: var(--mf-bg-panel);
		color: var(--mf-fg-primary);
	}

	.show-list button.selected {
		background: var(--mf-active-bg);
		border-color: var(--mf-active-line);
		color: var(--mf-fg-primary);
	}

	.show-initial {
		align-items: center;
		background: var(--mf-idle-bg);
		border-radius: 8px;
		color: var(--mf-idle-fg);
		display: inline-flex;
		font-size: 14px;
		font-weight: 600;
		height: 34px;
		justify-content: center;
		width: 34px;
	}

	.selected .show-initial {
		background: var(--mf-active-bg-strong);
		color: var(--mf-active-fg);
	}

	.show-copy {
		display: grid;
		flex: 1;
		gap: 1px;
		min-width: 0;
	}

	.show-copy strong {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.show-copy small {
		color: var(--mf-fg-tertiary);
		font-size: 12px;
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
		color: var(--mf-ready-fg);
		font-size: 12px;
		font-variant-numeric: tabular-nums;
		font-weight: 600;
	}

	.show-savings small,
	.season-savings small {
		color: var(--mf-fg-tertiary);
		font-size: 10px;
		white-space: nowrap;
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
		height: 100%;
		overflow-y: auto;
		padding: 0 18px 20px;
		scrollbar-gutter: stable;
	}

	.season-list__heading {
		align-items: center;
		background: var(--mf-bg-panel);
		border-bottom: 1px solid var(--mf-line-muted);
		display: flex;
		gap: 20px;
		justify-content: space-between;
		padding: 15px 2px 13px;
		position: sticky;
		top: 0;
		z-index: 2;
	}

	.season-list__title {
		display: grid;
		gap: 2px;
	}

	.season-list__heading span,
	.season-list__heading small {
		color: var(--mf-fg-tertiary);
		font-size: 12px;
	}

	.season-list__heading strong {
		font-size: 16px;
	}

	.show-summary {
		display: grid;
		gap: 2px;
		justify-items: end;
		min-width: 138px;
		text-align: right;
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
		align-items: center;
		border-left: 1px solid var(--mf-line-muted);
		display: flex;
		gap: 12px;
		padding-left: 16px;
	}

	.show-policy label,
	.show-policy > div {
		display: grid;
		gap: 3px;
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
		align-items: center;
		background: var(--mf-ready-bg);
		border: 1px solid var(--mf-ready-line);
		display: flex;
		gap: 16px;
		justify-content: space-between;
		margin: 12px 0 4px;
		padding: 12px;
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

	.show-action .primary-button {
		flex: 0 0 auto;
		min-height: 34px;
		white-space: nowrap;
	}

	.season-row {
		align-items: center;
		border-top: 1px solid var(--mf-line-muted);
		color: var(--mf-fg-primary);
		display: flex;
		gap: 12px;
		min-height: 64px;
		padding: 9px 4px;
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

	.status-chip {
		background: var(--mf-idle-bg);
		border-radius: 999px;
		color: var(--mf-idle-fg);
		display: inline-flex;
		font-size: 12px;
		font-weight: 600;
		line-height: 1;
		padding: 6px 9px;
		width: fit-content;
	}

	.status-chip[data-tone='active'] {
		background: var(--mf-active-bg);
		color: var(--mf-active-fg);
	}

	.status-chip[data-tone='ready'],
	.status-chip[data-tone='success'] {
		background: var(--mf-ready-bg);
		color: var(--mf-ready-fg);
	}

	.status-chip[data-tone='attention'] {
		background: var(--mf-wait-bg);
		color: var(--mf-wait-fg);
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
		color: var(--mf-fg-on-accent);
	}

	.primary-button:hover {
		background: var(--mf-active-solid-hi);
		color: var(--mf-fg-on-accent);
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
		color: var(--mf-fg-secondary);
		font-size: 14px;
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

	@media (max-width: 760px) {
		.season-library {
			padding: 26px 16px 48px;
		}

		.page-heading,
		.library-card__heading {
			align-items: stretch;
			flex-direction: column;
		}

		.library-total {
			align-self: flex-start;
			grid-template-columns: repeat(3, minmax(0, 1fr));
			width: 100%;
		}

		.resume-card {
			align-items: stretch;
			flex-direction: column;
		}

		.library-browser {
			display: block;
			height: auto;
		}

		.mobile-show-picker {
			background: var(--mf-bg-panel-2);
			border-bottom: 1px solid var(--mf-line-muted);
			display: grid;
			gap: 4px;
			padding: 10px 14px;
		}

		.mobile-show-picker select {
			width: 100%;
		}

		.show-list {
			display: none;
		}

		.season-list {
			height: auto;
			overflow: visible;
			padding: 0 14px 18px;
		}

		.season-list__heading {
			flex-wrap: wrap;
			position: static;
		}

		.show-policy {
			border-left: 0;
			border-top: 1px solid var(--mf-line-muted);
			justify-content: space-between;
			padding: 10px 0 0;
			width: 100%;
		}

		.show-action {
			align-items: stretch;
			flex-direction: column;
		}

		.show-action .primary-button {
			width: 100%;
		}
	}

	@media (max-width: 440px) {
		.season-library {
			padding-inline: 12px;
		}

		.page-heading {
			margin-bottom: 18px;
		}

		.resume-card,
		.library-card__heading {
			padding: 15px;
		}

		.library-total {
			grid-template-columns: 1fr;
			white-space: normal;
		}

		.library-total div + div {
			border-left: 0;
			border-top: 1px solid var(--mf-line-muted);
		}

		.library-controls {
			align-items: stretch;
			flex-direction: column;
		}

		.search-field,
		.sort-field {
			max-width: none;
			width: 100%;
		}

		.primary-button {
			width: 100%;
		}

		.season-list__heading {
			align-items: flex-start;
			flex-direction: column;
			gap: 8px;
		}

		.show-summary {
			justify-items: start;
			min-width: 0;
			text-align: left;
		}

		.season-row {
			align-items: flex-start;
			flex-wrap: wrap;
			gap: 9px;
		}

		.season-copy {
			min-width: calc(100% - 52px);
		}

		.season-savings {
			justify-items: start;
			margin-left: 47px;
		}

		.season-row .status-chip {
			margin-left: auto;
			max-width: 112px;
			overflow: hidden;
			text-overflow: ellipsis;
			white-space: nowrap;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.loading-state span {
			animation: none;
		}
	}
</style>
