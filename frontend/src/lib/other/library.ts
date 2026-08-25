import type { FolderWorkflowState, OtherLibraryPayload, OtherWorkUnit } from '$lib/api/types';

export interface OtherScopeSummary {
	included: string;
	untouched: string;
	confirmation: string;
}

export function otherScopeSummary(
	itemCount: number,
	includedItemCount: number,
	blockedItemCount: number,
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
	const includedNoun = includedItemCount === 1 ? 'file will' : 'files will';
	const untouchedCopy =
		blockedItemCount === 0
			? 'No files are left out.'
			: `${blockedItemCount} ${blockedItemCount === 1 ? 'file stays' : 'files stay'} untouched.`;
	return {
		included: `${includedItemCount} of ${itemCount}`,
		untouched:
			blockedItemCount === 0
				? 'None'
				: `${blockedItemCount} ${blockedItemCount === 1 ? 'file' : 'files'}`,
		confirmation: `${includedItemCount} ${includedNoun} be compressed. ${untouchedCopy}`
	};
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
