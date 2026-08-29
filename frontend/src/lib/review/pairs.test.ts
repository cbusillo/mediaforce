import { describe, expect, it } from 'vitest';

import type { FolderPayload } from '$lib/api/types';

import { reviewAvailability } from './availability';
import { normalizeReviewPairs, reviewSampleSizes, reviewSourceHasAudio } from './pairs';

function folder(calibration: Record<string, unknown>): FolderPayload {
	return {
		prefix: 'movies/Example Feature',
		media_scope: {
			match: 'folder',
			title: 'Example Feature'
		},
		pending: false,
		calibration,
		metric_support: { vmaf: false, xpsnr: false, ssim: false },
		metric_status_copy: ''
	} as unknown as FolderPayload;
}

function reviewPair(): Record<string, unknown> {
	return {
		source_clip: {
			path: '/review/source.mp4',
			timestamp_seconds: 30,
			duration_seconds: 8,
			size_bytes: 100,
			audio: { trustworthy: true, role: 'original' }
		},
		preview_clip: {
			path: '/review/preview.mp4',
			timestamp_seconds: 30,
			duration_seconds: 8,
			size_bytes: 25,
			audio: { trustworthy: true, role: 'new' }
		}
	};
}

describe('shared review pairs', () => {
	it('normalizes valid pairs, filters incomplete media, and summarizes sample sizes', () => {
		const subject = folder({
			review_pairs: [reviewPair(), { source_clip: { path: '/review/source-only.mp4' } }]
		});

		expect(normalizeReviewPairs(subject)).toEqual([
			expect.objectContaining({
				source: expect.objectContaining({ path: '/review/source.mp4', sizeBytes: 100 }),
				preview: expect.objectContaining({ path: '/review/preview.mp4', sizeBytes: 25 })
			})
		]);
		expect(reviewSampleSizes(subject)).toEqual({
			original: 100,
			smaller: 25,
			durationSeconds: 8,
			ratioPercent: 25
		});
	});

	it('detects source audio from current or retained sample metadata', () => {
		const currentSample = folder({});
		currentSample.sample_item = {
			audio_summary: [{ channels: 6 }]
		} as FolderPayload['sample_item'];
		expect(reviewSourceHasAudio(currentSample)).toBe(true);

		expect(
			reviewSourceHasAudio(folder({ sample_item: { audio_summary: [{ codec_name: 'aac' }] } }))
		).toBe(true);
		expect(reviewSourceHasAudio(folder({}))).toBe(false);
	});
});

describe('review availability', () => {
	it('requires both browser readiness and valid review pairs for inline comparison', () => {
		expect(
			reviewAvailability(
				folder({
					review_media_ready: true,
					browser_review_ready: false,
					review_pairs: [reviewPair()]
				})
			)
		).toMatchObject({
			isBrowserReady: false,
			canDownload: true,
			recovery: { kind: 'legacy_download_only' }
		});

		expect(
			reviewAvailability(
				folder({
					review_media_ready: false,
					browser_review_ready: true,
					review_pairs: [reviewPair()]
				})
			)
		).toMatchObject({ isBrowserReady: true, canDownload: true, recovery: null });
	});

	it('reports purged and missing browser review media truthfully', () => {
		expect(
			reviewAvailability(folder({ compare_clips_purged: true, review_media_ready: true }))
		).toMatchObject({
			isBrowserReady: false,
			canDownload: false,
			recovery: { kind: 'purged' }
		});
		expect(reviewAvailability(folder({ browser_review_ready: true }))).toMatchObject({
			isBrowserReady: false,
			canDownload: false,
			recovery: { kind: 'missing_pairs' }
		});
	});

	it('offers a legacy download only when a combined comparison exists', () => {
		expect(reviewAvailability(folder({ review_media_ready: true }))).toMatchObject({
			isBrowserReady: false,
			canDownload: false,
			recovery: { kind: 'missing_pairs' }
		});
		expect(
			reviewAvailability(
				folder({
					review_media_ready: true,
					compare_clips: [{ path: '/review/compare.mov' }]
				})
			)
		).toMatchObject({
			isBrowserReady: false,
			canDownload: true,
			recovery: { kind: 'legacy_download_only' }
		});
	});

	it('reports a missing-review gate even when readiness flags are absent', () => {
		const subject = folder({});
		subject.review_gate = { status: 'missing_review_media' } as FolderPayload['review_gate'];
		expect(reviewAvailability(subject)).toMatchObject({
			isBrowserReady: false,
			canDownload: false,
			recovery: { kind: 'missing_pairs' }
		});
	});
});
