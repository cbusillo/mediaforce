import { describe, expect, it } from 'vitest';

import {
	alignedScrollOffset,
	clampMomentIndex,
	comparisonSideMuted,
	comparisonKeyboardAction,
	followerCorrectionTime,
	followerPlaybackRate,
	formatPlaybackTime,
	frameAspectRatio,
	mediaElementReady,
	mediaSourceMatches,
	normalizedScrollPosition,
	reviewPairHasSound,
	scrollOffsetForPosition
} from './comparison';
import type { ReviewPair } from './experience';

function pair(sound: boolean): ReviewPair {
	return {
		source: {
			path: '/source.mp4',
			timestampSeconds: 0,
			durationSeconds: 8,
			sizeBytes: 100,
			audio: sound ? { trustworthy: true, role: 'original' } : null
		},
		preview: {
			path: '/preview.mp4',
			timestampSeconds: 0,
			durationSeconds: 8,
			sizeBytes: 25,
			audio: sound ? { trustworthy: true, role: 'new' } : null
		},
		comparePath: ''
	};
}

describe('comparison workspace helpers', () => {
	it('maps plain keyboard shortcuts to comparison actions', () => {
		expect(comparisonKeyboardAction(' ', 3)).toEqual({ kind: 'toggle_playback' });
		expect(comparisonKeyboardAction('o', 3)).toEqual({ kind: 'show', side: 'original' });
		expect(comparisonKeyboardAction('N', 3)).toEqual({ kind: 'show', side: 'new' });
		expect(comparisonKeyboardAction('2', 3)).toEqual({ kind: 'select_moment', index: 1 });
		expect(comparisonKeyboardAction('4', 3)).toBeNull();
	});

	it('only offers sound when both clips are trustworthy', () => {
		expect(reviewPairHasSound(pair(true))).toBe(true);
		const incomplete = pair(true);
		incomplete.preview.audio = null;
		expect(reviewPairHasSound(incomplete)).toBe(false);
		const mislabeled = pair(true);
		mislabeled.preview.audio = { trustworthy: true, role: 'original' };
		expect(reviewPairHasSound(mislabeled)).toBe(false);
	});

	it('keeps only the chosen comparison side audible', () => {
		expect(comparisonSideMuted('original', 'new', true)).toBe(true);
		expect(comparisonSideMuted('new', 'new', true)).toBe(false);
		expect(comparisonSideMuted('new', 'new', false)).toBe(true);
	});

	it('accepts only decoded data from the expected clip', () => {
		expect(mediaElementReady(1)).toBe(false);
		expect(mediaElementReady(2)).toBe(false);
		expect(mediaElementReady(3)).toBe(true);
		expect(
			mediaSourceMatches(
				'http://localhost/review/moment-2.mp4',
				'/review/moment-2.mp4',
				'http://localhost/folders/show'
			)
		).toBe(true);
		expect(
			mediaSourceMatches(
				'http://localhost/review/moment-1.mp4',
				'/review/moment-2.mp4',
				'http://localhost/folders/show'
			)
		).toBe(false);
	});

	it('keeps moments, time, and frame shape bounded and readable', () => {
		expect(clampMomentIndex(4, 3)).toBe(2);
		expect(clampMomentIndex(-1, 3)).toBe(0);
		expect(formatPlaybackTime(68.9)).toBe('1:08');
		expect(frameAspectRatio(1440, 1080)).toBeCloseTo(4 / 3);
		expect(frameAspectRatio(0, 0)).toBeCloseTo(16 / 9);
	});

	it('ignores ordinary clock drift and only corrects a genuinely separated follower', () => {
		expect(followerCorrectionTime(4.17, 4)).toBeNull();
		expect(followerCorrectionTime(4.31, 4)).toBe(4.31);
		expect(followerCorrectionTime(Number.NaN, 4)).toBeNull();
	});

	it('uses bounded rate adjustment for drift below the hard-correction threshold', () => {
		expect(followerPlaybackRate(4.02, 4)).toBe(1);
		expect(followerPlaybackRate(4.14, 4)).toBe(1.04);
		expect(followerPlaybackRate(4, 4.14)).toBe(0.96);
		expect(followerPlaybackRate(4.31, 4)).toBe(1);
	});

	it('keeps the same relative picture position across different frame sizes', () => {
		expect(alignedScrollOffset(400, 1600, 800, 2400, 800)).toBe(800);
		expect(alignedScrollOffset(900, 1600, 800, 1200, 800)).toBe(400);
		expect(alignedScrollOffset(200, 600, 800, 1200, 800)).toBe(200);
		expect(normalizedScrollPosition(400, 1600, 800)).toBe(0.5);
		expect(normalizedScrollPosition(0, 600, 800)).toBe(0.5);
		expect(scrollOffsetForPosition(0.5, 2400, 800)).toBe(800);
	});
});
