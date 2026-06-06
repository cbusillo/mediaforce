import { describe, expect, it } from 'vitest';
import type { EncodeQueueJob, FolderPayload, FolderStatusPayload } from '$lib/api/types';
import type { FolderCalibrationJob } from '$lib/folders/studio';
import {
	buildBenchHostOptions,
	buildBudgetEnforcementView,
	buildDecisionFacts,
	buildOutputReviewRows,
	buildReviewWorkspaceView,
	buildSampleFacts,
	buildSampleVerdict,
	buildWorkflowSteps,
	predictedFolderSizeBytes,
	projectedReclaimBytes,
	resolveBenchRequestState,
	resolveQueueSubmissionMode,
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

function workflowState(
	overrides: Partial<NonNullable<FolderPayload['workflow_state']>> = {}
): NonNullable<FolderPayload['workflow_state']> {
	return {
		prefix: 'tv/Example/Season 1',
		state: 'ready_to_validate',
		primary_lane: 'validate',
		label: 'Ready to validate',
		tone: 'ready',
		detail: '2 encoded output(s) need validation.',
		counts: {
			items: 2,
			encode_candidates: 0,
			ready_to_validate: 2,
			ready_to_promote: 0,
			processing: 0,
			complete: 0,
			blocked: 0
		},
		lane_counts: {},
		state_counts: {},
		next_action: {
			kind: 'validate_outputs',
			label: 'Validate outputs',
			enabled: true,
			target_prefix: 'tv/Example/Season 1'
		},
		blockers: [],
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
			{
				key: 'sample-host',
				label: 'Sample host',
				detail: 'folder match',
				available: true,
				scheduleOpen: null,
				state: 'Ready for samples'
			},
			{
				key: 'offline',
				label: 'Offline host',
				detail: 'missing media',
				available: false,
				scheduleOpen: null,
				state: 'Unavailable'
			}
		]);
		expect(buildBenchHostOptions(undefined)).toEqual([]);
	});

	it('marks off-schedule workers as usable for samples but not encode-ready', () => {
		const options = buildBenchHostOptions([
			{ key: 'm4', label: 'M4', available: true, schedule_open: false }
		]);

		expect(options).toEqual([
			{
				key: 'm4',
				label: 'M4',
				detail: '',
				available: true,
				scheduleOpen: false,
				state: 'Sample ok, encode later'
			}
		]);
		expect(resolveBenchRequestState('try 300MB', 'm4', options, null, false)).toMatchObject({
			disabled: false,
			blocker: ''
		});
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

	it('uses output validation workspace copy for validate workflows', () => {
		const workflow = resolveWorkflow(
			folderPayload({
				workflow_state: {
					prefix: 'tv/Terminator',
					state: 'mixed',
					primary_lane: 'validate',
					label: 'Mixed work',
					tone: 'ready',
					detail: '22 to validate, 9 to encode',
					counts: {
						items: 31,
						ready_to_validate: 22,
						encode_candidates: 9,
						ready_to_promote: 0,
						processing: 0,
						complete: 0,
						blocked: 0
					},
					lane_counts: { validate: 22, encode: 9, promote: 0 },
					state_counts: { ready_to_validate: 22, encode_candidate: 9, ready_to_promote: 0 },
					blockers: [],
					next_action: {
						kind: 'validate_outputs',
						label: 'Validate ready outputs',
						enabled: true,
						target_prefix: 'tv/Terminator'
					}
				}
			}),
			folderStatusPayload(),
			null,
			null,
			null,
			null,
			null
		);
		const workspace = buildReviewWorkspaceView(
			folderPayload({
				summary: folderSummary({ item_count: 31 }),
				workflow_state: {
					prefix: 'tv/Terminator',
					state: 'mixed',
					primary_lane: 'validate',
					label: 'Mixed work',
					tone: 'ready',
					detail: '22 to validate, 9 to encode',
					counts: {
						items: 31,
						ready_to_validate: 22,
						encode_candidates: 9,
						ready_to_promote: 0,
						processing: 0,
						complete: 0,
						blocked: 0
					},
					lane_counts: { validate: 22, encode: 9, promote: 0 },
					state_counts: { ready_to_validate: 22, encode_candidate: 9, ready_to_promote: 0 },
					blockers: [],
					next_action: {
						kind: 'validate_outputs',
						label: 'Validate ready outputs',
						enabled: true,
						target_prefix: 'tv/Terminator'
					}
				}
			}),
			null,
			null,
			workflow,
			false
		);

		expect(workspace).toMatchObject({
			badge: 'Validation review',
			title: 'Output validation'
		});
		expect(workspace.rows[0]).toMatchObject({
			label: 'Ready outputs',
			source: '22 to validate',
			output: 'Validate 22 outputs'
		});
		expect(workspace.rows.map((row) => row.detail).join(' ')).not.toContain(
			'representative sample'
		);
	});

	it('lets the operator approve an over-budget sample when the preview looks good', () => {
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
			resolveWorkflow(folder, folderStatusPayload(), calibration, null, null, null, null, true)
		).toMatchObject({
			label: 'Target missed',
			title: 'Approve this size or revise smaller',
			primary: 'Approve anyway and queue',
			primaryAction: 'approve-size-tradeoff',
			secondary: 'Revise smaller',
			secondaryAction: 'revise-smaller',
			revisionPrompt:
				'Revise this sample smaller toward 300 MB per episode. The last sample was 766 MiB · 2.6x target; keep the review quality as high as possible, but make the next sample materially smaller.'
		});
	});

	it('waits for review media before allowing an over-budget approval', () => {
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
				},
				run_verdict: { outcome: 'poor_fit' }
			}
		} as FolderCalibrationState;

		expect(
			resolveWorkflow(
				folderPayload({ calibration, sample_item: { rel_path: 'tv/show/e01.mkv' } }),
				folderStatusPayload(),
				calibration,
				null,
				null,
				null,
				null,
				false
			)
		).toMatchObject({
			label: 'Review pending',
			primary: 'Wait for review media',
			secondary: 'Revise smaller',
			secondaryAction: 'revise-smaller',
			revisionPrompt:
				'Revise this sample smaller toward 300 MB per episode. The last sample was 766 MiB · 2.6x target; keep the review quality as high as possible, but make the next sample materially smaller.'
		});
		expect(
			resolveWorkflowActionState('approve-size-tradeoff', {
				reviewPackReady: false,
				approvalReviewReady: false,
				pendingProposal: null,
				calibrationJob: null
			})
		).toEqual({ disabled: true, title: 'Review media is not ready yet.' });
	});

	it('keeps stale review packs downloadable without enabling approval', () => {
		const calibration = {
			browser_review_ready: true,
			review_media_ready: false,
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
							label: 'Old contact sheet',
							image_url: '/review-media/old.png'
						}
					]
				}
			}
		} as FolderCalibrationState;
		const folder = folderPayload({
			calibration,
			sample_item: { rel_path: 'tv/show/e01.mkv' }
		});

		expect(
			resolveWorkflow(folder, folderStatusPayload(), calibration, null, null, null, null, true)
		).toMatchObject({
			label: 'Review pending',
			primary: 'Wait for review media',
			primaryAction: 'monitor-review',
			secondary: 'Revise smaller'
		});
		expect(
			resolveWorkflow(
				folder,
				folderStatusPayload(),
				calibration,
				null,
				null,
				null,
				null,
				true,
				false
			)
		).toMatchObject({
			label: 'Review pending',
			primaryAction: 'monitor-review'
		});
		expect(
			resolveWorkflowActionState('approve-size-tradeoff', {
				reviewPackReady: true,
				approvalReviewReady: false,
				pendingProposal: null,
				calibrationJob: null
			})
		).toEqual({ disabled: true, title: 'Review media is not ready yet.' });
		expect(
			resolveWorkflowActionState('download-review-pack', {
				reviewPackReady: true,
				approvalReviewReady: false,
				pendingProposal: null,
				calibrationJob: null
			})
		).toEqual({ disabled: false, title: '' });
	});

	it('queues already approved folders even after review media goes stale', () => {
		expect(
			resolveWorkflowActionState('queue-encode', {
				reviewPackReady: true,
				approvalReviewReady: false,
				approvedProfileReady: true,
				pendingProposal: null,
				calibrationJob: null
			})
		).toEqual({ disabled: false, title: '' });
		expect(resolveQueueSubmissionMode('queue-encode', { status: 'accepted' })).toBe(
			'queue-approved'
		);
	});

	it('routes first-time queue actions through profile approval', () => {
		expect(resolveQueueSubmissionMode('queue-encode', null)).toBe('approve-profile');
		expect(resolveQueueSubmissionMode('approve-size-tradeoff', null)).toBe('approve-profile');
		expect(resolveQueueSubmissionMode('open-ops', null)).toBeNull();
	});

	it('makes approved folders queue-first and keeps review media secondary', () => {
		const workflow = resolveWorkflow(
			folderPayload(),
			folderStatusPayload(),
			null,
			null,
			{ status: 'accepted', message: 'Sample accepted.' },
			null,
			null,
			true,
			false
		);

		expect(workflow).toMatchObject({
			label: 'Approved',
			title: 'Queue the approved folder',
			primary: 'Queue encode',
			primaryAction: 'queue-encode',
			secondary: 'Download pack',
			secondaryAction: 'download-review-pack'
		});
	});

	it('routes approved folders with no encode candidates to the whole show', () => {
		const workflow = resolveWorkflow(
			folderPayload({
				encode_candidate_count: 0,
				series_context: { prefix: 'tv/Example', title: 'Example' }
			}),
			folderStatusPayload(),
			null,
			null,
			{ status: 'accepted', message: 'Sample accepted.' },
			null,
			null,
			true,
			false
		);

		expect(workflow).toMatchObject({
			label: 'Approved',
			title: 'Approved folder has no queueable items',
			primary: 'Open whole show',
			primaryAction: 'open-series',
			secondary: 'Download pack',
			secondaryAction: 'download-review-pack'
		});
	});

	it('prefers backend workflow state for validation-ready folders', () => {
		const workflow = resolveWorkflow(
			folderPayload({ workflow_state: workflowState(), encode_candidate_count: 0 }),
			folderStatusPayload(),
			null,
			null,
			{ status: 'accepted', message: 'Sample accepted.' },
			null,
			null,
			true,
			false
		);

		expect(workflow).toMatchObject({
			label: 'Ready to validate',
			title: 'Validate outputs',
			primary: 'Validate outputs',
			primaryAction: 'validate-outputs',
			copy: '2 encoded output(s) need validation.'
		});
		expect(
			resolveWorkflowActionState('validate-outputs', {
				reviewPackReady: false,
				pendingProposal: null,
				calibrationJob: null
			})
		).toEqual({ disabled: false, title: '' });
	});

	it('prefers backend workflow state for promotion-ready folders', () => {
		const workflow = resolveWorkflow(
			folderPayload({
				workflow_state: workflowState({
					state: 'ready_to_promote',
					primary_lane: 'promote',
					label: 'Ready to promote',
					detail: '1 validated output is ready to promote.',
					next_action: {
						kind: 'promote_outputs',
						label: 'Promote outputs',
						enabled: true,
						target_prefix: 'tv/Example/Season 1'
					}
				})
			}),
			folderStatusPayload(),
			null,
			null,
			{ status: 'accepted', message: 'Sample accepted.' },
			null,
			null,
			true,
			false
		);

		expect(workflow).toMatchObject({
			label: 'Ready to promote',
			title: 'Promote outputs',
			primaryAction: 'promote-outputs',
			copy: '1 validated output is ready to promote.'
		});
	});

	it('uses backend status polling workflow state when the folder payload is stale', () => {
		const workflow = resolveWorkflow(
			folderPayload({ encode_candidate_count: 0 }),
			folderStatusPayload({ workflow_state: workflowState() }),
			null,
			null,
			{ status: 'accepted', message: 'Sample accepted.' },
			null,
			null,
			true,
			false
		);

		expect(workflow.primaryAction).toBe('validate-outputs');
	});

	it('makes processing folders monitor-first and keeps review media secondary', () => {
		const workflow = resolveWorkflow(
			folderPayload({ encode_queue_summary: '1 folder is running.' }),
			folderStatusPayload(),
			null,
			null,
			null,
			null,
			{
				job_id: 'encode-1',
				prefix: 'tv/Example/Season 1',
				status: 'running',
				telemetry_summary: '2 workers active.'
			} as EncodeQueueJob,
			true
		);

		expect(workflow).toMatchObject({
			label: 'Processing',
			title: 'Approved folder is processing',
			primary: 'Monitor processing',
			primaryAction: 'monitor-processing',
			secondary: 'Download pack',
			secondaryAction: 'download-review-pack'
		});
	});

	it('makes active sampling a sample-state action with an explicit review-media prerequisite', () => {
		const workflow = resolveWorkflow(
			folderPayload(),
			folderStatusPayload({ calibration_status: 'running' }),
			null,
			null,
			null,
			{ status: 'running' } as FolderCalibrationJob,
			null
		);

		expect(workflow).toMatchObject({
			label: 'Sampling',
			primary: 'Monitor sample',
			primaryAction: 'monitor-sample',
			secondary: 'Stop sample'
		});
		expect(workflow.copy).toMatch(/Review media is the missing prerequisite/);
		expect(buildWorkflowSteps(workflow).find((step) => step.label === 'Sample')).toMatchObject({
			current: true,
			detail: 'Representative sample is running'
		});
	});

	it('surfaces a draft warning before over-budget review evidence', () => {
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
			label: 'Check draft',
			primary: 'Download review pack',
			primaryAction: 'download-review-pack',
			secondary: 'Revise'
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
			label: 'Capped draft ready',
			title: 'Run a sample with a 7% size ceiling',
			copy: 'Applied after 2.6x target miss against 300 MB per episode.',
			primary: 'Start sample',
			primaryAction: 'start-sample'
		});
	});

	it('surfaces blocked drafts before stale target-missed sample copy', () => {
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
			proposal_id: 'blocked-draft',
			can_queue: false,
			message: 'The draft lowers VMAF based only on a soft size target.'
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
			label: 'Draft blocked',
			title: 'The draft does not match your request yet',
			copy: 'The draft lowers VMAF based only on a soft size target.',
			primary: 'Revise draft',
			primaryAction: 'revise-proposal'
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

	it('makes retry processing the primary action for stopped folder jobs', () => {
		const workflow = resolveWorkflow(
			folderPayload(),
			folderStatusPayload(),
			null,
			null,
			null,
			null,
			{
				status: 'stopped',
				attempt_summary: 'Processing was stopped while pending.'
			} as never
		);

		expect(workflow).toMatchObject({
			label: 'Processing stopped',
			title: 'Retry the stopped folder job',
			copy: 'Processing was stopped while pending.',
			primary: 'Retry processing',
			primaryAction: 'retry-encode',
			secondary: 'Open Ops',
			secondaryAction: 'open-ops'
		});
		expect(
			resolveWorkflowActionState('retry-encode', {
				reviewPackReady: false,
				pendingProposal: null,
				calibrationJob: null
			})
		).toEqual({ disabled: false, title: '' });
		expect(buildWorkflowSteps(workflow).find((step) => step.label === 'Process')).toMatchObject({
			current: true,
			detail: 'Retry the stopped folder job'
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

	it('marks size tradeoff approvals as the approve step', () => {
		const steps = buildWorkflowSteps({
			tone: 'ready',
			label: 'Target missed',
			title: 'Approve this size or revise smaller',
			copy: 'Review the tradeoff.',
			primary: 'Approve anyway and queue',
			primaryAction: 'approve-size-tradeoff',
			secondary: 'Revise smaller',
			secondaryAction: 'revise-smaller'
		});

		expect(steps.map((step) => [step.label, step.current])).toEqual([
			['Sample', false],
			['Review', false],
			['Approve', true],
			['Process', false]
		]);
	});

	it('maps mixed work workflow state to prioritized validate and encode actions', () => {
		const workflow = resolveWorkflow(
			folderPayload({
				workflow_state: workflowState({
					prefix: 'tv/Terminator',
					state: 'mixed',
					primary_lane: 'validate',
					label: 'Mixed work',
					tone: 'ready',
					detail: '22 to validate, 9 to encode',
					counts: {
						items: 31,
						ready_to_validate: 22,
						encode_candidates: 9,
						ready_to_promote: 0,
						processing: 0,
						complete: 0,
						blocked: 0
					},
					lane_counts: { validate: 22, encode: 9, promote: 0 },
					state_counts: { ready_to_validate: 22, encode_candidate: 9, ready_to_promote: 0 },
					blockers: [],
					next_action: {
						kind: 'validate_outputs',
						label: 'Validate ready outputs',
						enabled: true,
						target_prefix: 'tv/Terminator'
					}
				})
			}),
			folderStatusPayload(),
			null,
			null,
			null,
			null,
			null
		);

		expect(workflow).toMatchObject({
			tone: 'ready',
			label: 'Mixed work',
			title: 'Multiple tasks pending',
			copy: '22 to validate, 9 to encode',
			primary: 'Validate 22 outputs',
			primaryAction: 'validate-outputs',
			secondary: 'Queue 9 encodes',
			secondaryAction: 'queue-encode'
		});
		const mixedFacts = buildDecisionFacts(
			folderPayload({
				summary: folderSummary({ item_count: 31 }),
				series_context: { prefix: 'tv/Terminator', title: 'Terminator' },
				workflow_state: {
					prefix: 'tv/Terminator',
					state: 'mixed',
					primary_lane: 'validate',
					label: 'Mixed work',
					tone: 'ready',
					detail: '22 to validate, 9 to encode',
					counts: {
						items: 31,
						ready_to_validate: 22,
						encode_candidates: 9,
						ready_to_promote: 0,
						processing: 0,
						complete: 0,
						blocked: 0
					},
					lane_counts: { validate: 22, encode: 9, promote: 0 },
					state_counts: { ready_to_validate: 22, encode_candidate: 9, ready_to_promote: 0 },
					blockers: [],
					next_action: {
						kind: 'validate_outputs',
						label: 'Validate ready outputs',
						enabled: true,
						target_prefix: 'tv/Terminator'
					}
				}
			}),
			null,
			null,
			workflow
		);
		expect(mixedFacts[0]).toEqual({
			label: 'Outputs',
			value: '22 ready',
			detail: 'Ready to validate'
		});
		expect(mixedFacts[1]).toEqual({
			label: 'Scope',
			value: 'Whole show',
			detail: '31 items'
		});
		expect(mixedFacts[2]).toEqual({
			label: 'Next action',
			value: 'Validate 22 outputs',
			detail: 'Queue 9 encodes also available'
		});
		expect(buildDecisionFacts(folderPayload(), null, null, workflow)[2]).toEqual({
			label: 'Next action',
			value: 'Validate 22 outputs',
			detail: 'Queue 9 encodes also available'
		});
	});
});
