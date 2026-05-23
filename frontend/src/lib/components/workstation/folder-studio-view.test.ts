import { describe, expect, it } from 'vitest';
import type { FolderPayload, FolderStatusPayload } from '$lib/api/types';
import type { FolderCalibrationJob } from '$lib/folders/studio';
import {
	buildBenchHostOptions,
	buildBudgetEnforcementView,
	buildDecisionFacts,
	buildOutputReviewRows,
	buildSampleFacts,
	buildSampleVerdict,
	buildWorkflowSteps,
	predictedFolderSizeBytes,
	projectedReclaimBytes,
	resolveBenchRequestState,
	resolveWorkflow,
	resolveWorkflowActionState
} from './folder-studio-view';
import type { FolderCalibrationState, PendingSampleProposal } from '$lib/folders/studio';

function folderPayload(overrides: Partial<FolderPayload> = {}): FolderPayload {
	return {
		prefix: 'tv/Example/Season 1',
		pending: true,
		summary: {
			prefix: 'tv/Example/Season 1',
			item_count: 1,
			total_size_bytes: 1,
			statuses: {},
			video_codecs: {},
			audio_codecs: {},
			seasons: {},
			resolved_policy: {}
		},
		metric_support: { vmaf: true, xpsnr: false, ssim: false },
		metric_status_copy: 'VMAF available',
		...overrides
	};
}

function folderStatusPayload(overrides: Partial<FolderStatusPayload> = {}): FolderStatusPayload {
	return {
		prefix: 'tv/Example/Season 1',
		polling_active: false,
		calibration_status: 'idle',
		folder_scan_status: 'idle',
		calibration_job: null,
		folder_scan_job: null,
		...overrides
	};
}

function folderSummary(overrides: Partial<NonNullable<FolderPayload['summary']>> = {}) {
	return {
		prefix: 'tv/Example/Season 1',
		item_count: 1,
		total_size_bytes: 1,
		statuses: {},
		video_codecs: {},
		audio_codecs: {},
		seasons: {},
		resolved_policy: {},
		...overrides
	};
}

describe('Folder Studio review request mapping', () => {
	it('uses only folder-scoped host options and removes empty keys', () => {
		const options = buildBenchHostOptions([
			{ key: 'sample-host', label: 'Sample host', detail: 'folder match', available: true },
			{ key: 'offline', label: 'Offline host', message: 'missing media', available: false },
			{ key: '   ', label: 'Invalid host', available: true }
		]);

		expect(options).toEqual([
			{ key: 'sample-host', label: 'Sample host', detail: 'folder match', available: true },
			{ key: 'offline', label: 'Offline host', detail: 'missing media', available: false }
		]);
		expect(buildBenchHostOptions(undefined)).toEqual([]);
	});

	it('enables send only for a note, available host, and inactive sample job', () => {
		const options = buildBenchHostOptions([
			{ key: 'studio-mini', label: 'Studio Mini', available: true },
			{ key: 'offline', label: 'Offline', available: false }
		]);

		expect(resolveBenchRequestState('', 'studio-mini', options, null, false)).toMatchObject({
			disabled: true,
			blocker: 'Describe the review request before sending.'
		});
		expect(
			resolveBenchRequestState('try a smaller sample', 'offline', options, null, false)
		).toMatchObject({
			disabled: true,
			blocker: 'Selected worker is not available right now.'
		});
		expect(
			resolveBenchRequestState(
				'try a smaller sample',
				'studio-mini',
				options,
				{ status: 'running' } as FolderCalibrationJob,
				false
			)
		).toMatchObject({
			disabled: true,
			blocker: 'A sample job is already active for this folder.',
			activeCalibrationJob: true
		});
		expect(
			resolveBenchRequestState(
				'try a smaller sample',
				'studio-mini',
				options,
				{ status: 'failed' } as FolderCalibrationJob,
				false
			)
		).toMatchObject({ disabled: false, blocker: '', activeCalibrationJob: false });
	});

	it('enables sample confirmation only for queueable review drafts', () => {
		expect(
			resolveWorkflowActionState('focus-bench', {
				reviewPackReady: false,
				pendingProposal: null,
				calibrationJob: null
			})
		).toEqual({ disabled: false, title: '' });
		expect(
			resolveWorkflowActionState('revise-proposal', {
				reviewPackReady: false,
				pendingProposal: null,
				calibrationJob: null
			})
		).toEqual({ disabled: false, title: '' });
		expect(
			resolveWorkflowActionState('stop-sample', {
				reviewPackReady: false,
				pendingProposal: null,
				calibrationJob: { status: 'running' } as FolderCalibrationJob
			})
		).toEqual({ disabled: false, title: '' });
		expect(
			resolveWorkflowActionState('stop-sample', {
				reviewPackReady: false,
				pendingProposal: null,
				calibrationJob: null
			})
		).toEqual({ disabled: true, title: 'No sample job is running.' });

		expect(
			resolveWorkflowActionState('queue-encode', {
				reviewPackReady: true,
				pendingProposal: null,
				calibrationJob: null
			})
		).toEqual({ disabled: false, title: '' });
		expect(
			resolveWorkflowActionState('queue-encode', {
				reviewPackReady: true,
				pendingProposal: {
					proposal_id: 'draft-blocked',
					can_queue: false,
					message: 'The draft needs revision before queueing.'
				} as PendingSampleProposal,
				calibrationJob: null
			})
		).toEqual({ disabled: true, title: 'The draft needs revision before queueing.' });

		expect(
			resolveWorkflowActionState('start-sample', {
				reviewPackReady: false,
				pendingProposal: null,
				calibrationJob: null
			})
		).toMatchObject({
			disabled: true,
			title: 'Ask the review assistant for a draft before starting the sample.'
		});

		expect(
			resolveWorkflowActionState('start-sample', {
				reviewPackReady: false,
				pendingProposal: {
					proposal_id: 'draft-1',
					can_queue: false,
					message: 'Review the self-check first.'
				} as PendingSampleProposal,
				calibrationJob: null
			})
		).toMatchObject({ disabled: true, title: 'Review the self-check first.' });

		expect(
			resolveWorkflowActionState('start-sample', {
				reviewPackReady: false,
				pendingProposal: { proposal_id: 'draft-2', can_queue: true } as PendingSampleProposal,
				calibrationJob: null
			})
		).toEqual({ disabled: false, title: '' });

		expect(
			resolveWorkflowActionState('start-sample', {
				reviewPackReady: false,
				pendingProposal: { proposal_id: 'draft-3', can_queue: true } as PendingSampleProposal,
				calibrationJob: { status: 'queued' } as FolderCalibrationJob
			})
		).toMatchObject({
			disabled: true,
			title: 'A sample job is already active for this folder.'
		});
	});

	it('projects sample output across the whole folder', () => {
		const folder = folderPayload({
			summary: folderSummary({
				item_count: 22,
				total_size_bytes: 80_158_807_611
			}),
			calibration: {
				sample_result: {
					predicted_total_size_bytes: 803_322_876,
					quality_metric: 'VMAF',
					quality_score: 95.0448
				}
			}
		});

		expect(predictedFolderSizeBytes(folder)).toBe(17_673_103_272);
		expect(projectedReclaimBytes(folder)).toBe(62_485_704_339);
	});

	it('surfaces concrete output, audio, subtitle, and review evidence facts', () => {
		const calibration = {
			browser_review_ready: true,
			review_media_ready: true,
			sample_result: {
				predicted_total_size_bytes: 803_322_876,
				quality_metric: 'VMAF',
				quality_score: 95.0448
			},
			advice: {
				operator_request: {
					budget_bytes: 314_572_800,
					budget_label: '300 MB per episode'
				},
				multimodal_review_pack: {
					artifacts: [
						{
							kind: 'video_contact_sheet',
							label: 'Review moment 1',
							image_url: '/review-media/moment-1.png'
						},
						{
							kind: 'audio_spectrogram_compare',
							label: 'Primary audio compare',
							image_url: '/review-media/audio.png'
						}
					],
					audio_plan: {
						summary: 'Primary track ac3 is planned for Opus at 256k.'
					}
				}
			}
		} as FolderCalibrationState;
		const pendingProposal = {
			proposal_id: 'draft-hard-cap',
			can_queue: true,
			operator_request: {
				request_text:
					'I am okay lowering quality or downscaling if needed to actually hit the target.'
			},
			preview_policy: {
				video: {
					encoder: 'libsvtav1',
					max_height: 720,
					max_encoded_percent: 7,
					quality_metric: 'vmaf',
					target_vmaf: 89,
					min_target_vmaf: 87,
					default_grain: 0
				},
				audio: {
					convert_to_opus_codecs: ['ac3'],
					keep_languages: ['eng'],
					surround_5_1_opus_bitrate: '256k'
				},
				subtitle: { keep_languages: ['eng'], prefer_text: true, keep_forced: true }
			}
		} as PendingSampleProposal;
		const rows = buildOutputReviewRows(
			folderPayload({
				summary: folderSummary({ item_count: 22, total_size_bytes: 80_158_807_611 }),
				sample_item: {
					rel_path: 'tv/Example/Season 1/Example.S01E01.mkv',
					source_size_bytes: 4_349_049_136,
					duration_seconds: 3161.376,
					video_codec: 'hevc',
					width: 1920,
					height: 1080,
					audio_summary: [
						{ codec_name: 'ac3', channels: 6, language: 'eng', bit_rate: '640000', default: 1 }
					],
					subtitle_summary: [{ codec_name: 'hdmv_pgs_subtitle', language: 'eng' }]
				},
				item_plan: {
					video: { source_codec: 'hevc', output_codec: 'av1' },
					audio: {
						source_codec: 'ac3',
						output_codec: 'opus',
						output_bitrate: '256k',
						channels: 6,
						language: 'eng',
						action: 'convert',
						source_track_count: 1,
						kept_track_count: 1
					},
					subtitles: {
						source_track_count: 1,
						kept_track_count: 1,
						languages: ['eng'],
						codecs: ['hdmv_pgs_subtitle']
					}
				},
				calibration,
				pending_proposal: pendingProposal
			}),
			calibration,
			pendingProposal
		);

		expect(rows).toEqual(
			expect.arrayContaining([
				expect.objectContaining({
					label: 'Measured sample',
					source: 'source 4.1 GiB',
					output: '766 MiB',
					detail: 'target 300 MB per episode · 2.6x target · folder 16.5 GiB',
					tone: 'wait'
				}),
				expect.objectContaining({
					label: 'Next sample draft',
					output: 'AV1 · max 720p',
					detail: 'VMAF target 89 · floor 87 · downscale allowed by the size request · grain off'
				}),
				expect.objectContaining({
					label: 'Audio',
					source: 'AC-3 · 640 kbps · 5.1 · ENG · default',
					output: 'Primary track ac3 is planned for Opus at 256k.'
				}),
				expect.objectContaining({
					label: 'Subtitles',
					source: '1 subtitle track · ENG PGS',
					output: 'Keep 1 subtitle track'
				}),
				expect.objectContaining({
					label: 'Review media',
					source: '2 artifacts ready',
					output: 'Visible below'
				})
			])
		);
	});

	it('turns an over-budget sample into a revise-first verdict and workflow', () => {
		const calibration = {
			browser_review_ready: true,
			review_media_ready: true,
			sample_result: {
				predicted_total_size_bytes: 803_322_876,
				quality_metric: 'VMAF',
				quality_score: 95.0448
			},
			advice: {
				operator_request: {
					budget_bytes: 314_572_800,
					budget_label: '300 MB per episode',
					request_text: 'Aim for 200-300 MB per episode.'
				},
				run_verdict: {
					outcome: 'poor_fit',
					next_step: 'Run another sample with a much lower video budget or a downscale.'
				}
			}
		} as FolderCalibrationState;
		const folder = folderPayload({
			summary: folderSummary({
				item_count: 22,
				total_size_bytes: 80_158_807_611
			}),
			sample_item: {
				rel_path: 'tv/Example/Season 1/Episode.mkv',
				source_size_bytes: 4_349_049_136,
				duration_seconds: 3161.376,
				video_codec: 'hevc'
			},
			calibration
		});

		expect(buildSampleVerdict(folder, calibration)).toMatchObject({
			label: 'Target missed',
			title: '766 MiB per episode misses 300 MB per episode.',
			predictedPerItem: '766 MiB',
			predictedFolderTotal: '16.5 GiB',
			reclaim: '58.2 GiB',
			predictedBitrate: '2 Mbps',
			targetBitrate: '796 kbps',
			targetDelta: '2.6x target',
			missesTarget: true
		});

		expect(
			resolveWorkflow(folder, folderStatusPayload(), calibration, null, null, null, null)
		).toMatchObject({
			label: 'Target missed',
			primary: 'Revise sample',
			primaryAction: 'focus-bench',
			secondary: 'Download review pack'
		});
	});

	it('keeps an over-budget sample revise-first when a stale warning draft exists', () => {
		const calibration = {
			browser_review_ready: true,
			review_media_ready: true,
			sample_result: {
				predicted_total_size_bytes: 803_322_876,
				quality_metric: 'VMAF',
				quality_score: 95.0448
			},
			advice: {
				operator_request: {
					budget_bytes: 314_572_800,
					budget_label: '300 MB per episode'
				}
			}
		} as FolderCalibrationState;
		const pendingProposal = {
			proposal_id: 'stale-draft',
			can_queue: true,
			message: 'Download review pack.',
			self_check: {
				status: 'warning',
				summary: 'Usable with caution.'
			}
		} as PendingSampleProposal;

		expect(
			resolveWorkflow(
				folderPayload({
					summary: folderSummary({
						item_count: 22,
						total_size_bytes: 80_158_807_611
					}),
					calibration,
					pending_proposal: pendingProposal
				}),
				folderStatusPayload(),
				calibration,
				pendingProposal,
				null,
				null,
				null
			)
		).toMatchObject({
			label: 'Target missed',
			primary: 'Revise sample',
			primaryAction: 'focus-bench',
			secondary: 'Download review pack'
		});
	});

	it('asks for a new sample when old evidence used older quality defaults', () => {
		const calibration = {
			browser_review_ready: true,
			review_media_ready: true,
			sample_result: {
				predicted_total_size_bytes: 803_322_876,
				quality_metric: 'VMAF',
				quality_target: 95,
				quality_score: 95.0448
			},
			advice: {
				operator_request: {
					budget_bytes: 314_572_800,
					budget_label: '300 MB per episode',
					request_text: 'Aim for 200-300 MB per episode.'
				},
				run_verdict: {
					outcome: 'poor_fit'
				}
			}
		} as FolderCalibrationState;
		const folder = folderPayload({
			summary: folderSummary({
				item_count: 22,
				total_size_bytes: 80_158_807_611,
				resolved_policy: {
					video: { quality_metric: 'vmaf', target_vmaf: 85, min_target_vmaf: 80, max_height: 0 }
				}
			}),
			sample_item: {
				rel_path: 'tv/Example/Season 1/Episode.mkv',
				source_size_bytes: 4_349_049_136,
				duration_seconds: 3161.376,
				video_codec: 'hevc'
			},
			calibration
		});

		expect(buildSampleVerdict(folder, calibration)).toMatchObject({
			stalePolicy: true,
			title: '766 MiB per episode came from older settings.'
		});
		expect(
			resolveWorkflow(folder, folderStatusPayload(), calibration, null, null, null, null)
		).toMatchObject({
			label: 'New sample needed',
			title: 'Previous sample used older settings',
			primary: 'Ask for sample',
			primaryAction: 'focus-bench',
			secondary: 'Download old pack'
		});
		expect(buildDecisionFacts(folder, calibration, null)).toEqual([
			{
				label: 'Old sample',
				value: '766 MiB · 2 Mbps',
				detail: 'VMAF 95.0 · 2.6x target · old target 300 MB per episode'
			},
			{
				label: 'Current target',
				value: 'source resolution',
				detail: 'VMAF low-bitrate target 85 · floor 80'
			},
			{
				label: 'Next action',
				value: 'Run fresh sample',
				detail: 'The old evidence does not match the current defaults.'
			}
		]);
	});

	it('compares sample evidence against the live folder policy before cached summary policy', () => {
		const calibration = {
			sample_result: {
				predicted_total_size_bytes: 803_322_876,
				quality_metric: 'VMAF',
				quality_target: 95,
				quality_score: 95.0448
			}
		} as FolderCalibrationState;
		const folder = folderPayload({
			summary: folderSummary({
				resolved_policy: {
					video: { quality_metric: 'vmaf', target_vmaf: 95, min_target_vmaf: 93 }
				}
			}),
			policy: {
				video: { quality_metric: 'vmaf', target_vmaf: 85, min_target_vmaf: 80, max_height: 0 }
			},
			calibration
		});

		expect(buildSampleVerdict(folder, calibration)).toMatchObject({
			stalePolicy: true,
			title: '766 MiB per episode came from older settings.'
		});
	});

	it('shows a missed measured sample separately from the next capped sample', () => {
		const calibration = {
			browser_review_ready: true,
			review_media_ready: true,
			sample_result: {
				predicted_total_size_bytes: 803_322_876,
				quality_metric: 'VMAF',
				quality_score: 95.0448
			},
			advice: {
				operator_request: {
					budget_bytes: 314_572_800,
					budget_label: '300 MB per episode'
				}
			}
		} as FolderCalibrationState;
		const pendingProposal = {
			proposal_id: 'capped-retry-draft',
			can_queue: true,
			message: 'Queue this representative sample.',
			operator_request: { budget_label: '300 MB per episode' },
			budget_enforcement: {
				status: 'enforced_after_miss',
				size_target_analysis: { predicted_to_budget_ratio: 2.55 },
				applied_policy: { video: { max_encoded_percent: 7 } }
			}
		} as PendingSampleProposal;

		expect(
			resolveWorkflow(
				folderPayload({
					summary: folderSummary({
						item_count: 22,
						total_size_bytes: 80_158_807_611
					}),
					calibration,
					pending_proposal: pendingProposal
				}),
				folderStatusPayload(),
				calibration,
				pendingProposal,
				null,
				null,
				null
			)
		).toMatchObject({
			label: 'Target missed',
			title: '766 MiB sample missed; run capped sample next',
			copy: '766 MiB per episode against 300 MB per episode. Next sample will use a 7% size ceiling. Applied after 2.6x target miss against 300 MB per episode.',
			primary: 'Start sample',
			primaryAction: 'start-sample'
		});
	});

	it('makes acceptable review evidence approve-first', () => {
		const calibration = {
			browser_review_ready: true,
			review_media_ready: true,
			sample_result: {
				predicted_total_size_bytes: 250_000_000,
				quality_metric: 'VMAF',
				quality_score: 95.1
			},
			advice: {
				operator_request: {
					budget_bytes: 314_572_800,
					budget_label: '300 MB per episode'
				},
				run_verdict: { outcome: 'good_fit' }
			}
		} as FolderCalibrationState;

		expect(
			resolveWorkflow(
				folderPayload({ calibration }),
				folderStatusPayload(),
				calibration,
				null,
				null,
				null,
				null
			)
		).toMatchObject({
			label: 'Review ready',
			primary: 'Approve and queue',
			primaryAction: 'queue-encode',
			secondary: 'Download pack'
		});
	});

	it('keeps acceptable evidence approve-first when its queueable draft is still present', () => {
		const calibration = {
			browser_review_ready: true,
			review_media_ready: true,
			sample_result: {
				predicted_total_size_bytes: 250_000_000,
				quality_metric: 'VMAF',
				quality_score: 95.1
			},
			advice: {
				operator_request: {
					budget_bytes: 314_572_800,
					budget_label: '300 MB per episode'
				},
				run_verdict: { outcome: 'good_fit' }
			}
		} as FolderCalibrationState;
		const pendingProposal = {
			proposal_id: 'draft-with-evidence',
			can_queue: true,
			message: 'Queue this representative sample.'
		} as PendingSampleProposal;

		expect(
			resolveWorkflow(
				folderPayload({ calibration, pending_proposal: pendingProposal }),
				folderStatusPayload(),
				calibration,
				pendingProposal,
				null,
				null,
				null
			)
		).toMatchObject({
			label: 'Review ready',
			primary: 'Approve and queue',
			primaryAction: 'queue-encode',
			secondary: 'Download pack'
		});
	});

	it('does not approve stale non-queueable drafts without review evidence', () => {
		const workflow = resolveWorkflow(
			folderPayload({
				pending_proposal: {
					proposal_id: 'blocked-draft',
					can_queue: false,
					message: 'The draft needs revision before queueing.'
				}
			}),
			folderStatusPayload(),
			null,
			{
				proposal_id: 'blocked-draft',
				can_queue: false,
				message: 'The draft needs revision before queueing.'
			} as PendingSampleProposal,
			null,
			null,
			null
		);

		expect(workflow).toMatchObject({
			label: 'Not sampled',
			primary: 'Ask for draft',
			primaryAction: 'focus-bench'
		});
	});

	it('keeps review drafts on the sample confirmation action', () => {
		const workflow = resolveWorkflow(
			folderPayload({
				pending_proposal: {
					proposal_id: 'draft-2',
					can_queue: true,
					message: 'Queue this representative sample.'
				}
			}),
			folderStatusPayload(),
			null,
			{
				proposal_id: 'draft-2',
				can_queue: true,
				message: 'Queue this representative sample.'
			} as PendingSampleProposal,
			null,
			null,
			null
		);

		expect(workflow).toMatchObject({
			label: 'Draft ready',
			primary: 'Start sample',
			primaryAction: 'start-sample'
		});
	});

	it('names measured budget enforcement as the next sample action', () => {
		const pendingProposal = {
			proposal_id: 'draft-3',
			can_queue: true,
			message: 'Queue this representative sample.',
			operator_request: { budget_label: '300 MB per episode' },
			budget_enforcement: {
				status: 'enforced_after_miss',
				size_target_analysis: { predicted_to_budget_ratio: 2.55 },
				applied_policy: { video: { max_encoded_percent: 7 } }
			}
		} as PendingSampleProposal;

		expect(buildBudgetEnforcementView(pendingProposal)).toEqual({
			active: true,
			cap: '7%',
			capBytes: null,
			reason: 'Applied after 2.6x target miss against 300 MB per episode.'
		});

		const workflow = resolveWorkflow(
			folderPayload({ pending_proposal: pendingProposal }),
			folderStatusPayload(),
			null,
			pendingProposal,
			null,
			null,
			null
		);

		expect(workflow).toMatchObject({
			label: 'Capped draft ready',
			title: 'Run a sample with a 7% size ceiling',
			copy: 'Applied after 2.6x target miss against 300 MB per episode.',
			primary: 'Start sample',
			primaryAction: 'start-sample'
		});
	});

	it('summarizes the capped retry facts in the decision panel', () => {
		const calibration = {
			sample_result: {
				predicted_total_size_bytes: 803_322_876,
				quality_metric: 'VMAF',
				quality_score: 95.0448
			},
			advice: {
				operator_request: {
					budget_bytes: 314_572_800,
					budget_label: '300 MB per episode'
				}
			}
		} as FolderCalibrationState;
		const pendingProposal = {
			proposal_id: 'capped-retry-draft',
			can_queue: true,
			operator_request: { budget_label: '300 MB per episode' },
			preview_policy: {
				video: {
					encoder: 'libsvtav1',
					max_height: 720,
					max_encoded_percent: 7,
					quality_metric: 'vmaf',
					target_vmaf: 89,
					min_target_vmaf: 87,
					default_grain: 0
				}
			},
			budget_enforcement: {
				status: 'enforced_after_miss',
				size_target_analysis: { predicted_to_budget_ratio: 2.55 },
				applied_policy: { video: { max_encoded_percent: 7 } }
			}
		} as PendingSampleProposal;
		const folder = folderPayload({
			calibration,
			pending_proposal: pendingProposal,
			sample_item: {
				rel_path: 'tv/Example/Season 1/Episode.mkv',
				source_size_bytes: 4_388_646_674,
				duration_seconds: 3161.376,
				video_codec: 'h264'
			},
			summary: folderSummary({ item_count: 22, total_size_bytes: 80_158_807_611 })
		});

		expect(buildDecisionFacts(folder, calibration, pendingProposal)).toEqual([
			{
				label: 'Last sample',
				value: '766 MiB · 2 Mbps',
				detail: '2.6x target · target 300 MB per episode · target 796 kbps'
			},
			{
				label: 'Next size ceiling',
				value: '293 MiB max',
				detail: '7% of selected source · 300 MB per episode'
			},
			{
				label: 'Next video plan',
				value: 'AV1 · max 720p · 7% cap',
				detail: 'VMAF target 89 · floor 87 · downscale enforced after the measured miss · grain off'
			}
		]);
	});

	it('surfaces duration and bitrate in representative sample facts', () => {
		const facts = buildSampleFacts(
			{
				rel_path: 'tv/Example/Season 1/Episode.mkv',
				source_size_bytes: 4_349_049_136,
				duration_seconds: 3161.376,
				width: 1920,
				height: 1080,
				video_codec: 'hevc'
			},
			folderSummary({ total_size_bytes: 4_349_049_136 })
		);

		expect(facts).toEqual([
			{ label: 'File', value: 'Episode.mkv' },
			{ label: 'Runtime', value: '52m 41s' },
			{ label: 'Resolution', value: '1,920x1,080' },
			{ label: 'Source rate', value: '11 Mbps' },
			{ label: 'Codec', value: 'HEVC' },
			{ label: 'Size', value: '4.1 GiB' }
		]);
	});

	it('makes unsampled folders start with the review assistant instead of a disabled sample action', () => {
		const workflow = resolveWorkflow(
			folderPayload(),
			folderStatusPayload(),
			null,
			null,
			null,
			null,
			null
		);

		expect(workflow).toMatchObject({
			label: 'Not sampled',
			primary: 'Ask for draft',
			primaryAction: 'focus-bench'
		});
	});

	it('marks the current folder workflow step', () => {
		const steps = buildWorkflowSteps({
			tone: 'ready',
			label: 'Draft ready',
			title: 'Review draft is ready to sample',
			copy: 'Review the draft.',
			primary: 'Start sample',
			primaryAction: 'start-sample',
			secondary: 'Revise',
			secondaryAction: 'revise-proposal'
		});

		expect(steps.map((step) => [step.label, step.current])).toEqual([
			['Sample', true],
			['Review', false],
			['Approve', false],
			['Process', false]
		]);
	});
});
