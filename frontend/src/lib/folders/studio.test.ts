import { describe, expect, it } from 'vitest';

import {
	approvalReviewSignature,
	buildCalibrationThreadScrollSignature,
	describeHighImpactApprovalGate,
	normalizeReviewArtifacts,
	resolveBenchDraftNote,
	summarizeVideoTransformPolicy,
	type ComparisonRow
} from './studio';

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
