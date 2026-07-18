import { describe, expect, it } from 'vitest';

import type { FolderCard, LifecycleState, SeasonLifecycleState } from '$lib/api/types';

import {
	applySeriesLifecycle,
	buildShowCards,
	filterShowCards,
	mergeFolderPayloads,
	savingsPercent,
	seasonsByShow,
	sortShowCards
} from './library';

function card(prefix: string, size: number, savings: number): FolderCard {
	return {
		prefix,
		title: prefix.replace('tv/', '').replace('/', ' · '),
		subtitle: '',
		scope_label: 'Season',
		item_count: 10,
		pending_count: 10,
		total_size_bytes: size,
		estimated_savings_bytes: savings,
		known_saved_bytes: 0,
		projected_reclaim_bytes: savings,
		average_age_days: 20,
		sort_score: savings,
		statuses: { discovered: 10 },
		video_codecs: { h264: 10 },
		details_loading: false
	};
}

function seasonLifecycle(prefix: string, held: number): SeasonLifecycleState {
	return {
		prefix,
		label: prefix.split('/').at(-1) ?? prefix,
		season_number: Number(prefix.match(/Season (\d+)$/)?.[1] ?? 0),
		is_special: false,
		ambiguous: false,
		is_current_season: prefix.endsWith('Season 2'),
		candidate_count: 10,
		eligible_candidate_count: 10 - held,
		held_candidate_count: held,
		hold_reasons: held
			? [{ code: 'current_season', label: 'Current season', detail: 'Protected.' }]
			: []
	};
}

function lifecycle(mode: 'auto' | 'on' | 'off', held: number): LifecycleState {
	const seasons = [
		seasonLifecycle('tv/Alpha/Season 1', 0),
		seasonLifecycle('tv/Alpha/Season 2', held)
	];
	return {
		schema_version: 1,
		prefix: 'tv/Alpha',
		series_prefix: 'tv/Alpha',
		policy_mode: mode,
		provider_state: 'active',
		candidate_count: 20,
		eligible_candidate_count: 20 - held,
		held_candidate_count: held,
		hold_reason_counts: held ? { current_season: held } : {},
		seasons
	};
}

describe('season library grouping', () => {
	it('aggregates fallback show cards without losing savings', () => {
		const seasons = [
			card('tv/Big Brother (US)/Season 1', 1000, 700),
			card('tv/Big Brother (US)/Season 2', 2000, 1500)
		];
		const [show] = buildShowCards([], seasons);

		expect(show).toMatchObject({
			prefix: 'tv/Big Brother (US)',
			item_count: 20,
			total_size_bytes: 3000,
			projected_reclaim_bytes: 2200
		});
	});

	it('keeps a show pending until every season has details', () => {
		const seasons = [
			card('tv/Big Brother (US)/Season 1', 1000, 700),
			{ ...card('tv/Big Brother (US)/Season 2', 2000, 0), details_loading: true }
		];

		const [show] = buildShowCards([], seasons);

		expect(show.details_loading).toBe(true);
		expect(show.projected_reclaim_bytes).toBe(700);
	});

	it('sorts shows by projected savings and finds matching seasons', () => {
		const seasons = [card('tv/Alpha/Season 1', 1000, 300), card('tv/Beta/Season 9', 1000, 800)];
		const grouped = seasonsByShow(seasons);
		const shows = buildShowCards([], seasons);

		expect(sortShowCards(shows, grouped, 'savings').map((show) => show.title)).toEqual([
			'Beta',
			'Alpha'
		]);
		expect(filterShowCards(shows, grouped, 'season 9').map((show) => show.title)).toEqual(['Beta']);
	});

	it('keeps equal-size shows alphabetic when savings hydrate', () => {
		const structure = [
			{ ...card('tv/Alpha/Season 1', 1000, 0), details_loading: true },
			{ ...card('tv/Beta/Season 1', 1000, 0), details_loading: true }
		];
		const hydrated = [card('tv/Alpha/Season 1', 1000, 100), card('tv/Beta/Season 1', 1000, 900)];

		expect(
			sortShowCards(buildShowCards([], structure), seasonsByShow(structure), 'size').map(
				(show) => show.title
			)
		).toEqual(['Alpha', 'Beta']);
		expect(
			sortShowCards(buildShowCards([], hydrated), seasonsByShow(hydrated), 'size').map(
				(show) => show.title
			)
		).toEqual(['Alpha', 'Beta']);
	});

	it('reports the projected savings share', () => {
		expect(savingsPercent(card('tv/Alpha/Season 1', 1000, 725))).toBe(73);
	});

	it('hydrates structure in place without removing unmatched seasons', () => {
		const alpha = { ...card('tv/Alpha/Season 1', 1000, 0), details_loading: true };
		const beta = { ...card('tv/Beta/Season 1', 2000, 0), details_loading: true };
		const hydratedAlpha = card('tv/Alpha/Season 1', 1000, 700);

		const merged = mergeFolderPayloads(
			{
				folders: [beta, alpha],
				series_folders: [],
				catalog_empty: false,
				folder_cache_key: 'structure'
			},
			{
				folders: [hydratedAlpha],
				series_folders: [],
				catalog_empty: false,
				folder_cache_key: 'details'
			}
		);

		expect(merged.folders.map((entry) => entry.prefix)).toEqual([
			'tv/Beta/Season 1',
			'tv/Alpha/Season 1'
		]);
		expect(merged.folders[0].details_loading).toBe(true);
		expect(merged.folders[1].projected_reclaim_bytes).toBe(700);
		expect(merged.folder_cache_key).toBe('details');
	});

	it('appends seasons discovered by the detail request after stable structure rows', () => {
		const alpha = { ...card('tv/Alpha/Season 1', 1000, 0), details_loading: true };
		const beta = card('tv/Beta/Season 1', 2000, 900);

		const merged = mergeFolderPayloads(
			{
				folders: [alpha],
				series_folders: [],
				catalog_empty: false,
				folder_cache_key: 'structure'
			},
			{
				folders: [beta],
				series_folders: [],
				catalog_empty: false,
				folder_cache_key: 'details'
			}
		);

		expect(merged.folders.map((entry) => entry.prefix)).toEqual([
			'tv/Alpha/Season 1',
			'tv/Beta/Season 1'
		]);
	});

	it('applies a saved series lifecycle to its show and individual season cards', () => {
		const before = lifecycle('auto', 10);
		const seasons = [
			{
				...card('tv/Alpha/Season 1', 1000, 300),
				lifecycle: { ...before, seasons: [before.seasons[0]] }
			},
			{
				...card('tv/Alpha/Season 2', 1000, 300),
				lifecycle: { ...before, seasons: [before.seasons[1]] }
			}
		];
		const [show] = buildShowCards([], seasons);
		const saved = lifecycle('off', 0);

		const updated = applySeriesLifecycle(
			{
				folders: seasons,
				series_folders: [show],
				catalog_empty: false,
				folder_cache_key: 'details'
			},
			saved
		);

		expect(updated.series_folders?.[0].lifecycle?.policy_mode).toBe('off');
		expect(updated.folders.map((entry) => entry.lifecycle?.policy_mode)).toEqual(['off', 'off']);
		expect(updated.folders[0].lifecycle?.seasons.map((season) => season.prefix)).toEqual([
			'tv/Alpha/Season 1'
		]);
		expect(updated.folders[1].lifecycle).toMatchObject({
			eligible_candidate_count: 10,
			held_candidate_count: 0,
			hold_reason_counts: {}
		});
	});
});
