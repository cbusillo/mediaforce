<script lang="ts">
	import { onMount, tick, untrack } from 'svelte';

	import {
		alignedScrollOffset,
		comparisonKeyboardAction,
		comparisonSideMuted,
		followerCorrectionTime,
		followerPlaybackRate,
		formatPlaybackTime,
		frameAspectRatio,
		mediaElementReady,
		mediaSourceMatches,
		normalizedScrollPosition,
		reviewPairHasSound,
		scrollOffsetForPosition,
		type ComparisonLayout,
		type ComparisonScale,
		type ComparisonSide
	} from '$lib/season/comparison';
	import type { ReviewPair } from '$lib/season/experience';

	let {
		pairs,
		selectedMoment,
		audioChoice,
		episodeLabel,
		originalSizeLabel,
		newSizeLabel,
		canMakeSoundTest,
		soundTestDisabled,
		onMomentChange,
		onAudioChange,
		onRequestSoundTest
	}: {
		pairs: ReviewPair[];
		selectedMoment: number;
		audioChoice: ComparisonSide;
		episodeLabel: string;
		originalSizeLabel: string;
		newSizeLabel: string;
		canMakeSoundTest: boolean;
		soundTestDisabled: boolean;
		onMomentChange: (index: number) => void;
		onAudioChange: (side: ComparisonSide) => void;
		onRequestSoundTest: () => void;
	} = $props();

	let workspaceElement = $state<HTMLElement | null>(null);
	let sourceVideo = $state<HTMLVideoElement | null>(null);
	let previewVideo = $state<HTMLVideoElement | null>(null);
	let sourcePane = $state<HTMLElement | null>(null);
	let previewPane = $state<HTMLElement | null>(null);
	let closeButton = $state<HTMLButtonElement | null>(null);
	let returnFocus = $state<HTMLElement | null>(null);
	let isOpen = $state(false);
	let nativeFullscreen = $state(false);
	let layout = $state<ComparisonLayout>('side_by_side');
	let visibleSide = $state<ComparisonSide>('new');
	let scale = $state<ComparisonScale>('fit');
	let playing = $state(false);
	let playbackRequested = $state(false);
	let preparing = $state(false);
	let scrubbing = $state(false);
	let pairSeekPending = $state(false);
	let playbackError = $state('');
	let currentTime = $state(0);
	let duration = $state(0);
	let sourceWidth = $state(0);
	let sourceHeight = $state(0);
	let previewWidth = $state(0);
	let previewHeight = $state(0);
	let sourceReady = $state(false);
	let previewReady = $state(false);
	let sourceError = $state(false);
	let previewError = $state(false);
	let previousBodyOverflow = '';
	let scrollSyncPending = false;
	let mediaGeneration = 0;
	let playbackSequence = 0;
	let sourceCorrectionPending = false;
	let sourceCorrectionCooldownUntil = 0;
	let picturePositionX = 0.5;
	let picturePositionY = 0.5;

	const currentPair = $derived(pairs[Math.min(selectedMoment, pairs.length - 1)]);
	const hasSound = $derived(reviewPairHasSound(currentPair));
	const sourceFrameWidth = $derived(sourceWidth || previewWidth || 1920);
	const sourceFrameHeight = $derived(sourceHeight || previewHeight || 1080);
	const previewFrameWidth = $derived(previewWidth || sourceWidth || 1920);
	const previewFrameHeight = $derived(previewHeight || sourceHeight || 1080);
	const frameAspect = $derived(frameAspectRatio(sourceFrameWidth, sourceFrameHeight));
	const workspaceTitle = $derived(hasSound ? 'Compare picture and sound' : 'Compare picture');
	const timeText = $derived(`${formatPlaybackTime(currentTime)} / ${formatPlaybackTime(duration)}`);
	const workspaceStyle = $derived(`--frame-aspect:${frameAspect}`);
	const sourceFrameStyle = $derived(
		`--media-width:${sourceFrameWidth}px;--media-height:${sourceFrameHeight}px`
	);
	const previewFrameStyle = $derived(
		`--media-width:${previewFrameWidth}px;--media-height:${previewFrameHeight}px`
	);
	const pairKey = $derived(`${currentPair?.source.path ?? ''}|${currentPair?.preview.path ?? ''}`);

	$effect(() => {
		if (!pairKey) return;
		mediaGeneration += 1;
		untrack(pauseBoth);
		preparing = false;
		currentTime = 0;
		duration = currentPair?.preview.durationSeconds || currentPair?.source.durationSeconds || 0;
		sourceWidth = 0;
		sourceHeight = 0;
		previewWidth = 0;
		previewHeight = 0;
		sourceReady = false;
		previewReady = false;
		sourceError = false;
		previewError = false;
		playbackError = '';
		picturePositionX = 0.5;
		picturePositionY = 0.5;
		void tick().then(() => {
			sourceVideo?.load();
			previewVideo?.load();
		});
	});

	onMount(() => {
		const handleFullscreenChange = () => {
			if (nativeFullscreen && document.fullscreenElement !== workspaceElement) finishClose();
		};
		document.addEventListener('fullscreenchange', handleFullscreenChange);
		return () => {
			document.removeEventListener('fullscreenchange', handleFullscreenChange);
			document.body.style.overflow = previousBodyOverflow;
		};
	});

	function chooseMoment(index: number) {
		pauseBoth();
		onMomentChange(index);
	}

	function chooseSound(side: ComparisonSide) {
		if (!hasSound) return;
		onAudioChange(side);
	}

	function showSide(side: ComparisonSide) {
		chooseLayout('one_at_a_time');
		visibleSide = side;
		schedulePicturePosition();
	}

	function chooseLayout(nextLayout: ComparisonLayout) {
		layout = nextLayout;
		schedulePicturePosition();
	}

	function chooseScale(nextScale: ComparisonScale) {
		scale = nextScale;
		if (nextScale === 'actual') schedulePicturePosition();
	}

	function openWorkspace(event: MouseEvent) {
		returnFocus = event.currentTarget as HTMLElement;
		layout = 'side_by_side';
		visibleSide = 'new';
		scale = 'fit';
		picturePositionX = 0.5;
		picturePositionY = 0.5;
		playbackError = '';
		pauseBoth();
		isOpen = true;
		previousBodyOverflow = document.body.style.overflow;
		document.body.style.overflow = 'hidden';
		const fullscreenRequest = workspaceElement?.requestFullscreen?.();
		if (fullscreenRequest) {
			void fullscreenRequest
				.then(() => (nativeFullscreen = true))
				.catch(() => (nativeFullscreen = false));
		}
		void tick().then(() => {
			sourceVideo?.load();
			previewVideo?.load();
			closeButton?.focus();
		});
	}

	async function closeWorkspace() {
		if (document.fullscreenElement === workspaceElement) {
			await document.exitFullscreen().catch(() => undefined);
		}
		finishClose();
	}

	function finishClose() {
		if (!isOpen) return;
		pauseBoth();
		isOpen = false;
		nativeFullscreen = false;
		document.body.style.overflow = previousBodyOverflow;
		void tick().then(() => returnFocus?.focus());
	}

	async function togglePlayback() {
		if (!sourceVideo || !previewVideo) return;
		playbackError = '';
		if (playbackRequested || playing || preparing) {
			pauseBoth();
			return;
		}
		await startPlayback();
	}

	async function startPlayback() {
		if (!sourceVideo || !previewVideo) return;
		const sequence = ++playbackSequence;
		playbackRequested = true;
		playbackError = '';
		sourceVideo.playbackRate = 1;
		preparing = true;
		if (previewVideo.ended || previewVideo.currentTime >= previewVideo.duration) {
			pairSeekPending = true;
			currentTime = 0;
			await Promise.all([settleMediaTime(sourceVideo, 0), settleMediaTime(previewVideo, 0)]);
			if (sequence !== playbackSequence) return;
			pairSeekPending = false;
			currentTime = previewVideo.currentTime;
		} else if (Math.abs(sourceVideo.currentTime - previewVideo.currentTime) > 0.1) {
			pairSeekPending = true;
			await settleMediaTime(sourceVideo, previewVideo.currentTime);
			if (sequence !== playbackSequence) return;
			pairSeekPending = false;
		}
		try {
			await Promise.all([sourceVideo.play(), previewVideo.play()]);
			if (sequence !== playbackSequence || !playbackRequested) {
				sourceVideo.pause();
				previewVideo.pause();
				return;
			}
			playing = true;
		} catch {
			if (sequence !== playbackSequence) return;
			pauseBoth();
			playbackError = 'The clips could not start together. Try again.';
		} finally {
			if (sequence === playbackSequence) preparing = false;
		}
	}

	function pauseBoth() {
		playbackSequence += 1;
		sourceVideo?.pause();
		previewVideo?.pause();
		if (sourceVideo) sourceVideo.playbackRate = 1;
		playbackRequested = false;
		playing = false;
		preparing = false;
		pairSeekPending = false;
		scrubbing = false;
		sourceCorrectionPending = false;
	}

	function settleMediaTime(video: HTMLVideoElement, value: number): Promise<void> {
		return new Promise((resolve) => {
			let settled = false;
			const finish = () => {
				if (settled) return;
				settled = true;
				window.clearTimeout(timeout);
				video.removeEventListener('seeked', finish);
				video.removeEventListener('error', finish);
				resolve();
			};
			const timeout = window.setTimeout(finish, 1500);
			video.addEventListener('seeked', finish, { once: true });
			video.addEventListener('error', finish, { once: true });
			video.currentTime = value;
			if (!video.seeking && Math.abs(video.currentTime - value) < 0.025) {
				queueMicrotask(finish);
			}
		});
	}

	async function seekTo(value: number) {
		if (!sourceVideo || !previewVideo) return;
		const next = Math.min(Math.max(value, 0), duration || value);
		const resumeAfterSeek = playbackRequested;
		const sequence = ++playbackSequence;
		sourceVideo.pause();
		previewVideo.pause();
		playing = false;
		preparing = true;
		pairSeekPending = true;
		scrubbing = false;
		currentTime = next;
		sourceCorrectionPending = false;
		await Promise.all([settleMediaTime(sourceVideo, next), settleMediaTime(previewVideo, next)]);
		if (sequence !== playbackSequence) return;
		pairSeekPending = false;
		preparing = false;
		currentTime = previewVideo.currentTime;
		if (resumeAfterSeek && playbackRequested) await startPlayback();
	}

	function handleTimelinePreview(event: Event) {
		scrubbing = true;
		currentTime = Number((event.currentTarget as HTMLInputElement).value);
	}

	function handleTimelineCommit(event: Event) {
		void seekTo(Number((event.currentTarget as HTMLInputElement).value));
	}

	function handleTimeUpdate() {
		if (!previewVideo || !sourceVideo) return;
		if (!scrubbing) currentTime = previewVideo.currentTime;
		if (
			!playbackRequested ||
			pairSeekPending ||
			sourceCorrectionPending ||
			sourceVideo.seeking ||
			previewVideo.seeking ||
			performance.now() < sourceCorrectionCooldownUntil
		)
			return;
		const correction = followerCorrectionTime(previewVideo.currentTime, sourceVideo.currentTime);
		if (correction == null) {
			sourceVideo.playbackRate = followerPlaybackRate(
				previewVideo.currentTime,
				sourceVideo.currentTime
			);
			return;
		}
		sourceVideo.playbackRate = 1;
		sourceCorrectionPending = true;
		sourceCorrectionCooldownUntil = performance.now() + 1000;
		sourceVideo.currentTime = correction;
	}

	function handlePreviewMetadata() {
		if (!previewVideo) return;
		previewWidth = previewVideo.videoWidth;
		previewHeight = previewVideo.videoHeight;
		duration = Number.isFinite(previewVideo.duration)
			? previewVideo.duration
			: currentPair?.preview.durationSeconds || 0;
		if (scale === 'actual') void tick().then(centerActualSize);
	}

	function handleMediaError(side: ComparisonSide) {
		if (side === 'original') {
			sourceReady = false;
			sourceError = true;
		} else {
			previewReady = false;
			previewError = true;
		}
		playbackError = `${side === 'original' ? 'The original' : 'The new'} clip could not be decoded in this browser.`;
	}

	function handleMediaLoaded(side: ComparisonSide, video: HTMLVideoElement) {
		const expectedSource =
			side === 'original' ? currentPair?.source.path : currentPair?.preview.path;
		const loadedSource = video.currentSrc;
		const generation = mediaGeneration;
		requestAnimationFrame(() => {
			requestAnimationFrame(() => {
				if (generation !== mediaGeneration) return;
				if (video.currentSrc !== loadedSource) return;
				if (!mediaElementReady(video.readyState)) return;
				if (!mediaSourceMatches(video.currentSrc, expectedSource ?? '', document.baseURI)) return;
				if (side === 'original') {
					sourceReady = true;
					sourceError = false;
				} else {
					previewReady = true;
					previewError = false;
				}
			});
		});
	}

	function handleSourceMetadata() {
		if (!sourceVideo) return;
		sourceWidth = sourceVideo.videoWidth;
		sourceHeight = sourceVideo.videoHeight;
	}

	function handleEnded() {
		pauseBoth();
		currentTime = duration;
	}

	function syncPaneScroll(source: HTMLElement, target: HTMLElement | null) {
		if (!target || scrollSyncPending) return;
		scrollSyncPending = true;
		picturePositionX = normalizedScrollPosition(
			source.scrollLeft,
			source.scrollWidth,
			source.clientWidth,
			picturePositionX
		);
		picturePositionY = normalizedScrollPosition(
			source.scrollTop,
			source.scrollHeight,
			source.clientHeight,
			picturePositionY
		);
		target.scrollLeft = alignedScrollOffset(
			source.scrollLeft,
			source.scrollWidth,
			source.clientWidth,
			target.scrollWidth,
			target.clientWidth
		);
		target.scrollTop = alignedScrollOffset(
			source.scrollTop,
			source.scrollHeight,
			source.clientHeight,
			target.scrollHeight,
			target.clientHeight
		);
		requestAnimationFrame(() => (scrollSyncPending = false));
	}

	function centerActualSize() {
		picturePositionX = 0.5;
		picturePositionY = 0.5;
		applyPicturePosition();
	}

	function applyPicturePosition() {
		for (const pane of [sourcePane, previewPane]) {
			if (!pane) continue;
			pane.scrollLeft = scrollOffsetForPosition(
				picturePositionX,
				pane.scrollWidth,
				pane.clientWidth
			);
			pane.scrollTop = scrollOffsetForPosition(
				picturePositionY,
				pane.scrollHeight,
				pane.clientHeight
			);
		}
	}

	function schedulePicturePosition() {
		void tick().then(() => requestAnimationFrame(applyPicturePosition));
	}

	function handleWorkspaceKeydown(event: KeyboardEvent) {
		if (!isOpen) return;
		if (event.key === 'Tab') {
			trapFocus(event);
			return;
		}
		const target = event.target;
		if (
			target instanceof HTMLInputElement ||
			target instanceof HTMLTextAreaElement ||
			target instanceof HTMLSelectElement ||
			(target instanceof HTMLButtonElement && (event.key === ' ' || event.key === 'Enter'))
		) {
			return;
		}
		const action = comparisonKeyboardAction(event.key, pairs.length);
		if (!action) return;
		event.preventDefault();
		if (action.kind === 'close') void closeWorkspace();
		else if (action.kind === 'toggle_playback') void togglePlayback();
		else if (action.kind === 'seek') void seekTo(currentTime + action.seconds);
		else if (action.kind === 'show') showSide(action.side);
		else if (action.kind === 'toggle_layout') {
			chooseLayout(layout === 'side_by_side' ? 'one_at_a_time' : 'side_by_side');
		} else if (action.kind === 'toggle_scale') chooseScale(scale === 'fit' ? 'actual' : 'fit');
		else if (action.kind === 'select_moment') chooseMoment(action.index);
	}

	function trapFocus(event: KeyboardEvent) {
		if (!workspaceElement) return;
		const focusable = Array.from(
			workspaceElement.querySelectorAll<HTMLElement>(
				'button:not([disabled]), input:not([disabled]), [href], [tabindex]:not([tabindex="-1"])'
			)
		).filter((element) => !element.hasAttribute('hidden'));
		if (!focusable.length) return;
		const first = focusable[0];
		const last = focusable[focusable.length - 1];
		if (event.shiftKey && document.activeElement === first) {
			event.preventDefault();
			last.focus();
		} else if (!event.shiftKey && document.activeElement === last) {
			event.preventDefault();
			first.focus();
		}
	}
</script>

{#if currentPair}
	<section class="comparison-block">
		<div
			bind:this={workspaceElement}
			class="comparison-workspace"
			class:is-open={isOpen}
			class:is-native-fullscreen={nativeFullscreen}
			class:one-at-a-time={layout === 'one_at_a_time'}
			class:show-original={visibleSide === 'original'}
			class:actual-size={scale === 'actual'}
			style={workspaceStyle}
			role={isOpen ? 'dialog' : 'group'}
			aria-modal={isOpen ? 'true' : undefined}
			aria-label={isOpen ? workspaceTitle : 'Original and new video comparison'}
			onkeydown={handleWorkspaceKeydown}
		>
			{#if isOpen}
				<header class="workspace-header">
					<div>
						<span class="workspace-kicker">{episodeLabel}</span>
						<h2>{workspaceTitle}</h2>
					</div>
					<div class="workspace-moments" role="group" aria-label="Test moments">
						{#each pairs as pair, index (`${pair.source.path}-${index}`)}
							<button
								type="button"
								class:active={selectedMoment === index}
								onclick={() => chooseMoment(index)}
								aria-pressed={selectedMoment === index}
							>
								Moment {index + 1}
							</button>
						{/each}
					</div>
					<button
						bind:this={closeButton}
						class="close-comparison"
						type="button"
						onclick={closeWorkspace}
					>
						Close comparison
					</button>
				</header>
			{/if}

			{#if isOpen}
				<div class="workspace-tools">
					<div class="tool-group" role="group" aria-label="Picture arrangement">
						<button
							type="button"
							class:active={layout === 'side_by_side'}
							onclick={() => chooseLayout('side_by_side')}
							aria-pressed={layout === 'side_by_side'}>Side by side</button
						>
						<button
							type="button"
							class:active={layout === 'one_at_a_time'}
							onclick={() => chooseLayout('one_at_a_time')}
							aria-pressed={layout === 'one_at_a_time'}>One at a time</button
						>
					</div>
					{#if layout === 'one_at_a_time'}
						<div class="tool-group" role="group" aria-label="Picture shown">
							<button
								type="button"
								class:active={visibleSide === 'original'}
								onclick={() => (visibleSide = 'original')}
								aria-pressed={visibleSide === 'original'}>Original</button
							>
							<button
								type="button"
								class:active={visibleSide === 'new'}
								onclick={() => (visibleSide = 'new')}
								aria-pressed={visibleSide === 'new'}>New</button
							>
						</div>
					{/if}
					<div class="tool-group" role="group" aria-label="Picture size">
						<button
							type="button"
							class:active={scale === 'fit'}
							onclick={() => chooseScale('fit')}
							aria-pressed={scale === 'fit'}>Fit</button
						>
						<button
							type="button"
							class:active={scale === 'actual'}
							onclick={() => chooseScale('actual')}
							aria-pressed={scale === 'actual'}>Actual size</button
						>
					</div>
				</div>
			{/if}

			<div class="comparison-media">
				<div
					bind:this={sourcePane}
					class="media-pane media-pane--original"
					aria-hidden={isOpen && layout === 'one_at_a_time' && visibleSide !== 'original'}
					onscroll={(event) => syncPaneScroll(event.currentTarget, previewPane)}
				>
					<div class="media-label">
						<div><strong>Original</strong><span>what you have now</span></div>
						<small>{originalSizeLabel}</small>
					</div>
					<div class="media-frame" style={sourceFrameStyle}>
						<video
							bind:this={sourceVideo}
							src={currentPair.source.path}
							muted={comparisonSideMuted('original', audioChoice, hasSound)}
							playsinline
							preload="auto"
							aria-label="Original test moment"
							tabindex="-1"
							onloadedmetadata={(event) => {
								handleSourceMetadata();
								handleMediaLoaded('original', event.currentTarget);
							}}
							onloadeddata={(event) => handleMediaLoaded('original', event.currentTarget)}
							oncanplay={(event) => handleMediaLoaded('original', event.currentTarget)}
							onseeked={() => (sourceCorrectionPending = false)}
							onerror={() => handleMediaError('original')}
						></video>
						{#if sourceError}
							<span class="media-loading media-loading--error" role="alert"
								>Original clip could not load.</span
							>
						{:else if !sourceReady}
							<span class="media-loading" aria-live="polite">Loading original picture…</span>
						{/if}
					</div>
				</div>
				<div
					bind:this={previewPane}
					class="media-pane media-pane--new"
					aria-hidden={isOpen && layout === 'one_at_a_time' && visibleSide !== 'new'}
					onscroll={(event) => syncPaneScroll(event.currentTarget, sourcePane)}
				>
					<div class="media-label">
						<div><strong>New</strong><span>the test version</span></div>
						<small>{newSizeLabel}</small>
					</div>
					<div class="media-frame" style={previewFrameStyle}>
						<video
							bind:this={previewVideo}
							src={currentPair.preview.path}
							muted={comparisonSideMuted('new', audioChoice, hasSound)}
							playsinline
							preload="auto"
							aria-label="New test moment"
							tabindex="-1"
							onloadedmetadata={(event) => {
								handlePreviewMetadata();
								handleMediaLoaded('new', event.currentTarget);
							}}
							onloadeddata={(event) => handleMediaLoaded('new', event.currentTarget)}
							oncanplay={(event) => {
								handleMediaLoaded('new', event.currentTarget);
								if (playbackRequested) preparing = false;
							}}
							onerror={() => handleMediaError('new')}
							onplaying={() => {
								if (playbackRequested) {
									playing = true;
									preparing = false;
								}
							}}
							onwaiting={() => {
								if (playbackRequested) preparing = true;
							}}
							onpause={() => (playing = false)}
							ontimeupdate={handleTimeUpdate}
							onended={handleEnded}
						></video>
						{#if previewError}
							<span class="media-loading media-loading--error" role="alert"
								>New clip could not load.</span
							>
						{:else if !previewReady}
							<span class="media-loading" aria-live="polite">Loading new picture…</span>
						{/if}
					</div>
				</div>
			</div>

			<footer class="workspace-controls" aria-label="Comparison playback controls">
				<div class="playback-primary">
					<button
						class="play-control"
						type="button"
						onclick={togglePlayback}
						disabled={!sourceReady || !previewReady || sourceError || previewError}
					>
						{sourceError || previewError
							? 'Unavailable'
							: !sourceReady || !previewReady
								? 'Loading…'
								: playbackRequested
									? 'Pause'
									: 'Play'}
					</button>
					{#if preparing && playbackRequested}
						<span class="playback-status" aria-live="polite">Buffering…</span>
					{/if}
				</div>
				<label class="timeline-control">
					<span class="sr-only">Playback position</span>
					<input
						type="range"
						min="0"
						max={Math.max(duration, 0.01)}
						step="0.01"
						value={currentTime}
						oninput={handleTimelinePreview}
						onchange={handleTimelineCommit}
						aria-valuetext={timeText}
						disabled={!sourceReady || !previewReady || sourceError || previewError}
					/>
				</label>
				<output class="time-display">{timeText}</output>
				{#if hasSound}
					<div class="sound-control" role="group" aria-label="Listen to">
						<span>Listen to</span>
						<button
							type="button"
							class:active={audioChoice === 'original'}
							onclick={() => chooseSound('original')}
							aria-pressed={audioChoice === 'original'}>Original</button
						>
						<button
							type="button"
							class:active={audioChoice === 'new'}
							onclick={() => chooseSound('new')}
							aria-pressed={audioChoice === 'new'}>New</button
						>
					</div>
				{:else}
					<p class="picture-only-note">These clips show picture only.</p>
				{/if}
				{#if playbackError}<p class="playback-error" role="alert">{playbackError}</p>{/if}
			</footer>
		</div>

		<div class="comparison-entry">
			{#if pairs.length > 1}
				<div class="inline-moments" role="group" aria-label="Test moments">
					{#each pairs as pair, index (`${pair.source.path}-${index}`)}
						<button
							type="button"
							class:active={selectedMoment === index}
							onclick={() => chooseMoment(index)}
							aria-pressed={selectedMoment === index}
						>
							Moment {index + 1}
						</button>
					{/each}
				</div>
			{/if}
			<div class="entry-action">
				<button type="button" onclick={openWorkspace}>Compare in full screen</button>
				<small>Opens side by side, paused. Nothing is changed.</small>
			</div>
		</div>
		{#if !hasSound}
			<div class="sound-unavailable">
				<p>
					{canMakeSoundTest
						? 'These clips show picture only. A new test is needed to compare sound.'
						: 'This episode does not have sound to compare.'}
				</p>
				{#if canMakeSoundTest}
					<button type="button" onclick={onRequestSoundTest} disabled={soundTestDisabled}>
						Make a new test with sound
					</button>
				{/if}
			</div>
		{/if}
	</section>
{/if}

<style>
	.comparison-block {
		display: grid;
		gap: 10px;
	}

	.comparison-workspace {
		background: #090b0d;
		border-radius: 12px;
		box-shadow: 0 8px 28px rgb(20 22 25 / 16%);
		color: #f4f5f2;
		display: grid;
		gap: 10px;
		overflow: hidden;
		padding: 10px;
	}

	.comparison-workspace.is-open {
		background: #0c0f12;
		border-radius: 0;
		inset: 0;
		padding: 16px;
		position: fixed;
		z-index: 1000;
	}

	.comparison-workspace:fullscreen {
		height: 100%;
		width: 100%;
	}

	.workspace-header {
		align-items: center;
		border-bottom: 1px solid rgb(255 255 255 / 11%);
		display: grid;
		gap: 16px;
		grid-template-columns: minmax(220px, 1fr) auto minmax(220px, 1fr);
		padding: 2px 2px 13px;
	}

	.is-native-fullscreen .workspace-header {
		padding-left: 64px;
	}

	.workspace-header > div:first-child {
		display: grid;
		gap: 3px;
	}

	.workspace-header h2 {
		font-size: 20px;
		margin: 0;
	}

	.workspace-kicker {
		color: #9aa4a0;
		font-size: 11px;
		font-weight: 700;
		letter-spacing: 0.05em;
		text-transform: uppercase;
	}

	.workspace-moments,
	.inline-moments,
	.tool-group,
	.sound-control {
		align-items: center;
		background: #151a1e;
		border: 1px solid rgb(255 255 255 / 10%);
		border-radius: 999px;
		display: flex;
		gap: 2px;
		padding: 3px;
	}

	.workspace-moments button,
	.inline-moments button,
	.tool-group button,
	.sound-control button {
		background: transparent;
		border: 0;
		border-radius: 999px;
		color: #aeb7b3;
		cursor: pointer;
		font: inherit;
		font-size: 12px;
		font-weight: 650;
		min-height: 32px;
		padding: 0 12px;
		white-space: nowrap;
	}

	.workspace-moments button.active,
	.inline-moments button.active,
	.tool-group button.active,
	.sound-control button.active {
		background: #e8eeeb;
		color: #17201c;
	}

	.close-comparison {
		background: transparent;
		border: 1px solid rgb(255 255 255 / 18%);
		border-radius: 7px;
		color: #f4f5f2;
		cursor: pointer;
		font: inherit;
		font-size: 12px;
		font-weight: 700;
		justify-self: end;
		min-height: 38px;
		padding: 0 14px;
	}

	.workspace-tools {
		align-items: center;
		display: flex;
		gap: 8px;
		justify-content: center;
	}

	.comparison-media {
		display: grid;
		gap: 10px;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		min-height: 0;
	}

	.is-open .comparison-media {
		flex: 1;
		min-height: 0;
	}

	.media-pane {
		background: #050607;
		border: 1px solid rgb(255 255 255 / 12%);
		border-radius: 8px;
		display: grid;
		grid-template-rows: auto minmax(0, 1fr);
		min-width: 0;
		overflow: hidden;
	}

	.media-pane--new {
		border-color: rgb(120 208 190 / 62%);
	}

	.media-label {
		align-items: center;
		background: #111519;
		display: flex;
		gap: 16px;
		justify-content: space-between;
		min-height: 44px;
		padding: 0 12px;
	}

	.media-label > div {
		align-items: baseline;
		display: flex;
		gap: 8px;
	}

	.media-label strong {
		font-size: 12px;
		letter-spacing: 0.06em;
		text-transform: uppercase;
	}

	.media-label span,
	.media-label small {
		color: #9da7a3;
		font-size: 11px;
	}

	.media-frame {
		aspect-ratio: var(--frame-aspect);
		display: grid;
		min-height: 0;
		place-items: center;
		position: relative;
	}

	.media-frame video {
		grid-area: 1 / 1;
		display: block;
		height: 100%;
		object-fit: contain;
		width: 100%;
	}

	.media-loading {
		background: rgb(5 6 7 / 82%);
		color: #c5ceca;
		font-size: 12px;
		grid-area: 1 / 1;
		padding: 7px 10px;
		pointer-events: none;
	}

	.media-loading--error {
		background: rgb(52 20 18 / 90%);
		color: #ffd1c8;
	}

	.is-open {
		grid-template-rows: auto auto minmax(0, 1fr) auto;
	}

	.is-open .comparison-media {
		overflow: hidden;
	}

	.is-open .media-frame {
		aspect-ratio: auto;
		height: 100%;
	}

	.one-at-a-time .comparison-media {
		grid-template-columns: minmax(0, 1fr);
	}

	.one-at-a-time .media-pane {
		grid-column: 1;
		grid-row: 1;
	}

	.one-at-a-time.show-original .media-pane--new,
	.one-at-a-time:not(.show-original) .media-pane--original {
		opacity: 0;
		pointer-events: none;
		visibility: hidden;
	}

	.actual-size .media-pane {
		overflow: auto;
	}

	.actual-size .media-frame {
		display: block;
		height: var(--media-height);
		width: var(--media-width);
	}

	.actual-size .media-frame video {
		height: var(--media-height);
		max-width: none;
		object-fit: contain;
		width: var(--media-width);
	}

	.workspace-controls {
		align-items: center;
		border-top: 1px solid rgb(255 255 255 / 11%);
		display: grid;
		gap: 12px;
		grid-template-columns: auto minmax(180px, 1fr) auto auto;
		padding: 13px 2px 0;
	}

	.playback-primary {
		align-items: center;
		display: flex;
		gap: 8px;
	}

	.play-control,
	.entry-action button {
		background: #dfece6;
		border: 1px solid #c2d8ce;
		border-radius: 7px;
		color: #183027;
		cursor: pointer;
		font: inherit;
		font-size: 12px;
		font-weight: 750;
		min-height: 38px;
		padding: 0 16px;
	}

	.play-control:disabled {
		cursor: not-allowed;
		opacity: 0.68;
	}

	.playback-status {
		color: #c7d0cc;
		font-size: 11px;
		white-space: nowrap;
	}

	.timeline-control {
		display: block;
	}

	.timeline-control input {
		accent-color: #83c9b6;
		width: 100%;
	}

	.time-display {
		color: #abb4b0;
		font-size: 12px;
		font-variant-numeric: tabular-nums;
	}

	.sound-control {
		background: transparent;
		border: 0;
	}

	.sound-control > span {
		color: #aeb7b3;
		font-size: 12px;
		padding-inline: 5px;
	}

	.picture-only-note,
	.playback-error {
		color: #b9c0bd;
		font-size: 12px;
		margin: 0;
	}

	.playback-error {
		color: #f0b7aa;
		grid-column: 1 / -1;
	}

	.comparison-entry {
		align-items: center;
		display: flex;
		gap: 12px;
		justify-content: space-between;
	}

	.inline-moments {
		background: var(--mf-bg-raised);
		border-color: var(--mf-line);
	}

	.inline-moments button {
		color: var(--mf-fg-secondary);
	}

	.inline-moments button.active {
		background: var(--mf-bg-panel);
		box-shadow: 0 1px 2px rgb(30 34 39 / 10%);
		color: var(--mf-active-fg);
	}

	.entry-action {
		align-items: flex-end;
		display: flex;
		flex-direction: column;
		gap: 10px;
	}

	.entry-action button {
		min-width: 190px;
		white-space: nowrap;
	}

	.entry-action small {
		max-width: 230px;
		text-align: right;
	}

	.entry-action small,
	.sound-unavailable {
		color: var(--mf-fg-tertiary);
		font-size: 11px;
	}

	.sound-unavailable {
		align-items: center;
		background: var(--mf-wait-bg);
		border: 1px solid var(--mf-wait-line);
		border-radius: 7px;
		display: flex;
		gap: 12px;
		justify-content: space-between;
		padding: 9px 11px;
	}

	.sound-unavailable p {
		margin: 0;
	}

	.sound-unavailable button {
		background: transparent;
		border: 1px solid var(--mf-wait-line);
		border-radius: 6px;
		color: var(--mf-wait-fg);
		cursor: pointer;
		font: inherit;
		font-size: 11px;
		font-weight: 700;
		min-height: 32px;
		padding: 0 11px;
	}

	.sound-unavailable button:disabled {
		cursor: not-allowed;
		opacity: 0.5;
	}

	.sr-only {
		height: 1px;
		margin: -1px;
		overflow: hidden;
		padding: 0;
		position: absolute;
		width: 1px;
		clip: rect(0, 0, 0, 0);
	}

	button:focus-visible,
	input:focus-visible {
		outline: 2px solid #86d8c2;
		outline-offset: 2px;
	}

	@media (max-width: 760px) {
		.comparison-workspace.is-open {
			padding: 10px;
		}

		.workspace-header {
			align-items: stretch;
			grid-template-columns: 1fr auto;
		}

		.workspace-header > div:first-child {
			grid-column: 1;
		}

		.workspace-moments {
			grid-column: 1 / -1;
			grid-row: 2;
			justify-self: center;
		}

		.close-comparison {
			grid-column: 2;
			grid-row: 1;
		}

		.workspace-tools,
		.comparison-entry {
			align-items: stretch;
			flex-wrap: wrap;
		}

		.comparison-entry {
			flex-direction: column;
		}

		.sound-unavailable {
			align-items: stretch;
			flex-direction: column;
		}

		.inline-moments,
		.entry-action {
			justify-content: center;
			width: 100%;
		}

		.entry-action {
			align-items: center;
			flex-direction: column;
		}

		.workspace-controls {
			grid-template-columns: auto minmax(100px, 1fr) auto;
		}

		.sound-control,
		.picture-only-note {
			grid-column: 1 / -1;
			justify-self: center;
		}
	}

	@media (max-width: 520px) {
		.comparison-media {
			grid-template-columns: 1fr;
		}

		.is-open .comparison-media {
			grid-template-columns: 1fr;
		}

		.is-open:not(.one-at-a-time) .comparison-media {
			grid-template-rows: repeat(2, minmax(0, 1fr));
		}

		.is-open:not(.one-at-a-time) .media-pane {
			min-height: 0;
		}

		.is-open:not(.one-at-a-time) .media-frame {
			min-height: 90px;
		}

		.workspace-controls {
			grid-template-columns: auto 1fr;
		}

		.timeline-control {
			grid-column: 1 / -1;
			grid-row: 2;
		}

		.time-display {
			justify-self: end;
		}
	}
</style>
