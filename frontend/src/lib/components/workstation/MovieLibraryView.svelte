<script lang="ts">
	import { resolve } from '$app/paths';
	import { onMount } from 'svelte';

	import type { MovieLibraryPayload, MovieMember, MovieTitle } from '$lib/api/types';
	import { folderRoutePath } from '$lib/folder-display';
	import { formatFileSize } from '$lib/format';
	import {
		movieCompositionDetail,
		movieEstimateEvidence,
		movieEstimatedOutputTotalIsLowerBound,
		movieExpectedOutputBytes,
		movieLibraryStateGroup,
		moviePendingReviewBadge,
		moviePrimaryStudioPrefix,
		movieReclaimLowerBound,
		movieReclaimTotalIsLowerBound,
		movieTitleNeedsAction,
		movieTitleRuntimeSeconds,
		movieWorkflowIsComplete,
		movieWorkflowLabel,
		selectMovieLeadTitle,
		selectMovieTitle,
		sortMovieTitles,
		type MovieLibrarySortMode,
		type MovieLibraryStateKey
	} from '$lib/movies/library';
	import LibraryLayout from './LibraryLayout.svelte';
	import StateBadge from './StateBadge.svelte';
	import { summarizeWorkStates } from './library-layout';
	import type {
		LibraryMetric,
		LibraryNotice,
		LibraryTone,
		LibraryWorkSegment
	} from './library-layout';

	let {
		payload,
		structurePending = false,
		detailsPending = false,
		loadError = '',
		detailsError = ''
	}: {
		payload: MovieLibraryPayload;
		structurePending?: boolean;
		detailsPending?: boolean;
		loadError?: string;
		detailsError?: string;
	} = $props();

	let query = $state('');
	let rootFilter = $state('all');
	let stateFilter = $state<'all' | Exclude<MovieLibraryStateKey, 'idle'>>('all');
	let sortMode = $state<MovieLibrarySortMode>('priority');
	let selectedPrefix = $state('');
	let selectionTouched = $state(false);
	let detailExpanded = $state(true);

	onMount(() => {
		if (window.matchMedia('(max-width: 760px)').matches) detailExpanded = false;
	});

	const titles = $derived.by(() => {
		const normalizedQuery = query.trim().toLocaleLowerCase();
		const filtered = payload.titles.filter((title) => {
			if (rootFilter !== 'all' && title.root !== rootFilter) return false;
			if (normalizedQuery) {
				const haystack = [
					title.title,
					title.prefix,
					...title.members.map((member) => `${member.label} ${member.edition_label ?? ''}`)
				]
					.join(' ')
					.toLocaleLowerCase();
				if (!haystack.includes(normalizedQuery)) return false;
			}
			return stateFilter === 'all' || movieLibraryStateGroup(title).key === stateFilter;
		});
		return sortMovieTitles(filtered, sortMode);
	});

	const selectedTitle = $derived(selectMovieTitle(titles, selectedPrefix));
	const selectedTitleIndex = $derived(
		selectedTitle ? titles.findIndex((title) => title.prefix === selectedTitle.prefix) : -1
	);
	const selectedEstimateEvidence = $derived(
		selectedTitle ? movieEstimateEvidence(selectedTitle) : null
	);
	const leadTitle = $derived(selectMovieLeadTitle(titles, sortMode, query));
	const totalSize = $derived(
		payload.titles.reduce((total, title) => total + title.total_size_bytes, 0)
	);
	const totalReclaim = $derived(
		payload.titles.reduce((total, title) => total + (movieReclaimLowerBound(title) ?? 0), 0)
	);
	const reclaimCoverage = $derived(
		payload.titles.filter((title) => movieReclaimLowerBound(title) != null).length
	);
	const reclaimHasUnknowns = $derived(movieReclaimTotalIsLowerBound(payload.titles));
	const totalEstimatedOutput = $derived(
		payload.titles.reduce((total, title) => total + (movieExpectedOutputBytes(title) ?? 0), 0)
	);
	const outputCoverage = $derived(
		payload.titles.filter((title) => movieExpectedOutputBytes(title) != null).length
	);
	const outputHasUnknowns = $derived(movieEstimatedOutputTotalIsLowerBound(payload.titles));
	const actionableCount = $derived(payload.titles.filter(movieTitleNeedsAction).length);
	const totalOutputValue = $derived(
		detailsPending ? '…' : outputCoverage === 0 ? '—' : formatBytes(totalEstimatedOutput)
	);
	const totalOutputDetail = $derived(
		detailsPending
			? undefined
			: outputCoverage === 0
				? 'No estimate'
				: outputHasUnknowns
					? `At least · ${outputCoverage} of ${payload.titles.length}`
					: undefined
	);
	const totalReclaimValue = $derived(
		detailsPending
			? '…'
			: reclaimCoverage === 0
				? '—'
				: totalReclaim < 0
					? `−${formatBytes(Math.abs(totalReclaim))}`
					: totalReclaim === 0
						? '0 B'
						: formatBytes(totalReclaim)
	);
	const totalReclaimDetail = $derived(
		detailsPending
			? undefined
			: reclaimCoverage === 0
				? 'No estimate'
				: totalReclaim < 0
					? `${reclaimHasUnknowns ? 'Known net growth' : 'Net growth'}${reclaimHasUnknowns ? ` · ${reclaimCoverage} of ${payload.titles.length}` : ''}`
					: totalReclaim === 0
						? reclaimHasUnknowns
							? `Known titles only · ${reclaimCoverage} of ${payload.titles.length}`
							: 'No savings'
						: reclaimHasUnknowns
							? `Known amount · ${reclaimCoverage} of ${payload.titles.length}`
							: undefined
	);
	const conflictCount = $derived(
		payload.titles.reduce((total, title) => total + title.promotion_conflicts.length, 0)
	);
	const libraryMetrics = $derived<LibraryMetric[]>([
		{
			value: `${payload.titles.length}`,
			label: payload.titles.length === 1 ? 'Title' : 'Titles',
			detail: `${actionableCount} ${actionableCount === 1 ? 'needs' : 'need'} work`
		},
		{ value: formatBytes(totalSize), label: 'Current size' },
		{
			value: totalOutputValue,
			label: 'Estimated output',
			detail: totalOutputDetail,
			pending: detailsPending
		},
		{
			value: totalReclaimValue,
			label: 'Estimated space saved',
			detail: totalReclaimDetail,
			pending: detailsPending
		}
	]);
	const libraryWorkSegments = $derived<LibraryWorkSegment[]>(
		summarizeWorkStates(payload.titles, movieLibraryStateGroup)
	);
	const libraryNotices = $derived.by<LibraryNotice[]>(() => {
		const notices: LibraryNotice[] = [];
		if (loadError) {
			notices.push({ title: 'The movie library could not open', detail: loadError, tone: 'fail' });
		}
		if (detailsError && payload.titles.length) {
			notices.push({
				title: 'Titles are available',
				detail: `${detailsError} Workflow and savings details may be stale.`
			});
		}
		if (conflictCount) {
			notices.push({
				title: `${conflictCount} replacement ${conflictCount === 1 ? 'conflict needs' : 'conflicts need'} review`,
				detail: 'Mediaforce will not replace a file that is already in the destination.',
				tone: 'fail'
			});
		}
		return notices;
	});
	const titleCountSummary = $derived(`${titles.length} of ${payload.titles.length} titles`);

	$effect(() => {
		if (!selectionTouched) {
			selectedPrefix = titles[0]?.prefix ?? '';
			return;
		}
		if (selectedTitle && selectedPrefix !== selectedTitle.prefix)
			selectedPrefix = selectedTitle.prefix;
	});

	function formatBytes(value: number | null | undefined): string {
		return formatFileSize(value, 'No estimate');
	}

	function selectWorkSegment(key: string) {
		query = '';
		rootFilter = 'all';
		stateFilter = stateFilter === key ? 'all' : (key as Exclude<MovieLibraryStateKey, 'idle'>);
	}

	function formatReclaim(title: MovieTitle): string {
		const lowerBound = movieReclaimLowerBound(title);
		if (lowerBound == null) return 'No estimate';
		if (lowerBound < 0) return `Grows by about ${formatBytes(Math.abs(lowerBound))}`;
		if (lowerBound === 0) return 'None';
		if (title.projected_reclaim_bytes == null) return `At least ${formatBytes(lowerBound)}`;
		if (title.savings_confidence === 'estimated') return `About ${formatBytes(lowerBound)}`;
		return formatBytes(lowerBound);
	}

	function formatRuntime(title: MovieTitle): string {
		const durationSeconds = movieTitleRuntimeSeconds(title);
		if (durationSeconds == null) return title.details_loading ? 'Measuring…' : 'Not available';
		const roundedSeconds = Math.round(durationSeconds);
		const hours = Math.floor(roundedSeconds / 3600);
		const minutes = Math.floor((roundedSeconds % 3600) / 60);
		const seconds = roundedSeconds % 60;
		return hours ? `${hours}h ${minutes}m` : `${minutes}m ${seconds}s`;
	}

	function formatExpectedOutput(title: MovieTitle): string {
		if (title.details_loading) return 'Estimating…';
		const expectedOutput = movieExpectedOutputBytes(title);
		return expectedOutput == null ? 'No estimate' : formatBytes(expectedOutput);
	}

	function reclaimSummary(title: MovieTitle): string {
		if (title.details_loading) return 'Estimating space saved…';
		const lowerBound = movieReclaimLowerBound(title);
		if (lowerBound == null) return 'No savings estimate';
		if (lowerBound < 0) return `Grows by about ${formatBytes(Math.abs(lowerBound))}`;
		if (lowerBound === 0) return 'No estimated savings';
		if (title.projected_reclaim_bytes == null) return `Save at least ${formatBytes(lowerBound)}`;
		if (title.savings_confidence === 'estimated') return `Save about ${formatBytes(lowerBound)}`;
		return `Save ${formatBytes(lowerBound)}`;
	}

	function rowReclaimSummary(title: MovieTitle): string {
		if (title.details_loading) return 'Estimating…';
		return movieReclaimLowerBound(title) == null ? '' : reclaimSummary(title);
	}

	function memberLabel(member: MovieMember): string {
		if (member.edition_label) return member.edition_label;
		if (member.role === 'feature') return 'Main movie';
		if (member.role === 'extra') return member.extra_category ?? 'Extra';
		return 'Uncertain file';
	}

	function workflowTone(title: MovieTitle): LibraryTone {
		if (title.promotion_conflicts.length) return 'fail';
		if (movieWorkflowIsComplete(title.workflow_state)) return 'ready';
		const pendingReview = moviePendingReviewBadge(title);
		return normalizeTone(pendingReview?.tone ?? title.workflow_state?.tone);
	}

	function normalizeTone(tone: string | undefined): LibraryTone {
		if (tone === 'attention' || tone === 'fail') return 'fail';
		if (tone === 'success' || tone === 'ready') return 'ready';
		if (tone === 'active') return 'active';
		if (tone === 'wait') return 'wait';
		return 'idle';
	}

	function workflowExplanation(title: MovieTitle): string {
		if (title.promotion_conflicts.length) {
			return 'A file already exists where this movie would be placed. Open Studio to review it.';
		}
		if (title.availability === 'browse_only' || title.workflow_state?.state === 'browse_only') {
			return 'You can review these files, but Mediaforce cannot change this library.';
		}
		if (title.workflow_state?.state === 'explicit_selection_required') {
			const explicitCount = title.members.filter((member) => !member.included_by_default).length;
			return `${explicitCount} ${explicitCount === 1 ? 'file needs' : 'files need'} you to choose ${explicitCount === 1 ? 'it' : 'them'} individually.`;
		}
		if (movieWorkflowIsComplete(title.workflow_state)) return 'This movie is finished.';
		const pendingReview = moviePendingReviewBadge(title);
		if (pendingReview) {
			return pendingReview.detail?.trim()
				? pendingReview.detail
				: 'Review the current sample before production compression can begin.';
		}
		const count = title.included_item_count || title.item_count;
		const fileWord = count === 1 ? 'file' : 'files';
		switch (title.workflow_state?.primary_lane) {
			case 'promote':
				return `${count} checked ${fileWord} can replace the current library copy.`;
			case 'validate':
				return `${count} compressed ${fileWord} ${count === 1 ? 'needs' : 'need'} a final safety check.`;
			case 'encode':
				return `${count} ${fileWord} ${count === 1 ? 'is' : 'are'} ready to compress.`;
			case 'processing':
				return 'Mediaforce is compressing this title now.';
			case 'attention':
				return 'This title needs review before work can continue.';
			case 'mixed': {
				const lanes: Array<[number, string]> = [
					[title.workflow_state?.lane_counts.encode ?? 0, 'ready to compress'],
					[title.workflow_state?.lane_counts.validate ?? 0, 'ready to check'],
					[title.workflow_state?.lane_counts.promote ?? 0, 'ready to replace']
				];
				const parts = lanes
					.filter(([laneCount]) => laneCount > 0)
					.map(([laneCount, label]) => `${laneCount} ${label}`);
				return parts.length
					? `${parts.join(', ')} across this title.`
					: 'This title has several steps ready for review.';
			}
			case 'blocked':
				return 'Open Studio to see what must be fixed before work can start.';
			default:
				return title.details_loading
					? 'Mediaforce is checking what this title needs next.'
					: 'No work is waiting for this title.';
		}
	}

	function policyValue(title: MovieTitle, key: string, fallback: string): string {
		const value = title.policy[key];
		return typeof value === 'string' && value ? value : fallback;
	}

	function policyExceptions(title: MovieTitle): string[] {
		const exceptions: string[] = [];
		if (policyValue(title, 'editions', 'separate') !== 'separate') {
			exceptions.push('Editions run together');
		}
		if (policyValue(title, 'extras', 'exclude') === 'include') {
			exceptions.push('Extras run with the title');
		}
		return exceptions;
	}

	function memberStatusLabel(member: MovieMember, title: MovieTitle): string {
		if (title.promotion_conflicts.length && member.status === 'validated') {
			return 'File checked · replacement blocked';
		}
		return (
			{
				discovered: 'Not started',
				planned: 'Ready to compress',
				encoding: 'Compressing',
				encoded: 'Ready to check',
				validated: 'Ready to replace',
				promoted: 'Finished',
				missing: 'File missing'
			}[member.status] ?? 'Needs review'
		);
	}

	function memberSelectionReason(member: MovieMember, title: MovieTitle): string {
		if (title.members.length === 1) {
			return 'Studio keeps this as an explicit file choice instead of adding it to automatic title work.';
		}
		if (member.role === 'extra') {
			return 'This extra stays separate unless you open it directly.';
		}
		return 'Mediaforce is not sure this is the main movie, so it only runs when you open it directly.';
	}

	function selectTitle(prefix: string, revealInspector = false) {
		selectionTouched = true;
		selectedPrefix = prefix;
		detailExpanded = revealInspector && !window.matchMedia('(max-width: 760px)').matches;
	}

	function toggleTitleDetail(prefix: string) {
		selectionTouched = true;
		if (selectedTitle?.prefix === prefix) detailExpanded = !detailExpanded;
		else {
			selectedPrefix = prefix;
			detailExpanded = true;
		}
		requestAnimationFrame(() => {
			document
				.querySelector<HTMLElement>(`[data-movie-title-row="${CSS.escape(prefix)}"]`)
				?.scrollIntoView({ block: 'nearest' });
		});
	}

	function moveTitleSelection(event: KeyboardEvent, currentIndex: number) {
		let nextIndex: number;
		if (event.key === 'ArrowDown') nextIndex = Math.min(currentIndex + 1, titles.length - 1);
		else if (event.key === 'ArrowUp') nextIndex = Math.max(currentIndex - 1, 0);
		else if (event.key === 'Home') nextIndex = 0;
		else if (event.key === 'End') nextIndex = titles.length - 1;
		else return;
		event.preventDefault();
		const nextTitle = titles[nextIndex];
		if (!nextTitle) return;
		selectionTouched = true;
		selectedPrefix = nextTitle.prefix;
		requestAnimationFrame(() => {
			document
				.querySelector<HTMLElement>(`[data-movie-title-row="${CSS.escape(nextTitle.prefix)}"]`)
				?.focus();
		});
	}
</script>

<svelte:head>
	<title>Movie Library · Mediaforce</title>
	<meta
		name="description"
		content="Browse movie titles, see what needs attention, and open the next safe action."
	/>
</svelte:head>

<LibraryLayout
	active="movie"
	title="Movie Library"
	metrics={libraryMetrics}
	workSegments={libraryWorkSegments}
	activeWorkSegment={stateFilter === 'all' ? '' : stateFilter}
	onWorkSegmentSelect={selectWorkSegment}
	notices={libraryNotices}
	toolbarSummary={titleCountSummary}
	loading={structurePending || detailsPending}
>
	{#snippet toolbar()}
		<label class="search-field" for="movie-search">
			<span class="sr-only">Filter this list</span>
			<input
				id="movie-search"
				bind:value={query}
				type="search"
				placeholder="Type part of a title"
			/>
		</label>
		{#if payload.libraries.length > 1}
			<label>
				<span>Library</span>
				<select bind:value={rootFilter}>
					<option value="all">All movie libraries</option>
					{#each payload.libraries as library (library.key)}
						<option value={library.key}>{library.label}</option>
					{/each}
				</select>
			</label>
		{/if}
		<label>
			<span>Status</span>
			<select bind:value={stateFilter}>
				<option value="all">All states</option>
				<option value="attention">Needs attention</option>
				<option value="blocked">Cannot start</option>
				<option value="processing">Compressing</option>
				<option value="ready">Ready to act on</option>
				<option value="explicit">Needs a file choice</option>
			</select>
		</label>
		<label>
			<span>Sort</span>
			<select bind:value={sortMode}>
				<option value="priority">What to work on next</option>
				<option value="name">Title A–Z</option>
				<option value="size">Largest current size</option>
				<option value="savings">Most estimated space saved</option>
				<option value="oldest">Oldest added</option>
			</select>
		</label>
	{/snippet}

	{#if structurePending && !payload.titles.length}
		<div class="empty-state" role="status">
			<span class="loading-mark" aria-hidden="true"></span>
			<strong>Loading movie library…</strong>
			<p>Files appear first; workflow details follow.</p>
		</div>
	{:else if !payload.titles.length && !loadError}
		<div class="empty-state">
			<strong>No movies found.</strong>
			<p>
				Add or scan a typed Movies root in Settings. Root-level files and title folders both appear
				here.
			</p>
			<a class="primary-link" href={resolve('/settings')}>Open Settings</a>
		</div>
	{:else if !titles.length}
		<div class="empty-state">
			<strong>No movies match these filters.</strong>
			<button
				class="empty-state__action"
				type="button"
				onclick={() => {
					query = '';
					rootFilter = 'all';
					stateFilter = 'all';
				}}>Clear filters</button
			>
		</div>
	{:else}
		<div class="library-register">
			<div class="title-index__chrome library-register__header">
				<div class="title-index__header" aria-hidden="true">
					<span>Title</span><span>Files</span><span>Size / estimated saved</span><span
						>Next step</span
					>
				</div>
			</div>
			<div class="workbench__body library-workbench">
				<div class="title-index library-index">
					{#each titles as title, index (title.prefix)}
						<div class="ledger-row">
							<button
								type="button"
								class="title-row"
								class:selected={selectedTitle?.prefix === title.prefix}
								data-movie-title-row={title.prefix}
								data-library-state={movieLibraryStateGroup(title).key}
								tabindex={selectedTitle?.prefix === title.prefix ? 0 : -1}
								onclick={() => selectTitle(title.prefix, true)}
								onkeydown={(event) => moveTitleSelection(event, index)}
								aria-pressed={selectedTitle?.prefix === title.prefix}
							>
								<span class="title-row__identity">
									<strong>{title.title}</strong>
									{#if leadTitle?.prefix === title.prefix}
										<small class="recommended">Recommended next · {title.library_label}</small>
									{:else if title.scope_mode === 'single_file'}
										<small>One movie file</small>
									{:else if payload.libraries.length > 1}
										<small>{title.library_label}</small>
									{/if}
									<small class="title-row__mobile-meta">
										{#if rowReclaimSummary(title)}{rowReclaimSummary(title)} ·
										{/if}{formatBytes(title.total_size_bytes)} stored
									</small>
								</span>
								<span class="title-row__count">
									<strong>{title.item_count} {title.item_count === 1 ? 'file' : 'files'}</strong>
									{#if movieCompositionDetail(title)}
										<small>{movieCompositionDetail(title)}</small>
									{/if}
								</span>
								<span class="title-row__size">
									<strong>{formatBytes(title.total_size_bytes)}</strong>
									{#if rowReclaimSummary(title)}<small>{rowReclaimSummary(title)}</small>{/if}
								</span>
								<StateBadge
									tone={workflowTone(title)}
									label={movieWorkflowLabel(title)}
									compact
									quiet={workflowTone(title) === 'ready' &&
										selectedTitle?.prefix !== title.prefix &&
										leadTitle?.prefix !== title.prefix}
								/>
							</button>
							<button
								class="row-inspect"
								type="button"
								aria-controls={`movie-title-detail-${index}`}
								aria-expanded={selectedTitle?.prefix === title.prefix && detailExpanded}
								aria-label={`${selectedTitle?.prefix === title.prefix && detailExpanded ? 'Close' : 'Inspect'} ${title.title}`}
								onclick={() => toggleTitleDetail(title.prefix)}
							>
								<span aria-hidden="true"
									>{selectedTitle?.prefix === title.prefix && detailExpanded
										? 'Close'
										: 'Inspect'}</span
								>
							</button>
						</div>
					{/each}
				</div>

				{#if selectedTitle && detailExpanded}
					<aside
						id={`movie-title-detail-${selectedTitleIndex}`}
						class="title-inspector library-inspector"
						style:grid-row={selectedTitleIndex + 2}
						aria-label={`${selectedTitle.title} details`}
					>
						<span class="sr-only" aria-live="polite">
							Selected {selectedTitle.title}. {movieWorkflowLabel(selectedTitle)}.
						</span>
						<header class="inspector-heading">
							<div>
								<span class="eyebrow">{selectedTitle.library_label}</span>
								<h2>{selectedTitle.title}</h2>
								<p>{selectedTitle.prefix}</p>
							</div>
							<StateBadge
								tone={workflowTone(selectedTitle)}
								label={movieWorkflowLabel(selectedTitle)}
								compact
							/>
							<button
								class="inspector-collapse"
								type="button"
								onclick={() => (detailExpanded = false)}
							>
								Collapse ↑
							</button>
						</header>

						<div class="selection-command">
							<div>
								<p>{workflowExplanation(selectedTitle)}</p>
							</div>
							{#if selectedTitle.details_loading}
								<span class="primary-link primary-link--disabled" aria-disabled="true">
									Loading Studio route…
								</span>
							{:else}
								<a
									class="primary-link"
									href={resolve(folderRoutePath(moviePrimaryStudioPrefix(selectedTitle)))}
								>
									Open in Studio
								</a>
							{/if}
						</div>

						<div class="inspector-facts">
							<div>
								<span>Runtime</span><strong>{formatRuntime(selectedTitle)}</strong>
							</div>

							<div>
								<span>Current size</span><strong
									>{formatBytes(selectedTitle.total_size_bytes)}</strong
								>
							</div>
							<div>
								<span>Estimated output</span><strong>{formatExpectedOutput(selectedTitle)}</strong>
							</div>
							<div>
								<span>Estimated space saved</span><strong
									>{selectedTitle.details_loading
										? 'Estimating…'
										: formatReclaim(selectedTitle)}</strong
								>
							</div>
						</div>

						{#if selectedEstimateEvidence}
							<p class="estimate-evidence">{selectedEstimateEvidence}</p>
						{/if}

						{#if selectedTitle.availability === 'browse_only'}
							<div class="inline-alert">
								<strong>View only</strong>
								<span>You can browse these files, but Mediaforce cannot change this library.</span>
							</div>
						{/if}
						{#if selectedTitle.promotion_conflicts.length}
							<div class="inline-alert inline-alert--danger">
								<strong>Cannot replace yet</strong>
								{#each selectedTitle.promotion_conflicts as conflict (`${conflict.kind}:${conflict.destination_path}`)}
									<span>{conflict.detail} <code>{conflict.destination_path}</code></span>
								{/each}
							</div>
						{/if}

						{#if policyExceptions(selectedTitle).length}
							<div class="policy-strip" aria-label="Movie library policy exceptions">
								{#each policyExceptions(selectedTitle) as exception (exception)}
									<span>{exception}</span>
								{/each}
							</div>
						{/if}

						<section class="member-list" aria-labelledby="movie-files-heading">
							<div class="section-heading">
								<div>
									<h3 id="movie-files-heading">
										{selectedTitle.members.length === 1 ? 'Movie file' : 'Files and editions'}
									</h3>
									{#if selectedTitle.members.length > 1}
										<p>
											Open an individual edition, extra, or uncertain file only when you want to
											work outside the whole-title scope.
										</p>
									{/if}
								</div>
								<span>{selectedTitle.members.length}</span>
							</div>
							{#each selectedTitle.members as member (member.item_id)}
								<div class="member-row" data-role={member.role}>
									<div class="member-row__copy">
										<div class="member-row__heading">
											<strong>{memberLabel(member)}</strong>
											<span>{memberStatusLabel(member, selectedTitle)}</span>
										</div>
										<p>{member.label}</p>
										<small>
											{formatBytes(member.size_bytes)}
											{#if member.video_codec}
												· {member.video_codec.toUpperCase()}{/if}
											{#if member.included_by_default}
												· Runs with the whole title{:else}
												· Only runs if you pick it{/if}
										</small>
										{#if member.selection_blocker && !member.included_by_default}
											<span class="member-row__reason"
												>{memberSelectionReason(member, selectedTitle)}</span
											>
										{/if}
									</div>
									<a
										class="member-link"
										href={resolve(folderRoutePath(member.prefix))}
										aria-label={`Open ${memberLabel(member)} as an exact file in Studio`}
										>Open exact file in Studio</a
									>
								</div>
							{/each}
						</section>
					</aside>
				{/if}
			</div>
		</div>
	{/if}
</LibraryLayout>

<style>
	.eyebrow {
		color: var(--mf-fg-muted);
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-semibold);
		letter-spacing: 0.08em;
		text-transform: uppercase;
	}

	h2,
	h3,
	p {
		margin: 0;
	}

	.inspector-heading p,
	.section-heading p,
	.selection-command p {
		color: var(--mf-fg-secondary);
		font-size: 13px;
		line-height: 1.55;
	}

	.inline-alert {
		border-top: 1px solid var(--mf-line-muted);
		display: grid;
		gap: var(--mf-space-2);
		padding: var(--mf-space-5) 0;
	}

	.inline-alert span {
		color: var(--mf-fg-secondary);
		font-size: 12px;
	}

	.inline-alert--danger {
		border-left: 3px solid var(--mf-fail-fg);
		padding-left: var(--mf-space-5);
	}

	.inline-alert {
		margin: 0;
	}

	.inline-alert code {
		font-size: 11px;
		word-break: break-all;
	}

	.estimate-evidence {
		border-left: 3px solid var(--mf-wait-fg);
		color: var(--mf-fg-secondary);
		font-size: 12px;
		line-height: 1.45;
		margin: 0;
		padding: var(--mf-space-4) var(--mf-space-5);
	}

	.title-index {
		display: contents;
	}

	.title-index__header,
	.title-row {
		display: grid;
		grid-template-columns: minmax(320px, 1fr) 88px 180px 190px;
	}

	.title-index__chrome {
		background: var(--mf-bg-panel);
		position: sticky;
		top: 0;
		z-index: 2;
	}

	.title-index__header {
		background: var(--mf-bg-panel);
		border-bottom: 1px solid var(--mf-line-strong);
		color: var(--mf-fg-muted);
		font-size: 10px;
		font-weight: var(--mf-weight-semibold);
		letter-spacing: 0.1em;
		padding: var(--mf-space-4) var(--mf-space-6);
		text-transform: uppercase;
	}

	.ledger-row {
		position: relative;
	}

	.title-row {
		align-items: center;
		background: transparent;
		border: 0;
		border-bottom: 1px solid var(--mf-line-muted);
		color: inherit;
		cursor: pointer;
		font: inherit;
		gap: var(--mf-space-5);
		min-height: 56px;
		padding: var(--mf-space-4) var(--mf-space-6);
		text-align: left;
		width: 100%;
	}

	.title-row:hover {
		background: var(--mf-bg-panel-2);
	}

	.title-row.selected {
		background: var(--mf-bg-panel-2);
		box-shadow: inset 3px 0 0 var(--mf-active-fg);
	}

	.title-row__identity,
	.title-row__count,
	.title-row__size {
		min-width: 0;
	}

	.title-row strong,
	.title-row small {
		display: block;
	}

	.title-row small.recommended {
		color: var(--mf-active-fg);
		font-weight: var(--mf-weight-semibold);
	}

	.title-row__mobile-meta {
		display: none !important;
	}

	.title-row__identity strong {
		font-size: 14px;
		font-weight: var(--mf-weight-semibold);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.title-row small {
		color: var(--mf-fg-muted);
		font-size: 12px;
		line-height: 1.4;
		margin-top: var(--mf-space-1);
	}

	.title-row :global(.state-badge) {
		max-width: 100%;
		white-space: nowrap;
	}

	.row-inspect {
		display: none;
	}

	.title-inspector {
		display: grid;
		gap: var(--mf-space-7);
		grid-column: 1;
		grid-template-columns: minmax(230px, 0.75fr) minmax(390px, 1.45fr) minmax(260px, 0.8fr);
		min-width: 0;
		padding: var(--mf-space-7) var(--mf-space-6) var(--mf-space-8);
		box-shadow:
			inset 3px 0 0 var(--mf-active-fg),
			0 10px 24px color-mix(in srgb, var(--mf-fg-primary) 8%, transparent);
	}

	.inspector-heading,
	.section-heading {
		align-items: start;
		display: grid;
		gap: var(--mf-space-6);
	}

	.inspector-heading {
		align-self: start;
		grid-column: 1;
		grid-row: 1;
		padding-right: var(--mf-space-8);
		position: relative;
	}

	.inspector-heading :global(.state-badge) {
		justify-self: start;
	}

	.inspector-heading h2 {
		font-size: 22px;
		font-weight: var(--mf-weight-semibold);
		letter-spacing: -0.025em;
		margin: 4px 0;
	}

	.inspector-heading p {
		font-family: var(--mf-font-mono);
		font-size: 11px;
		word-break: break-all;
	}

	.selection-command {
		align-content: start;
		border-left: 1px solid var(--mf-line-strong);
		box-sizing: border-box;
		display: grid;
		gap: var(--mf-space-5);
		grid-column: 3;
		grid-row: 1 / span 8;
		grid-template-columns: minmax(0, 1fr);
		min-width: 0;
		padding: 0 0 0 var(--mf-space-7);
		width: 100%;
	}

	.selection-command > div {
		min-width: 0;
	}

	.selection-command p {
		margin-top: var(--mf-space-2);
	}

	.selection-command .primary-link {
		justify-self: start;
		max-width: 100%;
		min-width: 0;
		overflow-wrap: anywhere;
		text-align: center;
		white-space: normal;
	}

	.inspector-collapse {
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

	.inspector-collapse:hover {
		background: var(--mf-bg-panel);
		color: var(--mf-fg-primary);
	}

	.selection-command .primary-link--disabled {
		background: var(--mf-bg-muted);
		border-color: var(--mf-line);
		color: var(--mf-fg-tertiary);
		cursor: wait;
	}

	.inspector-facts {
		display: grid;
		gap: var(--mf-space-6) var(--mf-space-8);
		grid-column: 2;
		grid-row: 1;
		grid-template-columns: repeat(2, minmax(0, 1fr));
	}

	.inspector-facts div {
		padding: 0;
	}

	.inspector-facts span,
	.inspector-facts strong {
		display: block;
	}

	.inspector-facts span {
		color: var(--mf-fg-muted);
		font-size: 10px;
		font-weight: var(--mf-weight-semibold);
		letter-spacing: 0.1em;
		text-transform: uppercase;
	}

	.inspector-facts strong {
		font-size: 15px;
		font-variant-numeric: tabular-nums;
		font-weight: var(--mf-weight-semibold);
		margin-top: var(--mf-space-2);
	}

	.policy-strip {
		display: flex;
		flex-wrap: wrap;
		gap: var(--mf-space-3);
		grid-column: 2;
	}

	.policy-strip span {
		border: 1px solid var(--mf-line);
		color: var(--mf-fg-secondary);
		font-size: 10px;
		font-weight: var(--mf-weight-semibold);
		padding: var(--mf-space-2) var(--mf-space-3);
	}

	.member-list {
		display: grid;
		gap: 0;
		grid-column: 2;
		grid-template-columns: minmax(0, 1fr);
		min-width: 0;
	}

	.estimate-evidence,
	.inline-alert {
		grid-column: 2;
	}

	.section-heading {
		border-bottom: 1px solid var(--mf-line-muted);
		min-width: 0;
		padding-bottom: var(--mf-space-4);
	}

	.section-heading h3 {
		font-size: var(--mf-text-lg);
		font-weight: var(--mf-weight-semibold);
	}

	.section-heading > span {
		color: var(--mf-fg-muted);
		font-size: 12px;
		font-weight: var(--mf-weight-semibold);
	}

	.member-row {
		align-items: center;
		border-bottom: 1px solid var(--mf-line-muted);
		box-sizing: border-box;
		display: flex;
		gap: var(--mf-space-5);
		justify-content: space-between;
		min-width: 0;
		padding: var(--mf-space-4) 0;
		width: 100%;
	}

	.member-row[data-role='extra'],
	.member-row[data-role='uncertain'] {
		border-left: 3px solid var(--mf-line-strong);
		padding-left: var(--mf-space-4);
	}

	.member-row__copy {
		min-width: 0;
		overflow: hidden;
	}

	.member-row__heading {
		align-items: baseline;
		display: flex;
		gap: var(--mf-space-4);
	}

	.member-row__heading span,
	.member-row small,
	.member-row__reason {
		color: var(--mf-fg-muted);
		font-size: 10px;
	}

	.member-row p {
		font-family: var(--mf-font-mono);
		font-size: 11px;
		margin: 3px 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.member-row__reason {
		display: block;
		line-height: 1.4;
		margin-top: 5px;
	}

	.member-link,
	.primary-link {
		background: var(--mf-active-solid);
		border: 1px solid var(--mf-active-solid);
		border-radius: 4px;
		color: var(--mf-active-contrast);
		font-size: 11px;
		font-weight: 800;
		padding: 8px 10px;
		text-decoration: none;
		white-space: nowrap;
	}

	.member-link {
		background: transparent;
		border-color: var(--mf-active-fg);
		color: var(--mf-active-fg);
	}

	.empty-state {
		align-items: center;
		display: grid;
		gap: 7px;
		justify-items: center;
		min-height: 180px;
		padding: 28px;
		text-align: center;
	}

	.empty-state__action {
		background: var(--mf-bg-panel);
		border: 1px solid var(--mf-line-strong);
		border-radius: var(--mf-radius-1);
		color: var(--mf-fg-primary);
		cursor: pointer;
		font: inherit;
		font-weight: var(--mf-weight-semibold);
		padding: 8px 12px;
	}

	.empty-state p {
		color: var(--mf-fg-secondary);
		font-size: 13px;
		max-width: 520px;
	}

	.loading-mark {
		animation: pulse 900ms ease-in-out infinite;
		background: var(--mf-active-fg);
		height: 8px;
		width: 8px;
	}

	.sr-only {
		height: 1px;
		margin: -1px;
		overflow: hidden;
		padding: 0;
		position: absolute;
		width: 1px;
		clip: rect(0, 0, 0, 0);
		white-space: nowrap;
	}

	@keyframes pulse {
		50% {
			opacity: 0.25;
		}
	}

	@media (max-width: 1100px) {
		.title-index__header,
		.title-row {
			grid-template-columns: minmax(260px, 1fr) 160px 190px;
		}

		.title-index__header > :nth-child(2),
		.title-row > :nth-child(2) {
			display: none;
		}

		.title-inspector {
			gap: var(--mf-space-6);
			grid-template-columns: minmax(210px, 0.75fr) minmax(320px, 1.25fr);
		}
		.selection-command {
			border-left: 0;
			border-top: 1px solid var(--mf-line-strong);
			grid-column: 1 / -1;
			grid-row: auto;
			grid-template-columns: minmax(0, 1fr);
			padding: var(--mf-space-6) 0 0;
		}

		.selection-command .primary-link {
			max-width: none;
		}
	}

	@media (max-width: 760px) {
		.title-index__header {
			display: none;
		}

		.ledger-row {
			position: relative;
		}

		.title-row {
			min-height: 68px;
			padding-right: 88px;
			grid-template-columns: minmax(0, 1fr) auto;
		}

		.title-row__count,
		.title-row__size {
			display: none;
		}

		.title-row__mobile-meta {
			display: block !important;
		}

		.title-inspector {
			grid-template-columns: minmax(0, 1fr);
			padding: var(--mf-space-6) var(--mf-space-5) var(--mf-space-7);
		}

		.inspector-heading,
		.member-row {
			align-items: stretch;
			flex-direction: column;
		}

		.inspector-heading,
		.inspector-facts,
		.estimate-evidence,
		.inline-alert,
		.policy-strip,
		.member-list,
		.selection-command {
			grid-column: 1;
			grid-row: auto;
			grid-template-columns: minmax(0, 1fr);
		}

		.selection-command .primary-link {
			max-width: none;
		}

		.member-link,
		.primary-link {
			text-align: center;
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
	}

	@media (max-width: 460px) {
		.inspector-facts {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.loading-mark {
			animation: none;
		}
	}
</style>
