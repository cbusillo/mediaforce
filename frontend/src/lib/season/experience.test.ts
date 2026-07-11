import { describe, expect, it } from 'vitest';

import type {
	DashboardSummaryPayload,
	FolderCard,
	FolderPayload,
	FolderStatusPayload,
	FolderWorkflowState,
	WorkflowLane
} from '$lib/api/types';
import {
	activeSeasonCards,
	approvalGuardFromMessage,
	detailSeasonState,
	episodeLabel,
	folderSizeTargetAnalysis,
	isSeriesPrefix,
	formatFileSize,
	measuredFollowupRequest,
	librarySeasonState,
	normalizeReviewPairs,
	reviewSampleSizes,
	seasonIdentity,
	sizeGoals,
	technicalVideoPolicy
} from './experience';

const card: FolderCard = {
	prefix: 'tv/Big Brother (US)/Season 19',
	title: 'Big Brother (US) · Season 19',
	subtitle: 'Big Brother (US)',
	scope_label: 'Season',
	item_count: 39,
	pending_count: 39,
	total_size_bytes: 184_347_579_367,
	estimated_savings_bytes: 0,
	known_saved_bytes: 0,
	projected_reclaim_bytes: 0,
	average_age_days: 0,
	sort_score: 0,
	statuses: { discovered: 39 },
	video_codecs: { h264: 39 },
	details_loading: false
};

const dashboard = {
	calibration_queue: {
		sample: {
			running: [],
			queued: [],
			pending_review: [],
			running_count: 0,
			queued_count: 0,
			pending_review_count: 0
		},
		full: {
			running: [],
			queued: [],
			pending_review: [],
			running_count: 0,
			queued_count: 0,
			pending_review_count: 0
		},
		active_count: 0
	},
	encode_queue: {
		running_count: 0,
		queued_count: 0,
		running: [],
		queued: [],
		state: { is_paused: false, stop_requested: false }
	},
	folders_preview: [],
	library_colors: {},
	scan_job: null,
	catalog_empty: false,
	folder_cache_key: '',
	metric_support: { vmaf: true, xpsnr: true, ssim: true },
	metric_status_copy: ''
} satisfies DashboardSummaryPayload;

const status = {
	prefix: card.prefix,
	polling_active: false,
	calibration_status: 'idle',
	folder_scan_status: 'idle',
	calibration_job: null,
	folder_scan_job: null
} satisfies FolderStatusPayload;

function folder(overrides: Partial<FolderPayload> = {}): FolderPayload {
	return {
		prefix: card.prefix,
		pending: false,
		metric_support: dashboard.metric_support,
		metric_status_copy: '',
		summary: {
			prefix: card.prefix,
			item_count: 39,
			total_size_bytes: card.total_size_bytes,
			statuses: card.statuses,
			video_codecs: card.video_codecs,
			audio_codecs: {},
			seasons: { 'Season 19': 39 },
			resolved_policy: { video: { target_size_mb: 300 } }
		},
		...overrides
	};
}

function workflowState(primaryLane: WorkflowLane, state = primaryLane): FolderWorkflowState {
	return {
		prefix: card.prefix,
		state,
		primary_lane: primaryLane,
		label: '',
		tone: primaryLane === 'complete' ? 'success' : 'ready',
		detail: '',
		counts: {},
		lane_counts: {},
		state_counts: {},
		next_action: {
			kind: 'none',
			label: '',
			enabled: false,
			target_prefix: card.prefix
		},
		blockers: []
	};
}

describe('season experience translation', () => {
	it('parses a human show and season identity', () => {
		expect(seasonIdentity(card.prefix)).toEqual({
			library: 'tv',
			show: 'Big Brother (US)',
			season: 'Season 19',
			showPrefix: 'tv/Big Brother (US)'
		});
	});

	it('shows the queued sample target instead of the folder default', () => {
		const videoPolicy = technicalVideoPolicy(
			folder({
				policy: { video: { target_size_mb: 300, target_runtime_minutes: 45 } },
				calibration_job: {
					status: 'queued',
					policy: { video: { target_size_mb: 225, target_runtime_minutes: 88 } }
				}
			})
		);

		expect(videoPolicy.target_size_mb).toBe(225);
		expect(videoPolicy.target_runtime_minutes).toBe(88);
	});

	it('shows a pending proposal target ahead of an active sample target', () => {
		const videoPolicy = technicalVideoPolicy(
			folder({
				pending_proposal: {
					preview_policy: { video: { target_size_mb: 200, target_runtime_minutes: 88 } }
				},
				calibration_job: {
					status: 'running',
					policy: { video: { target_size_mb: 225, target_runtime_minutes: 88 } }
				}
			})
		);

		expect(videoPolicy.target_size_mb).toBe(200);
	});

	it('falls through a pending proposal without video settings', () => {
		const videoPolicy = technicalVideoPolicy(
			folder({
				pending_proposal: { preview_policy: { audio: { stereo_opus_bitrate: '112k' } } },
				calibration_job: {
					status: 'running',
					policy: { video: { target_size_mb: 225, target_runtime_minutes: 88 } }
				}
			})
		);

		expect(videoPolicy.target_size_mb).toBe(225);
	});

	it('reports an over-target measured sample without confusing it with the requested size', () => {
		const analysis = folderSizeTargetAnalysis(
			folder({
				size_target_analysis: {
					status: 'over_target',
					budget_bytes: 225 * 1024 ** 2,
					predicted_total_size_bytes: 1_089_842_509,
					lower_bound_bytes: 200_540_160,
					upper_bound_bytes: 271_319_040,
					predicted_to_budget_ratio: 4.619
				}
			})
		);

		expect(analysis).toMatchObject({
			status: 'over_target',
			budgetBytes: 225 * 1024 ** 2,
			predictedBytes: 1_089_842_509,
			predictedToBudgetRatio: 4.619
		});
	});

	it('describes preview moments as a ratio while retaining their measured duration', () => {
		const sizes = reviewSampleSizes(
			folder({
				calibration: {
					review_pairs: [0, 1, 2].map((index) => ({
						source_clip: {
							path: `/source-${index}.mp4`,
							duration_seconds: 8,
							size_bytes: 15 * 1024 ** 2
						},
						preview_clip: {
							path: `/preview-${index}.mp4`,
							duration_seconds: 8,
							size_bytes: 1.5 * 1024 ** 2
						}
					}))
				}
			})
		);

		expect(sizes).toEqual({
			original: 45 * 1024 ** 2,
			smaller: 4.5 * 1024 ** 2,
			durationSeconds: 24,
			ratioPercent: 10
		});
	});

	it('turns a missed target into an explicit measured follow-up request', () => {
		const request = measuredFollowupRequest({
			status: 'over_target',
			budgetBytes: 225 * 1024 ** 2,
			predictedBytes: 1_089_842_509,
			lowerBoundBytes: 200_540_160,
			upperBoundBytes: 271_319_040,
			predictedToBudgetRatio: 4.619
		});

		expect(request).toContain('Measured follow-up');
		expect(request).toContain('keep the 225 MB per episode goal');
		expect(request).toContain('4.6 times over that goal');
		expect(request).toContain('materially smaller toward the goal');
		expect(request).toContain('Keep the current resolution');
	});

	it('keeps the measured target when asking for an under-target quality pass', () => {
		const request = measuredFollowupRequest({
			status: 'under_target',
			budgetBytes: 225 * 1024 ** 2,
			predictedBytes: 150 * 1024 ** 2,
			lowerBoundBytes: 200_540_160,
			upperBoundBytes: 271_319_040,
			predictedToBudgetRatio: 0.667
		});

		expect(request).toContain('keep the 225 MB per episode goal');
		expect(request).toContain('spend more of the available size');
	});

	it('repeats the preserved target when the episode estimate is missing', () => {
		const request = measuredFollowupRequest({
			status: 'missing_prediction',
			budgetBytes: 225 * 1024 ** 2,
			predictedBytes: 0,
			lowerBoundBytes: 200_540_160,
			upperBoundBytes: 271_319_040,
			predictedToBudgetRatio: 0
		});

		expect(request).toContain('225 MB per episode');
		expect(request).toContain('previous run did not produce a usable full-episode size estimate');
	});

	it('distinguishes a whole show from one season', () => {
		expect(isSeriesPrefix('tv/Big Brother (US)')).toBe(true);
		expect(isSeriesPrefix('tv/Big Brother (US)/Season 19')).toBe(false);
	});

	it('does not call a newly discovered season ready to encode', () => {
		expect(librarySeasonState(card, dashboard)).toMatchObject({
			key: 'needs_test',
			label: 'Make a test'
		});
	});

	it('keeps finishing states visible on the home screen', () => {
		const finishingCard = { ...card, workflow_state: workflowState('promote') };
		expect(librarySeasonState(finishingCard, dashboard)).toMatchObject({
			key: 'ready_to_finish',
			label: 'Ready to finish'
		});
		expect(activeSeasonCards([finishingCard], dashboard)).toHaveLength(1);
	});

	it.each([
		['attention', 'needs_help'],
		['validate', 'ready_to_check'],
		['promote', 'ready_to_finish'],
		['processing', 'making_season'],
		['complete', 'finished']
	] as const)('shows the %s workflow before an older approved-test badge', (lane, expectedKey) => {
		expect(
			librarySeasonState(
				{
					...card,
					review_badge_label: 'Approved draft',
					workflow_state: workflowState(lane)
				},
				dashboard
			)
		).toMatchObject({ key: expectedKey });
	});

	it('uses review evidence over the generic folder state', () => {
		expect(
			detailSeasonState(
				folder({
					calibration: {
						draft_hash: 'draft-1',
						accepted_draft_hash: null,
						browser_review_ready: true
					},
					workflow_state: {
						prefix: card.prefix,
						state: 'encode_candidates',
						primary_lane: 'encode',
						label: 'Ready to encode',
						tone: 'ready',
						detail: '',
						counts: {},
						lane_counts: {},
						state_counts: {},
						next_action: {
							kind: 'queue_encode',
							label: 'Queue encode',
							enabled: true,
							target_prefix: card.prefix
						},
						blockers: []
					}
				}),
				status
			)
		).toMatchObject({ key: 'ready_to_compare', label: 'Test ready' });
	});

	it('shows an interrupted season before an older approved test', () => {
		expect(
			detailSeasonState(
				folder({
					calibration: {
						draft_hash: 'draft-1',
						accepted_draft_hash: 'draft-1',
						browser_review_ready: true
					},
					encode_job: {
						job_id: 'job-1',
						prefix: card.prefix,
						status: 'needs_attention',
						error: 'Encode queue job was interrupted by a web process restart.'
					}
				}),
				status
			)
		).toMatchObject({ key: 'needs_help', label: 'The season stopped' });
	});

	it('recognizes a saved test that never produced comparison media', () => {
		expect(
			detailSeasonState(
				folder({
					calibration: {
						job_id: 'sample-1',
						mode: 'sample',
						review_media_ready: false
					}
				}),
				status
			)
		).toMatchObject({ key: 'needs_help', label: 'The test needs another try' });
	});

	it('recognizes a measured test whose review clips are missing', () => {
		expect(
			detailSeasonState(
				folder({
					calibration: {
						job_id: 'sample-1',
						draft_hash: 'draft-1',
						review_media_ready: false
					},
					review_gate: { status: 'missing_review_media', can_confirm_full: false }
				}),
				status
			)
		).toMatchObject({ key: 'needs_help', label: 'The test needs another try' });
	});

	it.each([
		['validate', 'ready_to_check'],
		['promote', 'ready_to_finish'],
		['complete', 'finished']
	] as const)('translates the %s delivery step', (lane, expectedKey) => {
		expect(
			detailSeasonState(folder({ workflow_state: workflowState(lane) }), status)
		).toMatchObject({ key: expectedKey });
	});

	it('shows real active work before the generic folder state', () => {
		expect(
			detailSeasonState(
				folder({
					encode_job: {
						job_id: 'job-1',
						prefix: card.prefix,
						status: 'running',
						progress: { percent_complete: 42 }
					}
				}),
				status
			)
		).toMatchObject({ key: 'making_season', label: 'Making the season' });
	});

	it('makes real size goals around the resolved target', () => {
		expect(sizeGoals(folder()).map((goal) => goal.megabytesPerEpisode)).toEqual([300, 225, 450]);
	});

	it('normalizes actual source and preview clips', () => {
		expect(
			normalizeReviewPairs(
				folder({
					calibration: {
						review_pairs: [
							{
								source_clip: { path: '/source.mp4', size_bytes: 120 },
								preview_clip: { path: '/preview.mp4', size_bytes: 35 },
								compare_clip: { path: '/compare.mkv' }
							}
						]
					}
				})
			)
		).toEqual([
			{
				source: {
					path: '/source.mp4',
					timestampSeconds: 0,
					durationSeconds: 0,
					sizeBytes: 120
				},
				preview: {
					path: '/preview.mp4',
					timestampSeconds: 0,
					durationSeconds: 0,
					sizeBytes: 35
				},
				comparePath: '/compare.mkv'
			}
		]);
	});

	it('formats filenames and sizes for people', () => {
		expect(episodeLabel('tv/Show/Season 2/Show.S02E07.mkv')).toBe('Episode 7');
		expect(formatFileSize(1_073_741_824)).toBe('1.0 GB');
	});

	it('turns backend approval gates into explicit human confirmations', () => {
		expect(
			approvalGuardFromMessage(
				'This draft includes high-impact policy changes. Review the diff, then confirm approval again.'
			)
		).toMatchObject({ kind: 'high_impact', confirmHighImpact: true });
		expect(
			approvalGuardFromMessage(
				'The sampled run predicts 400 MB, above the requested 300 MB target band.',
				true
			)
		).toMatchObject({
			kind: 'size_tradeoff',
			confirmHighImpact: true,
			confirmSizeTradeoff: true
		});
	});
});
