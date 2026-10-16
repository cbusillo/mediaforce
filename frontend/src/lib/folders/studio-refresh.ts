import type { StagedIntegrityPayload } from '$lib/api/types';

export interface StudioRefreshPlan {
	prefix: string;
	navigate: boolean;
}

export function studioRefreshPlan(
	currentPrefix: string,
	requestedPrefix?: string | null
): StudioRefreshPlan {
	const current = normalizePrefix(currentPrefix);
	const requested = normalizePrefix(requestedPrefix ?? '');
	const prefix = requested || current;
	return { prefix, navigate: prefix !== current };
}

function normalizePrefix(prefix: string): string {
	return prefix.trim().replace(/^\/+|\/+$/g, '');
}

function integritySummaryKey(integrity: StagedIntegrityPayload | undefined): string {
	if (!integrity) return '';
	return JSON.stringify({
		counts: integrity.counts,
		blockerCount: integrity.blocker_count,
		blockers: integrity.blockers,
		databaseTruncated: integrity.database_truncated,
		discovery: integrity.discovery,
		promotionReadiness: integrity.promotion_readiness
	});
}

/**
 * Notices staged-file changes between status polls. Each poll is compared with the previous status
 * read, never with the fuller staged-integrity read the page shows, which also searches for files on
 * the share and would differ on every poll.
 */
export function stagedIntegrityPollTracker() {
	let lastKey = '';
	return {
		/** Records the status read that a full page load used. */
		reset(integrity: StagedIntegrityPayload | undefined) {
			lastKey = integritySummaryKey(integrity);
		},
		/** Records a poll and reports whether its staged files differ from the previous status read. */
		changed(integrity: StagedIntegrityPayload | undefined): boolean {
			const key = integritySummaryKey(integrity);
			const changed = key !== lastKey;
			lastKey = key;
			return changed;
		}
	};
}
