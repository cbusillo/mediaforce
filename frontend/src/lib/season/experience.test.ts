import { describe, expect, it } from 'vitest';

import type {
	DashboardScanJob,
	DashboardScanWarning,
	DashboardSummaryPayload,
	EncodeQueueJob,
	FolderCard,
	FolderQualityMemoryPayload,
	FolderPayload,
	FolderStatusPayload,
	FolderWorkflowState,
	CompressionIntentOptionPayload,
	OperatorIntentRequestPayload,
	SizeGoalOptionPayload,
	WorkflowLane
} from '$lib/api/types';
import {
	activeSeasonCards,
	approvalGuardFromMessage,
	calibrationActivityStatusLabel,
	calibrationEtaSummary,
	calibrationAcceptsUnderTargetResult,
	calibrationFreshnessLabel,
	calibrationJobTargetContract,
	calibrationLivenessLabel,
	calibrationStageLabel,
	calibrationWorkLabel,
	calibrationWorkProgress,
	catalogWarningNotice,
	compareRiskSummary,
	currentOperatorIntent,
	detailSeasonState,
	episodeLabel,
	exactItemFilename,
	exactReviewSizeFacts,
	expectedSizeChange,
	folderSizeTargetAnalysis,
	formatDecimalFileSize,
	formatFileSize,
	goalRequest,
	isSeriesPrefix,
	isSizeGoalSelectionConfirmed,
	measuredFollowupRequest,
	librarySeasonState,
	normalizedSizePaceBytes,
	overlappingCalibrationActivity,
	plainFailureMessage,
	qualityMemoryView,
	resolvedTargetSummary,
	reviewFeedbackIntent,
	reviewFeedbackRequest,
	reviewAdjustmentIntent,
	reviewSizeAdjustment,
	scopedEncodeProgress,
	seasonIdentity,
	seasonEpisodeNavigationUnavailable,
	seasonPromotionIntegrity,
	seasonEpisodeOptions,
	sampleSearchTechnicalDetail,
	stagedEpisodeLinks,
	shouldPrioritizeScopeActivity,
	sizeGoals,
	targetConstraintSummary,
	targetProvenanceSummary,
	testRequestWithInstructions,
	technicalVideoPolicy,
	withCompressionIntent
} from './experience';
import { normalizeReviewPairs, reviewSampleSizes } from '$lib/review/pairs';

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

function promotionStatus({
	blockerCount = 0,
	databaseTruncated = false,
	discoveryTruncated = false
}: {
	blockerCount?: number;
	databaseTruncated?: boolean;
	discoveryTruncated?: boolean;
} = {}): FolderStatusPayload {
	return {
		...status,
		staged_integrity: {
			scope: status.media_scope,
			counts:
				blockerCount > 0
					? { promotable: 6, tracked: 1, not_started: blockerCount }
					: { promotable: 7, tracked: 1 },
			blocker_count: blockerCount,
			blockers:
				blockerCount > 0
					? [
							{
								code: 'staged_integrity_not_started',
								count: blockerCount,
								next_action: 'queue_encode'
							}
						]
					: [],
			database_truncated: databaseTruncated,
			discovery: {
				requested: true,
				truncated: discoveryTruncated,
				entries_scanned: 8
			},
			offset: 0,
			limit: 8,
			records: [],
			next_offset: null,
			promotion_readiness: {
				applicable: true,
				can_promote: blockerCount === 0 && !databaseTruncated && !discoveryTruncated,
				blockers: []
			}
		}
	};
}

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

function browserReadyReviewPairs(): Array<Record<string, unknown>> {
	return [
		{
			source_clip: { path: '/review/source.mp4', duration_seconds: 8, size_bytes: 100 },
			preview_clip: { path: '/review/preview.mp4', duration_seconds: 8, size_bytes: 25 }
		}
	];
}

function qualityMemoryPayload(): FolderQualityMemoryPayload {
	return {
		schema_version: 1,
		algorithm_version: 'qsh2',
		observation_id: 'qso1_fixture',
		source_rel_path: 'tv/Big Brother (US)/Season 19/Episode 03.mkv',
		recorded_at: '2026-07-24T17:00:00+00:00',
		evidence_cutoff_at: '2026-07-24T16:00:00+00:00',
		measured: {
			selected_crf: 51,
			quality_metric: 'VMAF',
			quality_score: 86.5,
			quality_target: 85,
			quality_floor: 84,
			quality_margin: 2.5,
			output_bytes: 510_000_000,
			size_error_percent: 2,
			candidate_count: 5,
			search_duration_seconds: 100
		},
		recommendation: {
			first_crf: 50,
			scope: 'season',
			confidence: 'high',
			sample_count: 12,
			evidence_count: 12,
			minimum_crf: 48,
			maximum_crf: 52,
			iqr: 2,
			median_absolute_deviation: 1
		},
		comparison: { crf_delta: 1, within_one_crf: true },
		fallback_reason: null,
		reason: 'Season memory stayed within one CRF of the production result.',
		production_search_changed: false,
		warm_start: null
	};
}

describe('quality memory explanation', () => {
	it('keeps the empty state compact and explicit about unchanged policy', () => {
		const view = qualityMemoryView(folder({ quality_memory: null }));

		expect(view).toMatchObject({
			state: 'empty',
			tone: 'quiet',
			badge: 'No memory yet',
			measured: []
		});
		expect(view.reason).toContain('No completed quality search');
		expect(view.policyCopy).toContain('Quality floors and saved policy remain unchanged');
	});

	it('separates a measured production run from a high-confidence recommendation', () => {
		const view = qualityMemoryView(folder({ quality_memory: qualityMemoryPayload() }));

		expect(view).toMatchObject({
			state: 'high-confidence',
			tone: 'success',
			badge: 'High confidence',
			source: 'Episode 03',
			recommendation: {
				label: 'Shadow first CRF',
				value: '50.0',
				detail: '12 observations · season · high confidence'
			},
			comparison: 'Within 1 CRF · Δ +1.0',
			dispersion: 'CRF 48.0–52.0 · IQR 2.0 · MAD 1.0'
		});
		expect(view.measured).toEqual([
			{ label: 'Chosen CRF', value: '51.0', detail: '5 candidates · 1m 40s' },
			{
				label: 'Measured VMAF',
				value: '86.5',
				detail: 'target 85.0 · floor 84.0 · margin +2.5'
			},
			{ label: 'Final size', value: '510 MB', detail: '2% over saved size target' }
		]);
	});

	it('flags a high-confidence recommendation that differed from production', () => {
		const payload = qualityMemoryPayload();
		payload.comparison = { crf_delta: 3, within_one_crf: false };

		const view = qualityMemoryView(folder({ quality_memory: payload }));

		expect(view.tone).toBe('attention');
		expect(view.badge).toBe('High confidence · differed');
		expect(view.comparison).toBe('Outside 1 CRF · Δ +3.0');
	});

	it('explains an accepted warm start without claiming observation-only behavior', () => {
		const payload = qualityMemoryPayload();
		payload.production_search_changed = true;
		payload.measured.candidate_count = 1;
		payload.measured.search_duration_seconds = 20;
		payload.warm_start = {
			eligible: true,
			block_reason: null,
			requested_crf: 50,
			candidate_crf: 50,
			adjusted: false,
			status: 'accepted',
			attempted: true,
			fallback_used: false,
			fallback_reason: null,
			candidate_count: 1,
			baseline_candidate_count: 0,
			total_candidate_count: 1,
			duration_seconds: 20,
			baseline_median_candidate_count: 5,
			baseline_median_search_seconds: 100,
			estimated_candidate_savings_count: 4,
			estimated_candidate_savings_rate: 0.8,
			estimated_search_time_savings_seconds: 80,
			estimated_search_time_savings_rate: 0.8
		};

		const view = qualityMemoryView(folder({ quality_memory: payload }));

		expect(view.badge).toBe('Warm start accepted');
		expect(view.title).toBe('Measured run used trusted memory');
		expect(view.recommendation.label).toBe('Tried first CRF');
		expect(view.comparison).toBe('Accepted first candidate · estimated 80% fewer candidate passes');
		expect(view.policyCopy).toContain('full baseline search was not needed');
		expect(view.policyCopy).not.toContain('observation-only');
	});

	it('shows the rounded or clamped CRF that was actually tried', () => {
		const payload = qualityMemoryPayload();
		payload.production_search_changed = true;
		payload.warm_start = {
			eligible: true,
			block_reason: null,
			requested_crf: 50,
			candidate_crf: 49,
			adjusted: true,
			status: 'accepted',
			attempted: true,
			fallback_used: false,
			fallback_reason: null,
			candidate_count: 1,
			baseline_candidate_count: 0,
			total_candidate_count: 1,
			duration_seconds: 20,
			baseline_median_candidate_count: 5,
			baseline_median_search_seconds: 100,
			estimated_candidate_savings_count: 4,
			estimated_candidate_savings_rate: 0.8,
			estimated_search_time_savings_seconds: 80,
			estimated_search_time_savings_rate: 0.8
		};

		const view = qualityMemoryView(folder({ quality_memory: payload }));

		expect(view.recommendation.value).toBe('49.0');
		expect(view.recommendation.detail).toContain('adjusted from 50.0');
	});

	it('keeps attempted CRF facts visible when baseline relaxation invalidates the passive recommendation', () => {
		const payload = qualityMemoryPayload();
		payload.production_search_changed = true;
		payload.recommendation = null;
		payload.fallback_reason = 'search_target_changed';
		payload.warm_start = {
			eligible: true,
			block_reason: null,
			requested_crf: 50,
			candidate_crf: 49,
			adjusted: true,
			status: 'rejected_fallback',
			attempted: true,
			fallback_used: true,
			fallback_reason: 'quality_target_miss',
			candidate_count: 1,
			baseline_candidate_count: 3,
			total_candidate_count: 4,
			duration_seconds: 20,
			baseline_median_candidate_count: 5,
			baseline_median_search_seconds: 100,
			estimated_candidate_savings_count: 1,
			estimated_candidate_savings_rate: 0.2,
			estimated_search_time_savings_seconds: 20,
			estimated_search_time_savings_rate: 0.2
		};

		const view = qualityMemoryView(folder({ quality_memory: payload }));

		expect(view.title).toBe('Memory tried first; baseline selected');
		expect(view.recommendation).toMatchObject({
			label: 'Tried first CRF',
			value: '49.0'
		});
		expect(view.recommendation.detail).toContain('Measured memory candidate');
		expect(view.recommendation.detail).toContain('adjusted from 50.0');
		expect(view.reason).toContain('missed the strict quality target');
	});

	it('explains a guard miss and unchanged full baseline fallback', () => {
		const payload = qualityMemoryPayload();
		payload.production_search_changed = true;
		payload.measured.candidate_count = 6;
		payload.warm_start = {
			eligible: true,
			block_reason: null,
			requested_crf: 50,
			candidate_crf: 50,
			adjusted: false,
			status: 'rejected_fallback',
			attempted: true,
			fallback_used: true,
			fallback_reason: 'target_band_miss',
			candidate_count: 1,
			baseline_candidate_count: 5,
			total_candidate_count: 6,
			duration_seconds: 20,
			baseline_median_candidate_count: 5,
			baseline_median_search_seconds: 100,
			estimated_candidate_savings_count: -1,
			estimated_candidate_savings_rate: -0.2,
			estimated_search_time_savings_seconds: -20,
			estimated_search_time_savings_rate: -0.2
		};

		const view = qualityMemoryView(folder({ quality_memory: payload }));

		expect(view.badge).toBe('Warm start fell back');
		expect(view.title).toBe('Memory tried first; baseline selected');
		expect(view.comparison).toBe('Full baseline fallback · 6 candidates total');
		expect(view.reason).toContain('missed the saved size band');
		expect(view.policyCopy).toContain('full baseline search ran normally');
	});

	it.each([
		['sparse_cohort', 'sparse', 'Sparse memory'],
		['final_retry_terminal', 'sparse', 'Sparse memory'],
		['stale_signature', 'stale', 'Memory invalidated'],
		['shadow_evaluation_error', 'unavailable', 'Memory unavailable'],
		['conflicting_quality_evidence', 'conflicting', 'Evidence conflict']
	] as const)('maps %s into a compact %s state', (fallbackReason, state, badge) => {
		const payload = qualityMemoryPayload();
		payload.recommendation = null;
		payload.comparison = { crf_delta: null, within_one_crf: null };
		payload.fallback_reason = fallbackReason;

		const view = qualityMemoryView(folder({ quality_memory: payload }));

		expect(view.state).toBe(state);
		expect(view.badge).toBe(badge);
		expect(view.recommendation.value).toBe('Held back');
		expect(view.reason.length).toBeGreaterThan(20);
		expect(view.policyCopy).toContain('saved policy remain unchanged');
	});
});

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
			schema_version: 2,
			size_goal: {
				mode,
				value_mb: valueMegabytes,
				...(referenceRuntimeMinutes ? { reference_runtime_minutes: referenceRuntimeMinutes } : {}),
				sample_projection_tolerance_percent: 10,
				final_output_tolerance_percent: 5
			},
			resolution: { mode: 'source', max_height: null },
			compression_intent: {
				schema_version: 1,
				level: 'balanced',
				confirmed: true
			}
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

function compressionIntentOptions(): CompressionIntentOptionPayload[] {
	return (['reference', 'transparent', 'balanced', 'perceptual_floor'] as const).map((level) => ({
		key: level,
		title: level,
		detail: level,
		selected: level === 'balanced',
		accepts_under_target_result: level === 'transparent' || level === 'perceptual_floor',
		compression_intent: {
			schema_version: 1,
			level,
			confirmed: true
		}
	}));
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

	it('keeps show and season identity for an exact episode path', () => {
		expect(
			seasonIdentity('tv/The Wonder Years/Season 2/The.Wonder.Years.S02E01.DVDRip.x264-DEiMOS.mkv')
		).toEqual({
			library: 'tv',
			show: 'The Wonder Years',
			season: 'Season 2',
			showPrefix: 'tv/The Wonder Years'
		});
	});

	it('does not mistake a show name beginning with Season for the season folder', () => {
		expect(seasonIdentity('tv/Season of the Witch/Season 1/S01E01.mkv')).toEqual({
			library: 'tv',
			show: 'Season of the Witch',
			season: 'Season 1',
			showPrefix: 'tv/Season of the Witch'
		});
	});

	it('uses the active encode job scope for progress counts', () => {
		const activeJob: EncodeQueueJob = {
			job_id: 'folder-encode',
			prefix: 'tv/Big Brother (US)',
			status: 'queued',
			progress: {
				total_item_count: 269,
				completed_item_count: 0,
				percent_complete: 0
			}
		};

		expect(scopedEncodeProgress(activeJob, 90, 359)).toMatchObject({
			total: 269,
			completed: 0,
			percent: 0
		});
		expect(scopedEncodeProgress(null, 90, 359)).toMatchObject({
			total: 359,
			completed: 90
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
			authorityDetail: 'No decision has been saved for this sample.',
			hasSavedDecision: false,
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

	it('keeps exact-item review facts separate from comparison clip measurements', () => {
		expect(exactReviewSizeFacts(1_200_000_000, 480_000_000)).toEqual({
			currentSizeBytes: 1_200_000_000,
			estimatedOutputBytes: 480_000_000,
			estimatedSpaceSavedBytes: 720_000_000
		});
		expect(exactReviewSizeFacts(480_000_000, 600_000_000).estimatedSpaceSavedBytes).toBe(0);
		expect(exactReviewSizeFacts(480_000_000, null)).toEqual({
			currentSizeBytes: 480_000_000,
			estimatedOutputBytes: null,
			estimatedSpaceSavedBytes: null
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

	it('formats concise exact-item target provenance for the operator surface', () => {
		expect(
			targetProvenanceSummary({
				schema_version: 1,
				source: 'ancestor_override',
				override_prefix: 'tv/Big Brother (US)',
				requested_target_bytes: 225_000_000,
				status: 'resolved',
				size_goal_mode: 'absolute',
				policy_hash: 'policy-hash'
			})
		).toBe('Inherited folder override (tv/Big Brother (US))');
		expect(targetProvenanceSummary(null)).toBeNull();
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

	it('does not spend unused size automatically at the perceptual floor', () => {
		const request = measuredFollowupRequest(
			{
				status: 'under_target',
				budgetBytes: 225_000_000,
				predictedBytes: 150_000_000,
				lowerBoundBytes: 202_500_000,
				upperBoundBytes: 247_500_000,
				predictedToBudgetRatio: 0.667
			},
			true
		);

		expect(request).toContain('acceptable for this compression goal');
		expect(request).toContain('do not spend unused size automatically');
		expect(request).not.toContain('spend more of the available size');
	});

	it('treats transparent as an acceptable under-target result', () => {
		const request = measuredFollowupRequest(
			{
				status: 'under_target',
				budgetBytes: 225_000_000,
				predictedBytes: 150_000_000,
				lowerBoundBytes: 202_500_000,
				upperBoundBytes: 247_500_000,
				predictedToBudgetRatio: 0.667
			},
			true
		);

		expect(request).toContain('do not spend unused size automatically');
	});

	it('keeps a typed unaccepted under-target result actionable', () => {
		const request = measuredFollowupRequest(
			{
				status: 'under_target',
				budgetBytes: 225_000_000,
				predictedBytes: 150_000_000,
				lowerBoundBytes: 202_500_000,
				upperBoundBytes: 247_500_000,
				predictedToBudgetRatio: 0.667
			},
			false
		);

		expect(request).toContain('spend more of the available size');
	});

	it('attaches a confirmed compression goal to a size request', () => {
		const request = withCompressionIntent(
			sizeOption('recommended', 'absolute', 225, 225).operator_intent,
			{
				schema_version: 1,
				level: 'transparent',
				confirmed: false
			}
		);

		expect(request.schema_version).toBe(2);
		expect(request.compression_intent).toEqual({
			schema_version: 1,
			level: 'transparent',
			confirmed: true
		});
	});

	it('changes review size and compression while preserving the reviewed resolution', () => {
		const currentIntent = sizeOption('recommended', 'absolute', 225, 225).operator_intent;
		currentIntent.resolution = {
			mode: 'max_height',
			max_height: 1080
		};
		const goals = sizeGoals(
			folder({
				size_goal_options: [
					sizeOption('recommended', 'absolute', 225, 225),
					sizeOption('smaller', 'absolute', 150, 150)
				]
			})
		);
		const adjustment = reviewSizeAdjustment(goals, compressionIntentOptions(), 'smaller');

		const request = reviewAdjustmentIntent(currentIntent, adjustment!);

		expect(request.size_goal.value_mb).toBe(150);
		expect(request.resolution).toEqual(currentIntent.resolution);
		expect(request.resolution).not.toBe(currentIntent.resolution);
		expect(request.compression_intent).toMatchObject({
			level: 'perceptual_floor',
			confirmed: true
		});
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
			label: 'Needs sample'
		});
	});

	it('distinguishes a waiting sample from a running sample', () => {
		const queuedDashboard = {
			...dashboard,
			calibration_queue: {
				...dashboard.calibration_queue,
				sample: {
					...dashboard.calibration_queue.sample,
					queued: [{ job_id: 'sample-queued', prefix: card.prefix, status: 'queued' }],
					queued_count: 1
				}
			}
		} as DashboardSummaryPayload;
		const runningDashboard = {
			...dashboard,
			calibration_queue: {
				...dashboard.calibration_queue,
				sample: {
					...dashboard.calibration_queue.sample,
					running: [{ job_id: 'sample-running', prefix: card.prefix, status: 'running' }],
					running_count: 1
				}
			}
		} as DashboardSummaryPayload;
		const retryingDashboard = {
			...dashboard,
			calibration_queue: {
				...dashboard.calibration_queue,
				sample: {
					...dashboard.calibration_queue.sample,
					running: [{ job_id: 'sample-retrying', prefix: card.prefix, status: 'retry_backoff' }],
					running_count: 1
				}
			}
		} as DashboardSummaryPayload;

		expect(librarySeasonState(card, queuedDashboard)).toMatchObject({
			key: 'sample_waiting',
			label: 'Sample waiting',
			detail: 'This sample has not started. It is waiting for an available computer.'
		});
		expect(librarySeasonState(card, runningDashboard)).toMatchObject({
			key: 'making_test',
			label: 'Creating sample',
			detail: 'One representative episode is being compressed into comparison clips now.'
		});
		expect(librarySeasonState(card, retryingDashboard)).toMatchObject({
			key: 'sample_waiting',
			label: 'Sample waiting',
			detail: 'The last attempt stopped. Mediaforce will retry this sample shortly.',
			tone: 'attention'
		});
	});

	it('keeps technical job nouns out of first-level sample copy', () => {
		const states = [
			librarySeasonState(card, dashboard),
			detailSeasonState(folder(), status),
			{
				label: calibrationStageLabel({ job_id: 'sample-1', status: 'running' }),
				detail: calibrationLivenessLabel({ job_id: 'sample-1', status: 'running' })
			}
		];
		const firstLevelCopy = states.map((state) => `${state.label} ${state.detail}`).join(' ');

		expect(firstLevelCopy).not.toMatch(/\b(calibration|candidate|worker|host|Mac|preview)\b/i);
	});

	it('keeps finishing states visible on the home screen', () => {
		const finishingCard = { ...card, workflow_state: workflowState('promote') };
		expect(librarySeasonState(finishingCard, dashboard)).toMatchObject({
			key: 'ready_to_finish',
			label: 'Ready to replace'
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
						browser_review_ready: true,
						review_pairs: browserReadyReviewPairs()
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
		).toMatchObject({ key: 'ready_to_compare', label: 'Ready to review' });
	});

	it('shows an interrupted season before an older approved test', () => {
		expect(
			detailSeasonState(
				folder({
					calibration: {
						draft_hash: 'draft-1',
						accepted_draft_hash: 'draft-1',
						browser_review_ready: true,
						review_pairs: browserReadyReviewPairs()
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

	it('uses episode language when exact-item work stops', () => {
		expect(
			detailSeasonState(
				folder({
					media_scope: {
						schema_version: 1,
						prefix: `${card.prefix}/1e01-Pilot.mkv`,
						root: 'tv',
						domain: 'tv',
						kind: 'media_file',
						match: 'exact_item',
						title: '1e01 Pilot',
						subtitle: 'Constellation · Season 4',
						scope_label: 'Episode'
					},
					encode_job: {
						job_id: 'job-1',
						prefix: `${card.prefix}/1e01-Pilot.mkv`,
						status: 'needs_attention',
						error: 'Final output size missed the approved target band.'
					}
				}),
				status
			)
		).toMatchObject({
			key: 'needs_help',
			label: 'The episode stopped',
			detail: 'Nothing was replaced. Try this episode again.'
		});
	});

	it('shows a newer recovery test instead of stale encode attention', () => {
		expect(
			detailSeasonState(
				folder({
					calibration: {
						job_id: 'sample-new',
						draft_hash: 'draft-new',
						accepted_draft_hash: null,
						browser_review_ready: true,
						review_pairs: browserReadyReviewPairs()
					},
					calibration_job: {
						job_id: 'sample-new',
						prefix: card.prefix,
						status: 'completed',
						finished_at: '2026-08-16T05:42:00+00:00'
					},
					review_gate: { status: 'needs_approval', can_confirm_full: false },
					encode_job: {
						job_id: 'encode-old',
						prefix: card.prefix,
						status: 'needs_attention',
						finished_at: '2026-08-16T05:21:35+00:00',
						error: 'Final output size missed the approved target band.'
					}
				}),
				status
			)
		).toMatchObject({ key: 'ready_to_compare', label: 'Ready to review' });
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
		).toMatchObject({ key: 'needs_help', label: 'Sample needs retry' });
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
		).toMatchObject({ key: 'needs_help', label: 'Sample needs retry' });
	});

	it('returns an approved sample to review when current evidence is rejected', () => {
		expect(
			detailSeasonState(
				folder({
					calibration: {
						job_id: 'sample-1',
						draft_hash: 'draft-1',
						accepted_draft_hash: 'draft-1',
						browser_review_ready: true,
						review_pairs: browserReadyReviewPairs()
					},
					review_gate: { status: 'accepted', can_confirm_full: true },
					quality_risk: {
						operator_decision: { status: 'rejected' }
					}
				}),
				status
			)
		).toMatchObject({ key: 'ready_to_compare', label: 'Ready to review' });
	});

	it('trusts an accepted review gate when non-policy draft metadata changed', () => {
		expect(
			detailSeasonState(
				folder({
					calibration: {
						job_id: 'sample-1',
						draft_hash: 'draft-with-refreshed-metadata',
						accepted_draft_hash: 'original-draft',
						browser_review_ready: true,
						review_pairs: browserReadyReviewPairs()
					},
					review_gate: { status: 'accepted', can_confirm_full: true },
					quality_risk: {
						blocked: false,
						operator_decision: { status: 'approved' }
					}
				}),
				status
			)
		).toMatchObject({ key: 'ready_to_make', label: 'Sample approved' });
	});

	it('keeps an approved sample ready when only legacy review media remains', () => {
		expect(
			detailSeasonState(
				folder({
					calibration: {
						job_id: 'sample-1',
						draft_hash: 'draft-1',
						accepted_draft_hash: 'draft-1',
						review_media_ready: true,
						compare_clips: [{ path: '/review/compare.mov' }]
					},
					review_gate: { status: 'accepted', can_confirm_full: true }
				}),
				status
			)
		).toMatchObject({ key: 'ready_to_make', label: 'Sample approved' });
	});

	it('requires a fresh sample when legacy approval no longer matches review media', () => {
		expect(
			detailSeasonState(
				folder({
					calibration: {
						job_id: 'sample-1',
						draft_hash: 'draft-1',
						accepted_draft_hash: 'draft-1',
						review_media_ready: false
					},
					review_gate: { status: 'missing_review_media', can_confirm_full: false }
				}),
				status
			)
		).toMatchObject({ key: 'needs_help', label: 'Sample needs retry' });
	});

	it.each([
		['validate', 'ready_to_check'],
		['promote', 'finish_blocked'],
		['complete', 'finished']
	] as const)('translates the %s delivery step', (lane, expectedKey) => {
		expect(
			detailSeasonState(folder({ workflow_state: workflowState(lane) }), status)
		).toMatchObject({ key: expectedKey });
	});

	it('keeps season finishing blocked until the complete integrity inventory is loaded', () => {
		expect(
			detailSeasonState(folder({ workflow_state: workflowState('promote') }), status)
		).toMatchObject({ key: 'finish_blocked', label: 'Checking the season' });
	});

	it('explains exact season blockers and keeps finish unavailable', () => {
		const integrity = seasonPromotionIntegrity(promotionStatus({ blockerCount: 2 }));

		expect(integrity).toMatchObject({
			available: true,
			canFinish: false,
			readyCount: 6,
			alreadyPlacedCount: 1,
			unresolvedCount: 2,
			totalCount: 9
		});
		expect(integrity.blockers).toEqual([
			{
				code: 'staged_integrity_not_started',
				count: 2,
				label: 'Not compressed yet',
				nextAction: 'Compress the remaining episode before replacing the originals.'
			}
		]);
		expect(
			detailSeasonState(
				folder({ workflow_state: workflowState('promote') }),
				promotionStatus({ blockerCount: 2 })
			)
		).toMatchObject({ key: 'finish_blocked', label: 'Season not ready' });
	});

	it('enables whole-season finishing only for a complete unblocked inventory', () => {
		const integrity = seasonPromotionIntegrity(promotionStatus());

		expect(integrity).toMatchObject({
			available: true,
			canFinish: true,
			reportComplete: true,
			readyCount: 7,
			alreadyPlacedCount: 1,
			unresolvedCount: 0,
			totalCount: 8
		});
		expect(
			detailSeasonState(folder({ workflow_state: workflowState('promote') }), promotionStatus())
		).toMatchObject({ key: 'ready_to_finish', label: 'Ready to replace' });
	});

	it('enables exact-item finishing without season-wide promotion applicability', () => {
		const exactStatus = promotionStatus();
		exactStatus.staged_integrity!.scope = {
			...exactStatus.staged_integrity!.scope,
			kind: 'media_file',
			match: 'exact_item'
		};
		exactStatus.staged_integrity!.counts = { promotable: 1 };
		exactStatus.staged_integrity!.promotion_readiness = {
			applicable: false,
			can_promote: true,
			blockers: []
		};

		expect(seasonPromotionIntegrity(exactStatus)).toMatchObject({
			available: true,
			canFinish: true,
			readyCount: 1,
			unresolvedCount: 0
		});
	});

	it('blocks finishing when the integrity report is truncated', () => {
		const integrity = seasonPromotionIntegrity(promotionStatus({ databaseTruncated: true }));

		expect(integrity.canFinish).toBe(false);
		expect(integrity.blockers.at(-1)).toMatchObject({
			code: 'staged_integrity_incomplete_report',
			label: 'Inventory incomplete'
		});
	});

	it('blocks finishing when staged episodes do not share the approved settings', () => {
		const mixedPolicyStatus = promotionStatus();
		mixedPolicyStatus.staged_integrity!.promotion_readiness = {
			applicable: true,
			can_promote: false,
			blockers: [
				{
					code: 'season_policy_mixed',
					count: 8,
					next_action: 'recreate_outputs_with_one_policy'
				}
			]
		};

		const integrity = seasonPromotionIntegrity(mixedPolicyStatus);

		expect(integrity).toMatchObject({ canFinish: false, unresolvedCount: 0 });
		expect(integrity.blockers).toContainEqual({
			code: 'season_policy_mixed',
			count: 8,
			label: 'Mixed season settings',
			nextAction: 'Make the affected episodes with one coherent approved setup.'
		});
	});

	it('keeps finish blocked and exposes an integrity load failure', () => {
		const failedStatus = promotionStatus();
		failedStatus.staged_integrity = {
			...failedStatus.staged_integrity!,
			discovery: { requested: false, truncated: false, entries_scanned: 0 },
			load_error: 'Mediaforce could not load the staged-file inventory.',
			promotion_readiness: undefined
		};

		expect(seasonPromotionIntegrity(failedStatus)).toMatchObject({
			available: false,
			canFinish: false,
			error: 'Mediaforce could not load the staged-file inventory.'
		});
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
		).toMatchObject({ key: 'making_season', label: 'Compressing the season' });
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

	it('maps a smaller review judgment to the smaller goal and perceptual floor', () => {
		const goals = sizeGoals(
			folder({
				size_goal_options: [
					sizeOption('recommended', 'normalized', 300, 149, 45),
					sizeOption('smaller', 'normalized', 225, 112, 45),
					sizeOption('roomier', 'normalized', 450, 223, 45)
				]
			})
		);

		const adjustment = reviewSizeAdjustment(goals, compressionIntentOptions(), 'smaller');

		expect(adjustment).toMatchObject({
			direction: 'smaller',
			goal: { key: 'smaller', targetSizeBytes: 112_000_000 },
			compressionIntent: { level: 'perceptual_floor', confirmed: true }
		});
	});

	it('maps a better-quality judgment to the roomier goal and reference intent', () => {
		const goals = sizeGoals(
			folder({
				size_goal_options: [
					sizeOption('recommended', 'normalized', 300, 149, 45),
					sizeOption('smaller', 'normalized', 225, 112, 45),
					sizeOption('roomier', 'normalized', 450, 223, 45)
				]
			})
		);

		const adjustment = reviewSizeAdjustment(goals, compressionIntentOptions(), 'higher_quality');
		const currentIntent: OperatorIntentRequestPayload = {
			...goals[0].operatorIntent,
			quality_risk_tags: ['softness_detail_loss'],
			quality_risk_details: 'The reviewed version lost fine texture.',
			evidence_authority: 'rejected_visual_result' as const
		};
		const operatorIntent = reviewAdjustmentIntent(currentIntent, adjustment!);

		expect(adjustment).toMatchObject({
			direction: 'higher_quality',
			goal: { key: 'roomier', targetSizeBytes: 223_000_000 },
			compressionIntent: { level: 'reference', confirmed: true }
		});
		expect(operatorIntent).not.toHaveProperty('evidence_authority');
		expect(operatorIntent).not.toHaveProperty('quality_risk_tags');
		expect(operatorIntent).not.toHaveProperty('quality_risk_details');
	});

	it('does not invent a review adjustment when its goal or intent is unavailable', () => {
		const recommendedOnly = sizeGoals(
			folder({ size_goal_options: [sizeOption('recommended', 'normalized', 300, 149, 45)] })
		);

		expect(reviewSizeAdjustment(recommendedOnly, compressionIntentOptions(), 'smaller')).toBeNull();
		expect(reviewSizeAdjustment([], [], 'higher_quality')).toBeNull();
	});

	it('only offers directional review presets that move the current target correctly', () => {
		const goals = sizeGoals(
			folder({
				size_goal_options: [
					sizeOption('recommended', 'normalized', 300, 149, 45),
					sizeOption('smaller', 'normalized', 225, 112, 45),
					sizeOption('roomier', 'normalized', 450, 223, 45)
				]
			})
		);

		expect(
			reviewSizeAdjustment(goals, compressionIntentOptions(), 'smaller', 100_000_000)
		).toBeNull();
		expect(
			reviewSizeAdjustment(goals, compressionIntentOptions(), 'smaller', 149_000_000, 100_000_000)
		).toBeNull();
		expect(
			reviewSizeAdjustment(goals, compressionIntentOptions(), 'higher_quality', 250_000_000)
		).toBeNull();
		expect(
			reviewSizeAdjustment(goals, compressionIntentOptions(), 'smaller', 149_000_000)
		).toMatchObject({ goal: { key: 'smaller' } });
		expect(
			reviewSizeAdjustment(goals, compressionIntentOptions(), 'higher_quality', 149_000_000)
		).toMatchObject({ goal: { key: 'roomier' } });
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
			referenceSizeBytes: 300_000_000,
			referenceRuntimeMinutes: 45,
			mode: 'normalized'
		});
		expect(
			normalizedSizePaceBytes(target?.referenceSizeBytes, target?.referenceRuntimeMinutes)
		).toBe(200_000_000);
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
			detail: expect.stringContaining('Retrying that saved sample would repeat the same limit')
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
			title: 'The sample reached a configured limit.'
		});
		expect(plainFailureMessage(failedFolder, status)).toContain(
			'Retrying that saved sample would repeat the same limit'
		);
	});

	it('explains that a final size-band miss needs a fresh goal', () => {
		const failedFolder = folder({
			encode_job: {
				job_id: 'final-size-miss',
				prefix: card.prefix,
				status: 'needs_attention',
				error:
					'Final output size missed the approved target band: status=under_target, actual=112263931, target=153560889, lower=145882845, upper=161238933.'
			}
		});

		expect(plainFailureMessage(failedFolder, status)).toBe(
			'The finished file was smaller than the approved range. Choose a fresh size or quality preference and create another sample.'
		);
	});

	it('keeps final-size recovery primary when the fresh baseline also fails', () => {
		const failedFolder = folder({
			encode_job: {
				job_id: 'final-size-and-baseline-miss',
				prefix: card.prefix,
				status: 'needs_attention',
				error:
					'Final output size missed the approved target band: status=missing_target, actual=112263931, target=None, lower=None, upper=None. The fresh baseline search also failed: Failed to find a suitable crf for the quality target.'
			}
		});

		expect(plainFailureMessage(failedFolder, status)).toBe(
			'Mediaforce could not verify the finished file against the approved size range. Choose a fresh size or quality preference and create another sample.'
		);
	});

	it('prefers a newer encode failure over stale sample failure copy', () => {
		const failedFolder = folder({
			encode_job: {
				job_id: 'newer-final-size-miss',
				prefix: card.prefix,
				status: 'needs_attention',
				finished_at: '2026-08-16T12:00:00Z',
				error:
					'Final output size missed the approved target band: status=under_target, actual=112263931, target=153560889, lower=145882845, upper=161238933.'
			}
		});
		const staleSampleStatus: FolderStatusPayload = {
			...status,
			retryable_sample_job: {
				job_id: 'older-sample-failure',
				prefix: card.prefix,
				status: 'failed',
				finished_at: '2026-08-16T10:00:00Z',
				error: 'Failed to find a suitable crf for the quality target.'
			}
		};

		expect(plainFailureMessage(failedFolder, staleSampleStatus)).toBe(
			'The finished file was smaller than the approved range. Choose a fresh size or quality preference and create another sample.'
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

	it('returns a schema-two resolved request with compression intent', () => {
		const option = sizeOption('recommended', 'absolute', 225, 225);
		const request = withCompressionIntent(option.operator_intent, {
			schema_version: 1,
			level: 'transparent',
			confirmed: true
		});

		expect(
			currentOperatorIntent(
				folder({
					resolved_operator_intent: {
						schema_version: 2,
						requires_confirmation: false,
						size_goal: option.resolved_size_goal,
						resolution: { mode: 'source' },
						compression_intent: {
							schema_version: 1,
							level: 'transparent',
							confirmed: true,
							source: 'operator',
							requires_confirmation: false,
							accepts_under_target_result: true,
							semantic_id: 'ci1_test',
							snapshot_id: 'cis1_test',
							title: 'No visible difference',
							detail: 'Smallest transparent result'
						},
						request
					}
				})
			)
		).toEqual(request);
	});

	it('uses the frozen calibration intent for completed test interpretation', () => {
		expect(
			calibrationAcceptsUnderTargetResult(
				folder({
					calibration: {
						sample_item: {
							compression_intent: {
								schema_version: 1,
								level: 'perceptual_floor',
								confirmed: true,
								accepts_under_target_result: true
							}
						}
					}
				})
			)
		).toBe(true);
	});

	it('does not infer acceptance from an unconfirmed frozen snapshot', () => {
		expect(
			calibrationAcceptsUnderTargetResult(
				folder({
					calibration: {
						sample_item: {
							compression_intent: {
								schema_version: 1,
								level: 'perceptual_floor',
								confirmed: false,
								accepts_under_target_result: true
							}
						}
					}
				})
			)
		).toBeNull();
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
		expect(
			exactItemFilename({
				prefix: 'tv/Show/Season 2',
				media_scope: {
					prefix: 'tv/Show/Season 2/Show.S02E07.mkv'
				}
			} as FolderPayload)
		).toBe('Show.S02E07.mkv');
		expect(formatFileSize(1_073_741_824)).toBe('1.07 GB');
	});

	it('links staged season output to the exact episode workspace', () => {
		const relPath = 'tv/Bluey (2018)/Season 3/Bluey.2018.S03E49.1080p.BluRay.mkv';
		const links = stagedEpisodeLinks({
			...status,
			staged_integrity: {
				scope: status.media_scope,
				counts: { remote_only_or_unreachable: 1, not_started: 49 },
				blocker_count: 50,
				blockers: [],
				database_truncated: false,
				discovery: { requested: true, truncated: false, entries_scanned: 50 },
				records: [
					{
						disposition: 'remote_only_or_unreachable',
						item_id: 8421,
						rel_path: relPath,
						staging_path: '/Volumes/media/transcode/Bluey.mkv',
						code: 'staged_integrity_remote_only_or_unreachable',
						next_action: 'restore_staging_access',
						detail: 'Reconnect storage.'
					},
					{
						disposition: 'not_started',
						item_id: 8422,
						rel_path: 'tv/Bluey (2018)/Season 3/Bluey.S03E50.mkv',
						staging_path: null,
						code: 'staged_integrity_not_started',
						next_action: 'queue_encode',
						detail: 'Not encoded.'
					}
				]
			}
		});

		expect(links).toEqual([
			{
				label: 'Episode 49',
				relPath,
				href: '/folders/tv/Bluey%20(2018)/Season%203/Bluey.2018.S03E49.1080p.BluRay.mkv'
			}
		]);
	});

	it('lists every catalog episode for exact-item navigation in numeric order', () => {
		const episodeStatus: FolderStatusPayload = {
			...status,
			staged_integrity: {
				scope: status.media_scope,
				counts: { not_started: 2, tracked: 1, orphaned: 1 },
				blocker_count: 3,
				blockers: [],
				database_truncated: false,
				discovery: { requested: true, truncated: true, entries_scanned: 4 },
				records: [
					{
						disposition: 'not_started',
						item_id: 10,
						rel_path: 'tv/Show/Season 1/Show.S01E10.mkv',
						staging_path: null,
						code: 'staged_integrity_not_started',
						next_action: 'queue_encode',
						detail: 'Not encoded.'
					},
					{
						disposition: 'tracked',
						item_id: 2,
						rel_path: 'tv/Show/Season 1/Show.S01E02.mkv',
						staging_path: null,
						code: 'staged_integrity_tracked',
						next_action: '',
						detail: 'Already placed.'
					},
					{
						disposition: 'not_started',
						item_id: 1,
						rel_path: 'tv/Show/Season 1/Show.S01E01.mkv',
						staging_path: null,
						code: 'staged_integrity_not_started',
						next_action: 'queue_encode',
						detail: 'Not encoded.'
					},
					{
						disposition: 'orphaned',
						item_id: null,
						rel_path: null,
						staging_path: '/Volumes/transcode/unknown.mkv',
						code: 'staged_integrity_orphaned',
						next_action: 'inspect',
						detail: 'Untracked.'
					}
				]
			}
		};
		const options = seasonEpisodeOptions(episodeStatus);

		expect(options).toEqual([
			{
				itemId: 1,
				label: 'Episode 1',
				statusLabel: 'Not compressed yet',
				relPath: 'tv/Show/Season 1/Show.S01E01.mkv',
				href: '/folders/tv/Show/Season%201/Show.S01E01.mkv'
			},
			{
				itemId: 2,
				label: 'Episode 2',
				statusLabel: 'Already in the library',
				relPath: 'tv/Show/Season 1/Show.S01E02.mkv',
				href: '/folders/tv/Show/Season%201/Show.S01E02.mkv'
			},
			{
				itemId: 10,
				label: 'Episode 10',
				statusLabel: 'Not compressed yet',
				relPath: 'tv/Show/Season 1/Show.S01E10.mkv',
				href: '/folders/tv/Show/Season%201/Show.S01E10.mkv'
			}
		]);
		expect(seasonEpisodeNavigationUnavailable(episodeStatus)).toBe(false);
		expect(
			seasonEpisodeNavigationUnavailable({
				...episodeStatus,
				staged_integrity: { ...episodeStatus.staged_integrity!, database_truncated: true }
			})
		).toBe(true);
		expect(
			seasonEpisodeNavigationUnavailable({
				...episodeStatus,
				staged_integrity: {
					...episodeStatus.staged_integrity!,
					load_error: 'Could not load episode inventory.'
				}
			})
		).toBe(true);
	});

	it('formats operator-facing target totals with decimal units', () => {
		expect(formatDecimalFileSize(586_667_000 * 39)).toBe('22.9 GB');
	});

	it('preserves whether an approved output is smaller or larger than the original', () => {
		expect(expectedSizeChange(7_040_000_000, 581_200_000)).toEqual({
			direction: 'smaller',
			bytes: 6_458_800_000
		});
		expect(expectedSizeChange(500_000_000, 620_000_000)).toEqual({
			direction: 'larger',
			bytes: 120_000_000
		});
		expect(expectedSizeChange(500_000_000, 500_000_000)).toEqual({
			direction: 'unchanged',
			bytes: 0
		});
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

	it('does not hide an owned review behind completed related activity', () => {
		const activity = {
			schema_version: 1 as const,
			relation: 'descendant' as const,
			job: {
				job_id: 'season-test',
				prefix: 'tv/Big Brother (US)/Season 19',
				status: 'completed'
			}
		};

		expect(shouldPrioritizeScopeActivity(activity, true)).toBe(false);
		expect(shouldPrioritizeScopeActivity(activity, false)).toBe(true);
		expect(shouldPrioritizeScopeActivity(activity, false, true)).toBe(false);
		expect(
			shouldPrioritizeScopeActivity(
				{ ...activity, job: { ...activity.job, status: 'running' } },
				true
			)
		).toBe(true);
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
		expect(calibrationWorkLabel(job)).toBe('2 of 3 comparison clips built');
		expect(calibrationWorkProgress(job)).toEqual({
			label: '2 of 3 comparison clips built',
			determinate: true
		});
		expect(calibrationLivenessLabel(job)).toBe('Computer is reporting normally');
		expect(calibrationActivityStatusLabel(job)).toBe('Creating sample');
	});

	it('keeps queued status and missing heartbeat freshness truthful', () => {
		expect(
			calibrationActivityStatusLabel({
				job_id: 'test-queued',
				prefix: card.prefix,
				status: 'queued'
			})
		).toBe('Sample waiting');
		expect(calibrationFreshnessLabel(undefined)).toBe('No update yet');
		expect(calibrationFreshnessLabel(0)).toBe('Updated just now');
		expect(calibrationFreshnessLabel(75)).toBe('Updated 1 min ago');
		expect(calibrationActivityStatusLabel({ job_id: 'test-starting', status: 'starting' })).toBe(
			'Sample starting'
		);
		expect(calibrationLivenessLabel({ job_id: 'test-retrying', status: 'retry_backoff' })).toBe(
			'Waiting before retry'
		);
		expect(calibrationStageLabel({ job_id: 'test-failed', status: 'failed' })).toBe(
			'Sample needs retry'
		);
	});

	it('describes bounded target-search candidate progress without implying job completion', () => {
		expect(
			calibrationWorkLabel({
				job_id: 'test-search',
				prefix: card.prefix,
				status: 'running',
				progress: {
					schema_version: 1,
					stage: 'searching_target',
					work: { completed: 3, total: 6 }
				}
			})
		).toBe('Trying size settings');
		expect(
			calibrationWorkProgress({
				job_id: 'test-search',
				status: 'running',
				progress: {
					schema_version: 1,
					stage: 'searching_target',
					work: { completed: 3, total: 6 }
				}
			})
		).toEqual({ label: 'Trying size settings', determinate: false });
		expect(
			sampleSearchTechnicalDetail({
				job_id: 'test-search',
				status: 'running',
				progress: {
					schema_version: 1,
					stage: 'searching_target',
					work: { completed: 3, total: 6 }
				}
			})
		).toBe('3 of up to 6 size settings tried');
		expect(
			calibrationWorkProgress({
				job_id: 'sample-measuring',
				status: 'running',
				progress: {
					schema_version: 1,
					stage: 'measuring_quality',
					work: { completed: 1, total: 2 }
				}
			})
		).toEqual({ label: 'Working', determinate: false });
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
		expect(summary.detail).toContain('5 comparable completed samples');
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

		expect(calibrationLivenessLabel(completed)).toBe('Sample finished');
		expect(calibrationEtaSummary(completed)).toMatchObject({ value: 'Finished', tone: 'quiet' });
	});
});
