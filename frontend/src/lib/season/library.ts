import type {
	DashboardFoldersPayload,
	FolderCard,
	LifecycleState,
	SeasonLifecycleState
} from '$lib/api/types';

import { seasonIdentity, seasonNumberLabel } from './experience';

export type LibrarySort = 'savings' | 'size' | 'seasons' | 'name';
export type TvLibraryStateKey = 'attention' | 'processing' | 'ready' | 'idle';

export interface TvLibraryStateGroup {
	key: TvLibraryStateKey;
	label: string;
	tone: 'active' | 'ready' | 'wait' | 'fail' | 'idle';
}

export interface OlderSeasonLibraryAction {
	latestSeasonLabel: string;
	seasonCount: number;
	episodeCount: number;
	overriddenSeasonCount: number;
	overriddenEpisodeCount: number;
}

const OVERRIDEABLE_HOLD_CODES = new Set(['current_season', 'recent_acquisition']);

export function tvLibraryStateGroup(
	states: Array<{ key?: string; tone: string }>
): TvLibraryStateGroup {
	const keys = new Set(states.map((state) => state.key));
	const tones = new Set(states.map((state) => normalizeLibraryTone(state.tone)));
	if (tones.has('fail') || tones.has('wait')) {
		return { key: 'attention', tone: 'fail', label: 'Needs attention' };
	}
	if (tones.has('active') || keys.has('sample_waiting')) {
		return { key: 'processing', tone: 'active', label: 'In progress' };
	}
	if (tones.has('ready') || keys.has('needs_test')) {
		return { key: 'ready', tone: 'ready', label: 'Ready to act on' };
	}
	return { key: 'idle', tone: 'idle', label: 'No active work' };
}

function normalizeLibraryTone(tone: string): TvLibraryStateGroup['tone'] {
	if (tone === 'attention' || tone === 'fail') return 'fail';
	if (tone === 'ready') return 'ready';
	if (tone === 'active') return 'active';
	if (tone === 'wait') return 'wait';
	return 'idle';
}

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

function lifecycleForSeason(
	lifecycle: LifecycleState,
	season: SeasonLifecycleState
): LifecycleState {
	const canOverrideHolds =
		season.held_candidate_count > 0 &&
		season.hold_reasons.length > 0 &&
		season.hold_reasons.every((reason) =>
			['current_season', 'recent_acquisition'].includes(reason.code)
		);
	return {
		...lifecycle,
		prefix: season.prefix,
		candidate_count: season.candidate_count,
		eligible_candidate_count: season.eligible_candidate_count,
		held_candidate_count: season.held_candidate_count,
		hold_reason_counts: Object.fromEntries(
			season.hold_reasons.map((reason) => [reason.code, season.held_candidate_count])
		),
		can_override_holds: canOverrideHolds,
		seasons: [season]
	};
}

export function applySeriesLifecycle(
	payload: DashboardFoldersPayload,
	lifecycle: LifecycleState
): DashboardFoldersPayload {
	const seriesPrefix = lifecycle.series_prefix ?? lifecycle.prefix;
	const seasonsByPrefix = new Map(lifecycle.seasons.map((season) => [season.prefix, season]));
	const updateCard = (card: FolderCard): FolderCard => {
		if (card.prefix === seriesPrefix) return { ...card, lifecycle };
		const season = seasonsByPrefix.get(card.prefix);
		return season ? { ...card, lifecycle: lifecycleForSeason(lifecycle, season) } : card;
	};
	return {
		...payload,
		folders: payload.folders.map(updateCard),
		series_folders: payload.series_folders?.map(updateCard)
	};
}

export function olderSeasonLibraryAction(
	lifecycle: LifecycleState
): OlderSeasonLibraryAction | null {
	const numberedSeasons = lifecycle.seasons.filter(
		(season) =>
			!season.is_special &&
			Number.isInteger(season.season_number) &&
			Number(season.season_number) > 0
	);
	const latestSeasonNumber = Math.max(
		...numberedSeasons.map((season) => Number(season.season_number)),
		0
	);
	if (latestSeasonNumber <= 0) return null;
	const prefixCountByNumber = new Map<number, number>();
	for (const season of numberedSeasons) {
		const seasonNumber = Number(season.season_number);
		prefixCountByNumber.set(seasonNumber, (prefixCountByNumber.get(seasonNumber) ?? 0) + 1);
	}
	const included = numberedSeasons.filter((season) => {
		const seasonNumber = Number(season.season_number);
		if (
			seasonNumber >= latestSeasonNumber ||
			season.ambiguous ||
			(prefixCountByNumber.get(seasonNumber) ?? 0) > 1 ||
			season.candidate_count <= 0
		)
			return false;
		if (season.held_candidate_count <= 0) return season.eligible_candidate_count > 0;
		return (
			season.hold_reasons.length > 0 &&
			season.hold_reasons.every((reason) => OVERRIDEABLE_HOLD_CODES.has(reason.code))
		);
	});
	const overridden = included.filter((season) => season.held_candidate_count > 0);
	if (!included.length) return null;
	return {
		latestSeasonLabel: `Season ${latestSeasonNumber}`,
		seasonCount: included.length,
		episodeCount: included.reduce((total, season) => total + season.candidate_count, 0),
		overriddenSeasonCount: overridden.length,
		overriddenEpisodeCount: overridden.reduce(
			(total, season) => total + season.held_candidate_count,
			0
		)
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
		lifecycle: first.lifecycle
			? {
					...first.lifecycle,
					prefix: showPrefix,
					series_prefix: showPrefix,
					candidate_count: seasons.reduce(
						(total, season) => total + (season.lifecycle?.candidate_count ?? 0),
						0
					),
					eligible_candidate_count: seasons.reduce(
						(total, season) => total + (season.lifecycle?.eligible_candidate_count ?? 0),
						0
					),
					held_candidate_count: seasons.reduce(
						(total, season) => total + (season.lifecycle?.held_candidate_count ?? 0),
						0
					),
					seasons: seasons.flatMap((season) => season.lifecycle?.seasons ?? [])
				}
			: null,
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

export function compareSeasonCards(left: FolderCard, right: FolderCard): number {
	const leftNumber = Number(seasonNumberLabel(seasonIdentity(left.prefix).season));
	const rightNumber = Number(seasonNumberLabel(seasonIdentity(right.prefix).season));
	if (Number.isFinite(leftNumber) && Number.isFinite(rightNumber)) return leftNumber - rightNumber;
	if (Number.isFinite(leftNumber)) return -1;
	if (Number.isFinite(rightNumber)) return 1;
	return left.title.localeCompare(right.title, undefined, { numeric: true, sensitivity: 'base' });
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
