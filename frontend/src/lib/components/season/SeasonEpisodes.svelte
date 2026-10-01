<script lang="ts">
	import { fetchJson } from '$lib/api/client';
	import type { EpisodeProgress, FolderEpisodesPayload } from '$lib/api/types';
	import StateBadge from '$lib/components/workstation/StateBadge.svelte';
	import {
		episodeDetail,
		episodeListCopy,
		episodeListStartsCollapsed,
		episodeName,
		episodeStageCopy,
		episodeSummary,
		focusedEpisodes,
		hasEpisodeProgress
	} from '$lib/season/episodes';

	let {
		prefix,
		refreshKey
	}: {
		prefix: string;
		/** Any value that changes when the season's status refreshes; the list reloads with it. */
		refreshKey: unknown;
	} = $props();

	// The page's own decision blocks; a row links to one only while it is on screen.
	const SIZE_HELD_ANCHOR = 'season-size-held';
	const DECISIONS_ANCHOR = 'season-decisions';

	let episodes = $state<EpisodeProgress[]>([]);
	let loadFailed = $state(false);
	let expanded = $state(false);
	let anchorsOnPage = $state<string[]>([]);
	let loadedPrefix = '';
	let generation = 0;
	let retryTimer: ReturnType<typeof setTimeout> | undefined;
	const RETRY_AFTER_MS = 15000;

	// Only a question the page can actually answer gets a link; other reasons have no control above.
	function answerAnchor(episode: EpisodeProgress): string {
		if (episode.stage !== 'needs_you') return '';
		const target =
			episode.owner_action === 'keep_or_remake'
				? SIZE_HELD_ANCHOR
				: episode.size_question
					? DECISIONS_ANCHOR
					: '';
		return target && anchorsOnPage.includes(target) ? target : '';
	}

	const collapsed = $derived(episodeListStartsCollapsed(episodes) && !expanded);
	const visible = $derived(collapsed ? focusedEpisodes(episodes) : episodes);

	async function load(currentPrefix: string) {
		const request = ++generation;
		clearTimeout(retryTimer);
		try {
			const encoded = currentPrefix
				.split('/')
				.map((segment) => encodeURIComponent(segment))
				.join('/');
			const payload = await fetchJson<FolderEpisodesPayload>(`/api/folders/${encoded}/episodes`);
			if (request !== generation) return;
			episodes = payload.available ? payload.episodes : [];
			loadFailed = false;
			anchorsOnPage = [SIZE_HELD_ANCHOR, DECISIONS_ANCHOR].filter((id) =>
				document.getElementById(id)
			);
		} catch {
			// Keep the last list on a failed refresh; with nothing to show, say so and try again, since
			// a finished season's status no longer refreshes on its own.
			if (request === generation && episodes.length === 0) {
				loadFailed = true;
				retryTimer = setTimeout(() => void load(currentPrefix), RETRY_AFTER_MS);
			}
		}
	}

	$effect(() => {
		void refreshKey;
		const currentPrefix = prefix;
		if (currentPrefix !== loadedPrefix) {
			loadedPrefix = currentPrefix;
			episodes = [];
			expanded = false;
		}
		void load(currentPrefix);
		return () => {
			// A request still in flight belongs to a page that is gone; it must not schedule a retry.
			generation += 1;
			clearTimeout(retryTimer);
		};
	});
</script>

{#if hasEpisodeProgress(episodes)}
	<section class="season-episodes" aria-labelledby="season-episodes-title">
		<header class="season-episodes__header">
			<h2 id="season-episodes-title">{episodeListCopy.title}</h2>
			<p>{episodeSummary(episodes)}</p>
		</header>
		<ul class="season-episodes__rows" id="season-episodes-rows">
			{#each visible as episode (episode.rel_path)}
				{@const copy = episodeStageCopy[episode.stage]}
				{@const detail = episodeDetail(episode)}
				{@const anchor = answerAnchor(episode)}
				<li class="season-episodes__row" data-episode-stage={episode.stage}>
					<strong class="season-episodes__name">{episodeName(episode.rel_path)}</strong>
					<StateBadge tone={copy.tone} label={copy.label} compact />
					<span class="season-episodes__detail">
						{detail}
						{#if anchor}
							<a href={`#${anchor}`}>{episodeListCopy.answer}</a>
						{/if}
					</span>
				</li>
			{/each}
		</ul>
		{#if episodeListStartsCollapsed(episodes)}
			<button
				class="season-episodes__toggle"
				type="button"
				aria-expanded={expanded}
				aria-controls="season-episodes-rows"
				onclick={() => (expanded = !expanded)}
			>
				{expanded ? episodeListCopy.showFewer : episodeListCopy.showAll(episodes.length)}
			</button>
		{/if}
	</section>
{:else if loadFailed}
	<p class="season-episodes__unavailable" role="status">{episodeListCopy.unavailable}</p>
{/if}

<style>
	.season-episodes {
		background: var(--mf-bg-panel);
		border: 1px solid var(--mf-line);
		border-radius: var(--mf-radius-3);
		margin: 18px 0 0;
		padding: 16px 18px;
	}

	.season-episodes__header {
		align-items: baseline;
		display: flex;
		flex-wrap: wrap;
		gap: 4px 16px;
		justify-content: space-between;
		padding-bottom: 12px;
	}

	.season-episodes__header h2,
	.season-episodes__header p {
		margin: 0;
	}

	.season-episodes__header h2 {
		font-size: 18px;
		letter-spacing: -0.02em;
	}

	.season-episodes__header p,
	.season-episodes__unavailable {
		color: var(--mf-fg-secondary);
		font-size: 12px;
		font-variant-numeric: tabular-nums;
	}

	.season-episodes__rows {
		list-style: none;
		margin: 0;
		padding: 0;
	}

	.season-episodes__row {
		align-items: center;
		border-top: 1px solid var(--mf-line-muted);
		display: grid;
		gap: 4px 12px;
		grid-template-columns: minmax(160px, 1fr) minmax(150px, auto) minmax(0, 1.4fr);
		min-height: 44px;
		padding: 7px 10px;
	}

	.season-episodes__name {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.season-episodes__detail {
		color: var(--mf-fg-secondary);
		font-size: 12px;
		font-variant-numeric: tabular-nums;
		min-width: 0;
	}

	.season-episodes__detail a {
		color: var(--mf-active-fg);
		margin-left: 6px;
		white-space: nowrap;
	}

	.season-episodes__toggle {
		background: none;
		border: 0;
		border-top: 1px solid var(--mf-line-muted);
		color: var(--mf-active-fg);
		cursor: pointer;
		font: inherit;
		font-size: 13px;
		padding: 10px 10px 0;
		text-align: left;
		width: 100%;
	}

	@media (max-width: 760px) {
		.season-episodes__row {
			grid-template-columns: minmax(0, 1fr) auto;
			padding-block: 10px;
		}

		.season-episodes__detail {
			grid-column: 1 / -1;
		}
	}
</style>
