import type { DashboardFoldersPayload, FolderCard } from '$lib/api/types';

import { seasonIdentity } from './experience';

export type LibrarySort = 'savings' | 'size' | 'seasons' | 'name';

export function mergeFolderPayloads(
	structure: DashboardFoldersPayload,
	details: DashboardFoldersPayload
): DashboardFoldersPayload {
	const detailsByPrefix = new Map(details.folders.map((card) => [card.prefix, card]));
	const mergedFolders = structure.folders.map((card) => detailsByPrefix.get(card.prefix) ?? card);
	const knownPrefixes = new Set(mergedFolders.map((card) => card.prefix));
	for (const card of details.folders) {
		if (!knownPrefixes.has(card.prefix)) mergedFolders.push(card);
	}
	const detailSeriesFolders = details.series_folders ?? [];
	return {
		folders: mergedFolders,
		series_folders:
			detailSeriesFolders.length > 0 ? detailSeriesFolders : (structure.series_folders ?? []),
		catalog_empty: structure.catalog_empty && details.catalog_empty,
		folder_cache_key:
			details.folder_cache_key === 'loading' ? structure.folder_cache_key : details.folder_cache_key
	};
}

function addCounts(
	left: Record<string, number>,
	right: Record<string, number>
): Record<string, number> {
	const result = { ...left };
	for (const [key, value] of Object.entries(right)) {
		result[key] = (result[key] ?? 0) + value;
	}
	return result;
}

function aggregateShowCard(showPrefix: string, seasons: FolderCard[]): FolderCard {
	const first = seasons[0];
	const itemCount = seasons.reduce((total, season) => total + Math.max(0, season.item_count), 0);
	const ageWeight = seasons.reduce(
		(total, season) =>
			total + Math.max(0, season.average_age_days) * Math.max(1, season.item_count),
		0
	);
	return {
		...first,
		prefix: showPrefix,
		title: seasonIdentity(first.prefix).show,
		subtitle: 'TV series',
		scope_label: 'Series',
		item_count: itemCount,
		pending_count: seasons.reduce((total, season) => total + Math.max(0, season.pending_count), 0),
		total_size_bytes: seasons.reduce(
			(total, season) => total + Math.max(0, season.total_size_bytes),
			0
		),
		estimated_savings_bytes: seasons.reduce(
			(total, season) => total + Math.max(0, season.estimated_savings_bytes),
			0
		),
		known_saved_bytes: seasons.reduce(
			(total, season) => total + Math.max(0, season.known_saved_bytes),
			0
		),
		projected_reclaim_bytes: seasons.reduce(
			(total, season) => total + Math.max(0, season.projected_reclaim_bytes),
			0
		),
		average_age_days: itemCount > 0 ? Math.round((ageWeight / itemCount) * 10) / 10 : 0,
		sort_score: seasons.reduce((total, season) => total + Math.max(0, season.sort_score), 0),
		statuses: seasons.reduce(
			(counts, season) => addCounts(counts, season.statuses),
			{} as Record<string, number>
		),
		video_codecs: seasons.reduce(
			(counts, season) => addCounts(counts, season.video_codecs),
			{} as Record<string, number>
		),
		review_badge_label: null,
		review_badge_tone: null,
		review_badge_detail: null,
		workflow_state: null,
		details_loading: seasons.some((season) => season.details_loading)
	};
}

export function seasonsByShow(seasons: FolderCard[]): Map<string, FolderCard[]> {
	const grouped = new Map<string, FolderCard[]>();
	for (const season of seasons) {
		const showPrefix = seasonIdentity(season.prefix).showPrefix;
		const showSeasons = grouped.get(showPrefix) ?? [];
		showSeasons.push(season);
		grouped.set(showPrefix, showSeasons);
	}
	return grouped;
}

export function buildShowCards(seriesCards: FolderCard[], seasons: FolderCard[]): FolderCard[] {
	const groupedSeasons = seasonsByShow(seasons);
	const cards = new Map(seriesCards.map((card) => [card.prefix, card]));
	for (const [showPrefix, showSeasons] of groupedSeasons) {
		if (!cards.has(showPrefix) && showSeasons.length) {
			cards.set(showPrefix, aggregateShowCard(showPrefix, showSeasons));
		}
	}
	return [...cards.values()];
}

export function filterShowCards(
	shows: FolderCard[],
	groupedSeasons: Map<string, FolderCard[]>,
	rawQuery: string
): FolderCard[] {
	const normalized = rawQuery.trim().toLowerCase();
	if (!normalized) return shows;
	return shows.filter((show) => {
		if (show.title.toLowerCase().includes(normalized)) return true;
		return (groupedSeasons.get(show.prefix) ?? []).some((season) => {
			const identity = seasonIdentity(season.prefix);
			return `${identity.show} ${identity.season} ${season.title}`
				.toLowerCase()
				.includes(normalized);
		});
	});
}

export function sortShowCards(
	shows: FolderCard[],
	groupedSeasons: Map<string, FolderCard[]>,
	sort: LibrarySort
): FolderCard[] {
	return [...shows].sort((left, right) => {
		if (sort === 'name') return left.title.localeCompare(right.title);
		if (sort === 'seasons') {
			const seasonDifference =
				(groupedSeasons.get(right.prefix)?.length ?? 0) -
				(groupedSeasons.get(left.prefix)?.length ?? 0);
			if (seasonDifference) return seasonDifference;
		}
		if (sort === 'size') {
			const sizeDifference = right.total_size_bytes - left.total_size_bytes;
			if (sizeDifference) return sizeDifference;
		}
		if (sort === 'savings') {
			const savingsDifference = right.projected_reclaim_bytes - left.projected_reclaim_bytes;
			if (savingsDifference) return savingsDifference;
		}
		return left.title.localeCompare(right.title);
	});
}

export function savingsPercent(card: FolderCard): number {
	if (card.total_size_bytes <= 0) return 0;
	return Math.max(
		0,
		Math.min(100, Math.round((card.projected_reclaim_bytes / card.total_size_bytes) * 100))
	);
}
