import type {
	FolderCard,
	FolderWorkflowState,
	PageStateAction,
	PageStateFailure,
	PageStateKey,
	PageStatePayload
} from '$lib/api/types';
import type { SeasonPromotionIntegrity } from './experience';

// The server decides which state a folder page or library card leads with (`page_state`); this module
// only words it. Nothing here orders jobs or re-decides precedence.

export type HumanSeasonStateKey = PageStateKey | 'unavailable';
export type HumanSeasonTone = PageStatePayload['tone'];

export interface HumanSeasonState {
	key: HumanSeasonStateKey;
	label: string;
	detail: string;
	tone: HumanSeasonTone;
	recoveryKind?: 'test' | 'season' | 'files';
	failure?: PageStateFailure | null;
	/** The recovery button's words for a state that needs help. */
	actionLabel?: string;
}

interface Copy {
	label: string;
	detail: string;
}

type Scope = 'season' | 'episode';
type ScopedCopy = Copy | Record<Scope, Copy>;

const NEEDS_A_LITTLE_HELP: Copy = {
	label: 'Needs a little help',
	detail: 'Open the season for one clear recovery step.'
};

/** The words for each state; an exact episode's page uses its own where it has them. */
export const PAGE_STATE_COPY = {
	needs_test: { label: 'Needs sample', detail: 'Choose a size, then compare one sample first.' },
	sample_waiting: {
		label: 'Sample waiting',
		detail: 'This sample has not started. It is waiting for an available computer.'
	},
	sample_retrying: {
		label: 'Sample waiting',
		detail: 'The last attempt stopped. Mediaforce will retry this sample shortly.'
	},
	making_test: {
		season: {
			label: 'Creating sample',
			detail: 'One representative episode is being compressed into comparison clips now.'
		},
		episode: {
			label: 'Creating sample',
			detail: 'This episode is being compressed into comparison clips now.'
		}
	},
	ready_to_compare: { label: 'Ready to review', detail: 'Compare the original and the sample.' },
	ready_to_make: {
		season: { label: 'Sample approved', detail: 'The rest of the season can be compressed.' },
		episode: { label: 'Sample approved', detail: 'This episode can be compressed.' }
	},
	motion_held: {
		season: {
			label: 'Stays original',
			detail:
				'The remaining episodes stay original because Mediaforce can’t convert their motion pattern safely.'
		},
		episode: {
			label: 'Stays original',
			detail:
				'This episode stays original because Mediaforce can’t convert its motion pattern safely.'
		}
	},
	// Some held episodes have an unclear motion pattern the show page can encode as-is.
	motion_decision: {
		season: {
			label: 'Motion decision',
			detail: 'The show page can encode episodes with an unclear motion pattern as-is.'
		},
		episode: {
			label: 'Motion decision',
			detail: 'The show page can encode this episode’s unclear motion pattern as-is.'
		}
	},
	making_season: {
		season: {
			label: 'Compressing the season',
			detail: 'The smaller episodes are being compressed now.'
		},
		episode: {
			label: 'Compressing the episode',
			detail: 'The smaller episode is being compressed now.'
		}
	},
	ready_to_check: {
		season: {
			label: 'Ready to check',
			detail: 'The new episodes are ready for a safety check.'
		},
		episode: { label: 'Ready to check', detail: 'The new episode is ready for a safety check.' }
	},
	finish_checking: {
		season: {
			label: 'Checking the season',
			detail: 'Mediaforce is confirming every episode before enabling finish.'
		},
		episode: {
			label: 'Checking the episode',
			detail: 'Mediaforce is confirming the episode before enabling finish.'
		}
	},
	finish_blocked: {
		season: {
			label: 'Season not ready',
			detail: 'Each episode is checked separately. Files that are not ready stay untouched.'
		},
		episode: {
			label: 'Episode not ready',
			detail: 'The episode must be accounted for before its file is replaced.'
		}
	},
	ready_to_finish: {
		season: { label: 'Ready to replace', detail: 'The smaller episodes passed their checks.' },
		episode: { label: 'Ready to replace', detail: 'The smaller episode passed its checks.' }
	},
	finished: {
		season: { label: 'Finished', detail: 'Every episode is in place.' },
		episode: { label: 'Finished', detail: 'The episode is in place.' }
	},
	stopped: {
		season: {
			label: 'The season stopped',
			detail: 'Completed episodes are safe. Try the unfinished work again.'
		},
		episode: {
			label: 'The episode stopped',
			detail: 'Nothing was replaced. Try this episode again.'
		}
	},
	// The detail is the workflow state's own list of what blocks the files.
	files_blocked: { label: 'Needs attention', detail: '' },
	sample_failed: {
		label: 'Sample needs retry',
		detail: 'Nothing was replaced. You can retry the same sample.'
	},
	unavailable: {
		label: 'Status unavailable',
		detail: 'Refresh the page to see where this stands.'
	}
} as const satisfies Record<string, ScopedCopy>;

/** Library and show-page season rows say less, and send the owner into the season for the details. */
export const CARD_STATE_COPY = {
	ready_to_make: {
		label: 'Ready to compress',
		detail: 'The season has an approved setup and can be compressed.'
	},
	making_season_needs_you: {
		label: 'Compressing · needs you',
		detail: PAGE_STATE_COPY.making_season.season.detail
	},
	stopped: NEEDS_A_LITTLE_HELP,
	sample_failed: NEEDS_A_LITTLE_HELP
} as const satisfies Partial<Record<PageStateCopyKey | 'making_season_needs_you', Copy>>;

export type PageStateCopyKey = keyof typeof PAGE_STATE_COPY;

const GOAL_TOO_SMALL = 'That size goal was too small for this episode. Try a roomier goal.';

/** What a stopped sample or run says happened, by the failure the server recorded. */
export const FAILURE_COPY: Record<PageStateFailure, string> = {
	missing_clips:
		'The previous sample ended before its comparison clips were ready. Nothing was replaced.',
	unexplained: 'The work stopped before it finished. Your completed files are still safe.',
	restarted: 'The work stopped when Mediaforce restarted. Your completed files are still safe.',
	no_access:
		'The selected computer could not reach one of the files. Check its connection, then try again.',
	size_under_target:
		'The finished file was smaller than the approved range. Choose a fresh size or quality preference and create another sample.',
	size_over_target:
		'The finished file was larger than the approved range. Choose a fresh size or quality preference and create another sample.',
	size_unverified:
		'Mediaforce could not verify the finished file against the approved size range. Choose a fresh size or quality preference and create another sample.',
	goal_too_small: GOAL_TOO_SMALL,
	// A size goal that cannot be met; the page's size explanation leads when its measurements are on hand.
	arithmetic_infeasible: GOAL_TOO_SMALL,
	bound_exhausted: GOAL_TOO_SMALL,
	quality_conflict: GOAL_TOO_SMALL,
	stopped: 'The work was stopped before it finished. Nothing was replaced.',
	other: 'The work stopped before it finished. Nothing was replaced, and you can try again.'
};

/** The recovery button for each action a state that needs help offers. */
export const RECOVERY_ACTION_COPY: Partial<
	Record<PageStateAction, string | Record<Scope, string>>
> = {
	retry_sample: 'Retry sample',
	// A sample that cannot be retried as saved is made again from its saved request.
	make_sample: 'Retry sample',
	choose_size: 'Choose size and settings',
	retry_encode: { season: 'Retry unfinished episodes', episode: 'Retry this episode' }
};

export function plainFailureMessage(failure: PageStateFailure | null | undefined): string {
	return FAILURE_COPY[failure ?? 'unexplained'];
}

export interface PageStateWordingContext {
	exactEpisode?: boolean;
	/** The page's workflow state, whose blockers explain blocked files. */
	workflow?: FolderWorkflowState | null;
	/**
	 * The page's complete staged-file check. The finish action stays unavailable until it allows
	 * finishing, so a season the server calls ready to finish still reads as being checked.
	 */
	integrity?: SeasonPromotionIntegrity;
}

/** The copy entry a state uses. */
export function pageStateCopyKey(
	state: PageStatePayload,
	integrity?: SeasonPromotionIntegrity
): PageStateCopyKey {
	switch (state.key) {
		case 'sample_waiting':
			return state.source_job?.status === 'retry_backoff' ? 'sample_retrying' : 'sample_waiting';
		case 'ready_to_finish':
			if (!integrity || integrity.canFinish) return 'ready_to_finish';
			return integrity.available ? 'finish_blocked' : 'finish_checking';
		case 'finish_blocked':
			return integrity && !integrity.available ? 'finish_checking' : 'finish_blocked';
		case 'motion_held':
			return state.primary_action === 'decide_motion' ? 'motion_decision' : 'motion_held';
		case 'needs_help':
			return state.recovery_kind === 'files'
				? 'files_blocked'
				: state.recovery_kind === 'test'
					? 'sample_failed'
					: 'stopped';
		default:
			return state.key;
	}
}

function scoped<T>(value: T | Record<Scope, T>, scope: Scope): T {
	return typeof value === 'object' && value !== null && 'season' in value
		? (value as Record<Scope, T>)[scope]
		: (value as T);
}

function blockersDetail(workflow: FolderWorkflowState | null | undefined): string {
	return workflow ? workflow.blockers.join(' ') || workflow.detail : '';
}

function recoveryActionLabel(action: PageStateAction | null, scope: Scope): string | undefined {
	const copy = action ? RECOVERY_ACTION_COPY[action] : undefined;
	return copy === undefined ? undefined : scoped(copy, scope);
}

/** How a folder page words the server's page state. */
export function pageStateWording(
	state: PageStatePayload | null | undefined,
	context: PageStateWordingContext = {}
): HumanSeasonState {
	if (!state) return { key: 'unavailable', tone: 'quiet', ...PAGE_STATE_COPY.unavailable };
	const scope: Scope = context.exactEpisode ? 'episode' : 'season';
	const copyKey = pageStateCopyKey(state, context.integrity);
	const copy = scoped<Copy>(PAGE_STATE_COPY[copyKey], scope);
	const key =
		copyKey === 'finish_blocked' || copyKey === 'finish_checking' ? 'finish_blocked' : state.key;
	return {
		key,
		label: copy.label,
		detail: copyKey === 'files_blocked' ? blockersDetail(context.workflow) : copy.detail,
		tone: key === 'finish_blocked' ? 'attention' : state.tone,
		recoveryKind: state.recovery_kind ?? undefined,
		failure: state.failure,
		actionLabel: recoveryActionLabel(state.primary_action, scope)
	};
}

/** How a library or show-page season row words its card's page state. */
export function cardStateWording(card: FolderCard): HumanSeasonState {
	const wording = pageStateWording(card.page_state, { workflow: card.workflow_state });
	if (!card.page_state) return wording;
	const copyKey = pageStateCopyKey(card.page_state);
	// A working season still says so when some of its files already need the owner.
	const needsYou =
		copyKey === 'making_season' &&
		(card.review_badge_label ?? '').toLowerCase().includes('attention');
	const cardCopy: Copy | undefined = needsYou
		? {
				...CARD_STATE_COPY.making_season_needs_you,
				detail: card.review_badge_detail || CARD_STATE_COPY.making_season_needs_you.detail
			}
		: CARD_STATE_COPY[copyKey as keyof typeof CARD_STATE_COPY];
	return cardCopy ? { ...wording, label: cardCopy.label, detail: cardCopy.detail } : wording;
}
