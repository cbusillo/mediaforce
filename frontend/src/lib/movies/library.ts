import type {
	FolderWorkflowState,
	MovieLibraryPayload,
	MovieMember,
	MovieTitle
} from '$lib/api/types';

export type MovieLibrarySortMode = 'priority' | 'name' | 'size' | 'savings' | 'oldest';

export interface MovieWorkflowDisplayState {
	workflow_state?: FolderWorkflowState | null;
	review_badge?: MovieTitle['review_badge'];
	promotion_conflicts: unknown[];
	details_loading: boolean;
	availability: 'production' | 'browse_only';
}

export interface MoviePendingReviewBadge {
	label: string;
	tone?: string | null;
	detail?: string | null;
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
	return featureDurations.length ? Math.max(...featureDurations) : null;
}

export function movieExpectedOutputBytes(title: MovieTitle): number | null {
	if (title.estimated_output_bytes != null) return title.estimated_output_bytes;
	return title.projected_reclaim_bytes == null
		? null
		: Math.max(0, title.total_size_bytes - title.projected_reclaim_bytes);
}

export function movieEstimatedOutputTotalIsLowerBound(titles: MovieTitle[]): boolean {
	return titles.some((title) => movieExpectedOutputBytes(title) == null);
}

export function movieEstimateEvidence(title: MovieTitle): string | null {
	if (title.estimate_provenance === 'sampled_calibration') {
		return 'Estimated from completed samples for every included movie file.';
	}
	const coverage = title.estimate_coverage;
	return title.estimate_provenance === 'unavailable' &&
		coverage &&
		!coverage.complete &&
		coverage.required_included_members > 1
		? 'No title estimate until every included movie file has its own current sample.'
		: null;
}

export function moviePrimaryStudioPrefix(title: MovieTitle): string {
	return title.members.length === 1 && title.members[0] && !movieTitleOwnsActiveWork(title)
		? title.members[0].prefix
		: title.prefix;
}

export function movieTitleOwnsActiveWork(
	title: Pick<MovieTitle, 'workflow_state' | 'review_badge'>
): boolean {
	if (title.review_badge?.label?.trim()) return true;
	const workflow = title.workflow_state;
	if (!workflow) return false;
	return ['processing', 'validate', 'promote', 'mixed', 'attention', 'blocked'].includes(
		workflow.primary_lane ?? ''
	);
}

export function moviePendingReviewBadge(
	title: Pick<MovieTitle, 'workflow_state' | 'review_badge'>
): MoviePendingReviewBadge | null {
	const badge = title.review_badge;
	const label = badge?.label?.trim();
	if (!label || badge?.tone === 'ok') return null;
	if (
		['processing', 'validate', 'promote', 'blocked', 'complete'].includes(
			title.workflow_state?.primary_lane ?? ''
		)
	) {
		return null;
	}
	return { label, tone: badge?.tone, detail: badge?.detail };
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
	if (title.availability === 'browse_only' || title.workflow_state?.state === 'browse_only') {
		return false;
	}
	if (title.promotion_conflicts.length) return true;
	if (title.workflow_state?.state === 'explicit_selection_required') return true;
	if (movieWorkflowIsComplete(title.workflow_state)) return false;
	if (moviePendingReviewBadge(title)) return true;
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
	const pendingReview = moviePendingReviewBadge(title);
	if (pendingReview) return pendingReview.label;
	switch (title.workflow_state?.primary_lane ?? 'none') {
		case 'encode':
			return 'Ready to compress';
		case 'validate':
			return 'Ready to check';
		case 'promote':
			return 'Ready to replace';
		case 'processing':
			return 'Compressing';
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

export function sortMovieTitles(
	titles: MovieTitle[],
	sortMode: MovieLibrarySortMode
): MovieTitle[] {
	return [...titles].sort((left, right) => compareMovieTitles(left, right, sortMode));
}

export function selectMovieTitle(titles: MovieTitle[], selectedPrefix: string): MovieTitle | null {
	return titles.find((title) => title.prefix === selectedPrefix) ?? titles[0] ?? null;
}

export function selectMovieLeadTitle(
	titles: MovieTitle[],
	sortMode: MovieLibrarySortMode,
	query: string
): MovieTitle | null {
	if (sortMode !== 'priority' || query.trim()) return null;
	return titles.find(movieTitleCanBeRecommended) ?? null;
}

function compareMovieTitles(
	left: MovieTitle,
	right: MovieTitle,
	sortMode: MovieLibrarySortMode
): number {
	if (sortMode === 'name') return compareMovieTitleNames(left, right);
	if (sortMode === 'size') {
		return right.total_size_bytes - left.total_size_bytes || compareMovieTitleNames(left, right);
	}
	if (sortMode === 'savings') {
		return compareMovieReclaim(right, left) || compareMovieTitleNames(left, right);
	}
	if (sortMode === 'oldest') {
		return movieAgeValue(left) - movieAgeValue(right) || compareMovieTitleNames(left, right);
	}
	return (
		moviePriorityStage(left) - moviePriorityStage(right) ||
		compareMovieReclaim(right, left) ||
		compareMovieTitleNames(left, right)
	);
}

function moviePriorityStage(title: MovieTitle): number {
	if (title.availability === 'browse_only' || title.workflow_state?.state === 'browse_only')
		return 9;
	if (title.promotion_conflicts.length) return 0;
	if (movieWorkflowIsComplete(title.workflow_state)) return 10;
	if (title.workflow_state?.state === 'explicit_selection_required') return 6;
	if (moviePendingReviewBadge(title)) return 1;
	return {
		attention: 1,
		promote: 2,
		validate: 3,
		mixed: 4,
		encode: 5,
		blocked: 7,
		processing: 8,
		none: 9,
		complete: 10
	}[title.workflow_state?.primary_lane ?? 'none'];
}

function movieTitleCanBeRecommended(title: MovieTitle): boolean {
	if (title.availability === 'browse_only' || title.workflow_state?.state === 'browse_only') {
		return false;
	}
	if (title.promotion_conflicts.length || movieWorkflowIsComplete(title.workflow_state))
		return false;
	if (title.workflow_state?.state === 'explicit_selection_required') return true;
	if (moviePendingReviewBadge(title)) return true;
	return ['promote', 'validate', 'mixed', 'encode'].includes(
		title.workflow_state?.primary_lane ?? ''
	);
}

function compareMovieReclaim(left: MovieTitle, right: MovieTitle): number {
	const leftReclaim = movieReclaimLowerBound(left);
	const rightReclaim = movieReclaimLowerBound(right);
	if (leftReclaim == null) return rightReclaim == null ? 0 : -1;
	if (rightReclaim == null) return 1;
	return leftReclaim - rightReclaim;
}

function compareMovieTitleNames(left: MovieTitle, right: MovieTitle): number {
	return left.title.localeCompare(right.title) || left.prefix.localeCompare(right.prefix);
}

function movieAgeValue(title: MovieTitle): number {
	const timestamp = title.age?.timestamp;
	return timestamp ? Date.parse(timestamp) || Number.MAX_SAFE_INTEGER : Number.MAX_SAFE_INTEGER;
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
