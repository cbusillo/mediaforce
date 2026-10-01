import type { EpisodeProgress, EpisodeStage } from '$lib/api/types';

import { formatDecimalFileSize } from './experience';

type Tone = 'active' | 'ready' | 'wait' | 'fail' | 'idle';

/** The owner-facing words for each episode stage; the list and its tests read them from here. */
export const episodeStageCopy: Record<
	EpisodeStage,
	{ label: string; tone: Tone; counted: string }
> = {
	needs_you: { label: 'Needs you', tone: 'fail', counted: 'need you' },
	compressing: { label: 'Compressing now', tone: 'active', counted: 'compressing' },
	measuring: { label: 'Finding the right size', tone: 'active', counted: 'compressing' },
	getting_ready: { label: 'Getting ready', tone: 'active', counted: 'compressing' },
	checking: { label: 'Being checked', tone: 'active', counted: 'being checked' },
	waiting: { label: 'Waiting', tone: 'wait', counted: 'waiting' },
	published: { label: 'Published', tone: 'ready', counted: 'published' },
	kept_original: { label: 'Kept as the original', tone: 'idle', counted: 'kept as the original' },
	held: { label: 'Stays as it is', tone: 'idle', counted: 'staying as they are' },
	not_started: { label: 'Not started', tone: 'idle', counted: 'not started' }
};

export const episodeListCopy = {
	title: 'Episodes',
	showAll: (count: number) => `Show all ${count} episodes`,
	showFewer: 'Show fewer',
	answer: 'Answer this above',
	unavailable: 'The episode list could not be loaded. Mediaforce will try again shortly.'
};

// Seasons longer than this open with only the episodes that need the owner or are being worked on.
export const EPISODE_LIST_COLLAPSE_AT = 12;
const FOCUS_STAGES = new Set<EpisodeStage>([
	'needs_you',
	'compressing',
	'measuring',
	'getting_ready',
	'checking'
]);

export function episodeName(relPath: string): string {
	const fileName = relPath.split('/').at(-1) ?? relPath;
	return fileName.replace(/\.[^.]+$/, '');
}

function sentence(text: string): string {
	const trimmed = text.trim();
	return trimmed ? `${trimmed[0].toUpperCase()}${trimmed.slice(1)}` : '';
}

/** One short line under the stage, or nothing when the stage says it all. */
export function episodeDetail(episode: EpisodeProgress): string {
	switch (episode.stage) {
		case 'compressing':
			return episode.percent_complete !== null ? `${episode.percent_complete}% done` : '';
		case 'published':
			return episode.bytes_saved ? `Saved ${formatDecimalFileSize(episode.bytes_saved)}` : '';
		case 'kept_original':
			return 'Your choice';
		case 'needs_you':
			if (episode.size_question) {
				const question = episode.size_question;
				return `Keeping the picture quality needs ${formatDecimalFileSize(question.smallest_quality_safe_bytes)}, over its ${formatDecimalFileSize(question.goal_bytes)} goal`;
			}
			return sentence(episode.detail ?? '');
		case 'waiting':
			// The shared reasons count files ("their turn"); one episode waits for its own.
			return sentence((episode.detail ?? '').replace('their turn', 'its turn'));
		default:
			return sentence(episode.detail ?? '');
	}
}

/** "1 needs you · 2 compressing · 9 published", in the list's own order. */
export function episodeSummary(episodes: EpisodeProgress[]): string {
	const counts = new Map<string, number>();
	for (const episode of episodes) {
		const counted = episodeStageCopy[episode.stage].counted;
		counts.set(counted, (counts.get(counted) ?? 0) + 1);
	}
	return [...counts]
		.map(([counted, count]) =>
			counted === 'need you'
				? `${count} ${count === 1 ? 'needs' : 'need'} you`
				: `${count} ${counted}`
		)
		.join(' · ');
}

/** The list is worth showing once any episode has started, finished, or needs the owner. */
export function hasEpisodeProgress(episodes: EpisodeProgress[]): boolean {
	return episodes.some((episode) => episode.stage !== 'not_started' && episode.stage !== 'held');
}

export function focusedEpisodes(episodes: EpisodeProgress[]): EpisodeProgress[] {
	return episodes.filter((episode) => FOCUS_STAGES.has(episode.stage));
}

export function episodeListStartsCollapsed(episodes: EpisodeProgress[]): boolean {
	return episodes.length > EPISODE_LIST_COLLAPSE_AT && focusedEpisodes(episodes).length > 0;
}
