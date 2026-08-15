export function canRetrySampleJob(jobId: unknown, hasPendingProposal: boolean): boolean {
	return typeof jobId === 'string' && jobId.trim().length > 0 && !hasPendingProposal;
}
