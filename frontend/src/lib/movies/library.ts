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

export function movieWorkflowIsComplete(workflow: FolderWorkflowState | null | undefined): boolean {
	return workflow?.state === 'complete' || workflow?.primary_lane === 'complete';
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

export function movieTitleRuntimeSeconds(title: MovieTitle): number | null {
	const featureDurations = title.members
		.filter((member) => member.role === 'feature' && member.duration_seconds != null)
		.map((member) => member.duration_seconds as number);
	const availableDurations = featureDurations.length
		? featureDurations
		: title.members
				.filter((member) => member.duration_seconds != null)
				.map((member) => member.duration_seconds as number);
	return availableDurations.length ? Math.max(...availableDurations) : null;
}

export function movieExpectedOutputBytes(title: MovieTitle): number | null {
	return title.projected_reclaim_bytes == null
		? null
		: Math.max(0, title.total_size_bytes - title.projected_reclaim_bytes);
}

export function moviePrimaryStudioPrefix(title: MovieTitle): string {
	return title.members.length === 1 && title.members[0] ? title.members[0].prefix : title.prefix;
}

export function movieCompositionDetail(title: MovieTitle): string | null {
	const details: string[] = [];
	if (title.edition_count > 1) details.push(`${title.edition_count} editions`);
	if (title.extra_count)
		details.push(`${title.extra_count} ${title.extra_count === 1 ? 'extra' : 'extras'}`);
	if (title.uncertain_count) {
		details.push(
			`${title.uncertain_count} ${title.uncertain_count === 1 ? 'file needs' : 'files need'} a choice`
		);
	}
	return details.length ? details.join(' · ') : null;
}

export function movieTitleNeedsAction(title: MovieTitle): boolean {
	if (title.promotion_conflicts.length) return true;
	if (title.workflow_state?.state === 'explicit_selection_required') return true;
	if (movieWorkflowIsComplete(title.workflow_state)) return false;
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
	if (movieWorkflowIsComplete(title.workflow_state)) return 'Finished';
	switch (title.workflow_state?.primary_lane ?? 'none') {
		case 'encode':
			return 'Ready to compress';
		case 'validate':
			return 'Ready to check';
		case 'promote':
			return 'Ready to replace';
		case 'processing':
			return 'In progress';
		case 'attention':
			return 'Needs review';
		case 'mixed':
			return 'Several steps';
		case 'blocked':
			return 'Cannot start';
		case 'complete':
			return 'Finished';
		case 'none':
			return title.details_loading ? 'Loading' : 'No work needed';
		default:
			return title.details_loading ? 'Loading' : 'No work needed';
	}
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
