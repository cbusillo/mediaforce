import { describe, expect, it } from 'vitest';

import {
	approvalReviewSignature,
	buildCalibrationThreadScrollSignature,
	calibrationFailureHeading,
	calibrationFailureLede,
	describeHighImpactApprovalGate,
	encodeStatusTone,
	normalizeReviewArtifacts,
	noteAfterPreview,
	noteAfterPrepareAgain,
	noteAfterProposalHydration,
	prepareAgainRequest,
	proposalRecoveryView,
	resolveBenchDraftNote,
	summarizeCalibrationFailureDetail,
	summarizeVideoTransformPolicy,
	type ComparisonRow
} from './studio';

describe('proposalRecoveryView', () => {
	it('keeps assistant retry bounded to the stored request and worker', () => {
		const proposal = {
			can_queue: false,
			operator_note: 'Keep grain, but reduce size.',
			host: { key: 'worker-stored' },
			recovery: {
				cause: 'assistant_failure' as const,
				headline: 'Mediaforce could not prepare this sample',
				detail: 'The assistant did not answer. Nothing was queued.',
				nothing_queued: true,
				action: 'prepare_again' as const,
				same_request_retryable: true
			}
		};

		expect(proposalRecoveryView(proposal)).toMatchObject({
			cause: 'assistant_failure',
			action: 'prepare_again',
			nothingQueued: true,
			sameRequestRetryable: true
		});
		expect(prepareAgainRequest(proposal)).toEqual({
			note: 'Keep grain, but reduce size.',
			hostKey: 'worker-stored'
		});
	});

	it('requires the stored worker for an exact retry', () => {
		expect(prepareAgainRequest({ operator_note: 'Keep grain.' })).toEqual({
			note: 'Keep grain.',
			hostKey: ''
		});
	});

	it('preserves the composer note for a nonqueueable preview and clears only queueable plans', () => {
		expect(noteAfterPreview('Keep my newer note', { can_queue: false })).toBe('Keep my newer note');
		expect(noteAfterPreview('Finished note', { can_queue: true })).toBe('');
	});

	it('restores a saved nonqueueable note only before the operator types newer text', () => {
		const proposal = { can_queue: false, operator_note: 'Saved request' };
		expect(noteAfterProposalHydration('', false, proposal)).toBe('Saved request');
		expect(noteAfterProposalHydration('Newer request', true, proposal)).toBe('Newer request');
		expect(noteAfterProposalHydration('', false, { ...proposal, can_queue: true })).toBe('');
	});

	it('keeps newer composer text when an exact stored retry becomes queueable', () => {
		expect(noteAfterPrepareAgain('New request', 'Saved request', true, { can_queue: true })).toBe(
			'New request'
		);
		expect(noteAfterPrepareAgain('Saved request', 'Saved request', true, { can_queue: true })).toBe(
			''
		);
		expect(
			noteAfterPrepareAgain('Saved request', 'Saved request', false, { can_queue: false })
		).toBe('Saved request');
	});

	it('keeps unclear and deterministic recoveries on an edit path', () => {
		expect(
			proposalRecoveryView({
				can_queue: false,
				recovery: {
					cause: 'unclear_request',
					action: 'edit_request',
					nothing_queued: true
				}
			})
		).toMatchObject({ action: 'edit_request', nothingQueued: true });
		expect(
			proposalRecoveryView({
				can_queue: false,
				recovery: {
					cause: 'deterministic_blocker',
					action: 'change_request',
					nothing_queued: true
				}
			})
		).toMatchObject({ action: 'change_request', nothingQueued: true });
	});

	it('accepts stale sample plans only on the prepare-again path', () => {
		expect(
			proposalRecoveryView({
				can_queue: false,
				recovery: {
					cause: 'stale_plan',
					headline: 'Sample plan is out of date',
					detail: 'Your compression goal changed after this plan was made.',
					nothing_queued: true,
					action: 'prepare_again',
					same_request_retryable: true
				}
			})
		).toEqual({
			cause: 'stale_plan',
			headline: 'Sample plan is out of date',
			detail: 'Your compression goal changed after this plan was made.',
			action: 'prepare_again',
			nothingQueued: true,
			sameRequestRetryable: true
		});
	});
});

describe('describeHighImpactApprovalGate', () => {
	it('keeps the standard approval button for normal drafts', () => {
		expect(
			describeHighImpactApprovalGate({
				reviewGateStatus: 'needs_approval',
				highImpactPolicyCount: 0,
				armed: false
			})
		).toEqual({
			requiresConfirmation: false,
			armed: false,
			buttonLabel: 'Approve'
		});
	});

	it('requires a second confirmation click for high-impact drafts', () => {
		expect(
			describeHighImpactApprovalGate({
				reviewGateStatus: 'needs_approval',
				highImpactPolicyCount: 2,
				armed: false
			})
		).toEqual({
			requiresConfirmation: true,
			armed: false,
			buttonLabel: 'Approve'
		});

		expect(
			describeHighImpactApprovalGate({
				reviewGateStatus: 'needs_approval',
				highImpactPolicyCount: 2,
				armed: true
			})
		).toEqual({
			requiresConfirmation: true,
			armed: true,
			buttonLabel: 'Approve'
		});
	});

	it('stops requesting confirmation after approval is complete', () => {
		expect(
			describeHighImpactApprovalGate({
				reviewGateStatus: 'accepted',
				highImpactPolicyCount: 3,
				armed: true
			})
		).toEqual({
			requiresConfirmation: false,
			armed: false,
			buttonLabel: 'Approved'
		});
	});
});

describe('encodeStatusTone', () => {
	it('uses blocked tone for stopped encode states', () => {
		expect(encodeStatusTone('needs_attention')).toBe('blocked');
		expect(encodeStatusTone('failed')).toBe('blocked');
		expect(encodeStatusTone('stopped')).toBe('blocked');
	});

	it('keeps active and waiting encode tones distinct', () => {
		expect(encodeStatusTone('running')).toBe('live');
		expect(encodeStatusTone('queued')).toBe('queued');
		expect(encodeStatusTone('retry_backoff')).toBe('queued');
	});
});

describe('approvalReviewSignature', () => {
	it('changes when a non-high-impact row changes', () => {
		const baselineRows: ComparisonRow[] = [
			{
				label: 'Quality guardrail',
				current: { headline: '94.5 VMAF' },
				draft: { headline: '91.0 VMAF' },
				changed: true
			},
			{
				label: 'Sample cadence',
				current: { headline: '8m' },
				draft: { headline: '6m' },
				changed: true
			}
		];
		const updatedRows: ComparisonRow[] = [
			baselineRows[0],
			{
				label: 'Sample cadence',
				current: { headline: '8m' },
				draft: { headline: '10m' },
				changed: true
			}
		];

		expect(approvalReviewSignature(updatedRows)).not.toBe(approvalReviewSignature(baselineRows));
	});
});

describe('resolveBenchDraftNote', () => {
	it('uses an explicit string override when one is supplied', () => {
		expect(resolveBenchDraftNote('keep current note', '  refreshed guidance  ')).toBe(
			'refreshed guidance'
		);
	});

	it('falls back to the current note when a click event is passed through the button callback', () => {
		expect(resolveBenchDraftNote('keep current note', { type: 'click' })).toBe('keep current note');
	});
});

describe('summarizeVideoTransformPolicy', () => {
	it('summarizes crop and scale transforms compactly', () => {
		expect(
			summarizeVideoTransformPolicy({
				video: {
					max_height: 1080,
					downsample_algorithm: 'lanczos',
					black_bar_handling: 'smart'
				}
			})
		).toEqual({
			headline: 'smart black-bar detect + max 1080p',
			detail: 'lanczos'
		});
	});

	it('uses manual crop as the leading transform when present', () => {
		expect(
			summarizeVideoTransformPolicy({
				video: {
					max_height: 720,
					black_bar_handling: 'smart',
					crop: '1920:800:0:140'
				}
			})
		).toEqual({
			headline: 'manual crop + max 720p',
			detail: '1920:800:0:140'
		});
	});
});

describe('buildCalibrationThreadScrollSignature', () => {
	it('changes when a rendered thread chip changes', () => {
		const baseline = buildCalibrationThreadScrollSignature(
			{
				key: 'session-1',
				note: 'keep dark scenes cleaner',
				requestResponse: 'Working on it.',
				requestDisposition: 'accepted',
				summary: 'Adjusted the draft.',
				diagnosis: 'Dark scenes are undersized.',
				feasibilityNote: 'This should stay within budget.',
				confidence: 'high',
				suggestedFollowUp: 'Check the action scene again.',
				runSummary: 'Prior run passed.',
				runNextStep: 'Queue the next sample.',
				runOutcome: 'ok',
				runConfidence: 'medium',
				isCurrent: true
			},
			3
		);
		const updated = buildCalibrationThreadScrollSignature(
			{
				key: 'session-1',
				note: 'keep dark scenes cleaner',
				requestResponse: 'Working on it.',
				requestDisposition: 'accepted',
				summary: 'Adjusted the draft.',
				diagnosis: 'Dark scenes are undersized.',
				feasibilityNote: 'This should stay within budget.',
				confidence: 'high',
				suggestedFollowUp: 'Check the action scene again.',
				runSummary: 'Prior run passed.',
				runNextStep: 'Queue the next sample.',
				runOutcome: 'ok',
				runConfidence: 'low',
				isCurrent: true
			},
			3
		);

		expect(updated).not.toBe(baseline);
	});

	it('changes when the session currentness flips', () => {
		const baseline = buildCalibrationThreadScrollSignature(
			{
				key: 'session-1',
				note: 'keep dark scenes cleaner',
				summary: 'Adjusted the draft.',
				isCurrent: true
			},
			1
		);
		const updated = buildCalibrationThreadScrollSignature(
			{
				key: 'session-1',
				note: 'keep dark scenes cleaner',
				summary: 'Adjusted the draft.',
				isCurrent: false
			},
			1
		);

		expect(updated).not.toBe(baseline);
	});
});

describe('calibration failure copy', () => {
	it('summarizes ab-av1 progress logs as stopped progress instead of raw stderr', () => {
		expect(
			summarizeCalibrationFailureDetail(
				'[2026-04-22T14:48:43Z INFO ab_av1::command::sample_encode] encoding sample 1/8 crf 31',
				'M4 Studio',
				'stopped'
			)
		).toBe(
			'Host: M4 Studio · Stopped while ab-av1 was encoding sample 1/8 at CRF 31. No specific error line was recorded.'
		);
	});

	it('prefers actionable failure lines over progress chatter', () => {
		expect(
			summarizeCalibrationFailureDetail(
				'encoding sample 1/8 crf 31\nPermission denied (publickey).',
				'M4 Studio',
				'failed'
			)
		).toBe('Host: M4 Studio · Permission denied (publickey).');
	});

	it('translates failed CRF search logs into the quality and size conflict', () => {
		expect(
			summarizeCalibrationFailureDetail(
				[
					'[2026-04-22T14:48:44Z INFO  ab_av1::command::sample_encode] crf 31 VMAF 97.48 predicted video stream size 1.01 GiB (33%) taking 17 minutes (cache)',
					'[2026-04-22T14:48:44Z INFO  ab_av1::command::sample_encode] crf 44 VMAF 90.55 predicted video stream size 444.92 MiB (14%) taking 20 minutes (cache)',
					'Error: Failed to find a suitable crf'
				].join('\n'),
				'M4 Studio',
				'failed'
			)
		).toBe(
			'Host: M4 Studio · No CRF satisfied both the quality target and size limit. Search tried CRF 31 at VMAF 97.48 / 33% and CRF 44 at VMAF 90.55 / 14%.'
		);
	});

	it('ignores appended cleanup warnings when summarizing the primary failure', () => {
		expect(
			summarizeCalibrationFailureDetail(
				[
					'[2026-04-22T14:48:43Z INFO ab_av1::command::sample_encode] encoding sample 1/8 crf 31',
					'Cleanup warning: Failed to remove remote quality temp dir /tmp/mediaforce-quality: Permission denied'
				].join('\n'),
				'M4 Studio',
				'failed'
			)
		).toBe(
			'Host: M4 Studio · ab-av1 was still encoding when the run ended sample 1/8 at CRF 31. No specific error line was recorded.'
		);
	});

	it('still surfaces cleanup failures when cleanup is the only recorded error', () => {
		expect(
			summarizeCalibrationFailureDetail(
				'Cleanup warning: Failed to remove remote quality temp dir /tmp/mediaforce-quality: Permission denied',
				'M4 Studio',
				'failed'
			)
		).toBe(
			'Host: M4 Studio · Cleanup warning: Failed to remove remote quality temp dir /tmp/mediaforce-quality: Permission denied'
		);
	});

	it('uses direct stopped copy for stopped samples', () => {
		expect(calibrationFailureHeading('sample', 'stopped')).toBe(
			'Sample run stopped before review clips were ready'
		);
		expect(calibrationFailureLede('sample', 'stopped')).toBe(
			'The previous run ended before review clips were produced. If you stopped it intentionally, retry when ready; otherwise check the host log.'
		);
	});

	it('keeps proof encode failures out of sample retry copy', () => {
		expect(calibrationFailureHeading('full', 'failed')).toBe(
			'Representative-file proof failed before review clips were ready'
		);
		expect(calibrationFailureLede('full', 'failed')).toBe(
			'The previous proof encode ended before review clips were produced. Check the detail below, then rerun the proof.'
		);
	});
});

describe('normalizeReviewArtifacts', () => {
	it('keeps audio review artifacts separate from visual ones', () => {
		expect(
			normalizeReviewArtifacts({
				artifacts: [
					{
						kind: 'video_contact_sheet',
						label: 'Moment 1',
						image_url: '/review-media/moment-1.png'
					},
					{
						kind: 'audio_spectrogram_compare',
						label: 'Primary audio compare',
						image_url: '/review-media/audio.png'
					}
				]
			})
		).toEqual([
			{
				kind: 'video_contact_sheet',
				label: 'Moment 1',
				detail: '',
				imageUrl: '/review-media/moment-1.png',
				category: 'visual'
			},
			{
				kind: 'audio_spectrogram_compare',
				label: 'Primary audio compare',
				detail: '',
				imageUrl: '/review-media/audio.png',
				category: 'audio'
			}
		]);
	});
});
