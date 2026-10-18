import { describe, expect, it } from 'vitest';

import type { FolderCard, FolderWorkflowState, PageStatePayload } from '$lib/api/types';
import type { SeasonPromotionIntegrity } from './experience';
import {
	CARD_STATE_COPY,
	FAILURE_COPY,
	PAGE_STATE_COPY,
	RECOVERY_ACTION_COPY,
	cardStateWording,
	pageStateWording,
	plainFailureMessage
} from './page-state';

const SEASON = 'tv/Big Brother (US)/Season 19';

function serverState(
	key: PageStatePayload['key'],
	fields: Partial<PageStatePayload> = {}
): PageStatePayload {
	return {
		key,
		tone: 'ready',
		recovery_kind: null,
		primary_action: null,
		failure: null,
		source_job: null,
		...fields
	};
}

function sample(status: string): PageStatePayload['source_job'] {
	return { id: 'sample-1', kind: 'sample', prefix: SEASON, status };
}

function integrity(fields: Partial<SeasonPromotionIntegrity>): SeasonPromotionIntegrity {
	return {
		available: true,
		canFinish: true,
		error: '',
		reportComplete: true,
		readyCount: 1,
		alreadyPlacedCount: 0,
		unresolvedCount: 0,
		totalCount: 1,
		blockers: [],
		records: [],
		...fields
	};
}

const blockedWorkflow: FolderWorkflowState = {
	prefix: SEASON,
	state: 'blocked',
	primary_lane: 'blocked',
	label: '',
	tone: 'attention',
	detail: 'Some files need attention.',
	counts: {},
	lane_counts: {},
	state_counts: {},
	next_action: { kind: 'none', label: '', enabled: false, target_prefix: SEASON },
	blockers: ['A failed file check', 'A missing finished copy']
};

function card(pageState: PageStatePayload | null, fields: Partial<FolderCard> = {}): FolderCard {
	return {
		prefix: SEASON,
		title: 'Big Brother (US) · Season 19',
		subtitle: 'Big Brother (US)',
		scope_label: 'Season',
		item_count: 39,
		pending_count: 39,
		total_size_bytes: 1,
		estimated_savings_bytes: 0,
		known_saved_bytes: 0,
		projected_reclaim_bytes: 0,
		average_age_days: 0,
		sort_score: 0,
		statuses: {},
		video_codecs: {},
		details_loading: false,
		page_state: pageState,
		...fields
	};
}

describe('page state wording', () => {
	// Each server state, the copy entry it reads, and whether that entry has its own episode words.
	it.each([
		[serverState('needs_test', { tone: 'quiet' }), PAGE_STATE_COPY.needs_test, null],
		[
			serverState('sample_waiting', { tone: 'quiet', source_job: sample('queued') }),
			PAGE_STATE_COPY.sample_waiting,
			null
		],
		[
			serverState('sample_waiting', { tone: 'attention', source_job: sample('retry_backoff') }),
			PAGE_STATE_COPY.sample_retrying,
			null
		],
		[
			serverState('making_test', { tone: 'active' }),
			PAGE_STATE_COPY.making_test.season,
			PAGE_STATE_COPY.making_test.episode
		],
		[serverState('ready_to_compare'), PAGE_STATE_COPY.ready_to_compare, null],
		[
			serverState('ready_to_make'),
			PAGE_STATE_COPY.ready_to_make.season,
			PAGE_STATE_COPY.ready_to_make.episode
		],
		[
			serverState('motion_held', { tone: 'quiet' }),
			PAGE_STATE_COPY.motion_held.season,
			PAGE_STATE_COPY.motion_held.episode
		],
		[
			serverState('making_season', { tone: 'active' }),
			PAGE_STATE_COPY.making_season.season,
			PAGE_STATE_COPY.making_season.episode
		],
		[
			serverState('ready_to_check'),
			PAGE_STATE_COPY.ready_to_check.season,
			PAGE_STATE_COPY.ready_to_check.episode
		],
		[
			serverState('finish_blocked', { tone: 'attention' }),
			PAGE_STATE_COPY.finish_blocked.season,
			PAGE_STATE_COPY.finish_blocked.episode
		],
		[
			serverState('ready_to_finish'),
			PAGE_STATE_COPY.ready_to_finish.season,
			PAGE_STATE_COPY.ready_to_finish.episode
		],
		[
			serverState('finished', { tone: 'success' }),
			PAGE_STATE_COPY.finished.season,
			PAGE_STATE_COPY.finished.episode
		],
		[
			serverState('needs_help', {
				tone: 'attention',
				recovery_kind: 'season',
				primary_action: 'retry_encode'
			}),
			PAGE_STATE_COPY.stopped.season,
			PAGE_STATE_COPY.stopped.episode
		],
		[
			serverState('needs_help', {
				tone: 'attention',
				recovery_kind: 'test',
				primary_action: 'retry_sample'
			}),
			PAGE_STATE_COPY.sample_failed,
			null
		]
	])('words %j for a season and an exact episode', (state, seasonCopy, episodeCopy) => {
		const season = pageStateWording(state);
		const episode = pageStateWording(state, { exactEpisode: true });

		expect(season).toMatchObject({ key: state.key, tone: state.tone, ...seasonCopy });
		expect(episode).toMatchObject({
			key: state.key,
			tone: state.tone,
			...(episodeCopy ?? seasonCopy)
		});
	});

	it('explains blocked files with the workflow state’s own blockers', () => {
		const state = serverState('needs_help', {
			tone: 'attention',
			recovery_kind: 'files',
			primary_action: 'review_files'
		});

		expect(pageStateWording(state, { workflow: blockedWorkflow })).toMatchObject({
			key: 'needs_help',
			recoveryKind: 'files',
			label: PAGE_STATE_COPY.files_blocked.label,
			detail: blockedWorkflow.blockers.join(' ')
		});
		expect(pageStateWording(state, { workflow: { ...blockedWorkflow, blockers: [] } }).detail).toBe(
			blockedWorkflow.detail
		);
		expect(cardStateWording(card(state, { workflow_state: blockedWorkflow }))).toEqual(
			pageStateWording(state, { workflow: blockedWorkflow })
		);
	});

	it('keeps finish unavailable until the complete staged-file check allows it', () => {
		const ready = serverState('ready_to_finish');

		expect(pageStateWording(ready, { integrity: integrity({}) })).toMatchObject({
			key: 'ready_to_finish',
			...PAGE_STATE_COPY.ready_to_finish.season
		});
		expect(
			pageStateWording(ready, { integrity: integrity({ available: false, canFinish: false }) })
		).toMatchObject({
			key: 'finish_blocked',
			tone: 'attention',
			...PAGE_STATE_COPY.finish_checking.season
		});
		expect(
			pageStateWording(ready, {
				exactEpisode: true,
				integrity: integrity({ canFinish: false, readyCount: 0 })
			})
		).toMatchObject({ key: 'finish_blocked', ...PAGE_STATE_COPY.finish_blocked.episode });
	});

	it('offers the recovery the server chose', () => {
		const help = (fields: Partial<PageStatePayload>) =>
			serverState('needs_help', { tone: 'attention', ...fields });

		expect(
			pageStateWording(help({ recovery_kind: 'season', primary_action: 'retry_encode' }))
				.actionLabel
		).toBe((RECOVERY_ACTION_COPY.retry_encode as { season: string }).season);
		expect(
			pageStateWording(help({ recovery_kind: 'season', primary_action: 'retry_encode' }), {
				exactEpisode: true
			}).actionLabel
		).toBe((RECOVERY_ACTION_COPY.retry_encode as { episode: string }).episode);
		expect(
			pageStateWording(help({ recovery_kind: 'test', primary_action: 'make_sample' })).actionLabel
		).toBe(RECOVERY_ACTION_COPY.retry_sample);
		expect(
			pageStateWording(help({ recovery_kind: 'test', primary_action: 'choose_size' })).actionLabel
		).toBe(RECOVERY_ACTION_COPY.choose_size);
	});

	it('reads what happened from the failure the server recorded', () => {
		expect(plainFailureMessage('size_under_target')).toBe(FAILURE_COPY.size_under_target);
		expect(plainFailureMessage(null)).toBe(FAILURE_COPY.unexplained);
		// A size goal that cannot be met never offers the same retry.
		for (const failure of [
			'arithmetic_infeasible',
			'bound_exhausted',
			'quality_conflict'
		] as const) {
			expect(plainFailureMessage(failure)).toBe(FAILURE_COPY.goal_too_small);
		}
	});

	it('says nothing it cannot know when an older server sends no page state', () => {
		expect(pageStateWording(undefined)).toMatchObject({
			key: 'unavailable',
			tone: 'quiet',
			...PAGE_STATE_COPY.unavailable
		});
		expect(cardStateWording(card(null)).key).toBe('unavailable');
	});
});

describe('card state wording', () => {
	it('sends the owner into the season for the details', () => {
		const stopped = serverState('needs_help', { tone: 'attention', recovery_kind: 'season' });
		const sampleFailed = serverState('needs_help', { tone: 'attention', recovery_kind: 'test' });

		expect(cardStateWording(card(stopped))).toMatchObject({
			key: 'needs_help',
			recoveryKind: 'season',
			...CARD_STATE_COPY.stopped
		});
		expect(cardStateWording(card(sampleFailed))).toMatchObject({
			recoveryKind: 'test',
			...CARD_STATE_COPY.sample_failed
		});
		expect(cardStateWording(card(serverState('ready_to_make')))).toMatchObject({
			key: 'ready_to_make',
			...CARD_STATE_COPY.ready_to_make
		});
		expect(cardStateWording(card(serverState('ready_to_compare')))).toMatchObject(
			PAGE_STATE_COPY.ready_to_compare
		);
		// A season row whose remaining episodes are all held for motion never reads as ready to compress.
		expect(cardStateWording(card(serverState('motion_held', { tone: 'quiet' })))).toMatchObject({
			key: 'motion_held',
			...PAGE_STATE_COPY.motion_held.season
		});
	});

	it('keeps a working season working while naming the files that need the owner', () => {
		const working = serverState('making_season', { tone: 'active' });
		const detail = '1 had trouble reading or writing media · 4 waiting for a scheduled time';

		expect(
			cardStateWording(
				card(working, { review_badge_label: 'Needs attention', review_badge_detail: detail })
			)
		).toMatchObject({
			key: 'making_season',
			label: CARD_STATE_COPY.making_season_needs_you.label,
			detail
		});
		expect(cardStateWording(card(working))).toMatchObject(PAGE_STATE_COPY.making_season.season);
	});
});

describe('page state copy', () => {
	function strings(value: unknown): string[] {
		if (typeof value === 'string') return [value];
		if (value && typeof value === 'object') return Object.values(value).flatMap(strings);
		return [];
	}

	it('keeps technical job nouns out of first-level state copy', () => {
		const copy = [PAGE_STATE_COPY, CARD_STATE_COPY, FAILURE_COPY, RECOVERY_ACTION_COPY]
			.flatMap(strings)
			.join(' ');

		expect(copy).not.toMatch(
			/\b(calibration|candidate|worker|host|Mac|preview|CRF|VMAF|prefix|lane|job)\b/i
		);
	});
});
