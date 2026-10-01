import { describe, expect, it } from 'vitest';

import type { EpisodeProgress, EpisodeStage } from '$lib/api/types';

import {
	EPISODE_LIST_COLLAPSE_AT,
	episodeDetail,
	episodeListStartsCollapsed,
	episodeName,
	episodeStageCopy,
	episodeSummary,
	focusedEpisodes,
	hasEpisodeProgress
} from './episodes';

function episode(stage: EpisodeStage, overrides: Partial<EpisodeProgress> = {}): EpisodeProgress {
	return {
		rel_path: `tv/Show/Season 1/Episode ${stage}.mkv`,
		stage,
		detail: null,
		percent_complete: null,
		bytes_saved: null,
		size_question: null,
		owner_action: null,
		...overrides
	};
}

describe('season episode list', () => {
	it('names every stage in plain words', () => {
		const words = Object.values(episodeStageCopy)
			.map((copy) => `${copy.label} ${copy.counted}`)
			.join(' ');

		expect(words).not.toMatch(/\b(CRF|VMAF|ledger|cadence|shard|manifest|staged|promot)/i);
	});

	it('shows the episode name without its file extension', () => {
		expect(episodeName('tv/House/Season 8/House - S08E01 - Twenty Vicodin.mkv')).toBe(
			'House - S08E01 - Twenty Vicodin'
		);
	});

	it('gives each stage one short line', () => {
		expect(episodeDetail(episode('compressing', { percent_complete: 42 }))).toBe('42% done');
		expect(episodeDetail(episode('compressing'))).toBe('');
		expect(episodeDetail(episode('published', { bytes_saved: 1_200_000_000 }))).toBe(
			'Saved 1.2 GB'
		);
		expect(episodeDetail(episode('kept_original'))).toBe('Your choice');
		expect(episodeDetail(episode('waiting', { detail: 'waiting for their turn' }))).toBe(
			'Waiting for its turn'
		);
		expect(
			episodeDetail(episode('needs_you', { detail: "didn't pass the final size check" }))
		).toBe("Didn't pass the final size check");
		expect(
			episodeDetail(
				episode('needs_you', {
					size_question: {
						job_id: 'shard-1',
						rel_path: 'tv/Show/Season 1/Episode 01.mkv',
						goal_bytes: 191_800_000,
						smallest_quality_safe_bytes: 358_900_000
					}
				})
			)
		).toBe('Keeping the picture quality needs 359 MB, over its 192 MB goal');
	});

	it('counts episodes in the order the list reads', () => {
		expect(
			episodeSummary([
				episode('needs_you'),
				episode('compressing'),
				episode('measuring'),
				episode('waiting'),
				episode('published'),
				episode('published')
			])
		).toBe('1 needs you · 2 compressing · 1 waiting · 2 published');
		expect(episodeSummary([episode('needs_you'), episode('needs_you')])).toBe('2 need you');
	});

	it('appears once any episode has started, finished, or needs the owner', () => {
		expect(hasEpisodeProgress([episode('not_started'), episode('held')])).toBe(false);
		expect(hasEpisodeProgress([episode('not_started'), episode('waiting')])).toBe(true);
	});

	it('opens a long season on the episodes that need the owner or are being worked on', () => {
		const long = [
			episode('needs_you'),
			episode('checking'),
			...Array.from({ length: EPISODE_LIST_COLLAPSE_AT }, (_, index) =>
				episode('published', { rel_path: `tv/Show/Season 1/Episode ${index}.mkv` })
			)
		];
		const finished = long.map((item) => ({ ...item, stage: 'published' as const }));

		expect(episodeListStartsCollapsed(long)).toBe(true);
		expect(focusedEpisodes(long).map((item) => item.stage)).toEqual(['needs_you', 'checking']);
		// A long season with nothing under way just lists everything.
		expect(episodeListStartsCollapsed(finished)).toBe(false);
	});
});
