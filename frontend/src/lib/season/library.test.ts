import { describe, expect, it } from 'vitest';

import type { FolderCard } from '$lib/api/types';

import {
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
});
