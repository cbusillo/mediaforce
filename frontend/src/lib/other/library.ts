import type { FolderWorkflowState, OtherLibraryPayload, OtherWorkUnit } from '$lib/api/types';

export interface OtherScopeSummary {
	included: string;
	untouched: string;
	confirmation: string;
}

export function otherScopeSummary(
	itemCount: number,
	actionFileCount: number,
	membershipComplete: boolean,
	membershipLimit: number
): OtherScopeSummary {
	if (!membershipComplete) {
		return {
			included: `More than ${membershipLimit} files`,
			untouched: 'Not known until the folder is smaller',
			confirmation: 'Use one file at a time or split the folder before starting work.'
		};
	}
	const normalizedItemCount = Math.max(0, itemCount);
	const normalizedActionCount = Math.min(normalizedItemCount, Math.max(0, actionFileCount));
	const untouchedCount = normalizedItemCount - normalizedActionCount;
	const includedNoun = normalizedActionCount === 1 ? 'file will' : 'files will';
	const untouchedCopy =
		untouchedCount === 0
			? 'No files are left out.'
			: `${untouchedCount} ${untouchedCount === 1 ? 'file stays' : 'files stay'} untouched.`;
	return {
		included: `${normalizedActionCount} of ${normalizedItemCount}`,
		untouched:
			untouchedCount === 0
				? 'None'
				: `${untouchedCount} ${untouchedCount === 1 ? 'file' : 'files'}`,
		confirmation: `${normalizedActionCount} ${includedNoun} be compressed. ${untouchedCopy}`
	};
}

export function otherActionFileCount(
	workflow: FolderWorkflowState | null | undefined,
	eligibleItemCount: number,
	itemCount: number
): number {
	switch (workflow?.primary_lane) {
		case 'encode':
			return Math.max(0, eligibleItemCount);
		case 'validate':
			return laneCount(workflow, 'validate', 0);
		case 'promote':
			return laneCount(workflow, 'promote', 0);
		default:
			return Math.max(0, itemCount);
	}
}

export function otherReadinessBlockerCopy(value: string): string {
	return value
		.trim()
		.replace(/\bexact-file grouping\b/gi, 'one-file-at-a-time')
		.replace(/\bbounded work units?\b/gi, 'folder selection')
		.replace(/\bwork units?\b/gi, 'folders or files')
		.replace(/\bprocessing\b/gi, 'compression')
		.replace(/\bsampling\b/gi, 'creating a sample')
		.replace(/\bqueueing\b/gi, 'starting work on');
}

export function otherWorkflowLabel(
	workflow: FolderWorkflowState | null | undefined,
	detailsLoading = false
): string {
	switch (workflow?.primary_lane) {
		case 'encode':
			return 'Ready to compress';
		case 'validate':
			return 'Ready to check';
		case 'promote':
			return 'Ready to replace';
		case 'processing':
			return 'Compressing';
		case 'attention':
		case 'blocked':
			return 'Needs attention';
		case 'complete':
			return 'Finished';
		case 'mixed':
			return 'Several steps';
		default:
			return detailsLoading ? 'Loading details' : 'Ready';
	}
}

export function otherWorkflowDetail(
	workflow: FolderWorkflowState | null | undefined,
	itemCount: number
): string {
	const fallbackCount = Math.max(0, itemCount);
	switch (workflow?.primary_lane) {
		case 'encode':
			return countedFileCopy(
				laneCount(workflow, 'encode', fallbackCount),
				'',
				'is ready to compress',
				'are ready to compress'
			);
		case 'validate':
			return countedFileCopy(
				laneCount(workflow, 'validate', fallbackCount),
				'compressed',
				'needs a final safety check',
				'need a final safety check'
			);
		case 'promote':
			return countedFileCopy(
				laneCount(workflow, 'promote', fallbackCount),
				'checked',
				'can replace its original',
				'can replace their originals'
			);
		case 'processing':
			return fallbackCount === 1
				? 'Mediaforce is compressing this file now.'
				: 'Mediaforce is compressing these files now.';
		case 'mixed': {
			const parts = [
				[laneCount(workflow, 'encode', 0), 'ready to compress'],
				[laneCount(workflow, 'validate', 0), 'ready to check'],
				[laneCount(workflow, 'promote', 0), 'ready to replace']
			]
				.filter(([count]) => Number(count) > 0)
				.map(([count, label]) => `${count} ${label}`);
			return parts.length
				? `${parts.join(', ')} across these files.`
				: 'These files have several steps ready for review.';
		}
		case 'attention':
		case 'blocked':
			return 'Fix the issue shown below before work can continue.';
		case 'complete':
			return fallbackCount === 1 ? 'This file is finished.' : 'These files are finished.';
		default:
			return 'No work is waiting for these files.';
	}
}

export function mergeOtherLibraryPayloads(
	structure: OtherLibraryPayload,
	details: OtherLibraryPayload
): OtherLibraryPayload {
	const detailsByPrefix = new Map(details.work_units.map((unit) => [unit.prefix, unit]));
	const structurePrefixes = new Set(structure.work_units.map((unit) => unit.prefix));
	const workUnits = structure.work_units.map((unit) =>
		mergeOtherWorkUnit(unit, detailsByPrefix.get(unit.prefix))
	);
	for (const unit of details.work_units) {
		if (!structurePrefixes.has(unit.prefix)) workUnits.push(unit);
	}
	return {
		schema_version: Math.max(structure.schema_version, details.schema_version),
		libraries: details.libraries.length ? details.libraries : structure.libraries,
		work_units: workUnits,
		catalog_empty: structure.catalog_empty && details.catalog_empty,
		catalog_truncated: structure.catalog_truncated || details.catalog_truncated,
		catalog_item_limit: Math.min(structure.catalog_item_limit, details.catalog_item_limit),
		catalog_work_unit_limit: Math.min(
			structure.catalog_work_unit_limit,
			details.catalog_work_unit_limit
		),
		details_loading: details.details_loading
	};
}

function mergeOtherWorkUnit(structure: OtherWorkUnit, details?: OtherWorkUnit): OtherWorkUnit {
	return details ? { ...structure, ...details } : structure;
}

function laneCount(workflow: FolderWorkflowState, lane: string, fallback: number): number {
	const value = Number(workflow.lane_counts[lane]);
	return Number.isFinite(value) && value > 0 ? value : fallback;
}

function countedFileCopy(
	count: number,
	qualifier: string,
	singularPredicate: string,
	pluralPredicate: string
): string {
	const noun = count === 1 ? 'file' : 'files';
	const predicate = count === 1 ? singularPredicate : pluralPredicate;
	const qualifiedNoun = qualifier ? `${qualifier} ${noun}` : noun;
	return `${count} ${qualifiedNoun} ${predicate}.`;
}
