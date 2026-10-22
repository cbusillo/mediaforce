import { describe, expect, it } from 'vitest';

import type {
	DashboardScanJob,
	DashboardScanWarning,
	EncodeQueueJob,
	FolderCard,
	FolderQualityMemoryPayload,
	FolderPayload,
	FolderStatusPayload,
	CompressionIntentOptionPayload,
	LifecycleState,
	OperatorIntentRequestPayload,
	PageStatePayload,
	SizeGoalOptionPayload
} from '$lib/api/types';
import {
	sizeQuestionFinding,
	waitingScopeName,
	encodeWaitingReasons,
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
	compressionIntentContract,
	currentOperatorIntent,
	episodeLabel,
	exactItemFilename,
	exactReviewSizeFacts,
	soundCheckView,
	expectedSizeChange,
	folderSizeTargetAnalysis,
	formatDecimalFileSize,
	formatFileSize,
	goalRequest,
	isSeriesPrefix,
	isSizeGoalSelectionConfirmed,
	measuredFollowupRequest,
	normalizedSizePaceBytes,
	overlappingCalibrationActivity,
	qualityMemoryView,
	resolvedTargetSummary,
	reviewFeedbackIntent,
	reviewFeedbackRequest,
	reviewAdjustmentIntent,
	reviewedSampleCoverage,
	reviewSizeAdjustment,
	scopedEncodeProgress,
	seasonIdentity,
	seasonEpisodeNavigationUnavailable,
	seasonPromotionIntegrity,
	sizeHeldRecords,
	canKeepStagedCopy,
	seasonEpisodeOptions,
	sampleSearchTechnicalDetail,
	stagedEpisodeLinks,
	shouldPrioritizeScopeActivity,
	sizeGoals,
	approvalStartPlan,
	type QueueAdmission,
	productionScope,
	finishedEpisodesNote,
	motionHoldNotes,
	motionHoldReasonLabel,
	motionHolds,
	showsMotionKeepFiles,
	queueAdmission,
	targetConstraintSummary,
	targetProvenanceSummary,
	testRequestWithInstructions,
	technicalVideoPolicy,
	withCompressionIntent,
	showCopy
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

const metricSupport = { vmaf: true, xpsnr: true, ssim: true };

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
				can_promote: !databaseTruncated && !discoveryTruncated,
				promotable_count: blockerCount > 0 ? 6 : 7,
				blockers: [],
				waiting: []
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
		metric_support: metricSupport,
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
	it.each([
		'no_history',
		'stale_evidence',
		'high_dispersion',
		'shadow_evaluation_error',
		null
	] as const)('keeps backend jargon out of the rendered facts for %s', (reason) => {
		const payload = qualityMemoryPayload();
		payload.reason = 'Shadow quality-memory recommendation: CRF 50 at VMAF 85';
		for (const recommended of [true, false]) {
			payload.fallback_reason = reason;
			if (!recommended) payload.recommendation = null;
			const view = qualityMemoryView(folder({ quality_memory: payload }));
			const displayed = [
				view.title,
				view.badge,
				view.reason,
				view.policyCopy,
				view.evidence,
				view.dispersion,
				view.comparison,
				...view.measured.flatMap((fact) => [fact.label, fact.value, fact.detail]),
				...Object.values(view.recommendation)
			].join(' ');
			expect(displayed).not.toMatch(/\b(CRF|VMAF|shadow|quality.memory|sparse memory)\b/i);
		}
	});

	it('keeps the empty state compact and explicit about unchanged policy', () => {
		const view = qualityMemoryView(folder({ quality_memory: null }));

		expect(view).toMatchObject({
			state: 'empty',
			tone: 'quiet',
			badge: 'Nothing yet',
			title: showCopy.noPastResults,
			measured: []
		});
		expect(view.reason).toBe('Your size and quality settings stay as they are.');
	});

	it('separates a measured production run from a high-confidence recommendation', () => {
		const view = qualityMemoryView(folder({ quality_memory: qualityMemoryPayload() }));

		expect(view).toMatchObject({
			state: 'high-confidence',
			tone: 'success',
			badge: 'High confidence',
			source: 'Episode 03',
			recommendation: {
				label: 'Suggested first setting',
				value: '50.0',
				detail: '12 results · season · high confidence'
			},
			comparison: 'Within 1 compression step · difference +1.0',
			dispersion: 'Compression settings 48.0–52.0 · middle spread 2.0 · typical difference 1.0'
		});
		expect(view.measured).toEqual([
			{ label: 'Compression setting', value: '51.0', detail: '5 candidates · 1m 40s' },
			{
				label: 'Measured Picture appearance score',
				value: '86.5',
				detail: 'target 85.0 · minimum 84.0 · margin +2.5'
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
		expect(view.comparison).toBe('More than 1 compression step apart · difference +3.0');
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

		expect(view.badge).toBe('Starting suggestion worked');
		expect(view.title).toBe('Earlier results helped this run');
		expect(view.recommendation.label).toBe('First setting tried');
		expect(view.comparison).toBe('Starting setting passed · estimated 80% fewer attempts');
		expect(view.policyCopy).toContain('normal search was not needed');
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

		expect(view.title).toBe('Earlier results tried; normal search used');
		expect(view.recommendation).toMatchObject({
			label: 'First setting tried',
			value: '49.0'
		});
		expect(view.recommendation.detail).toContain('Suggested starting setting');
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

		expect(view.badge).toBe('Normal search used');
		expect(view.title).toBe('Earlier results tried; normal search used');
		expect(view.comparison).toBe('Normal search used · 6 candidates total');
		expect(view.reason).toContain('missed the saved size band');
		expect(view.policyCopy).toContain('normal search ran');
	});

	it.each([
		['sparse_cohort', 'sparse', 'More results needed'],
		['final_retry_terminal', 'sparse', 'More results needed'],
		['stale_signature', 'stale', 'Earlier results out of date'],
		['shadow_evaluation_error', 'unavailable', 'Earlier results unavailable'],
		['conflicting_quality_evidence', 'conflicting', 'Results disagree']
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
		expect(view.policyCopy).toContain('saved settings stayed unchanged');
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
	return (['reference', 'balanced', 'perceptual_floor'] as const).map((level) => ({
		key: level,
		title: level,
		detail: level,
		selected: level === 'balanced',
		accepts_under_target_result: level === 'perceptual_floor',
		compression_intent: {
			schema_version: 1,
			level,
			confirmed: true
		}
	}));
}

describe('season experience translation', () => {
	it('describes the size, search, quality, and final contract for every quality preference', () => {
		const contracts = Object.fromEntries(
			compressionIntentOptions().map((option) => [option.key, compressionIntentContract(option)])
		);

		// Each preference is described differently, fully, and without internal terms.
		const internalTerms = /\b(measured|fidelity|floor|band|candidate|CRF|VMAF)\b/i;
		for (const key of ['reference', 'balanced', 'perceptual_floor']) {
			const contract = contracts[key];
			for (const value of Object.values(contract)) {
				expect(value).not.toBe('');
				expect(value).not.toMatch(internalTerms);
			}
		}
		expect(
			new Set(
				[contracts.reference, contracts.balanced, contracts.perceptual_floor].map(
					(c) => c.searchRule
				)
			).size
		).toBe(3);
		const legacyTransparent = compressionIntentContract({
			key: 'transparent',
			title: 'No visible difference',
			detail: 'Legacy option',
			selected: false,
			accepts_under_target_result: true,
			compression_intent: { schema_version: 1, level: 'transparent', confirmed: true }
		});
		expect(legacyTransparent).toMatchObject({
			sizeLabel: contracts.perceptual_floor.sizeLabel,
			searchLabel: contracts.perceptual_floor.searchLabel,
			qualityLabel: contracts.perceptual_floor.qualityLabel,
			finalHeadline: contracts.perceptual_floor.finalHeadline
		});
	});

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

	it('says which episodes the season estimate leaves out', () => {
		const job = (progress: EncodeQueueJob['progress']): EncodeQueueJob => ({
			job_id: 'season-encode',
			prefix: 'tv/Show/Season 1',
			status: 'running',
			progress
		});

		expect(
			scopedEncodeProgress(
				job({ eta_copy: '2h 5m', left_out_needs_attention_item_count: 1 }),
				0,
				10
			).etaDetail
		).toBe('2h 5m · Leaves out 1 item waiting for you');
		expect(scopedEncodeProgress(job({ eta_copy: '2h 5m' }), 0, 10).etaDetail).toBe('2h 5m');
		// No estimate, nothing to qualify.
		expect(scopedEncodeProgress(job({ left_out_failed_item_count: 2 }), 0, 10).etaDetail).toBe('');
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

	it('shows the sound check result in plain words', () => {
		expect(
			soundCheckView({
				audio_quality: { status: 'passed', sound_score: 4.478, max_sound_score: 4.73 }
			})?.factValue
		).toBe('Passed · 4.48 of 4.73');

		const failed = soundCheckView({
			audio_quality: {
				status: 'failed',
				checks: [
					{ label: 'Sound score', passed: false, detail: '4.10 of 4.73; needs 4.30' },
					{ label: 'Dialogue', passed: true, detail: '+0.0 dB' }
				]
			}
		});
		expect(failed?.factValue).toBe('Did not pass');
		expect(failed?.problems).toEqual(['Sound score: 4.10 of 4.73; needs 4.30']);

		const unmeasured = soundCheckView({
			audio_quality: { status: 'unmeasured', reason: "Docker isn't running." }
		});
		expect(unmeasured?.factValue).toBe('Not measured');
		expect(unmeasured?.detail).toBe("Docker isn't running.");

		expect(soundCheckView({ audio_quality: { status: 'unchanged' } })?.factValue).toBe(
			'Kept as is'
		);
		expect(soundCheckView({})).toBeNull();
	});

	it('names the sound check stage', () => {
		expect(
			calibrationStageLabel({
				job_id: 'sample-1',
				status: 'running',
				progress: { schema_version: 1, stage: 'checking_sound' }
			})
		).toBe('Checking the sound');
		const waitingReason =
			'Waiting for M2 MBP to be available, such as in its next work window, to make this sample. Mediaforce tries again by itself a few times before it asks you.';
		expect(
			calibrationStageLabel({
				job_id: 'sample-1',
				status: 'queued',
				progress: {
					schema_version: 1,
					stage: 'waiting_for_computer',
					waiting_for_computer: 'M2 MBP',
					waiting_reason: waitingReason
				}
			})
		).toBe(waitingReason);
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

	it('lets ready episodes finish while explaining the ones still waiting', () => {
		const integrity = seasonPromotionIntegrity(promotionStatus({ blockerCount: 2 }));

		expect(integrity).toMatchObject({
			available: true,
			canFinish: true,
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
				nextAction: 'It will be compressed later. Nothing else waits for it.'
			}
		]);
	});

	it('keeps finish unavailable when no file is ready yet', () => {
		const nothingReady = promotionStatus({ blockerCount: 2 });
		nothingReady.staged_integrity!.promotion_readiness!.promotable_count = 0;

		expect(seasonPromotionIntegrity(nothingReady)).toMatchObject({
			canFinish: false,
			readyCount: 0
		});
	});

	it('enables finishing for a complete inventory with ready files', () => {
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

	it('explains files made under settings that were never approved without blocking the rest', () => {
		const mixedPolicyStatus = promotionStatus();
		mixedPolicyStatus.staged_integrity!.promotion_readiness = {
			applicable: true,
			can_promote: true,
			promotable_count: 5,
			blockers: [],
			waiting: [
				{
					code: 'season_policy_not_approved',
					count: 2,
					next_action: 'approve_matching_policy_or_recreate_outputs'
				}
			]
		};

		const integrity = seasonPromotionIntegrity(mixedPolicyStatus);

		expect(integrity).toMatchObject({ canFinish: true, readyCount: 5 });
		expect(integrity.blockers).toContainEqual({
			code: 'season_policy_not_approved',
			count: 2,
			label: 'Output does not match approval',
			nextAction: 'Approve matching settings or make the affected episodes again.'
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
			detail: expect.stringContaining('minimum Picture appearance score of 93')
		});

		const floorError =
			'The approved target size conflicts with the configured quality floor (target_band_violates_quality_floor); target=225000000 bytes, best_reachable=523458023 bytes.';
		const stoppedEncode = {
			job_id: 'ff5d63a529ce',
			prefix: 'tv/Big Brother (US)',
			status: 'needs_attention',
			error: floorError,
			finished_at: '2026-07-26T04:21:50+00:00'
		};
		const stoppedOnSize = (failure: PageStatePayload['failure']): FolderStatusPayload => ({
			...status,
			page_state: {
				key: 'needs_help',
				tone: 'attention',
				recovery_kind: 'season',
				primary_action: 'choose_size',
				failure,
				source_job: {
					id: stoppedEncode.job_id,
					kind: 'encode',
					prefix: stoppedEncode.prefix,
					status: stoppedEncode.status
				}
			}
		});
		expect(
			targetConstraintSummary(
				folder({ encode_job: stoppedEncode }),
				stoppedOnSize('quality_conflict')
			)
		).toMatchObject({
			kind: 'quality_conflict',
			recoveryLabel: 'Choose a roomier goal',
			detail: expect.stringMatching(/could not reach 225 MB.*about 523 MB.*Retrying would stop/)
		});
		// The page leads with something else, such as a newer sample, so the stopped run is not the question.
		expect(
			targetConstraintSummary(folder({ encode_job: stoppedEncode }), {
				...status,
				page_state: {
					key: 'ready_to_compare',
					tone: 'ready',
					recovery_kind: null,
					primary_action: 'review_sample',
					failure: null,
					source_job: null
				}
			})
		).toBeNull();
		expect(
			targetConstraintSummary(folder({ encode_job: stoppedEncode }), stoppedOnSize('no_access'))
		).toBeNull();

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

	it('offers a fresh smaller review without treating the failed measurements as approval', () => {
		const request = sizeOption('recommended', 'normalized', 300, 294, 45).operator_intent;
		const failed = folder({
			resolved_operator_intent: { ...folder().resolved_operator_intent!, request },
			failed_target_size_search: {
				status: 'bound_exhausted',
				selection_reason: 'largest_quality_safe_candidate_under_target_band',
				under_target_review_candidate: {
					crf: 42,
					metric: 'VMAF',
					metric_score: 90.8,
					minimum_metric_score: 80,
					predicted_whole_episode_bytes: 219_000_000,
					requires_fresh_sample: true
				}
			}
		});
		expect(targetConstraintSummary(failed)).toMatchObject({
			canReviewSmaller: true,
			recoveryLabel: 'Allow smaller and create review sample',
			detail: expect.stringContaining('219 MB with Picture appearance score 90.8')
		});
		expect(targetConstraintSummary(failed)?.detail).toContain('selected candidate may change');
		const intent = withCompressionIntent(currentOperatorIntent(failed)!, {
			schema_version: 1,
			level: 'perceptual_floor',
			confirmed: true
		});
		expect(intent.size_goal).toEqual(request.size_goal);
		expect(intent.resolution).toEqual(request.resolution);
		expect(intent.evidence_authority).not.toBe('approved_visual_result');
		expect(
			targetConstraintSummary({ ...failed, resolved_operator_intent: undefined })?.canReviewSmaller
		).toBeUndefined();
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
			title: 'The sample reached a configured limit.',
			detail: expect.stringContaining('Retrying that saved sample would repeat the same limit')
		});
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

	it('reads coverage from the selection stored with the reviewed sample', () => {
		const summary = 'Tested 1 of 22 files. Not yet tested: Season 3, HEVC (H.265) video.';
		expect(
			reviewedSampleCoverage(
				folder({
					representative_selection: {
						coverage_summary:
							'Tested 1 of 1 file. No season, video format, or resolution here is untested.'
					},
					calibration: {
						sample_item: {
							representative_selection: {
								coverage_summary: summary,
								coverage: {
									untested_groups: [
										{ kind: 'season', value: '3', label: 'Season 3', item_count: 6 },
										{
											kind: 'video_format',
											value: 'hevc',
											label: 'HEVC (H.265) video',
											item_count: 4
										}
									]
								}
							}
						}
					}
				})
			)
		).toEqual({ summary, hasUntestedGroups: true });
	});

	it('shows no coverage for a sample recorded before coverage was measured', () => {
		expect(
			reviewedSampleCoverage(
				folder({
					calibration: {
						sample_item: { representative_selection: { confidence: { level: 'low' } } }
					}
				})
			)
		).toBeNull();
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

describe('approvalStartPlan', () => {
	const admission = (overrides: Partial<QueueAdmission> = {}): QueueAdmission => ({
		clearedCount: 0,
		checkCount: 0,
		protectedCount: 0,
		protectedCheckCount: 0,
		motionHeldCount: 0,
		asIsCount: 0,
		...overrides
	});
	const series = (overrides: Partial<Parameters<typeof approvalStartPlan>[0]>) =>
		approvalStartPlan({
			scope: 'series',
			admission: admission({ clearedCount: 66, protectedCount: 22 }),
			olderSeasons: { candidateCount: 66, overriddenCount: 0, seasonCount: 3 },
			...overrides
		});

	it('starts the one unambiguous action', () => {
		expect(series({})).toMatchObject({ mode: 'scope', episodeCount: 66 });
		expect(series({})?.label).toBe('Keep and compress 66 episodes');
		expect(
			series({
				admission: admission(),
				olderSeasons: { candidateCount: 10, overriddenCount: 0, seasonCount: 3 }
			})
		).toMatchObject({ mode: 'older_seasons', episodeCount: 10 });
		expect(
			approvalStartPlan({
				scope: 'episode',
				admission: admission({ clearedCount: 1 }),
				olderSeasons: null
			})
		).toMatchObject({ mode: 'scope', label: 'Keep and compress this episode' });
	});

	it('keeps a separate step when a hold would be bypassed or the choice is ambiguous', () => {
		expect(
			series({ olderSeasons: { candidateCount: 66, overriddenCount: 4, seasonCount: 3 } })
		).toBeNull();
		expect(
			series({ olderSeasons: { candidateCount: 70, overriddenCount: 0, seasonCount: 3 } })
		).toBeNull();
		expect(series({ admission: admission(), olderSeasons: null })).toBeNull();
		expect(
			approvalStartPlan({
				scope: 'season',
				admission: admission({ protectedCount: 22 }),
				olderSeasons: null
			})
		).toBeNull();
	});

	it('offers nothing for an episode or season the queue would leave out for its motion pattern', () => {
		const held = admission({ motionHeldCount: 1 });
		expect(approvalStartPlan({ scope: 'episode', admission: held, olderSeasons: null })).toBeNull();
		expect(
			approvalStartPlan({
				scope: 'season',
				admission: admission({ motionHeldCount: 6 }),
				olderSeasons: null
			})
		).toBeNull();
	});

	it('starts motion-pattern checks when they are all the queue can start', () => {
		const checks = admission({ checkCount: 3 });
		const season = approvalStartPlan({ scope: 'season', admission: checks, olderSeasons: null });
		const show = approvalStartPlan({ scope: 'series', admission: checks, olderSeasons: null });

		expect(season).toMatchObject({ mode: 'scope', episodeCount: checks.checkCount });
		expect(show).toMatchObject({ mode: 'scope', episodeCount: checks.checkCount });
		// The label says the episodes are checked first, not compressed straight away.
		expect(season?.label).not.toBe(
			approvalStartPlan({
				scope: 'season',
				admission: admission({ clearedCount: 3 }),
				olderSeasons: null
			})?.label
		);
	});
});

describe('productionScope', () => {
	const lifecycle = (eligible: number, eligibleSizeBytes: number) => ({
		schema_version: 1,
		prefix: card.prefix,
		candidate_count: eligible,
		eligible_candidate_count: eligible,
		eligible_size_bytes: eligibleSizeBytes,
		held_candidate_count: 0,
		seasons: []
	});

	it('counts and sizes only the episodes queueing selects, leaving out the published ones', () => {
		const season = folder({ lifecycle: lifecycle(6, 5_400_000_000) });
		const selected = season.lifecycle!;

		expect(productionScope(season)).toEqual({
			episodeCount: selected.eligible_candidate_count,
			currentSizeBytes: selected.eligible_size_bytes
		});
	});

	it('keeps a whole show sized as the show and one episode as itself', () => {
		const show = folder({ prefix: 'tv/Big Brother (US)', lifecycle: lifecycle(6, 5_400_000_000) });
		expect(productionScope(show)).toEqual({
			episodeCount: show.lifecycle!.eligible_candidate_count,
			currentSizeBytes: show.summary!.total_size_bytes
		});
	});
});

describe('motionHolds', () => {
	const season = (held: string[], check: string[]) =>
		folder({ motion_hold_files: { held_files: held, check_files: check } });

	it('adds nothing when no episode is left out for its motion pattern', () => {
		expect(motionHoldNotes(motionHolds(folder()))).toEqual([]);
	});

	it('names how many stay original and how many wait for a check', () => {
		const holds = motionHolds(
			season(['tv/Show/Season 1/A.mkv', 'tv/Show/Season 1/B.mkv'], ['C.mkv'])
		);
		const [held, check] = motionHoldNotes(holds);

		expect(holds.heldFiles).toHaveLength(2);
		expect(held).toContain(`${holds.heldFiles.length} episodes`);
		expect(check).toContain(`${holds.checkFiles.length} episode `);
	});

	it('speaks only of the held episodes when none waits for a check', () => {
		const notes = motionHoldNotes(motionHolds(season(['A.mkv'], [])));
		expect(notes).toHaveLength(1);
		expect(notes[0]).toContain('1 episode ');
		// A held pattern no one can change sends the owner nowhere.
		expect(notes[0]).not.toContain('Activity');
	});

	it('points to the show page only when its encode-as-is decision covers a held episode', () => {
		const held = ['A.mkv', 'B.mkv'];
		const decided = motionHoldNotes(
			motionHolds(
				folder({ motion_hold_files: { held_files: held, check_files: [], as_is_count: 1 } })
			)
		);
		const settled = motionHoldNotes(motionHolds(season(held, [])));

		expect(decided.some((note) => note.includes('show page'))).toBe(true);
		expect(settled.some((note) => note.includes('show page'))).toBe(false);
		// The show page holds the decision itself, so it does not point to itself.
		const onShow = motionHoldNotes(
			motionHolds(
				folder({ motion_hold_files: { held_files: held, check_files: [], as_is_count: 1 } })
			),
			true
		);
		expect(onShow.some((note) => note.includes('show page'))).toBe(false);
	});
});

describe('queueAdmission', () => {
	const lifecycle = (overrides: Partial<LifecycleState>): LifecycleState => ({
		schema_version: 1,
		prefix: card.prefix,
		candidate_count: 6,
		eligible_candidate_count: 0,
		held_candidate_count: 0,
		seasons: [],
		...overrides
	});

	it('counts only protected episodes an override could take as protected', () => {
		const admission = queueAdmission(
			folder({
				lifecycle: lifecycle({ held_candidate_count: 6, protected_motion_held_candidate_count: 6 })
			})
		);
		expect(admission).toMatchObject({ protectedCount: 0, motionHeldCount: 6, clearedCount: 0 });
	});

	it('keeps episodes waiting for a check apart from the ones compressed now', () => {
		const admission = queueAdmission(
			folder({
				lifecycle: lifecycle({
					eligible_candidate_count: 1,
					motion_check_candidate_count: 5,
					motion_held_candidate_count: 2,
					motion_as_is_candidate_count: 1
				})
			})
		);
		expect(admission).toEqual({
			clearedCount: 1,
			checkCount: 5,
			protectedCount: 0,
			protectedCheckCount: 0,
			motionHeldCount: 2,
			asIsCount: 1
		});
	});
});

describe('encodeWaitingReasons', () => {
	const job = (groups: NonNullable<EncodeQueueJob['progress']>['unfinished_breakdown']) =>
		({ job_id: 'folder', progress: { unfinished_breakdown: groups } }) as EncodeQueueJob;

	it('keeps every reason, split into what needs the owner and what is only waiting', () => {
		const reasons = encodeWaitingReasons(
			job([
				{
					reason: 'final_size_target_miss',
					label: "didn't pass the final size check",
					count: 1,
					needs_owner: true,
					items: []
				},
				{
					reason: 'storage_io',
					label: 'had trouble reading or writing media',
					count: 2,
					needs_owner: true,
					items: []
				},
				{
					reason: 'waiting_schedule',
					label: 'waiting for a scheduled time',
					count: 20,
					needs_owner: false,
					items: []
				},
				{ reason: 'retrying', label: 'trying again soon', count: 0, needs_owner: false, items: [] }
			])
		);

		expect(reasons.needsYou.map((group) => `${group.count} ${group.label}`)).toEqual([
			"1 didn't pass the final size check",
			'2 had trouble reading or writing media'
		]);
		expect(reasons.waiting.map((group) => `${group.count} ${group.label}`)).toEqual([
			'20 waiting for a scheduled time'
		]);
		expect(reasons.fileCount).toBe(23);
	});

	it('treats rows saved before the owner flag by their reason', () => {
		const reasons = encodeWaitingReasons(
			job([
				{ reason: 'stopped', label: 'stopped', count: 1, items: [] },
				{ reason: 'retrying', label: 'trying again soon', count: 1, items: [] }
			])
		);

		expect(reasons.needsYou.map((group) => group.reason)).toEqual(['stopped']);
		expect(reasons.waiting.map((group) => group.reason)).toEqual(['retrying']);
	});

	it('says why a file does not fit its size goal in plain words', () => {
		const question = {
			job_id: 'shard-1',
			rel_path: 'tv/Futurama/Season 8/Futurama S08E02.mkv',
			goal_bytes: 82_500_000,
			smallest_quality_safe_bytes: 88_700_000
		};

		expect(sizeQuestionFinding(question)).toBe('Keeping the picture quality needs 88.7 MB.');
		expect(sizeQuestionFinding({ ...question, reason: 'quality_needs_more' })).toBe(
			'Keeping the picture quality needs 88.7 MB.'
		);
		expect(sizeQuestionFinding({ ...question, reason: 'no_setting_fits' })).toBe(
			'No setting lands in the size range. The closest one that keeps the picture right is 88.7 MB.'
		);
	});

	it('lists files the owner already answered for apart from what is waiting', () => {
		const reasons = encodeWaitingReasons(
			job([
				{
					reason: 'quality_floor_size_conflict',
					label: 'waiting for your OK to use more space',
					count: 1,
					needs_owner: true,
					items: [],
					size_questions: [
						{
							job_id: 'shard-1',
							rel_path: 'tv/Show/Season 1/Show S01E01.mkv',
							goal_bytes: 191_800_000,
							smallest_quality_safe_bytes: 358_900_000
						}
					]
				},
				{
					reason: 'size_exception_declined',
					label: 'kept as the original, your choice',
					count: 2,
					needs_owner: false,
					owner_choice: true,
					items: []
				}
			])
		);

		expect(reasons.needsYou.map((group) => group.size_questions?.length)).toEqual([1]);
		expect(reasons.waiting).toEqual([]);
		expect(reasons.yourChoices.map((group) => `${group.count} ${group.label}`)).toEqual([
			'2 kept as the original, your choice'
		]);
		expect(reasons.fileCount).toBe(3);
	});

	it('names the scope a borrowed reason list covers', () => {
		expect(waitingScopeName('tv/Show')).toBe('all of Show');
		expect(waitingScopeName('tv/Show/Season 2')).toBe('Show Season 2');
	});

	it('is empty without an encode job', () => {
		expect(encodeWaitingReasons(null)).toEqual({
			needsYou: [],
			waiting: [],
			yourChoices: [],
			fileCount: 0
		});
	});
});

describe('sizeHeldRecords', () => {
	const record = (overrides: Record<string, unknown>) => ({
		disposition: 'size_held',
		item_id: 7,
		rel_path: 'tv/Show/Season 1/Small.mkv',
		staging_path: '/staging/Small.mkv',
		code: 'staged_integrity_size_held',
		next_action: 'keep_or_remake_output',
		detail: 'The finished file is far smaller than its sample predicted and waits for the owner.',
		size_prediction: { predicted_bytes: 262_000_000, actual_bytes: 172_000_000, ratio: 0.656 },
		...overrides
	});

	it('asks only about files held for their size that the owner can act on', () => {
		const integrity = {
			records: [
				record({}),
				record({ item_id: 8, disposition: 'validation_failed', size_prediction: undefined }),
				record({ item_id: null })
			]
		} as unknown as Parameters<typeof sizeHeldRecords>[0];

		expect(sizeHeldRecords(integrity).map((held) => held.item_id)).toEqual([7]);
	});
});

describe('finished-file recovery', () => {
	it('shows size and settings-history remakes, including their blocked explanation', () => {
		const integrity = {
			records: [
				{
					item_id: 1,
					disposition: 'validation_failed',
					remake: { reason: 'final_size', blocked_reason: 'Approve a fresh sample.' }
				},
				{
					item_id: 2,
					disposition: 'promotable',
					remake: { reason: 'settings_history', blocked_reason: '' }
				},
				{ item_id: 3, disposition: 'validation_failed' },
				{ item_id: null, remake: { reason: 'final_size', blocked_reason: '' } }
			]
		} as unknown as Parameters<typeof sizeHeldRecords>[0];
		expect(sizeHeldRecords(integrity).map((file) => file.item_id)).toEqual([1, 2]);
	});

	it('offers keep for a held copy or a size-goal miss, never for other remakes', () => {
		type Record = Parameters<typeof canKeepStagedCopy>[0];
		const file = (overrides: object) =>
			({ item_id: 1, disposition: 'validation_failed', ...overrides }) as unknown as Record;
		expect(canKeepStagedCopy(file({ disposition: 'size_held' }))).toBe(true);
		expect(
			canKeepStagedCopy(
				file({ remake: { reason: 'final_size', blocked_reason: 'Approve a fresh sample.' } })
			)
		).toBe(true);
		expect(
			canKeepStagedCopy(
				file({ remake: { reason: 'final_size', blocked_reason: '', pending: true } })
			)
		).toBe(false);
		expect(
			canKeepStagedCopy(file({ remake: { reason: 'settings_history', blocked_reason: '' } }))
		).toBe(false);
		expect(canKeepStagedCopy(file({}))).toBe(false);
	});
});

describe('keeping motion-held episodes original', () => {
	const heldFile = {
		library_item_id: 4,
		rel_path: 'tv/Show/Season 1/A.mkv',
		reason: 'mixed' as const,
		as_is: false
	};
	const keptFile = {
		library_item_id: 5,
		rel_path: 'tv/Show/Season 1/B.mkv',
		reason: 'partly_interlaced' as const,
		kept_at: '2026-10-18T20:00:00Z'
	};

	it('passes on the held and kept episodes the page can decide about', () => {
		const holds = motionHolds(
			folder({
				motion_hold_files: {
					held_files: [heldFile.rel_path],
					check_files: [],
					held: [heldFile],
					kept: [keptFile]
				}
			})
		);

		expect(holds.heldItems).toEqual([heldFile]);
		expect(holds.keptItems).toEqual([keptFile]);
	});

	it('lists nothing to decide when the server names no held or kept episode', () => {
		const holds = motionHolds(folder({ motion_hold_files: { held_files: [], check_files: [] } }));
		expect(holds.heldItems).toEqual([]);
		expect(holds.keptItems).toEqual([]);
	});

	it('lists the keep decision whenever an episode is held or kept, whatever leads the page', () => {
		const files = (held: (typeof heldFile)[], kept: (typeof keptFile)[]) =>
			motionHolds(folder({ motion_hold_files: { held_files: [], check_files: [], held, kept } }));
		expect(showsMotionKeepFiles(files([heldFile], []))).toBe(true);
		expect(showsMotionKeepFiles(files([], [keptFile]))).toBe(true);
		expect(showsMotionKeepFiles(files([], []))).toBe(false);
	});

	it('names each reason a file is held in plain words', () => {
		const labels = (['mixed', 'partly_interlaced', 'unclear'] as const).map(motionHoldReasonLabel);
		expect(new Set(labels).size).toBe(3);
		// A reason the page does not know yet still reads as a motion pattern, not as a code.
		expect(motionHoldReasonLabel('telecine_low_confidence')).toBe(motionHoldReasonLabel('unclear'));
	});

	it('says how many finished episodes were kept original rather than made smaller', () => {
		expect(finishedEpisodesNote(39, 0)).toContain('All 39 smaller episodes');
		const mixed = finishedEpisodesNote(39, 6);
		expect(mixed).toContain('33 smaller episodes');
		expect(mixed).toContain('6 episodes stay original by your choice');
		expect(finishedEpisodesNote(1, 1)).toBe('1 episode stays original by your choice.');
	});
});
