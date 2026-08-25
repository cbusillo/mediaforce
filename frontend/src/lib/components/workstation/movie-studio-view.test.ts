import { describe, expect, it } from 'vitest';

import {
	canRetrySampleJob,
	formatMovieBytes,
	movieCurrentWorkView,
	movieGoalContractView,
	movieGoalFactsView,
	movieRetryResponseCopy,
	movieReviewStatusLabel,
	movieSampleSetupResult,
	movieSizeCapBlockView,
	parseServerTimestamp,
	parentSampleAppliesToExactItem,
	sampleStopResponseCopy
} from './movie-studio-view';
import type {
	PlannedStreamPayload,
	ResolvedOperatorIntentPayload,
	StreamBudgetLedgerPayload
} from '$lib/api/types';

function resolvedMovieIntent(
	overrides: Partial<ResolvedOperatorIntentPayload> = {}
): ResolvedOperatorIntentPayload {
	return {
		schema_version: 2,
		requires_confirmation: false,
		size_goal: {
			schema_version: 1,
			mode: 'normalized',
			source: 'config_default',
			status: 'resolved',
			requires_confirmation: false,
			reference_size_mb: 300,
			reference_runtime_minutes: 45,
			target_size_bytes: 720_000_000,
			sample_projection_tolerance_percent: 8,
			final_output_tolerance_percent: 5,
			rationale: 'Runtime-scaled target.'
		},
		resolution: {
			mode: 'source',
			max_height: null,
			source: 'config_default',
			requires_confirmation: false,
			rationale: 'Keep the source resolution.'
		},
		compression_intent: {
			schema_version: 1,
			level: 'balanced',
			confirmed: true,
			source: 'config_default',
			requires_confirmation: false,
			accepts_under_target_result: true,
			semantic_id: 'do-not-display-semantic-id',
			snapshot_id: 'do-not-display-snapshot-id',
			title: 'Balance size and detail',
			detail: 'Prefer a practical balance between file size and visible detail.'
		},
		request: null,
		quality: {
			metric: 'vmaf',
			target_vmaf: 85,
			min_target_vmaf: 80,
			source: 'config_default'
		},
		streams: { audio: {}, subtitle: {}, source: 'config_default' },
		...overrides
	};
}

function streamBudgetLedger(streams: PlannedStreamPayload[]): StreamBudgetLedgerPayload {
	return {
		schema_version: 1,
		ledger_id: 'do-not-display-ledger-id',
		source: {
			source_id: 'movie-source',
			source_fingerprint: 'fingerprint-1',
			source_size_bytes: 2_800_000_000,
			source_video_bitrate_bps: 10_000_000,
			duration_seconds: 6480
		},
		policy_hash: 'do-not-display-policy-hash',
		size_goal: resolvedMovieIntent().size_goal,
		stream_plan: {
			schema_version: 1,
			plan_id: 'do-not-display-plan-id',
			source_id: 'movie-source',
			source_fingerprint: 'fingerprint-1',
			policy_hash: 'do-not-display-policy-hash',
			output_container: 'mkv',
			attachments_known: true,
			copy_unknown_attachments: false,
			streams
		},
		entries: [],
		totals: {
			total_target_bytes: 720_000_000,
			audio_bytes: 50_000_000,
			subtitle_bytes: 1_000_000,
			attachment_bytes: 0,
			container_bytes: 1_000_000,
			non_video_bytes: 52_000_000,
			minimum_non_video_bytes: 52_000_000,
			maximum_non_video_bytes: 52_000_000,
			remaining_video_bytes: 668_000_000,
			remaining_video_bitrate_bps: 824_691
		},
		source_relative_cap: {
			configured_total_percent: 80,
			total_cap_bytes: 2_240_000_000,
			video_cap_bytes: 2_188_000_000,
			video_cap_bitrate_bps: 2_701_234,
			video_cap_percent: 78,
			status: 'available'
		},
		feasibility: {
			status: 'feasible',
			reasons: [],
			arithmetic_infeasible: false,
			aggressive: false,
			requires_measurement: false
		},
		uncertainty: {
			confidence: 'high',
			requires_measurement: false,
			minimum_non_video_bytes: 52_000_000,
			maximum_non_video_bytes: 52_000_000
		}
	};
}

function plannedStream(
	kind: PlannedStreamPayload['kind'],
	action: PlannedStreamPayload['action'],
	sourceIndex: number
): PlannedStreamPayload {
	return {
		kind,
		source_index: sourceIndex,
		source_codec: kind === 'audio' ? 'aac' : 'subrip',
		action,
		output_codec: action === 'drop' ? null : kind === 'audio' ? 'opus' : 'subrip',
		codec_argument: null,
		output_bitrate_bps: null,
		output_bitrate_text: null,
		channels: kind === 'audio' ? 6 : null,
		language: 'eng',
		default: sourceIndex === 0,
		forced: false,
		source_bitrate_bps: null,
		source_duration_seconds: 6480,
		source_size_bytes: null,
		file_name: null,
		mime_type: null
	};
}

function goalRow(view: ReturnType<typeof movieGoalContractView>, label: string) {
	for (const row of view.rows) {
		if (row.label === label) return row;
	}
	return undefined;
}

describe('formatMovieBytes', () =>
	it('uses the same decimal precision across movie facts and member rows', () => {
		expect(formatMovieBytes(11_800_000_000)).toBe('11.8 GB');
		expect(formatMovieBytes(400_000_000)).toBe('400 MB');
	}));

describe('canRetrySampleJob', () => {
	it('allows retry when the failed sample has no replacement proposal', () =>
		expect(canRetrySampleJob('sample-job-1', false)).toBe(true));

	it('suppresses retry after a replacement proposal is prepared', () =>
		expect(canRetrySampleJob('sample-job-1', true)).toBe(false));

	it('requires a retryable sample job identifier', () => {
		expect(canRetrySampleJob('', false)).toBe(false);
		expect(canRetrySampleJob(null, false)).toBe(false);
	});
});

describe('movie queue action responses', () => {
	it('does not call a nonqueueable sample setup ready', () => {
		expect(movieSampleSetupResult(true, 'Sample setup is ready.')).toEqual({
			message: 'The sample setup needs another request. Nothing was queued.',
			attention: true,
			attentionTitle: 'Sample setup needs attention.'
		});
		expect(movieSampleSetupResult(false, 'Sample setup is ready.')).toEqual({
			message: 'Sample setup is ready.',
			attention: false
		});
	});

	it('does not claim a retry when no work was queued', () => {
		expect(
			movieRetryResponseCopy(
				0,
				'Choose a fresh size or compression goal for movies/title before retrying.',
				'movie title'
			)
		).toEqual({
			message: 'Choose a fresh size or compression goal before trying again.',
			attention: true
		});
		expect(movieRetryResponseCopy(1, 'Retried one folder.', 'movie file')).toEqual({
			message: 'This movie file is waiting to try compression again.',
			attention: false
		});
	});

	it('keeps an already-idle sample queue neutral', () => {
		expect(sampleStopResponseCopy('Calibration queue was already idle.')).toBe(
			'Sample work was already stopped.'
		);
		expect(sampleStopResponseCopy('Stopped and cleaned the calibration queue.')).toBe(
			'Stopped waiting and running sample work.'
		);
	});
});

describe('movieSizeCapBlockView', () => {
	it('uses the configured cap instead of assuming 80 percent', () =>
		expect(
			movieSizeCapBlockView(
				{ primary_lane: 'blocked', detail: 'Target size exceeds the 75% source cap.' },
				{
					size_goal: { target_size_bytes: 800_000_000 },
					source_relative_cap: {
						configured_total_percent: 75,
						total_cap_bytes: 750_000_000,
						status: 'available'
					},
					feasibility: { reasons: [] }
				}
			)
		).toEqual({
			blocked: true,
			headline: 'The requested size is larger than the allowed 75% of the original file.',
			remedy:
				'Choose a smaller target in library settings. Then Mediaforce can prepare a valid movie plan.'
		}));

	it('gives a different remedy when preserved content consumes the cap', () =>
		expect(
			movieSizeCapBlockView(
				{
					primary_lane: 'blocked',
					detail: 'Preserved streams consume the 75% source cap.'
				},
				{
					size_goal: { target_size_bytes: 800_000_000 },
					source_relative_cap: {
						configured_total_percent: 75,
						total_cap_bytes: 750_000_000,
						status: 'arithmetically_infeasible'
					},
					feasibility: { reasons: ['source_relative_cap_consumed_by_non_video_budget'] }
				}
			)
		).toEqual({
			blocked: true,
			headline:
				'The sound, subtitles, and other content being kept already use the allowed movie size.',
			remedy: 'Review the movie settings and either keep less content or allow a larger output.'
		}));

	it('does not treat unrelated workflow blockers as size-cap failures', () =>
		expect(
			movieSizeCapBlockView(
				{ primary_lane: 'blocked', detail: 'A destination file already exists.' },
				{
					source_relative_cap: {
						configured_total_percent: 75,
						total_cap_bytes: 750_000_000,
						status: 'arithmetically_infeasible'
					},
					feasibility: { reasons: [] }
				}
			)
		).toEqual({ blocked: false, headline: '', remedy: '' }));
});

describe('parentSampleAppliesToExactItem', () => {
	it('recognizes a parent sample prepared from the exact file', () =>
		expect(
			parentSampleAppliesToExactItem('movies/title/movie.mkv', null, {
				job_id: 'sample-job-1',
				prefix: 'movies/title',
				status: 'completed',
				sample_item: { rel_path: 'movies/title/movie.mkv' }
			})
		).toBe(true));

	it('does not replace an exact-file sample job', () =>
		expect(
			parentSampleAppliesToExactItem(
				'movies/title/movie.mkv',
				{ job_id: 'exact-job', status: 'completed' },
				{
					job_id: 'title-job',
					prefix: 'movies/title',
					status: 'completed',
					sample_item: { rel_path: 'movies/title/movie.mkv' }
				}
			)
		).toBe(false));

	it('rejects a parent sample prepared from another file', () =>
		expect(
			parentSampleAppliesToExactItem('movies/title/extra.mkv', null, {
				job_id: 'title-job',
				prefix: 'movies/title',
				status: 'completed',
				sample_item: { rel_path: 'movies/title/movie.mkv' }
			})
		).toBe(false));
});

describe('movieReviewStatusLabel', () => {
	it('uses operator-facing copy for approval-ready and missing samples', () => {
		expect(movieReviewStatusLabel('needs_approval')).toBe('Ready to review');
		expect(movieReviewStatusLabel('missing_sample')).toBe('Needs sample');
		expect(movieReviewStatusLabel('accepted')).toBe('Sample approved');
	});

	it('directs inherited samples back to the title workspace', () =>
		expect(movieReviewStatusLabel('missing_sample', true)).toBe('Review at title level'));
});

describe('movieCurrentWorkView', () => {
	it('explains a paused queue with no available computer', () => {
		const view = movieCurrentWorkView(
			{
				job_id: 'encode-1',
				prefix: 'movies/title',
				status: 'queued',
				queue_position: 1,
				queue_depth: 1
			},
			{ is_paused: true, stop_requested: false },
			0
		);

		expect(view).toMatchObject({
			headline: 'Queued, but not able to start',
			detail: 'Clear the conditions below and Mediaforce starts this movie automatically.',
			queuePosition: '1 of 1',
			worker: 'Not assigned',
			availableWorkers: 'None ready',
			blockers: ['The compression queue is paused.', 'No computer is ready for compression.']
		});
		expect(view?.elapsed).toBeUndefined();
		expect(view?.speed).toBeUndefined();
		expect(view?.eta).toBeUndefined();
		expect(view?.nextCondition).toBe(
			'This movie starts automatically after the global compression queue is resumed and a computer becomes available.'
		);
	});

	it('blocks queued work while the compression queue is stopping', () => {
		const view = movieCurrentWorkView(
			{
				job_id: 'encode-1',
				prefix: 'movies/title',
				status: 'queued',
				queue_position: 1,
				queue_depth: 2
			},
			{ is_paused: false, stop_requested: true },
			1
		);

		expect(view).toMatchObject({
			label: 'Queue stopping',
			headline: 'Queued, but not able to start',
			detail: 'Clear the conditions below and Mediaforce starts this movie automatically.',
			blockers: ['The compression queue is stopping and will not start new work.'],
			nextCondition:
				'This movie starts automatically after the global compression queue is resumed.'
		});
	});

	it('shows a queued movie with no known blockers', () => {
		const view = movieCurrentWorkView(
			{
				job_id: 'encode-1',
				prefix: 'movies/title',
				status: 'queued',
				queue_position: 2,
				queue_depth: 4,
				scheduler_status_copy: 'Ready when a worker is free.'
			},
			{ is_paused: false, stop_requested: false },
			1
		);

		expect(view).toMatchObject({
			headline: 'Queued 2 of 4',
			detail: 'Ready when a computer is free.',
			blockers: [],
			queuePosition: '2 of 4',
			eta: undefined
		});
	});

	it('translates backend encode-host copy to the computer vocabulary', () => {
		const view = movieCurrentWorkView(
			{
				job_id: 'encode-1',
				prefix: 'movies/title',
				status: 'queued',
				scheduler_status_copy: 'Waiting for an available encode host.'
			},
			{ is_paused: false, stop_requested: false },
			1
		);

		expect(view?.detail).toBe('Waiting for an available computer.');
	});

	it('treats a queued host as preferred rather than assigned', () => {
		const view = movieCurrentWorkView(
			{
				job_id: 'encode-1',
				prefix: 'movies/title',
				status: 'queued',
				host: { key: 'studio-mac', label: 'Studio Mac' }
			},
			{ is_paused: false, stop_requested: false },
			1
		);

		expect(view).toMatchObject({
			worker: 'Not assigned',
			preferredWorker: 'Studio Mac',
			queuePosition: 'Not available'
		});
	});

	it('shows running progress, computer, speed, elapsed time, and ETA', () => {
		const view = movieCurrentWorkView(
			{
				job_id: 'encode-1',
				prefix: 'movies/title',
				status: 'running',
				started_at: '2026-08-15T12:00:00Z',
				progress: {
					percent_complete: 42.4,
					phase_label: 'Encoding',
					current_item_rel_path: 'movies/title/movie.mkv',
					active_host_labels: ['Studio Mac'],
					speed: 1.25,
					eta_copy: '24m'
				}
			},
			{ is_paused: false, stop_requested: false },
			1,
			Date.parse('2026-08-15T12:05:30Z')
		);

		expect(view).toMatchObject({
			headline: 'Compressing movie.mkv',
			detail: 'Compressing',
			percentComplete: 42.4,
			worker: 'Studio Mac',
			availableWorkers: '1 ready',
			elapsed: '5m 30s',
			speed: '1.25× realtime',
			eta: '24m'
		});
	});

	it('does not borrow the preferred computer when a running computer is not reported', () => {
		const view = movieCurrentWorkView(
			{
				job_id: 'encode-1',
				prefix: 'movies/title',
				status: 'running',
				host: { label: 'Preferred Mac' },
				progress: { percent_complete: 1 }
			},
			{ is_paused: false, stop_requested: false },
			0
		);

		expect(view?.worker).toBe('Not reported');
	});

	it('parses naive SQLite timestamps as UTC', () => {
		const view = movieCurrentWorkView(
			{
				job_id: 'encode-1',
				prefix: 'movies/title',
				status: 'running',
				started_at: '2026-08-15 12:00:00',
				progress: { percent_complete: 10 }
			},
			{ is_paused: false, stop_requested: false },
			1,
			Date.parse('2026-08-15T12:05:30Z')
		);

		expect(view?.elapsed).toBe('5m 30s');
	});

	it('clamps small future clock skew to zero elapsed time', () => {
		const view = movieCurrentWorkView(
			{
				job_id: 'encode-1',
				prefix: 'movies/title',
				status: 'running',
				started_at: '2026-08-15T12:00:30Z'
			},
			{ is_paused: false, stop_requested: false },
			1,
			Date.parse('2026-08-15T12:00:00Z')
		);

		expect(view?.elapsed).toBe('0s');
	});

	it('normalizes malformed running telemetry', () => {
		const view = movieCurrentWorkView(
			{
				job_id: 'encode-1',
				prefix: 'movies/title',
				status: 'running',
				progress: {
					percent_complete: Number.NaN,
					current_item_rel_path: '   '
				}
			},
			{ is_paused: false, stop_requested: false },
			1
		);

		expect(view).toMatchObject({
			percentComplete: 0,
			worker: 'Not reported',
			currentItem: null
		});
	});

	it('ignores non-active encode jobs', () =>
		expect(
			movieCurrentWorkView(
				{ job_id: 'encode-1', prefix: 'movies/title', status: 'completed' },
				{ is_paused: false, stop_requested: false },
				1
			)
		).toBeNull());
});

describe('parseServerTimestamp', () => {
	it('normalizes UTC, offset, and naive timestamps', () => {
		const expected = Date.parse('2026-08-15T12:00:00Z');
		expect(parseServerTimestamp('2026-08-15T12:00:00Z')).toBe(expected);
		expect(parseServerTimestamp('2026-08-15T12:00:00+00:00')).toBe(expected);
		expect(parseServerTimestamp('2026-08-15 12:00:00')).toBe(expected);
	});

	it('rejects missing and invalid timestamps', () => {
		expect(parseServerTimestamp('')).toBeNull();
		expect(parseServerTimestamp('not-a-date')).toBeNull();
	});
});

describe('movieGoalFactsView', () => {
	it('formats duration, expected output, savings, and final target range', () =>
		expect(
			movieGoalFactsView(6454.857, 2_824_183_089, {
				schema_version: 1,
				mode: 'absolute',
				source: 'profile',
				status: 'resolved',
				requires_confirmation: false,
				target_size_bytes: 717_206_333,
				sample_projection_tolerance_percent: 8,
				final_output_tolerance_percent: 5,
				final_lower_bound_bytes: 681_346_016,
				final_upper_bound_bytes: 753_066_650,
				rationale: 'Runtime-adjusted movie target.'
			})
		).toEqual({
			duration: '1h 47m 35s',
			sourceSize: '2.82 GB',
			expectedOutput: '717 MB',
			expectedSavings: '2.11 GB · 75%',
			targetRange: '681 MB–753 MB',
			estimateQuality: 'Planning range, not a guarantee'
		}));

	it('does not claim savings when the target is not smaller', () =>
		expect(
			movieGoalFactsView(3600, 1_000_000_000, {
				schema_version: 1,
				mode: 'absolute',
				source: 'profile',
				status: 'resolved',
				requires_confirmation: false,
				target_size_bytes: 1_000_000_000,
				sample_projection_tolerance_percent: 8,
				final_output_tolerance_percent: 5,
				rationale: 'No reduction.'
			}).expectedSavings
		).toBe('No size reduction planned'));
});

describe('movieGoalContractView', () => {
	it('explains normalized goals, preserved resolution, quality guardrail, and stream actions', () => {
		const view = movieGoalContractView(
			resolvedMovieIntent(),
			streamBudgetLedger([
				plannedStream('audio', 'copy', 0),
				plannedStream('audio', 'transcode', 1),
				plannedStream('subtitle', 'copy', 2),
				plannedStream('subtitle', 'drop', 3)
			]),
			{ width: 1920, height: 818 },
			{
				pre_test_instruction: {
					focus_labels: ['Motion breakup', 'Banding / dark-scene damage']
				}
			}
		);

		expect(view.status).toBe('ready');
		expect(view.rows).toEqual(
			expect.arrayContaining([
				expect.objectContaining({
					label: 'Size rule',
					value: 'Runtime-scaled target',
					detail: '300 MB per 45 minutes, scaled to this movie.',
					provenance: 'Mediaforce default'
				}),
				expect.objectContaining({
					label: 'Resolution',
					value: 'Preserve 1920×818',
					detail: 'Keep the source resolution; no downscale is planned.'
				}),
				expect.objectContaining({
					label: 'Quality floor',
					value: 'VMAF floor 80',
					detail: 'Aim for 85; the floor is an acceptance guardrail, not a guarantee.'
				}),
				expect.objectContaining({
					label: 'Audio',
					value: 'Keep 2 tracks',
					detail: expect.stringContaining('1 copied, 1 converted')
				}),
				expect.objectContaining({
					label: 'Subtitles',
					value: 'Keep 1 subtitle',
					detail: expect.stringContaining('1 copied, 1 dropped')
				})
			])
		);
		expect(view.findings).toEqual([
			{ kind: 'Advisory', label: 'Motion breakup' },
			{ kind: 'Advisory', label: 'Banding / dark-scene damage' }
		]);
	});

	it('distinguishes downscaling from a source already within the cap', () => {
		const downscaleIntent = resolvedMovieIntent({
			resolution: {
				mode: 'max_height',
				max_height: 1080,
				source: 'operator_request',
				requires_confirmation: false,
				rationale: 'Limit height.'
			}
		});
		const downscale = movieGoalContractView(
			downscaleIntent,
			streamBudgetLedger([]),
			{ width: 3840, height: 2160 },
			null
		);
		const preserve = movieGoalContractView(
			downscaleIntent,
			streamBudgetLedger([]),
			{ width: 1920, height: 818 },
			null
		);

		expect(goalRow(downscale, 'Resolution')).toMatchObject({
			value: 'Cap at 1080p',
			detail: 'Downscale the 3840×2160 source to fit the height cap.',
			provenance: 'You set this'
		});
		expect(goalRow(preserve, 'Resolution')).toMatchObject({
			value: 'Preserve 1920×818',
			detail: 'The source is already within the 1080p cap.'
		});
	});

	it('supports an absolute target, XPSNR floor, and unconfirmed compression choice', () => {
		const intent = resolvedMovieIntent({
			requires_confirmation: true,
			size_goal: {
				...resolvedMovieIntent().size_goal,
				mode: 'absolute',
				source: 'folder_override',
				reference_runtime_minutes: null
			},
			quality: {
				metric: 'xpsnr',
				target_xpsnr: 42,
				min_target_xpsnr: 40,
				source: 'folder_override'
			},
			compression_intent: {
				...resolvedMovieIntent().compression_intent!,
				level: 'legacy_unconfirmed',
				confirmed: false,
				requires_confirmation: true,
				source: 'legacy_snapshot'
			}
		});
		const view = movieGoalContractView(intent, streamBudgetLedger([]), {}, null);

		expect(goalRow(view, 'Size rule')).toMatchObject({
			value: 'Fixed target',
			detail: 'Use one fixed size target for this movie.',
			provenance: 'Library setting'
		});
		expect(goalRow(view, 'Quality floor')).toMatchObject({
			value: 'XPSNR floor 40',
			detail: 'Aim for 42; the floor is an acceptance guardrail, not a guarantee.'
		});
		expect(goalRow(view, 'Compression')).toMatchObject({
			value: 'Needs your choice',
			provenance: 'Needs your choice',
			tone: 'attention'
		});
	});

	it('uses the resolved metric for automatic quality guardrails', () => {
		const intent = resolvedMovieIntent({
			quality: {
				metric: 'auto',
				target_vmaf: 85,
				min_target_vmaf: 80,
				target_xpsnr: 41,
				min_target_xpsnr: 35,
				source: 'config_default'
			}
		});
		const resolved = movieGoalContractView(intent, streamBudgetLedger([]), {}, null, 'XPSNR');
		const unresolved = movieGoalContractView(intent, streamBudgetLedger([]), {}, null);

		expect(goalRow(resolved, 'Quality floor')).toMatchObject({
			value: 'XPSNR floor 35',
			detail: 'Aim for 41; the floor is an acceptance guardrail, not a guarantee.'
		});
		expect(goalRow(unresolved, 'Quality floor')).toMatchObject({
			value: 'Automatic metric guardrail',
			detail:
				'Use VMAF when available, otherwise XPSNR; Mediaforce resolves the active floor before sampling.'
		});
	});

	it('separates measured, advisory, and stale review evidence', () => {
		const view = movieGoalContractView(
			resolvedMovieIntent(),
			streamBudgetLedger([]),
			{
				media_fingerprint_decision: {
					status: 'measured',
					findings: [
						{ id: 'high_motion', confidence: 0.95 },
						{ id: 'dark_gradient_banding_risk', confidence: 0.62, advisory: true }
					]
				}
			},
			{
				typed_risks: [
					{
						tag: 'audio_quality_layout',
						label: 'Audio quality / layout',
						level: 'medium',
						rationale: 'Confirm the surround layout.',
						evidence_ids: ['evidence-id-must-not-display']
					},
					{
						tag: 'grain_noise_treatment',
						label: 'Grain / noise treatment',
						level: 'low',
						rationale: 'Possible grain needs a visual check.'
					}
				],
				blocking_reasons: ['The target-size search trace uses stale cadence evidence.']
			}
		);

		expect(view.findings).toEqual([
			{ kind: 'Measured', label: 'Motion breakup', detail: undefined },
			{ kind: 'Advisory', label: 'Banding / dark-scene damage', detail: undefined },
			{
				kind: 'Measured',
				label: 'Audio quality / layout',
				detail: 'Confirm the surround layout.'
			},
			{
				kind: 'Advisory',
				label: 'Grain / noise treatment',
				detail: 'Possible grain needs a visual check.'
			},
			{ kind: 'Out of date', label: 'Earlier evidence no longer matches this source' }
		]);
		expect(JSON.stringify(view)).not.toMatch(
			/semantic-id|snapshot-id|ledger-id|policy-hash|evidence-id/i
		);
	});

	it('maps every fingerprint producer id to operator-facing copy', () => {
		const producerIds = [
			'dark_luma',
			'dark_gradient_banding_risk',
			'high_motion',
			'high_texture',
			'duplicate_cadence',
			'animation_cues',
			'likely_film_grain',
			'likely_analog_noise',
			'compression_noise_advisory',
			'uncertain_noise_mix',
			'audio_complexity'
		];
		const view = movieGoalContractView(
			resolvedMovieIntent(),
			streamBudgetLedger([]),
			{
				media_fingerprint_decision: {
					status: 'measured',
					findings: producerIds.map((id) => ({ id, confidence: 0.9 }))
				}
			},
			null
		);
		const copy = JSON.stringify(view.findings);

		for (const id of producerIds) expect(copy).not.toContain(id);
		expect(view.findings.length).toBeGreaterThan(0);
	});

	it('uses one resolving state when operator intent is missing', () =>
		expect(movieGoalContractView(null, streamBudgetLedger([]), {}, null)).toEqual({
			status: 'resolving',
			rows: [],
			findings: []
		}));
});
