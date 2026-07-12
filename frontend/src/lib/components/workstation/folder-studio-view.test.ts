import { describe, expect, it } from 'vitest';
import type {
	EncodeQueueJob,
	FolderPayload,
	FolderStatusPayload,
	HostsPayload
} from '$lib/api/types';
import type { FolderCalibrationJob } from '$lib/folders/studio';
import {
	buildBenchHostOptions,
	buildBasicEncodeGuide,
	buildBudgetEnforcementView,
	buildDecisionFacts,
	buildFooterSignals,
	buildOutputReviewRows,
	buildQualityRiskFacts,
	buildProcessingHostOptions,
	buildReviewWorkspaceView,
	buildRuntimeFacts,
	approvalStatusCopy,
	buildSampleFacts,
	buildSeasonScopeRows,
	buildSampleVerdict,
	buildStatusTiles,
	buildWorkflowSteps,
	outputScopeLabel,
	predictedFolderSizeBytes,
	projectedReclaimBytes,
	REVIEW_ASSISTANT_PENDING_COPY,
	resolveBenchRequestState,
	resolveQueueSubmissionMode,
	resolveWorkflow,
	resolveWorkflowActionState,
	sampleStatusCopy,
	summarizeOutputWorkflowPending
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

function hostsPayload(overrides: Partial<HostsPayload> = {}): HostsPayload {
	return {
		compact: true,
		hosts: [],
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
	it('keeps the browser waiting for bounded backend inference', () => {
		expect(REVIEW_ASSISTANT_PENDING_COPY).toContain('a few minutes');
		expect(REVIEW_ASSISTANT_PENDING_COPY).toContain('nothing is queued');
		expect(REVIEW_ASSISTANT_PENDING_COPY).not.toContain('30 seconds');
	});

	it('keeps pending route placeholders in a loading workflow state', () => {
		const workflow = resolveWorkflow(
			folderPayload({ pending: true, summary: undefined }),
			folderStatusPayload({ calibration_status: 'loading', folder_scan_status: 'loading' }),
			null,
			null,
			null,
			null,
			null,
			false,
			false,
			true
		);

		expect(workflow).toMatchObject({
			tone: 'active',
			label: 'Loading',
			title: 'Loading folder state',
			primary: '',
			primaryAction: 'monitor-sample',
			secondary: 'Open Ops',
			secondaryAction: 'open-ops'
		});
	});

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

	it('maps worker states for output processing capacity', () => {
		const options = buildProcessingHostOptions({
			compact: true,
			hosts: [
				{
					key: 'm4',
					label: 'M4 Studio',
					available: true,
					message: 'ready',
					missing_paths: [],
					issues: [],
					detail: null,
					capabilities: ['encode_queue'],
					priority: 1,
					max_parallel_encodes: 2,
					active_encode_count: 1,
					schedule_profile_label: 'day shift',
					schedule_detail: 'open',
					schedule_open: true,
					active_flag: 'ready',
					active_reason: 'ready',
					queue_active: true
				},
				{
					key: 'night',
					label: 'Night host',
					available: true,
					message: 'scheduled',
					missing_paths: [],
					issues: [],
					detail: null,
					capabilities: ['encode_queue'],
					priority: 2,
					max_parallel_encodes: 1,
					active_encode_count: 0,
					schedule_profile_label: 'night',
					schedule_detail: 'opens at 23:00',
					schedule_open: false,
					active_flag: 'scheduled',
					active_reason: 'outside schedule',
					queue_active: true
				},
				{
					key: 'offline',
					label: 'Offline',
					available: false,
					message: 'ssh failed',
					missing_paths: [],
					issues: [],
					detail: null,
					capabilities: ['encode_queue'],
					priority: 3,
					max_parallel_encodes: 1,
					active_encode_count: 0,
					schedule_profile_label: 'always',
					schedule_detail: 'always',
					schedule_open: true,
					active_flag: 'unavailable',
					active_reason: 'ssh failed',
					queue_active: false
				}
			]
		});

		expect(options).toEqual([
			{
				key: 'm4',
				label: 'M4 Studio',
				state: 'Busy',
				tone: 'active',
				detail: '1/2 encoding'
			},
			{
				key: 'night',
				label: 'Night host',
				state: 'Off schedule',
				tone: 'wait',
				detail: 'opens at 23:00'
			},
			{
				key: 'offline',
				label: 'Offline',
				state: 'Unavailable',
				tone: 'fail',
				detail: 'ssh failed'
			}
		]);
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
			title: 'Ask the review assistant for a sample plan before starting the sample.'
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

	it('maps sample status into basic user copy', () => {
		expect(sampleStatusCopy('queued')).toBe('Sample queued');
		expect(sampleStatusCopy('running')).toBe('Sampling');
		expect(sampleStatusCopy('pending_review')).toBe('Ready to review');
		expect(sampleStatusCopy('failed')).toBe('Sample needs retry');
		expect(sampleStatusCopy('idle')).toBe('Needs sample');
	});

	it('maps approval status into basic user copy', () => {
		expect(approvalStatusCopy('missing_sample')).toBe('Needs sample');
		expect(approvalStatusCopy('accepted')).toBe('Approved');
		expect(approvalStatusCopy('blocked')).toBe('Blocked');
		expect(approvalStatusCopy('needs_review')).toBe('Needs review');
		expect(approvalStatusCopy('pending_review')).toBe('Ready to review');
		expect(approvalStatusCopy(null)).toBe('Not reviewed');
	});

	it('builds a single-folder guide around the current next step', () => {
		const sampleWorkflow = resolveWorkflow(
			folderPayload(),
			folderStatusPayload(),
			null,
			null,
			null,
			null,
			null
		);
		expect(buildBasicEncodeGuide(sampleWorkflow)).toMatchObject({
			label: 'Single folder run',
			title: 'Ask review assistant',
			primary: 'Ask review assistant',
			action: 'focus-bench'
		});
		expect(buildBasicEncodeGuide(sampleWorkflow).steps.map((step) => step.state)).toEqual([
			'done',
			'current',
			'upcoming',
			'upcoming',
			'upcoming',
			'upcoming',
			'upcoming'
		]);
		expect(buildBasicEncodeGuide(sampleWorkflow, 'whole show')).toMatchObject({
			label: 'Whole show run',
			title: 'Ask review assistant',
			primary: 'Ask review assistant',
			action: 'focus-bench'
		});
		expect(buildBasicEncodeGuide(sampleWorkflow, 'whole show').steps).toEqual(
			expect.arrayContaining([
				expect.objectContaining({ label: 'Pick show' }),
				expect.objectContaining({ label: 'Process show' })
			])
		);

		const validateWorkflow = resolveWorkflow(
			folderPayload({ workflow_state: workflowState() }),
			folderStatusPayload(),
			null,
			null,
			null,
			null,
			null
		);
		expect(buildBasicEncodeGuide(validateWorkflow)).toMatchObject({
			title: 'Validate output',
			primary: 'Validate outputs',
			action: 'validate-outputs'
		});
		expect(buildBasicEncodeGuide(validateWorkflow).steps[4]).toMatchObject({
			label: 'Validate output',
			state: 'current'
		});

		const reviewWorkflow = {
			tone: 'ready',
			label: 'Review ready',
			title: 'Review the sample video',
			copy: 'Download the side-by-side sample before approving.',
			primary: 'Download side-by-side video',
			primaryAction: 'download-review-pack',
			secondary: 'Approve and queue',
			secondaryAction: 'queue-encode'
		} as const;
		expect(buildBasicEncodeGuide(reviewWorkflow)).toMatchObject({
			title: 'Review sample video',
			primary: 'Download side-by-side video',
			action: 'download-review-pack'
		});
		expect(buildBasicEncodeGuide(reviewWorkflow).steps[2]).toMatchObject({
			label: 'Review sample',
			state: 'current'
		});

		const completeWorkflow = resolveWorkflow(
			folderPayload({
				workflow_state: workflowState({
					state: 'complete',
					label: 'Complete',
					detail: 'All folder outputs are promoted.',
					next_action: {
						kind: 'review_scope',
						label: 'Review scope',
						enabled: true,
						target_prefix: 'tv/Example/Season 1'
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
		expect(buildBasicEncodeGuide(completeWorkflow)).toMatchObject({
			title: 'Clean up',
			primary: 'Review cleanup',
			action: 'open-completed'
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
					label: 'Next sample plan',
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
			title: 'Output validation',
			layout: 'pipeline'
		});
		expect(workspace.rows.map((row) => [row.label, row.current, row.tone])).toEqual([
			['Encode', false, 'ready'],
			['Validate', true, 'ready'],
			['Promote', false, 'idle'],
			['Complete', false, 'idle']
		]);
		expect(workspace.rows[0]).toMatchObject({
			label: 'Encode',
			source: '9 not encoded · 31 items',
			output: 'Queue 9 encodes'
		});
		expect(workspace.rows[1]).toMatchObject({
			label: 'Validate',
			source: '22 ready outputs',
			output: 'Validate 22 outputs'
		});
		expect(workspace.rows.map((row) => row.detail).join(' ')).not.toContain(
			'representative sample'
		);
	});

	it('uses completed output copy when every pipeline step is done', () => {
		const folder = folderPayload({
			summary: folderSummary({ item_count: 2 }),
			workflow_state: workflowState({
				state: 'complete',
				primary_lane: 'complete',
				label: 'Complete',
				tone: 'success',
				detail: '2 complete',
				counts: {
					items: 2,
					ready_to_validate: 0,
					encode_candidates: 0,
					ready_to_promote: 0,
					processing: 0,
					complete: 2,
					blocked: 0
				},
				next_action: {
					kind: 'none',
					label: 'Complete',
					enabled: false,
					target_prefix: 'tv/Example/Season 1'
				}
			})
		});
		const workflow = resolveWorkflow(folder, folderStatusPayload(), null, null, null, null, null);
		const workspace = buildReviewWorkspaceView(folder, null, null, workflow, false);

		expect(workspace.layout).toBe('pipeline');
		expect(workspace).toMatchObject({
			badge: 'Completed outputs',
			title: 'Pipeline complete'
		});
		expect(workspace.rows.map((row) => [row.label, row.output])).toEqual([
			['Encode', 'No encode backlog'],
			['Validate', 'Validation complete'],
			['Promote', 'Promotion complete'],
			['Complete', '2 of 2']
		]);
	});

	it('uses output workflow runtime facts instead of sample approval state', () => {
		const folder = folderPayload({
			summary: folderSummary({ item_count: 31 }),
			workflow_state: workflowState({
				state: 'mixed',
				label: 'Mixed work',
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
				next_action: {
					kind: 'validate_outputs',
					label: 'Validate ready outputs',
					enabled: true,
					target_prefix: 'tv/Example/Season 1'
				}
			}),
			encode_queue_summary: '0 running · 0 queued'
		});
		const workflow = resolveWorkflow(
			folder,
			folderStatusPayload({ folder_scan_status: 'completed' }),
			null,
			null,
			null,
			null,
			null
		);

		expect(
			buildRuntimeFacts(
				folder,
				folderStatusPayload({ folder_scan_status: 'completed' }),
				{ status: 'missing_sample' } as never,
				null,
				workflow
			)
		).toEqual([
			{ label: 'Workflow', value: 'Mixed work' },
			{ label: 'Scan', value: 'completed' },
			{ label: 'Outputs', value: '22 validate · 9 encode' },
			{ label: 'Processing', value: '0 running · 0 queued' }
		]);
		expect(buildStatusTiles(folder, folderStatusPayload(), hostsPayload(), workflow)[1]).toEqual({
			label: 'Outputs',
			value: '0 / 31 complete',
			detail: '22 to validate · 9 to encode',
			tone: 'ready'
		});
		expect(summarizeOutputWorkflowPending(folder.workflow_state?.counts)).toBe(
			'22 validate · 9 encode'
		);
		expect(buildFooterSignals(folder, folderStatusPayload(), hostsPayload(), workflow)[0]).toEqual({
			label: 'Pipeline',
			value: 'mixed work',
			tone: 'ready'
		});
	});

	it('uses fresher status workflow counts for output shell and runtime facts', () => {
		const statusWorkflow = workflowState({
			state: 'processing',
			primary_lane: 'encode',
			label: 'Processing',
			tone: 'active',
			detail: '4 outputs are processing.',
			counts: {
				items: 31,
				ready_to_validate: 0,
				encode_candidates: 5,
				ready_to_promote: 0,
				processing: 4,
				complete: 22,
				blocked: 0
			},
			next_action: {
				kind: 'monitor_encode',
				label: 'Monitor processing',
				enabled: true,
				target_prefix: 'tv/Example/Season 1'
			}
		});
		const folder = folderPayload({
			summary: folderSummary({ item_count: 31 }),
			encode_queue_summary: '4 running · 5 queued'
		});
		const status = folderStatusPayload({
			folder_scan_status: 'completed',
			workflow_state: statusWorkflow
		});
		const workflow = resolveWorkflow(folder, status, null, null, null, null, null);

		expect(buildStatusTiles(folder, status, hostsPayload(), workflow)[1]).toEqual({
			label: 'Outputs',
			value: '22 / 31 complete',
			detail: '5 to encode · 4 processing',
			tone: 'active'
		});
		expect(buildRuntimeFacts(folder, status, null, null, workflow)[2]).toEqual({
			label: 'Outputs',
			value: '5 encode · 22 complete'
		});
		expect(summarizeOutputWorkflowPending(status.workflow_state?.counts)).toBe(
			'5 encode · 4 processing'
		);
	});

	it('keeps sample workflow runtime facts focused on calibration and approval', () => {
		const workflow = resolveWorkflow(
			folderPayload({ encode_queue_summary: 'No folder job queued' }),
			folderStatusPayload({ calibration_status: 'idle', folder_scan_status: 'idle' }),
			null,
			null,
			{ status: 'missing_sample' } as never,
			null,
			null
		);

		expect(
			buildRuntimeFacts(
				folderPayload({ encode_queue_summary: 'No folder job queued' }),
				folderStatusPayload({ calibration_status: 'idle', folder_scan_status: 'idle' }),
				{ status: 'missing_sample' } as never,
				null,
				workflow
			)
		).toEqual([
			{ label: 'Sample', value: 'Needs sample' },
			{ label: 'Scan', value: 'idle' },
			{ label: 'Approval', value: 'Needs sample' },
			{ label: 'Processing', value: 'No folder job queued' }
		]);
		expect(
			buildStatusTiles(
				folderPayload(),
				folderStatusPayload({ calibration_status: 'idle' }),
				hostsPayload(),
				workflow
			)[1]
		).toEqual({
			label: 'Sample',
			value: 'Needs sample',
			detail: 'polling idle',
			tone: 'idle'
		});
		expect(
			buildFooterSignals(
				folderPayload(),
				folderStatusPayload({ calibration_status: 'idle' }),
				hostsPayload(),
				workflow
			)[0]
		).toEqual({
			label: 'Sample',
			value: 'Needs sample',
			tone: 'idle'
		});
	});

	it('keeps sample review evidence on the evidence workspace layout', () => {
		const workspace = buildReviewWorkspaceView(
			folderPayload(),
			null,
			null,
			{
				tone: 'idle',
				label: 'Not sampled',
				title: 'No representative sample yet',
				copy: 'Ask for a sample.',
				primary: 'Ask for draft',
				primaryAction: 'focus-bench',
				secondary: 'Open Ops',
				secondaryAction: 'open-ops'
			},
			false
		);

		expect(workspace).toMatchObject({
			badge: 'No review media',
			title: 'Previous sample evidence',
			layout: 'evidence'
		});
		expect(workspace.rows.map((row) => row.label)).toContain('Measured sample');
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

	it('blocks size approval when deterministic quality-risk evidence is blocked', () => {
		expect(
			resolveWorkflowActionState('approve-size-tradeoff', {
				reviewPackReady: true,
				approvalReviewReady: true,
				pendingProposal: null,
				calibrationJob: null,
				qualityRiskBlocked: true,
				qualityRiskBlocker: 'Cadence evidence is missing.'
			})
		).toEqual({ disabled: true, title: 'Cadence evidence is missing.' });
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
		const folder = folderPayload({ encode_queue_summary: '1 folder is running.' });
		const workflow = resolveWorkflow(
			folder,
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

		expect(buildDecisionFacts(folder, null, null, workflow)).toEqual([
			{
				label: 'Processing',
				value: 'Active',
				detail: '2 workers active.'
			},
			{
				label: 'Scope',
				value: 'Folder',
				detail: '1 item'
			},
			{
				label: 'Next action',
				value: 'Monitor processing',
				detail: 'Download pack also available'
			}
		]);

		const workspace = buildReviewWorkspaceView(folder, null, null, workflow, true);
		expect(workspace).toMatchObject({
			badge: 'Processing run',
			title: 'Output encoding',
			layout: 'pipeline'
		});
		expect(workspace.rows.find((row) => row.label === 'Encode')).toMatchObject({
			current: true,
			tone: 'active'
		});
	});

	it('shows queued processing waits before active processing copy', () => {
		const folder = folderPayload({
			encode_queue_summary:
				'0 running · 1 queued · this folder is 1 of 1 · waiting for a host schedule window',
			workflow_state: workflowState({
				state: 'processing',
				primary_lane: 'processing',
				label: 'Processing',
				tone: 'active',
				detail: 'Encode job is queued for tv/Example/Season 1.',
				next_action: {
					kind: 'monitor_encode',
					label: 'Monitor encode',
					enabled: true,
					target_prefix: 'tv/Example/Season 1'
				}
			})
		});

		const workflow = resolveWorkflow(folder, folderStatusPayload(), null, null, null, null, null);

		expect(workflow).toMatchObject({
			tone: 'wait',
			label: 'Waiting for worker',
			title: 'Queued, not encoding yet',
			primary: 'Open Ops',
			primaryAction: 'open-ops'
		});
	});

	it('keeps retry processing on the output encoding facts and workspace', () => {
		const folder = folderPayload({
			summary: folderSummary({ item_count: 2 }),
			sample_item: { rel_path: 'tv/Example/Season 1/sample.mkv', source_size_bytes: 1 },
			calibration: {
				browser_review_ready: true,
				review_media_ready: true,
				sample_result: { predicted_total_size_bytes: 250_000_000 }
			} as FolderCalibrationState
		});
		const workflow = resolveWorkflow(
			folder,
			folderStatusPayload(),
			folder.calibration as FolderCalibrationState,
			null,
			null,
			null,
			{
				job_id: 'encode-retry',
				prefix: 'tv/Example/Season 1',
				status: 'failed',
				error: 'Worker stopped before output was written.'
			} as EncodeQueueJob,
			true
		);

		expect(workflow).toMatchObject({
			primaryAction: 'retry-encode',
			isOutputWorkflow: true
		});
		expect(
			buildDecisionFacts(folder, folder.calibration as FolderCalibrationState, null, workflow)[0]
		).toEqual({
			label: 'Processing',
			value: 'Needs retry',
			detail: 'Worker stopped before output was written.'
		});
		expect(buildReviewWorkspaceView(folder, null, null, workflow, true)).toMatchObject({
			badge: 'Processing run',
			title: 'Output encoding',
			layout: 'pipeline'
		});
	});

	it('makes active sampling a sample-state action with an explicit review-media prerequisite', () => {
		const workflow = resolveWorkflow(
			folderPayload(),
			folderStatusPayload({ calibration_status: 'running' }),
			null,
			null,
			null,
			{ status: 'running', host: { label: 'M2 MBP' } } as FolderCalibrationJob,
			null
		);

		expect(workflow).toMatchObject({
			label: 'Sampling',
			primary: 'Monitor sample',
			primaryAction: 'monitor-sample',
			secondary: 'Stop sample'
		});
		expect(workflow.copy).toContain('Worker M2 MBP');
		expect(workflow.copy).toMatch(/Review media is the missing prerequisite/);
		expect(buildBasicEncodeGuide(workflow)).toMatchObject({
			title: 'Sample running',
			detail: workflow.copy,
			primary: 'Monitor sample',
			action: 'monitor-sample'
		});
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
			label: 'Check sample plan',
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
				detail: 'VMAF target 85 · floor 80'
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
			label: 'Capped sample plan ready',
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
			label: 'Sample plan blocked',
			title: 'The sample plan does not match your request yet',
			copy: 'The draft lowers VMAF based only on a soft size target.',
			primary: 'Revise sample plan',
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
		const folder = folderPayload({ calibration });
		const workflow = resolveWorkflow(
			folder,
			folderStatusPayload(),
			calibration,
			null,
			null,
			null,
			null
		);

		expect(workflow).toMatchObject({
			label: 'Review ready',
			primary: 'Download side-by-side video',
			primaryAction: 'download-review-pack',
			secondary: 'Approve and queue',
			secondaryAction: 'queue-encode'
		});
		expect(buildDecisionFacts(folder, calibration, null, workflow)[0]).toMatchObject({
			label: 'Per episode',
			value: '238 MiB'
		});
	});

	it('keeps acceptable evidence download-first when its queueable draft is still present', () => {
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
			primary: 'Download side-by-side video',
			primaryAction: 'download-review-pack',
			secondary: 'Approve and queue',
			secondaryAction: 'queue-encode'
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
			label: 'Needs sample',
			primary: 'Ask review assistant',
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
			label: 'Sample plan ready',
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
		expect(buildWorkflowSteps(workflow).find((step) => step.label === 'Encode')).toMatchObject({
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
			label: 'Capped sample plan ready',
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
			label: 'Needs sample',
			primary: 'Ask review assistant',
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

	it('uses output workflow steps for validation-ready folders', () => {
		const steps = buildWorkflowSteps({
			tone: 'ready',
			label: 'Mixed work',
			title: 'Multiple tasks pending',
			copy: '22 to validate, 9 to encode',
			primary: 'Validate 22 outputs',
			primaryAction: 'validate-outputs',
			secondary: 'Queue 9 encodes',
			secondaryAction: 'queue-encode'
		});

		expect(steps.map((step) => [step.label, step.current, step.tone])).toEqual([
			['Encode', false, 'ready'],
			['Validate', true, 'ready'],
			['Promote', false, 'idle'],
			['Complete', false, 'idle']
		]);
		expect(steps[1].detail).toBe('Multiple tasks pending');
	});

	it('keeps output processing on the encode step when backend workflow is active', () => {
		const steps = buildWorkflowSteps({
			tone: 'active',
			label: 'Processing',
			title: 'Encoding approved outputs',
			copy: '3 running',
			primary: 'Monitor encode',
			primaryAction: 'monitor-processing',
			secondary: 'Open Ops',
			secondaryAction: 'open-ops',
			isOutputWorkflow: true
		});

		expect(steps.map((step) => [step.label, step.current, step.tone])).toEqual([
			['Encode', true, 'active'],
			['Validate', false, 'idle'],
			['Promote', false, 'idle'],
			['Complete', false, 'idle']
		]);
		expect(steps[0].detail).toBe('Encoding approved outputs');
	});

	it('maps mixed work workflow state to prioritized validate and encode actions', () => {
		const showFolder = folderPayload({
			prefix: 'tv/Terminator',
			summary: folderSummary({
				prefix: 'tv/Terminator',
				item_count: 31,
				seasons: { 'Season 2': 22, 'Season 1': 9 }
			}),
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
		});
		const workflow = resolveWorkflow(
			showFolder,
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
			primary: 'Validate show outputs (22)',
			primaryAction: 'validate-outputs',
			secondary: 'Queue show encodes (9)',
			secondaryAction: 'queue-encode'
		});
		const mixedFacts = buildDecisionFacts(showFolder, null, null, workflow);
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
			value: 'Validate show outputs (22)',
			detail: 'Queue show encodes (9) also available'
		});
		expect(buildDecisionFacts(folderPayload(), null, null, workflow)[2]).toEqual({
			label: 'Next action',
			value: 'Validate show outputs (22)',
			detail: 'Queue show encodes (9) also available'
		});
		expect(outputScopeLabel(showFolder)).toBe('whole show');
		expect(buildSeasonScopeRows(showFolder)).toEqual([
			{ label: 'Season 1', count: '9 items', href: 'tv/Terminator/Season 1' },
			{ label: 'Season 2', count: '22 items', href: 'tv/Terminator/Season 2' }
		]);
		expect(
			outputScopeLabel(
				folderPayload({ series_context: { prefix: 'tv/Terminator', title: 'Terminator' } })
			)
		).toBe('season');
		expect(outputScopeLabel(folderPayload())).toBe('folder');
	});
});

describe('quality risk facts', () => {
	it('projects a compact operator-facing quality risk summary', () => {
		const folder = folderPayload({
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
				}
			}
		});

		expect(buildQualityRiskFacts(folder)).toEqual([
			{
				label: 'Risk verdict',
				value: 'Request comparison',
				detail: 'Low-confidence grain findings require comparison.',
				tone: 'wait'
			},
			{
				label: 'Top concern',
				value: 'Grain / noise treatment',
				detail: 'Medium risk',
				tone: 'wait'
			},
			{
				label: 'Authority',
				value: 'No current decision',
				detail: 'Older or sibling evidence is not treated as current authority.',
				tone: 'idle'
			}
		]);
	});

	it('adds the top quality risk to decision facts', () => {
		const folder = folderPayload({
			quality_risk: {
				verdict: 'blocked',
				blocked: true,
				blocking_reasons: ['Current rejection blocks automatic reuse.'],
				typed_risks: [
					{
						tag: 'motion_breakup',
						label: 'Motion breakup',
						level: 'high',
						rationale: 'The current sample breaks up in high motion.'
					}
				],
				operator_decision: {
					status: 'rejected'
				}
			}
		});

		const facts = buildDecisionFacts(folder, null, null);
		expect(facts[3]).toEqual({
			label: 'Risk verdict',
			value: 'Blocked',
			detail: 'Current rejection blocks automatic reuse.'
		});
		expect(facts.slice(3).map((fact) => fact.label)).toEqual([
			'Risk verdict',
			'Top concern',
			'Authority'
		]);
	});
});
