import { describe, expect, it } from 'vitest';
import type { FolderPayload, FolderStatusPayload } from '$lib/api/types';
import type { FolderCalibrationJob } from '$lib/folders/studio';
import {
	buildBenchHostOptions,
	buildWorkflowSteps,
	resolveBenchRequestState,
	resolveWorkflow,
	resolveWorkflowActionState
} from './folder-studio-view';
import type { PendingSampleProposal } from '$lib/folders/studio';

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
