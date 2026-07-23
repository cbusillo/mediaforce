import { describe, expect, it } from 'vitest';

import type {
	DashboardScanJob,
	DashboardScanWarning,
	DashboardSummaryPayload,
	FolderCard,
	FolderPayload,
	FolderStatusPayload,
	FolderWorkflowState,
	SizeGoalOptionPayload,
	WorkflowLane
} from '$lib/api/types';
import {
	activeSeasonCards,
	approvalGuardFromMessage,
	calibrationEtaSummary,
	calibrationJobTargetContract,
	calibrationLivenessLabel,
	calibrationStageLabel,
	calibrationWorkLabel,
	catalogWarningNotice,
	compareRiskSummary,
	currentOperatorIntent,
	detailSeasonState,
	episodeLabel,
	folderSizeTargetAnalysis,
	formatDecimalFileSize,
	formatFileSize,
	goalRequest,
	isSeriesPrefix,
	isSizeGoalSelectionConfirmed,
	measuredFollowupRequest,
	librarySeasonState,
	normalizeReviewPairs,
	overlappingCalibrationActivity,
	plainFailureMessage,
	resolvedTargetSummary,
	reviewFeedbackIntent,
	reviewFeedbackRequest,
	reviewSampleSizes,
	seasonIdentity,
	sizeGoals,
	targetConstraintSummary,
	testRequestWithInstructions,
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
	media_scope: {
		schema_version: 1,
		prefix: card.prefix,
		root: 'tv',
		domain: 'tv',
		kind: 'tv_season',
		match: 'descendants',
		title: card.title,
		subtitle: card.subtitle,
		scope_label: card.scope_label,
		parent: { prefix: 'tv/Big Brother (US)', title: 'Big Brother (US)' }
	},
	polling_active: false,
	calibration_status: 'idle',
	folder_scan_status: 'idle',
	calibration_job: null,
	folder_scan_job: null
} satisfies FolderStatusPayload;

function scanJobWithWarnings(...warnings: DashboardScanWarning[]): DashboardScanJob {
	return {
		job_id: 'scan-warning',
		status: 'completed',
		scope: 'full',
		prefix: null,
		created_at: null,
		started_at: null,
		finished_at: null,
		error: null,
		stats: { unchanged: 0, warnings }
	};
}

describe('catalog scan warnings', () => {
	it('explains that cached catalog state was preserved for unavailable storage', () => {
		const notice = catalogWarningNotice({
			job_id: 'scan-1',
			status: 'completed',
			scope: 'full',
			prefix: null,
			created_at: null,
			started_at: null,
			finished_at: null,
			error: null,
			stats: {
				unchanged: 24,
				warnings: [
					{
						code: 'source_unavailable',
						library_key: 'movies',
						label: 'Movies',
						message: 'Movies is unavailable. Cached catalog state was preserved.',
						preserved_item_count: 24
					}
				]
			}
		});

		expect(notice).toEqual({
			title: 'Movies storage is unavailable.',
			detail:
				'Cached catalog state was preserved. Mediaforce will try this library again after storage returns.'
		});
		expect(JSON.stringify(notice)).not.toContain('/Volumes/');
	});

	it('explains the mass-disappearance circuit breaker', () => {
		const notice = catalogWarningNotice({
			job_id: 'scan-2',
			status: 'completed',
			scope: 'full',
			prefix: null,
			created_at: null,
			started_at: null,
			finished_at: null,
			error: null,
			stats: {
				unchanged: 5,
				warnings: [
					{
						code: 'source_mass_disappearance',
						library_key: 'movies',
						label: 'Movies',
						message: 'Movies returned far fewer media items than expected.',
						preserved_item_count: 25
					}
				]
			}
		});

		expect(notice?.title).toBe('Movies returned far fewer items than expected.');
		expect(notice?.detail).toBe(
			'Cached catalog state was preserved so surviving workflow status remains unchanged.'
		);
	});

	it('explains unexpectedly empty and incomplete library scans', () => {
		const empty = catalogWarningNotice(
			scanJobWithWarnings({
				code: 'source_unexpectedly_empty',
				library_key: 'movies',
				label: 'Movies',
				message: 'Movies returned no media.',
				preserved_item_count: 24
			})
		);
		const incomplete = catalogWarningNotice(
			scanJobWithWarnings({
				code: 'source_scan_incomplete',
				library_key: 'other',
				label: 'Other',
				message: 'Other could not be fully read.',
				preserved_item_count: 8
			})
		);

		expect(empty?.title).toBe('Movies returned no media.');
		expect(empty?.detail).toContain('Disable or remove the library in Settings');
		expect(incomplete).toEqual({
			title: 'Other could not be fully read.',
			detail:
				'Cached catalog state was preserved because the scan did not finish reading this library.'
		});
	});

	it('uses accurate generic copy when multiple warning types are present', () => {
		const notice = catalogWarningNotice(
			scanJobWithWarnings(
				{
					code: 'source_scan_incomplete',
					library_key: 'tv',
					label: 'TV',
					message: 'TV could not be fully read.',
					preserved_item_count: 12
				},
				{
					code: 'source_mass_disappearance',
					library_key: 'movies',
					label: 'Movies',
					message: 'Movies returned far fewer media items than expected.',
					preserved_item_count: 25
				}
			)
		);

		expect(notice).toEqual({
			title: '2 libraries need attention.',
			detail:
				'Mediaforce could not safely reconcile these libraries, so their cached catalog state was preserved.'
		});
	});
});

function folder(overrides: Partial<FolderPayload> = {}): FolderPayload {
	return {
		prefix: card.prefix,
		media_scope: status.media_scope,
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

function sizeOption(
	key: string,
	mode: 'normalized' | 'absolute',
	valueMegabytes: number,
	targetMegabytes: number,
	referenceRuntimeMinutes?: number,
	requiresExplicitSelection = false
): SizeGoalOptionPayload {
	const rationale =
		mode === 'normalized'
			? `${valueMegabytes} MB per ${referenceRuntimeMinutes} minutes scales to about ${Math.round(targetMegabytes)} MB for this episode.`
			: `${valueMegabytes} MB is an absolute per-episode target and does not scale with runtime.`;
	return {
		key,
		title: key === 'recommended' ? 'Recommended' : key,
		detail: rationale,
		requires_explicit_selection: requiresExplicitSelection,
		operator_intent: {
			schema_version: 1,
			size_goal: {
				mode,
				value_mb: valueMegabytes,
				...(referenceRuntimeMinutes ? { reference_runtime_minutes: referenceRuntimeMinutes } : {}),
				sample_projection_tolerance_percent: 10,
				final_output_tolerance_percent: 5
			},
			resolution: { mode: 'source', max_height: null }
		},
		resolved_size_goal: {
			schema_version: 1,
			mode,
			source: 'guided_preset',
			status: 'resolved',
			requires_confirmation: false,
			reference_size_mb: valueMegabytes,
			reference_runtime_minutes: referenceRuntimeMinutes ?? null,
			target_size_bytes: targetMegabytes * 1_000_000,
			target_size_mb: targetMegabytes,
			sample_projection_tolerance_percent: 10,
			final_output_tolerance_percent: 5,
			rationale
		}
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

	it('summarizes typed quality risk for the compare screen', () => {
		const summary = compareRiskSummary(
			folder({
				quality_risk: {
					verdict: 'request_comparison',
					request_comparison: true,
					comparison_reason: 'Low-confidence grain findings require comparison.',
					typed_risks: [
						{
							tag: 'grain_noise_treatment',
							label: 'Grain / noise treatment',
							level: 'medium',
							rationale: 'Measured temporal noise may be grain rather than removable noise.'
						}
					],
					operator_decision: {
						status: 'pending'
					},
					pre_test_instruction: {
						moments: [
							{
								moment: 2,
								timestamp_seconds: 89,
								role: 'hard',
								risk_tags: ['grain_noise_treatment']
							}
						]
					}
				}
			})
		);

		expect(summary).toEqual({
			verdict: 'Review recommended',
			blocked: false,
			requiresCadenceResolution: false,
			tone: 'ready',
			title: 'Texture and grain',
			detail: 'Compare the selected moments before deciding.',
			topRisk: 'Texture and grain',
			topRiskLevel: 'Worth checking',
			topRiskDetail:
				'Check natural texture for waxiness, crawling noise, or an overly smooth look.',
			authority: 'Not decided yet',
			authorityDetail: 'No decision has been saved for this test.',
			focusMoments: ['Moment 2 needs the closest look.'],
			picture: {
				label: 'Texture and grain',
				level: 'Worth checking',
				detail: 'Check natural texture for waxiness, crawling noise, or an overly smooth look.'
			},
			sound: {
				label: 'No specific sound warning',
				level: 'No specific warning',
				detail: 'Listen for clear dialogue, balanced sound, and anything distracting.'
			}
		});
	});

	it('identifies a blocked cadence decision that needs Activity resolution', () => {
		const summary = compareRiskSummary(
			folder({
				quality_risk: {
					verdict: 'blocked',
					blocked: true,
					cadence_gate: 'unknown',
					cadence_transform: null,
					blocking_reasons: ['Measured cadence needs explicit review.'],
					typed_risks: [
						{
							tag: 'cadence_interlace_artifacts',
							label: 'Cadence / interlace artifacts',
							level: 'medium',
							rationale: 'Measured cadence needs explicit review.'
						}
					]
				}
			})
		);

		expect(summary?.requiresCadenceResolution).toBe(true);
	});

	it('keeps unrelated quality blockers in the normal revision workflow', () => {
		const summary = compareRiskSummary(
			folder({
				quality_risk: {
					verdict: 'blocked',
					blocked: true,
					cadence_gate: 'progressive',
					cadence_transform: 'none',
					blocking_reasons: ['The target-size search is infeasible.']
				}
			})
		);

		expect(summary?.requiresCadenceResolution).toBe(false);
	});

	it('routes stored cadence blockers without structured gate fields to Activity', () => {
		const summary = compareRiskSummary(
			folder({
				quality_risk: {
					verdict: 'blocked',
					blocked: true,
					blocking_reasons: [
						'Cadence evidence is missing or inconclusive; refresh cadence analysis before encoding.'
					]
				}
			})
		);

		expect(summary?.requiresCadenceResolution).toBe(true);
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
					budget_bytes: 225_000_000,
					predicted_total_size_bytes: 1_089_842_509,
					lower_bound_bytes: 202_500_000,
					upper_bound_bytes: 247_500_000,
					predicted_to_budget_ratio: 4.619
				}
			})
		);

		expect(analysis).toMatchObject({
			status: 'over_target',
			budgetBytes: 225_000_000,
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
			budgetBytes: 225_000_000,
			predictedBytes: 1_089_842_509,
			lowerBoundBytes: 202_500_000,
			upperBoundBytes: 247_500_000,
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
			budgetBytes: 225_000_000,
			predictedBytes: 150_000_000,
			lowerBoundBytes: 202_500_000,
			upperBoundBytes: 247_500_000,
			predictedToBudgetRatio: 0.667
		});

		expect(request).toContain('keep the 225 MB per episode goal');
		expect(request).toContain('spend more of the available size');
	});

	it('repeats the preserved target when the episode estimate is missing', () => {
		const request = measuredFollowupRequest({
			status: 'missing_prediction',
			budgetBytes: 225_000_000,
			predictedBytes: 0,
			lowerBoundBytes: 202_500_000,
			upperBoundBytes: 247_500_000,
			predictedToBudgetRatio: 0
		});

		expect(request).toContain('225 MB per episode');
		expect(request).toContain('previous run did not produce a usable full-episode size estimate');
	});

	it('keeps an inside-target goal fixed when the operator requests a revision', () => {
		const request = measuredFollowupRequest({
			status: 'inside_target_band',
			budgetBytes: 587_000_000,
			predictedBytes: 590_000_000,
			lowerBoundBytes: 528_300_000,
			upperBoundBytes: 645_700_000,
			predictedToBudgetRatio: 1.005
		});

		expect(request).toContain('Measured revision');
		expect(request).toContain('keep the 587 MB per episode goal');
		expect(request).toContain('without changing the size target');
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

	it('returns an approved sample to review when current evidence is rejected', () => {
		expect(
			detailSeasonState(
				folder({
					calibration: {
						job_id: 'sample-1',
						draft_hash: 'draft-1',
						accepted_draft_hash: 'draft-1',
						review_media_ready: true
					},
					review_gate: { status: 'accepted', can_confirm_full: true },
					quality_risk: {
						operator_decision: { status: 'rejected' }
					}
				}),
				status
			)
		).toMatchObject({ key: 'ready_to_compare', label: 'Test ready' });
	});

	it('trusts an accepted review gate when non-policy draft metadata changed', () => {
		expect(
			detailSeasonState(
				folder({
					calibration: {
						job_id: 'sample-1',
						draft_hash: 'draft-with-refreshed-metadata',
						accepted_draft_hash: 'original-draft',
						review_media_ready: true
					},
					review_gate: { status: 'accepted', can_confirm_full: true },
					quality_risk: {
						blocked: false,
						operator_decision: { status: 'approved' }
					}
				}),
				status
			)
		).toMatchObject({ key: 'ready_to_make', label: 'Test approved' });
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

	it('uses the API-resolved runtime-normalized goal instead of rebuilding it from flat policy', () => {
		const options = [
			sizeOption('recommended', 'normalized', 300, 586.667, 45),
			sizeOption('smaller', 'normalized', 225, 440, 45),
			sizeOption('roomier', 'normalized', 450, 880, 45)
		];

		const goals = sizeGoals(folder({ size_goal_options: options }));

		expect(goals.map((goal) => goal.megabytesPerEpisode)).toEqual([587, 440, 880]);
		expect(goals[0].mode).toBe('normalized');
		expect(goalRequest(goals[0])).toContain('300 MB / 45 minute runtime-normalized goal');
	});

	it('publishes explicit sample and final bands for the active whole-episode target', () => {
		const option = sizeOption('recommended', 'normalized', 300, 586.667, 45);
		const target = resolvedTargetSummary(
			folder({
				resolved_operator_intent: {
					schema_version: 1,
					requires_confirmation: false,
					size_goal: {
						...option.resolved_size_goal,
						item_runtime_seconds: 88 * 60,
						sample_lower_bound_bytes: 528_000_300,
						sample_upper_bound_bytes: 645_333_700,
						final_lower_bound_bytes: 557_333_650,
						final_upper_bound_bytes: 616_000_350
					},
					resolution: { mode: 'source' },
					request: option.operator_intent
				}
			})
		);

		expect(target).toMatchObject({
			targetBytes: 586_667_000,
			sampleLowerBoundBytes: 528_000_300,
			sampleUpperBoundBytes: 645_333_700,
			finalLowerBoundBytes: 557_333_650,
			finalUpperBoundBytes: 616_000_350,
			itemRuntimeSeconds: 5280,
			mode: 'normalized'
		});
	});

	it('distinguishes arithmetic infeasibility from a quality-floor conflict', () => {
		expect(
			targetConstraintSummary(
				folder({
					stream_budget_ledger: {
						schema_version: 1,
						ledger_id: 'ledger-1',
						source: {
							source_id: 'source-1',
							source_fingerprint: null,
							source_size_bytes: 1_000_000_000,
							source_video_bitrate_bps: 5_000_000,
							duration_seconds: 5280
						},
						policy_hash: 'policy-1',
						size_goal: sizeOption('recommended', 'absolute', 1, 1).resolved_size_goal,
						stream_plan: {
							schema_version: 1,
							plan_id: 'plan-1',
							source_id: 'source-1',
							source_fingerprint: null,
							policy_hash: 'policy-1',
							output_container: 'mkv',
							attachments_known: true,
							copy_unknown_attachments: false,
							streams: []
						},
						entries: [],
						totals: {
							total_target_bytes: 1_000_000,
							audio_bytes: 2_000_000,
							subtitle_bytes: 0,
							attachment_bytes: 0,
							container_bytes: 0,
							non_video_bytes: 2_000_000,
							minimum_non_video_bytes: 2_000_000,
							maximum_non_video_bytes: 2_000_000,
							remaining_video_bytes: 0,
							remaining_video_bitrate_bps: 0
						},
						source_relative_cap: {
							configured_total_percent: null,
							total_cap_bytes: null,
							video_cap_bytes: null,
							video_cap_bitrate_bps: null,
							video_cap_percent: null,
							status: 'arithmetically_infeasible'
						},
						feasibility: {
							status: 'arithmetically_infeasible',
							reasons: ['required streams exceed target'],
							arithmetic_infeasible: true,
							aggressive: false,
							requires_measurement: false
						},
						uncertainty: {
							confidence: 'exact',
							requires_measurement: false,
							minimum_non_video_bytes: 2_000_000,
							maximum_non_video_bytes: 2_000_000
						}
					}
				})
			)
		).toMatchObject({ kind: 'arithmetic_infeasible', recoveryLabel: 'Choose a size that can fit' });

		expect(
			targetConstraintSummary(
				folder({
					quality_risk: {
						target_size_search: {
							status: 'quality_conflict',
							selected_metric: 'vmaf',
							minimum_metric_score: 93
						}
					}
				})
			)
		).toMatchObject({
			kind: 'quality_conflict',
			recoveryLabel: 'Choose a roomier goal',
			detail: expect.stringContaining('VMAF floor of 93')
		});

		expect(
			targetConstraintSummary(
				folder({
					quality_risk: {
						target_size_search: {
							status: 'bound_exhausted',
							selection_reason: 'smallest_quality_safe_candidate_over_target_band'
						}
					}
				})
			)
		).toMatchObject({
			kind: 'bound_exhausted',
			recoveryLabel: 'Review size and settings',
			detail: expect.stringContaining('Retrying that saved test would repeat the same limit')
		});
	});

	it('recognizes legacy CRF-bound failures before the generic missing-review message', () => {
		const failedFolder = folder({
			calibration: { job_id: 'legacy-bound-failure' },
			review_gate: { status: 'missing_review_media' },
			failed_target_size_search: {
				status: 'infeasible',
				selection_reason: 'smallest_quality_safe_candidate_over_target_band'
			}
		});

		expect(targetConstraintSummary(failedFolder, status)).toMatchObject({
			kind: 'bound_exhausted',
			title: 'The test reached a configured limit.'
		});
		expect(plainFailureMessage(failedFolder, status)).toContain(
			'Retrying that saved test would repeat the same limit'
		);
	});

	it('preserves typed size intent while recording structured review feedback', () => {
		const intent = sizeOption('recommended', 'normalized', 300, 586.667, 45).operator_intent;
		const feedbackIntent = reviewFeedbackIntent(
			intent,
			['motion_breakup', 'audio_quality_layout'],
			'Moment 2 loses texture and the center channel sounds thin.'
		);
		const request = reviewFeedbackRequest(
			'Keep the 587 MB goal.',
			['motion_breakup', 'audio_quality_layout'],
			'Moment 2 loses texture.',
			'Preserve the original surround layout.'
		);

		expect(feedbackIntent).toMatchObject({
			size_goal: intent.size_goal,
			quality_risk_tags: ['motion_breakup', 'audio_quality_layout'],
			evidence_authority: 'rejected_visual_result'
		});
		expect(request).toContain('Motion breaks up');
		expect(request).toContain('Sound quality or layout is wrong');
		expect(request).toContain('Preserve the original surround layout.');
		expect(testRequestWithInstructions('Keep the target.', 'Preserve grain.')).toBe(
			'Keep the target. Additional operator priorities: Preserve grain.'
		);
	});

	it('preserves an API-resolved absolute per-episode target', () => {
		const [goal] = sizeGoals(
			folder({ size_goal_options: [sizeOption('recommended', 'absolute', 225, 225)] })
		);

		expect(goal.megabytesPerEpisode).toBe(225);
		expect(goal.mode).toBe('absolute');
		expect(goalRequest(goal)).toContain('absolute 225 MB per-episode target');
	});

	it('requires a fresh explicit legacy choice after folder navigation', () => {
		const goals = sizeGoals(
			folder({
				size_goal_options: [
					sizeOption('normalized', 'normalized', 225, 440, 45, true),
					sizeOption('absolute', 'absolute', 225, 225, undefined, true)
				]
			})
		);

		expect(isSizeGoalSelectionConfirmed(goals, 'normalized', 'tv/Other', card.prefix)).toBe(false);
		expect(isSizeGoalSelectionConfirmed(goals, 'normalized', card.prefix, card.prefix)).toBe(true);
	});

	it('returns the resolved typed request for measured follow-up work', () => {
		const option = sizeOption('recommended', 'absolute', 225, 225);
		const request = option.operator_intent;

		expect(
			currentOperatorIntent(
				folder({
					resolved_operator_intent: {
						schema_version: 1,
						requires_confirmation: false,
						size_goal: option.resolved_size_goal,
						resolution: { mode: 'source' },
						request
					}
				})
			)
		).toEqual(request);
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
					sizeBytes: 120,
					audio: null
				},
				preview: {
					path: '/preview.mp4',
					timestampSeconds: 0,
					durationSeconds: 0,
					sizeBytes: 35,
					audio: null
				},
				comparePath: '/compare.mkv'
			}
		]);
	});

	it('preserves trustworthy sound metadata for review clips', () => {
		const [pair] = normalizeReviewPairs(
			folder({
				calibration: {
					review_pairs: [
						{
							source_clip: {
								path: '/source.mp4',
								audio: { trustworthy: true, role: 'original' }
							},
							preview_clip: {
								path: '/preview.mp4',
								audio: { trustworthy: true, role: 'new' }
							}
						}
					]
				}
			})
		);

		expect(pair.source.audio).toEqual({ trustworthy: true, role: 'original' });
		expect(pair.preview.audio).toEqual({ trustworthy: true, role: 'new' });
	});

	it('formats filenames and sizes for people', () => {
		expect(episodeLabel('tv/Show/Season 2/Show.S02E07.mkv')).toBe('Episode 7');
		expect(formatFileSize(1_073_741_824)).toBe('1.0 GB');
	});

	it('formats operator-facing target totals with decimal units', () => {
		expect(formatDecimalFileSize(586_667_000 * 39)).toBe('22.9 GB');
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

	it('keeps related show activity separate from a season target', () => {
		const activity = overlappingCalibrationActivity(
			{
				...status,
				scope_activity_status: 'running',
				scope_activity: {
					schema_version: 1,
					relation: 'ancestor',
					job: {
						job_id: 'show-test',
						prefix: 'tv/Big Brother (US)',
						status: 'running',
						target_contract: {
							schema_version: 1,
							target_size_bytes: 225_000_000,
							mode: 'absolute',
							source: 'operator',
							resolution_mode: 'source'
						}
					}
				}
			},
			card.prefix
		);

		expect(activity?.relation).toBe('ancestor');
		expect(calibrationJobTargetContract(activity?.job)?.target_size_bytes).toBe(225_000_000);
	});

	it('describes actual calibration stages, bounded work, and heartbeat state', () => {
		const job = {
			job_id: 'test-1',
			prefix: card.prefix,
			status: 'running',
			progress: {
				schema_version: 1 as const,
				stage: 'building_review',
				liveness: 'reporting' as const,
				work: { completed: 2, total: 3 }
			}
		};

		expect(calibrationStageLabel(job)).toBe('Building comparison clips');
		expect(calibrationWorkLabel(job)).toBe('2 of 3 review steps');
		expect(calibrationLivenessLabel(job)).toBe('Computer is reporting normally');
	});

	it('renders historical ETA ranges without false precision', () => {
		const summary = calibrationEtaSummary({
			job_id: 'test-2',
			prefix: card.prefix,
			status: 'running',
			progress: {
				schema_version: 1,
				liveness: 'reporting',
				estimate: {
					kind: 'historical_range',
					sample_size: 5,
					remaining_seconds_low: 480,
					remaining_seconds_high: 1020,
					total_seconds_low: 1200,
					total_seconds_high: 1800,
					longer_than_recent_runs: false,
					confidence: 'moderate',
					basis: 'Comparable completed tests'
				}
			}
		});

		expect(summary.value).toBe('8 min–20 min left');
		expect(summary.detail).toContain('5 comparable completed tests');
		expect(summary.tone).toBe('quiet');
	});

	it('calls out missing heartbeats instead of inventing an ETA', () => {
		expect(
			calibrationEtaSummary({
				job_id: 'test-3',
				prefix: card.prefix,
				status: 'running',
				progress: { schema_version: 1, liveness: 'not_reporting' }
			})
		).toMatchObject({ value: 'Progress signal lost', tone: 'attention' });
	});

	it('uses terminal copy instead of stale heartbeat language', () => {
		const completed = {
			job_id: 'test-4',
			prefix: card.prefix,
			status: 'completed',
			progress: { schema_version: 1 as const, liveness: 'not_reporting' as const }
		};

		expect(calibrationLivenessLabel(completed)).toBe('Test finished');
		expect(calibrationEtaSummary(completed)).toMatchObject({ value: 'Finished', tone: 'quiet' });
	});
});
