import type { CalibrationJobPayload } from '$lib/api/types';

export function canRetrySampleJob(jobId: unknown, hasPendingProposal: boolean): boolean {
	return typeof jobId === 'string' && jobId.trim().length > 0 && !hasPendingProposal;
}

export function parentSampleAppliesToExactItem(
	exactPrefix: string,
	exactJob: CalibrationJobPayload | null | undefined,
	overlappingJob: CalibrationJobPayload | null | undefined
): boolean {
	if (exactJob?.job_id || !overlappingJob?.job_id || overlappingJob.prefix === exactPrefix) {
		return false;
	}
	const sampleItem = overlappingJob.sample_item;
	return sampleItem?.rel_path === exactPrefix;
}

export function movieReviewStatusLabel(status: unknown, inheritedParentSample = false): string {
	const normalized = typeof status === 'string' ? status.trim() : '';
	if (inheritedParentSample) return 'Review at title level';
	return (
		{
			accepted: 'Approved',
			pending_review: 'Ready to review',
			needs_approval: 'Ready to review',
			rejected: 'Needs another sample',
			blocked: 'Needs review',
			missing_sample: 'Not prepared'
		}[normalized] ?? (normalized ? 'Status unavailable' : 'Not reviewed')
	);
}
