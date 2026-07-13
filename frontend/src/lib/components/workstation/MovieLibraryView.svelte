<script lang="ts">
	import { resolve } from '$app/paths';

	import type { MovieLibraryPayload, MovieMember, MovieTitle } from '$lib/api/types';
	import { folderRoutePath } from '$lib/folder-display';
	import { movieReclaimLowerBound, movieReclaimTotalIsLowerBound } from '$lib/movies/library';
	import LibraryModeNav from './LibraryModeNav.svelte';

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
	let stateFilter = $state<'all' | 'attention' | 'ready' | 'processing' | 'explicit'>('all');
	let sortMode = $state<'priority' | 'name' | 'size' | 'savings' | 'oldest'>('priority');
	let selectedPrefix = $state('');

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
			return stateFilter === 'all' || titleStateGroup(title) === stateFilter;
		});
		return [...filtered].sort((left, right) => compareTitles(left, right, sortMode));
	});

	const selectedTitle = $derived(
		titles.find((title) => title.prefix === selectedPrefix) ?? titles[0] ?? null
	);
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
	const actionableCount = $derived(
		payload.titles.filter((title) =>
			['encode', 'validate', 'promote', 'processing', 'attention'].includes(
				title.workflow_state?.primary_lane ?? ''
			)
		).length
	);
	const conflictCount = $derived(
		payload.titles.reduce((total, title) => total + title.promotion_conflicts.length, 0)
	);

	$effect(() => {
		if (selectedTitle && selectedPrefix !== selectedTitle.prefix)
			selectedPrefix = selectedTitle.prefix;
	});

	function compareTitles(left: MovieTitle, right: MovieTitle, mode: typeof sortMode): number {
		if (mode === 'name') return left.title.localeCompare(right.title);
		if (mode === 'size') return right.total_size_bytes - left.total_size_bytes;
		if (mode === 'savings')
			return (right.projected_reclaim_bytes ?? -1) - (left.projected_reclaim_bytes ?? -1);
		if (mode === 'oldest')
			return ageValue(left) - ageValue(right) || left.title.localeCompare(right.title);
		return titlePriority(left) - titlePriority(right) || left.title.localeCompare(right.title);
	}

	function titlePriority(title: MovieTitle): number {
		if (title.promotion_conflicts.length) return 0;
		return {
			attention: 1,
			processing: 2,
			validate: 3,
			promote: 4,
			encode: 5,
			mixed: 6,
			none: 7,
			complete: 8,
			blocked: 9
		}[title.workflow_state?.primary_lane ?? 'none'];
	}

	function titleStateGroup(title: MovieTitle): typeof stateFilter {
		if (title.promotion_conflicts.length || title.workflow_state?.primary_lane === 'attention') {
			return 'attention';
		}
		if (title.workflow_state?.primary_lane === 'processing') return 'processing';
		if (title.workflow_state?.state === 'explicit_selection_required') return 'explicit';
		if (
			['encode', 'validate', 'promote', 'mixed'].includes(title.workflow_state?.primary_lane ?? '')
		) {
			return 'ready';
		}
		return 'all';
	}

	function ageValue(title: MovieTitle): number {
		const timestamp = title.age?.timestamp;
		return timestamp ? Date.parse(timestamp) || Number.MAX_SAFE_INTEGER : Number.MAX_SAFE_INTEGER;
	}

	function formatBytes(value: number | null | undefined): string {
		if (value == null) return 'No estimate';
		if (value < 1024) return `${value} B`;
		const units = ['KB', 'MB', 'GB', 'TB'];
		let size = value;
		let unit = -1;
		while (size >= 1024 && unit < units.length - 1) {
			size /= 1024;
			unit += 1;
		}
		return `${size >= 10 ? size.toFixed(0) : size.toFixed(1)} ${units[unit]}`;
	}

	function formatReclaim(title: MovieTitle): string {
		const lowerBound = movieReclaimLowerBound(title);
		if (lowerBound == null) return 'No estimate';
		return title.projected_reclaim_bytes == null
			? `At least ${formatBytes(lowerBound)}`
			: formatBytes(lowerBound);
	}

	function formatAge(title: MovieTitle): string {
		if (title.details_loading) return 'Age pending';
		if (!title.age?.timestamp) return 'Age unavailable';
		const date = new Intl.DateTimeFormat(undefined, {
			year: 'numeric',
			month: 'short',
			day: 'numeric'
		}).format(new Date(title.age.timestamp));
		return `${date} · ${ageSourceLabel(title.age.source)}`;
	}

	function ageSourceLabel(source: string): string {
		if (source === 'plex') return 'Plex added';
		if (source === 'mediaforce_discovered') return 'first scan';
		if (source === 'filesystem_mtime') return 'file modified';
		return 'unknown source';
	}

	function memberLabel(member: MovieMember): string {
		if (member.edition_label) return member.edition_label;
		if (member.role === 'feature') return 'Main feature';
		if (member.role === 'extra') return member.extra_category ?? 'Extra';
		return 'Uncertain membership';
	}

	function workflowTone(title: MovieTitle): string {
		if (title.promotion_conflicts.length) return 'attention';
		return title.workflow_state?.tone ?? 'idle';
	}

	function policyValue(title: MovieTitle, key: string, fallback: string): string {
		const value = title.policy[key];
		return typeof value === 'string' && value ? value : fallback;
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
		selectedPrefix = nextTitle.prefix;
		requestAnimationFrame(() => {
			document
				.querySelector<HTMLElement>(`[data-movie-title-row="${CSS.escape(nextTitle.prefix)}"]`)
				?.focus();
		});
	}
</script>

<svelte:head>
	<title>Your movie library · Mediaforce</title>
	<meta
		name="description"
		content="Inspect movie titles, editions, extras, workflow state, and safe promotion readiness."
	/>
</svelte:head>

<main class="movie-library">
	<LibraryModeNav active="movie" />

	<header class="page-heading">
		<div class="page-heading__copy">
			<span class="eyebrow">Movie workstation</span>
			<h1>Movies</h1>
			<p>
				Process one title, edition, or deliberate extra without pulling unrelated files into the
				run.
			</p>
		</div>
		<div class="library-totals" aria-label="Movie library totals">
			<div><strong>{payload.titles.length}</strong><span>titles</span></div>
			<div><strong>{formatBytes(totalSize)}</strong><span>stored</span></div>
			<div>
				<strong
					>{detailsPending
						? '…'
						: reclaimCoverage === 0
							? 'No estimate'
							: reclaimHasUnknowns
								? `At least ${formatBytes(totalReclaim)}`
								: formatBytes(totalReclaim)}</strong
				><span>projected reclaim</span>
			</div>
			<div><strong>{actionableCount}</strong><span>active or ready</span></div>
		</div>
	</header>

	{#if loadError}
		<div class="notice notice--danger" role="alert">
			<strong>The movie library could not open.</strong>
			<span>{loadError}</span>
		</div>
	{/if}
	{#if detailsError && payload.titles.length}
		<div class="notice" role="status">
			<strong>Titles are available.</strong>
			<span>{detailsError} Workflow and savings details may be stale.</span>
		</div>
	{/if}
	{#if conflictCount}
		<div class="notice notice--danger" role="alert">
			<strong
				>{conflictCount} promotion {conflictCount === 1 ? 'conflict' : 'conflicts'} need review.</strong
			>
			<span>No conflicting destination is replaced automatically.</span>
		</div>
	{/if}

	<section class="workbench" aria-busy={structurePending}>
		<header class="workbench__toolbar">
			<div class="search-field">
				<label for="movie-search">Find title or edition</label>
				<input
					id="movie-search"
					bind:value={query}
					type="search"
					placeholder="Search movie files"
				/>
			</div>
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
				<span>State</span>
				<select bind:value={stateFilter}>
					<option value="all">All states</option>
					<option value="attention">Needs attention</option>
					<option value="processing">Processing</option>
					<option value="ready">Ready work</option>
					<option value="explicit">Explicit selection</option>
				</select>
			</label>
			<label>
				<span>Sort</span>
				<select bind:value={sortMode}>
					<option value="priority">Operational priority</option>
					<option value="name">Title A–Z</option>
					<option value="size">Largest stored</option>
					<option value="savings">Most reclaim</option>
					<option value="oldest">Oldest added</option>
				</select>
			</label>
		</header>

		{#if structurePending && !payload.titles.length}
			<div class="empty-state" role="status">
				<span class="loading-mark" aria-hidden="true"></span>
				<strong>Reading movie titles</strong>
				<p>Files appear first; workflow details follow.</p>
			</div>
		{:else if !payload.titles.length && !loadError}
			<div class="empty-state">
				<strong>No movie titles are indexed yet.</strong>
				<p>
					Add or scan a typed Movies root in Settings. Root-level files and title folders both
					appear here.
				</p>
				<a class="primary-link" href={resolve('/settings')}>Open Settings</a>
			</div>
		{:else if !titles.length}
			<div class="empty-state">
				<strong>No titles match these filters.</strong>
				<p>Clear the search or widen the workflow state filter.</p>
			</div>
		{:else}
			<div class="workbench__body">
				<div class="title-index" aria-label="Movie titles">
					<div class="title-index__header" aria-hidden="true">
						<span>Title</span><span>Files</span><span>Stored</span><span>Workflow</span>
					</div>
					{#each titles as title, index (title.prefix)}
						<button
							type="button"
							class="title-row"
							class:selected={selectedTitle?.prefix === title.prefix}
							data-movie-title-row={title.prefix}
							tabindex={selectedTitle?.prefix === title.prefix ? 0 : -1}
							onclick={() => (selectedPrefix = title.prefix)}
							onkeydown={(event) => moveTitleSelection(event, index)}
							aria-pressed={selectedTitle?.prefix === title.prefix}
						>
							<span class="title-row__identity">
								<strong>{title.title}</strong>
								<small
									>{title.scope_mode === 'single_file'
										? 'Exact movie file'
										: title.library_label}</small
								>
							</span>
							<span class="title-row__count">
								<strong>{title.item_count}</strong>
								<small>
									{title.feature_count}
									{title.feature_count === 1 ? 'feature' : 'features'}
									{#if title.extra_count}
										· {title.extra_count} extras{/if}
								</small>
							</span>
							<span class="title-row__size">
								<strong>{formatBytes(title.total_size_bytes)}</strong>
								<small>
									{title.details_loading
										? 'Reclaim pending'
										: title.savings_confidence === 'unavailable'
											? 'No estimate yet'
											: `${formatReclaim(title)} reclaim · ${title.savings_confidence}`}
								</small>
							</span>
							<span class="state-badge" data-tone={workflowTone(title)}>
								{title.promotion_conflicts.length
									? 'Promotion conflict'
									: (title.workflow_state?.label ?? (title.details_loading ? 'Loading' : 'Ready'))}
							</span>
						</button>
					{/each}
				</div>

				{#if selectedTitle}
					<aside class="title-inspector" aria-label={`${selectedTitle.title} details`}>
						<header class="inspector-heading">
							<div>
								<span class="eyebrow">{selectedTitle.library_label}</span>
								<h2>{selectedTitle.title}</h2>
								<p>{selectedTitle.prefix}</p>
							</div>
							<span class="state-badge" data-tone={workflowTone(selectedTitle)}>
								{selectedTitle.workflow_state?.label ?? 'Loading'}
							</span>
						</header>

						<div class="inspector-facts">
							<div>
								<span>Scope</span><strong
									>{selectedTitle.scope_mode === 'single_file'
										? 'Exact file'
										: 'Movie title'}</strong
								>
							</div>
							<div>
								<span>Stored</span><strong>{formatBytes(selectedTitle.total_size_bytes)}</strong>
							</div>
							<div>
								<span>Projected reclaim</span><strong
									>{selectedTitle.details_loading
										? 'Pending'
										: formatReclaim(selectedTitle)}</strong
								>
							</div>
							<div><span>Ranking age</span><strong>{formatAge(selectedTitle)}</strong></div>
						</div>

						{#if selectedTitle.availability === 'browse_only'}
							<div class="inline-alert">
								<strong>Browse only</strong>
								<span
									>Files remain visible, but processing actions are disabled for this library.</span
								>
							</div>
						{/if}
						{#if selectedTitle.promotion_conflicts.length}
							<div class="inline-alert inline-alert--danger">
								<strong>Promotion blocked</strong>
								{#each selectedTitle.promotion_conflicts as conflict (`${conflict.kind}:${conflict.destination_path}`)}
									<span>{conflict.detail} <code>{conflict.destination_path}</code></span>
								{/each}
							</div>
						{/if}

						<div class="policy-strip" aria-label="Movie library policy">
							<span>Titles grouped</span>
							<span>Editions {policyValue(selectedTitle, 'editions', 'separate')}</span>
							<span>Extras {policyValue(selectedTitle, 'extras', 'exclude')}</span>
						</div>

						<section class="member-list" aria-labelledby="movie-files-heading">
							<div class="section-heading">
								<div>
									<h3 id="movie-files-heading">Files and editions</h3>
									<p>
										Every indexed file stays reachable. Only identified features join title-wide
										work by default.
									</p>
								</div>
								<span>{selectedTitle.members.length}</span>
							</div>
							{#each selectedTitle.members as member (member.item_id)}
								<div class="member-row" data-role={member.role}>
									<div class="member-row__copy">
										<div class="member-row__heading">
											<strong>{memberLabel(member)}</strong>
											<span>{member.status.replaceAll('_', ' ')}</span>
										</div>
										<p>{member.label}</p>
										<small>
											{formatBytes(member.size_bytes)}
											{#if member.video_codec}
												· {member.video_codec.toUpperCase()}{/if}
											{#if member.included_by_default}
												· Included in title work{:else}
												· Exact selection only{/if}
										</small>
										{#if member.selection_blocker && !member.included_by_default}
											<span class="member-row__reason">{member.selection_blocker}</span>
										{/if}
									</div>
									<a class="member-link" href={resolve(folderRoutePath(member.prefix))}
										>Open exact file</a
									>
								</div>
							{/each}
						</section>

						<footer class="inspector-actions">
							<a class="primary-link" href={resolve(folderRoutePath(selectedTitle.prefix))}>
								Open {selectedTitle.scope_mode === 'single_file' ? 'file' : 'title'} Studio
							</a>
							<span>{selectedTitle.workflow_state?.detail ?? 'Workflow details are loading.'}</span>
						</footer>
					</aside>
				{/if}
			</div>
		{/if}
	</section>
</main>

<style>
	.movie-library {
		margin: 0 auto;
		max-width: 1400px;
		padding: 30px 28px 54px;
	}

	.page-heading {
		align-items: end;
		display: flex;
		gap: 28px;
		justify-content: space-between;
		margin-bottom: 22px;
	}

	.page-heading__copy {
		max-width: 660px;
	}

	.eyebrow {
		color: var(--mf-fg-muted);
		font-size: 11px;
		font-weight: 800;
		letter-spacing: 0.12em;
		text-transform: uppercase;
	}

	h1,
	h2,
	h3,
	p {
		margin: 0;
	}

	h1 {
		font-size: clamp(30px, 4vw, 44px);
		letter-spacing: -0.04em;
		margin-top: 4px;
	}

	.page-heading p,
	.inspector-heading p,
	.section-heading p,
	.inspector-actions span {
		color: var(--mf-fg-secondary);
		font-size: 13px;
		line-height: 1.55;
	}

	.library-totals {
		background: var(--mf-bg-panel);
		border: 1px solid var(--mf-line);
		display: grid;
		grid-template-columns: repeat(4, minmax(90px, 1fr));
		min-width: min(100%, 460px);
	}

	.library-totals div {
		border-left: 1px solid var(--mf-line);
		padding: 14px 16px;
	}

	.library-totals div:first-child {
		border-left: 0;
	}

	.library-totals strong,
	.library-totals span {
		display: block;
	}

	.library-totals strong {
		font-size: 17px;
	}

	.library-totals span {
		color: var(--mf-fg-muted);
		font-size: 10px;
		font-weight: 700;
		letter-spacing: 0.06em;
		margin-top: 3px;
		text-transform: uppercase;
	}

	.notice,
	.inline-alert {
		background: var(--mf-bg-panel);
		border: 1px solid var(--mf-line-strong);
		display: grid;
		gap: 4px;
		margin-bottom: 14px;
		padding: 12px 14px;
	}

	.notice span,
	.inline-alert span {
		color: var(--mf-fg-secondary);
		font-size: 12px;
	}

	.notice--danger,
	.inline-alert--danger {
		border-left: 4px solid var(--mf-fail-fg);
	}

	.inline-alert {
		margin: 0;
	}

	.inline-alert code {
		font-size: 11px;
		word-break: break-all;
	}

	.workbench {
		background: var(--mf-bg-panel);
		border: 1px solid var(--mf-line);
		box-shadow: var(--mf-shadow-popover);
	}

	.workbench__toolbar {
		align-items: end;
		border-bottom: 1px solid var(--mf-line);
		display: grid;
		gap: 12px;
		grid-template-columns: minmax(240px, 1fr) repeat(3, minmax(130px, auto));
		padding: 14px;
	}

	.workbench__toolbar label,
	.search-field {
		display: grid;
		gap: 5px;
	}

	.workbench__toolbar label > span,
	.search-field label {
		color: var(--mf-fg-muted);
		font-size: 10px;
		font-weight: 800;
		letter-spacing: 0.08em;
		text-transform: uppercase;
	}

	input,
	select {
		background: var(--mf-bg-input);
		border: 1px solid var(--mf-line-strong);
		border-radius: 4px;
		color: var(--mf-fg-primary);
		font: inherit;
		font-size: 13px;
		min-height: 38px;
		padding: 0 10px;
	}

	.workbench__body {
		display: grid;
		grid-template-columns: minmax(520px, 1.12fr) minmax(390px, 0.88fr);
		min-height: 570px;
	}

	.title-index {
		border-right: 1px solid var(--mf-line);
		min-width: 0;
	}

	.title-index__header,
	.title-row {
		display: grid;
		grid-template-columns: minmax(210px, 1.5fr) minmax(105px, 0.65fr) minmax(120px, 0.7fr) minmax(
				130px,
				0.8fr
			);
	}

	.title-index__header {
		background: var(--mf-bg-strip);
		border-bottom: 1px solid var(--mf-line);
		color: var(--mf-fg-muted);
		font-size: 10px;
		font-weight: 800;
		letter-spacing: 0.08em;
		padding: 9px 14px;
		text-transform: uppercase;
	}

	.title-row {
		align-items: center;
		background: transparent;
		border: 0;
		border-bottom: 1px solid var(--mf-line);
		color: inherit;
		cursor: pointer;
		font: inherit;
		gap: 12px;
		padding: 13px 14px;
		text-align: left;
		width: 100%;
	}

	.title-row:hover,
	.title-row.selected {
		background: var(--mf-active-bg);
	}

	.title-row.selected {
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

	.title-row__identity strong {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.title-row small {
		color: var(--mf-fg-muted);
		font-size: 11px;
		line-height: 1.4;
		margin-top: 3px;
	}

	.state-badge {
		border: 1px solid var(--mf-line-strong);
		color: var(--mf-fg-secondary);
		display: inline-flex;
		font-size: 10px;
		font-weight: 800;
		justify-self: start;
		letter-spacing: 0.04em;
		line-height: 1.3;
		padding: 5px 7px;
		text-transform: uppercase;
	}

	.state-badge[data-tone='active'] {
		border-color: var(--mf-active-fg);
		color: var(--mf-active-fg);
	}

	.state-badge[data-tone='attention'] {
		border-color: var(--mf-fail-fg);
		color: var(--mf-fail-fg);
	}

	.state-badge[data-tone='ready'] {
		border-color: var(--mf-wait-fg);
		color: var(--mf-wait-fg);
	}

	.state-badge[data-tone='success'] {
		border-color: var(--mf-ready-fg);
		color: var(--mf-ready-fg);
	}

	.title-inspector {
		display: grid;
		gap: 16px;
		min-width: 0;
		padding: 20px;
	}

	.inspector-heading,
	.section-heading,
	.inspector-actions {
		align-items: start;
		display: flex;
		gap: 16px;
		justify-content: space-between;
	}

	.inspector-heading h2 {
		font-size: 24px;
		letter-spacing: -0.025em;
		margin: 4px 0;
	}

	.inspector-heading p {
		font-family: var(--mf-font-mono);
		font-size: 11px;
		word-break: break-all;
	}

	.inspector-facts {
		border: 1px solid var(--mf-line);
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
	}

	.inspector-facts div {
		border-bottom: 1px solid var(--mf-line);
		border-right: 1px solid var(--mf-line);
		padding: 10px 12px;
	}

	.inspector-facts div:nth-child(2n) {
		border-right: 0;
	}

	.inspector-facts div:nth-last-child(-n + 2) {
		border-bottom: 0;
	}

	.inspector-facts span,
	.inspector-facts strong {
		display: block;
	}

	.inspector-facts span {
		color: var(--mf-fg-muted);
		font-size: 10px;
		font-weight: 800;
		letter-spacing: 0.07em;
		text-transform: uppercase;
	}

	.inspector-facts strong {
		font-size: 12px;
		margin-top: 4px;
	}

	.policy-strip {
		display: flex;
		flex-wrap: wrap;
		gap: 7px;
	}

	.policy-strip span {
		background: var(--mf-bg-strip);
		border: 1px solid var(--mf-line);
		color: var(--mf-fg-secondary);
		font-size: 10px;
		font-weight: 700;
		padding: 5px 7px;
		text-transform: capitalize;
	}

	.member-list {
		display: grid;
		gap: 8px;
	}

	.section-heading {
		border-bottom: 1px solid var(--mf-line);
		padding-bottom: 9px;
	}

	.section-heading h3 {
		font-size: 15px;
	}

	.section-heading > span {
		color: var(--mf-fg-muted);
		font-size: 12px;
		font-weight: 800;
	}

	.member-row {
		align-items: center;
		border: 1px solid var(--mf-line);
		display: flex;
		gap: 12px;
		justify-content: space-between;
		padding: 10px 11px;
	}

	.member-row[data-role='extra'],
	.member-row[data-role='uncertain'] {
		border-left: 3px solid var(--mf-line-strong);
	}

	.member-row__copy {
		min-width: 0;
	}

	.member-row__heading {
		align-items: baseline;
		display: flex;
		gap: 8px;
	}

	.member-row__heading span,
	.member-row small,
	.member-row__reason {
		color: var(--mf-fg-muted);
		font-size: 10px;
	}

	.member-row__heading span {
		text-transform: capitalize;
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
		background: var(--mf-active-fg);
		border: 1px solid var(--mf-active-fg);
		border-radius: 4px;
		color: var(--mf-fg-on-accent);
		font-size: 11px;
		font-weight: 800;
		padding: 8px 10px;
		text-decoration: none;
		white-space: nowrap;
	}

	.member-link {
		background: transparent;
		color: var(--mf-active-fg);
	}

	.inspector-actions {
		align-items: center;
		border-top: 1px solid var(--mf-line);
		padding-top: 14px;
	}

	.inspector-actions span {
		max-width: 56%;
		text-align: right;
	}

	.empty-state {
		align-items: center;
		display: grid;
		gap: 7px;
		justify-items: center;
		min-height: 420px;
		padding: 44px;
		text-align: center;
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

	@keyframes pulse {
		50% {
			opacity: 0.25;
		}
	}

	@media (max-width: 900px) {
		.page-heading {
			align-items: stretch;
			flex-direction: column;
		}

		.library-totals {
			min-width: 0;
		}

		.workbench__body {
			grid-template-columns: minmax(0, 1fr);
		}

		.title-index {
			border-bottom: 1px solid var(--mf-line);
			border-right: 0;
		}
	}

	@media (max-width: 760px) {
		.movie-library {
			padding: 20px 12px 42px;
		}

		.library-totals {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}

		.library-totals div:nth-child(3) {
			border-left: 0;
			border-top: 1px solid var(--mf-line);
		}

		.library-totals div:nth-child(4) {
			border-top: 1px solid var(--mf-line);
		}

		.workbench__toolbar {
			grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
		}

		.search-field {
			grid-column: 1 / -1;
		}

		.title-index__header {
			display: none;
		}

		.title-row {
			grid-template-columns: minmax(0, 1fr) auto;
		}

		.title-row__count,
		.title-row__size {
			display: none;
		}

		.title-inspector {
			padding: 16px;
		}

		.inspector-heading,
		.inspector-actions,
		.member-row {
			align-items: stretch;
			flex-direction: column;
		}

		.inspector-actions span {
			max-width: none;
			text-align: left;
		}

		.member-link,
		.primary-link {
			text-align: center;
		}
	}

	@media (max-width: 460px) {
		.workbench__toolbar,
		.inspector-facts {
			grid-template-columns: minmax(0, 1fr);
		}

		.inspector-facts div,
		.inspector-facts div:nth-child(2n),
		.inspector-facts div:nth-last-child(-n + 2) {
			border-bottom: 1px solid var(--mf-line);
			border-right: 0;
		}

		.inspector-facts div:last-child {
			border-bottom: 0;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.loading-mark {
			animation: none;
		}
	}
</style>
