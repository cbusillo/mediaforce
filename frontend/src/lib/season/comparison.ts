import type { ReviewPair } from './experience';

export type ComparisonLayout = 'side_by_side' | 'one_at_a_time';
export type ComparisonSide = 'original' | 'new';
export type ComparisonScale = 'fit' | 'actual';

export class MediaStartError extends Error {
	constructor(readonly side: ComparisonSide) {
		super(`${side} comparison clip failed to start`);
		this.name = 'MediaStartError';
	}
}

const SOFT_DRIFT_THRESHOLD_SECONDS = 0.06;
const STRONG_DRIFT_THRESHOLD_SECONDS = 0.15;
const HARD_DRIFT_THRESHOLD_SECONDS = 0.75;
const FOLLOWER_RATE_ADJUSTMENT = 0.04;
const STRONG_FOLLOWER_RATE_ADJUSTMENT = 0.12;
const DEFERRED_HARD_RATE_ADJUSTMENT = 0.2;

export type ComparisonKeyboardAction =
	| { kind: 'close' }
	| { kind: 'toggle_playback' }
	| { kind: 'seek'; seconds: number }
	| { kind: 'show'; side: ComparisonSide }
	| { kind: 'toggle_layout' }
	| { kind: 'toggle_scale' }
	| { kind: 'select_moment'; index: number };

export function comparisonKeyboardAction(
	key: string,
	momentCount: number
): ComparisonKeyboardAction | null {
	const normalized = key.toLowerCase();
	if (key === 'Escape') return { kind: 'close' };
	if (key === ' ' || normalized === 'k') return { kind: 'toggle_playback' };
	if (key === 'ArrowLeft') return { kind: 'seek', seconds: -1 };
	if (key === 'ArrowRight') return { kind: 'seek', seconds: 1 };
	if (normalized === 'o') return { kind: 'show', side: 'original' };
	if (normalized === 'n') return { kind: 'show', side: 'new' };
	if (normalized === 's') return { kind: 'toggle_layout' };
	if (normalized === 'f') return { kind: 'toggle_scale' };
	if (/^[1-9]$/.test(key)) {
		const index = Number(key) - 1;
		if (index < momentCount) return { kind: 'select_moment', index };
	}
	return null;
}

export function reviewPairHasSound(pair: ReviewPair | undefined): boolean {
	return Boolean(
		pair?.source.audio?.trustworthy &&
		pair.source.audio.role === 'original' &&
		pair.preview.audio?.trustworthy &&
		pair.preview.audio.role === 'new'
	);
}

export function comparisonSideMuted(
	side: ComparisonSide,
	audioChoice: ComparisonSide,
	hasSound: boolean
): boolean {
	return !hasSound || side !== audioChoice;
}

export function mediaElementReady(readyState: number): boolean {
	return readyState >= 3;
}

export function mediaSourceMatches(
	currentSource: string,
	expectedSource: string,
	baseUrl: string
): boolean {
	if (!currentSource || !expectedSource) return false;
	try {
		return new URL(currentSource, baseUrl).href === new URL(expectedSource, baseUrl).href;
	} catch {
		return false;
	}
}

export function mediaStartFailureMessage(side: ComparisonSide): string {
	return `${side === 'original' ? 'The original' : 'The new'} clip could not be decoded in this browser.`;
}

export function waitForMediaPlaying(
	video: HTMLVideoElement,
	side: ComparisonSide,
	timeoutMs: number
): Promise<void> {
	return new Promise((resolve, reject) => {
		let settled = false;
		const cleanup = () => {
			globalThis.clearTimeout(timeout);
			video.removeEventListener('playing', succeed);
			video.removeEventListener('error', fail);
		};
		const settle = (finish: () => void) => {
			if (settled) return;
			settled = true;
			cleanup();
			finish();
		};
		const succeed = () => settle(resolve);
		const fail = () => settle(() => reject(new MediaStartError(side)));

		const timeout = globalThis.setTimeout(succeed, timeoutMs);
		video.addEventListener('playing', succeed, { once: true });
		video.addEventListener('error', fail, { once: true });
		if (video.error) fail();
	});
}

export function clampMomentIndex(index: number, momentCount: number): number {
	if (momentCount <= 0) return 0;
	return Math.min(Math.max(index, 0), momentCount - 1);
}

export function formatPlaybackTime(value: number): string {
	const totalSeconds = Math.max(0, Math.floor(Number.isFinite(value) ? value : 0));
	const minutes = Math.floor(totalSeconds / 60);
	const seconds = totalSeconds % 60;
	return `${minutes}:${seconds.toString().padStart(2, '0')}`;
}

export function boundedReviewDuration(declaredDuration: number, mediaDuration: number): number {
	const declared = Number.isFinite(declaredDuration) && declaredDuration > 0 ? declaredDuration : 0;
	const media = Number.isFinite(mediaDuration) && mediaDuration > 0 ? mediaDuration : 0;
	if (declared && media) {
		if (media < declared * 0.75) return media;
		return declared;
	}
	return declared || media;
}

export function playbackBoundaryReached(
	currentTime: number,
	duration: number,
	toleranceSeconds = 0.05
): boolean {
	if (!Number.isFinite(currentTime) || !Number.isFinite(duration) || duration <= 0) return false;
	const tolerance = Number.isFinite(toleranceSeconds) ? Math.max(toleranceSeconds, 0) : 0;
	return currentTime >= Math.max(duration - tolerance, 0);
}

export function followerCorrectionTime(
	masterTime: number,
	followerTime: number,
	thresholdSeconds = HARD_DRIFT_THRESHOLD_SECONDS
): number | null {
	if (
		!Number.isFinite(masterTime) ||
		!Number.isFinite(followerTime) ||
		!Number.isFinite(thresholdSeconds) ||
		thresholdSeconds <= 0
	) {
		return null;
	}
	return Math.abs(masterTime - followerTime) >= thresholdSeconds ? masterTime : null;
}

export function followerPlaybackRate(
	masterTime: number,
	followerTime: number,
	deferHardCorrection = false
): number {
	if (!Number.isFinite(masterTime) || !Number.isFinite(followerTime)) return 1;
	const driftSeconds = masterTime - followerTime;
	const absoluteDrift = Math.abs(driftSeconds);
	if (absoluteDrift < SOFT_DRIFT_THRESHOLD_SECONDS) return 1;
	if (absoluteDrift >= HARD_DRIFT_THRESHOLD_SECONDS && !deferHardCorrection) return 1;
	const adjustment =
		absoluteDrift >= HARD_DRIFT_THRESHOLD_SECONDS
			? DEFERRED_HARD_RATE_ADJUSTMENT
			: absoluteDrift >= STRONG_DRIFT_THRESHOLD_SECONDS
				? STRONG_FOLLOWER_RATE_ADJUSTMENT
				: FOLLOWER_RATE_ADJUSTMENT;
	return driftSeconds > 0 ? 1 + adjustment : 1 - adjustment;
}

export function frameAspectRatio(width: number, height: number): number {
	if (width <= 0 || height <= 0) return 16 / 9;
	return width / height;
}

export function alignedScrollOffset(
	sourceOffset: number,
	sourceExtent: number,
	sourceViewport: number,
	targetExtent: number,
	targetViewport: number
): number {
	return scrollOffsetForPosition(
		normalizedScrollPosition(sourceOffset, sourceExtent, sourceViewport),
		targetExtent,
		targetViewport
	);
}

export function normalizedScrollPosition(
	offset: number,
	extent: number,
	viewport: number,
	fallback = 0.5
): number {
	const travel = Math.max(extent - viewport, 0);
	if (travel === 0) return fallback;
	return Math.min(Math.max(offset / travel, 0), 1);
}

export function scrollOffsetForPosition(
	position: number,
	extent: number,
	viewport: number
): number {
	const travel = Math.max(extent - viewport, 0);
	return Math.min(Math.max(position, 0), 1) * travel;
}
