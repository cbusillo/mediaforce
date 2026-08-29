import type { FolderPayload } from '$lib/api/types';

export interface ReviewClip {
	path: string;
	timestampSeconds: number;
	durationSeconds: number;
	sizeBytes: number;
	audio: ReviewAudio | null;
}

export interface ReviewAudio {
	trustworthy: boolean;
	role: 'original' | 'new' | '';
}

export interface ReviewPair {
	source: ReviewClip;
	preview: ReviewClip;
	comparePath: string;
}

export interface ReviewSampleSizes {
	original: number;
	smaller: number;
	durationSeconds: number;
	ratioPercent: number;
}

export function normalizeReviewPairs(folder: FolderPayload): ReviewPair[] {
	const calibration = record(folder.calibration);
	return records(calibration.review_pairs)
		.map((pair) => {
			const source = record(pair.source_clip);
			const preview = record(pair.preview_clip);
			const compare = record(pair.compare_clip);
			const sourceAudio = record(source.audio);
			const previewAudio = record(preview.audio);
			return {
				source: {
					path: text(source.path),
					timestampSeconds: numberValue(source.timestamp_seconds),
					durationSeconds: numberValue(source.duration_seconds),
					sizeBytes: numberValue(source.size_bytes),
					audio: Object.keys(sourceAudio).length
						? {
								trustworthy: sourceAudio.trustworthy === true,
								role: reviewAudioRole(sourceAudio.role)
							}
						: null
				},
				preview: {
					path: text(preview.path),
					timestampSeconds: numberValue(preview.timestamp_seconds),
					durationSeconds: numberValue(preview.duration_seconds),
					sizeBytes: numberValue(preview.size_bytes),
					audio: Object.keys(previewAudio).length
						? {
								trustworthy: previewAudio.trustworthy === true,
								role: reviewAudioRole(previewAudio.role)
							}
						: null
				},
				comparePath: text(compare.path)
			};
		})
		.filter((pair) => pair.source.path && pair.preview.path);
}

export function reviewSampleSizes(folder: FolderPayload): ReviewSampleSizes {
	const totals = normalizeReviewPairs(folder).reduce(
		(total, pair) => ({
			original: total.original + pair.source.sizeBytes,
			smaller: total.smaller + pair.preview.sizeBytes,
			durationSeconds: total.durationSeconds + pair.source.durationSeconds
		}),
		{ original: 0, smaller: 0, durationSeconds: 0 }
	);
	return {
		...totals,
		ratioPercent: totals.original > 0 ? Math.round((totals.smaller / totals.original) * 100) : 0
	};
}

export function reviewSourceHasAudio(folder: FolderPayload): boolean {
	const calibration = record(folder.calibration);
	const sampleItem = record(folder.sample_item);
	const calibrationSampleItem = record(calibration.sample_item);
	return [sampleItem.audio_summary, calibrationSampleItem.audio_summary].some(
		(summary) => records(summary).length > 0
	);
}

export function reviewSourceLabel(folder: FolderPayload, fallback: string): string {
	const calibration = record(folder.calibration);
	const sampleItem = record(folder.sample_item);
	const calibrationSampleItem = record(calibration.sample_item);
	const relPath = text(sampleItem.rel_path) || text(calibrationSampleItem.rel_path);
	return relPath.split('/').at(-1) || fallback;
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

function numberValue(value: unknown): number {
	const parsed = Number(value ?? 0);
	return Number.isFinite(parsed) ? parsed : 0;
}

function reviewAudioRole(value: unknown): ReviewAudio['role'] {
	return value === 'original' || value === 'new' ? value : '';
}
