import type { OtherLibraryPayload, OtherWorkUnit } from '$lib/api/types';

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
