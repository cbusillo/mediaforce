import type {
	FolderWorkflowState,
	MovieLibraryPayload,
	MovieMember,
	MovieTitle
} from '$lib/api/types';

export type MovieLibrarySortMode = 'priority' | 'name' | 'size' | 'savings' | 'oldest';

export interface MovieWorkflowDisplayState {
	workflow_state?: FolderWorkflowState | null;
	promotion_conflicts: unknown[];
	details_loading: boolean;
	availability: 'production' | 'browse_only';
}

export function mergeMovieLibraryPayloads(
	structure: MovieLibraryPayload,
	details: MovieLibraryPayload
): MovieLibraryPayload {
	const detailsByPrefix = new Map(details.titles.map((title) => [title.prefix, title]));
	const structurePrefixes = new Set(structure.titles.map((title) => title.prefix));
	const titles = structure.titles.map((title) =>
		mergeMovieTitle(title, detailsByPrefix.get(title.prefix))
	);
	for (const title of details.titles) {
		if (!structurePrefixes.has(title.prefix)) titles.push(title);
	}
	return {
		schema_version: Math.max(structure.schema_version, details.schema_version),
		libraries: details.libraries.length ? details.libraries : structure.libraries,
		titles,
		catalog_empty: structure.catalog_empty && details.catalog_empty,
		details_loading: details.details_loading
	};
}

export function movieReclaimLowerBound(title: MovieTitle): number | null {
	if (title.projected_reclaim_bytes != null) return title.projected_reclaim_bytes;
	return (title.known_saved_bytes ?? 0) > 0 ? (title.known_saved_bytes ?? null) : null;
}

export function movieReclaimTotalIsLowerBound(titles: MovieTitle[]): boolean {
	return titles.some((title) => title.projected_reclaim_bytes == null);
}

export function movieTitleNeedsAction(title: MovieTitle): boolean {
	if (title.promotion_conflicts.length) return true;
	if (title.workflow_state?.state === 'explicit_selection_required') return true;
	return ['encode', 'validate', 'promote', 'processing', 'attention', 'mixed'].includes(
		title.workflow_state?.primary_lane ?? ''
	);
}

export function movieWorkflowLabel(title: MovieWorkflowDisplayState): string {
	if (title.promotion_conflicts.length) return 'Replacement blocked';
	if (title.availability === 'browse_only' || title.workflow_state?.state === 'browse_only') {
		return 'View only';
	}
	if (title.workflow_state?.state === 'explicit_selection_required') return 'Choose a file';
	return {
		encode: 'Ready to compress',
		validate: 'Ready to check',
		promote: 'Ready to replace',
		processing: 'In progress',
		attention: 'Needs review',
		mixed: 'Several steps',
		complete: 'Finished',
		blocked: 'Cannot start',
		none: title.details_loading ? 'Loading' : 'No work needed'
	}[title.workflow_state?.primary_lane ?? 'none'];
}

export function selectMovieLeadTitle(
	titles: MovieTitle[],
	sortMode: MovieLibrarySortMode,
	query: string
): MovieTitle | null {
	if (sortMode !== 'priority' || query.trim()) return null;
	return titles.find(movieTitleNeedsAction) ?? null;
}

function mergeMovieTitle(structure: MovieTitle, details?: MovieTitle): MovieTitle {
	if (!details) return structure;
	const detailsByPrefix = new Map(details.members.map((member) => [member.prefix, member]));
	const structurePrefixes = new Set(structure.members.map((member) => member.prefix));
	const members = structure.members.map((member) =>
		mergeMovieMember(member, detailsByPrefix.get(member.prefix))
	);
	for (const member of details.members) {
		if (!structurePrefixes.has(member.prefix)) members.push(member);
	}
	return { ...structure, ...details, members };
}

function mergeMovieMember(structure: MovieMember, details?: MovieMember): MovieMember {
	return details ? { ...structure, ...details } : structure;
}
