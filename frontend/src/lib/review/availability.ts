import type { FolderPayload } from '$lib/api/types';

import { normalizeReviewPairs, type ReviewPair } from './pairs';

export type ReviewRecoveryKind = 'purged' | 'missing_pairs' | 'legacy_download_only';

export interface ReviewRecovery {
	kind: ReviewRecoveryKind;
	title: string;
	detail: string;
}

export interface ReviewAvailability {
	pairs: ReviewPair[];
	isBrowserReady: boolean;
	canDownload: boolean;
	recovery: ReviewRecovery | null;
}

export function reviewAvailability(folder: FolderPayload): ReviewAvailability {
	const calibration = record(folder.calibration);
	const reviewGate = record(folder.review_gate);
	const pairs = normalizeReviewPairs(folder);
	const browserReady = calibration.browser_review_ready === true;
	const downloadReady = pairs.length > 0 || hasClipPath(calibration.compare_clips);
	const purged =
		calibration.compare_clips_purged === true ||
		calibration.preview_clips_purged === true ||
		calibration.source_clips_purged === true;

	if (pairs.length > 0 && browserReady) {
		return { pairs, isBrowserReady: true, canDownload: true, recovery: null };
	}
	if (purged) {
		return {
			pairs,
			isBrowserReady: false,
			canDownload: false,
			recovery: {
				kind: 'purged',
				title: 'Comparison clips were cleaned up',
				detail: 'Create another sample before reviewing it in the browser.'
			}
		};
	}
	if (downloadReady && !browserReady) {
		return {
			pairs,
			isBrowserReady: false,
			canDownload: true,
			recovery: {
				kind: 'legacy_download_only',
				title: 'Inline comparison is unavailable',
				detail:
					'This older sample only has a downloadable comparison. Create another sample for synchronized in-browser review.'
			}
		};
	}
	if (
		browserReady ||
		calibration.review_media_ready === true ||
		reviewGate.status === 'missing_review_media'
	) {
		return {
			pairs,
			isBrowserReady: false,
			canDownload: false,
			recovery: {
				kind: 'missing_pairs',
				title: 'Comparison clips are unavailable',
				detail:
					'Mediaforce cannot verify matching original and sample clips. Create another sample before reviewing it.'
			}
		};
	}
	return { pairs, isBrowserReady: false, canDownload: false, recovery: null };
}

function hasClipPath(value: unknown): boolean {
	return records(value).some((clip) => text(clip.path));
}

function record(value: unknown): Record<string, unknown> {
	return value && typeof value === 'object' && !Array.isArray(value)
		? (value as Record<string, unknown>)
		: {};
}

function records(value: unknown): Array<Record<string, unknown>> {
	return Array.isArray(value) ? value.map(record) : [];
}

function text(value: unknown): string {
	return typeof value === 'string' ? value.trim() : '';
}
