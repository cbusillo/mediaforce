/** What the approval request reports about the encoding job it was asked to start. */
export type ApprovalStartResponse = {
	ok?: boolean;
	message?: string;
	already_active?: boolean;
	queued_count?: number;
	left_out_count?: number;
} | null;

export type ApprovalStartOutcome = {
	message: string;
	attention?: boolean;
	attentionTitle?: string;
};

export function approvalStartLabel(what: string): string {
	return `Keep and compress ${what}`;
}

export function approvalStartDetail(what: string, fileCount: number): string {
	const originals =
		fileCount === 1
			? 'The original stays unchanged until a checked replacement is installed.'
			: 'Originals stay unchanged until checked replacements are installed.';
	return `Keeping this version starts compressing ${what} now. ${originals}`;
}

/**
 * The approval is saved before the encoding job is queued, so a queue failure is reported as
 * needing attention beside the saved approval; the approved page then offers the retry.
 */
export function approvalStartOutcome(
	start: ApprovalStartResponse | undefined,
	queuedMessage: (queuedCount: number) => string,
	expectedCount: number
): ApprovalStartOutcome {
	if (!start || start.ok !== true) {
		return {
			message:
				`Your approval is saved, but compression did not start. ${start?.message ?? ''}`.trim(),
			attention: true,
			attentionTitle: 'Compression did not start'
		};
	}
	if (start.already_active) {
		return { message: 'Your approval is saved. This work was already waiting to compress.' };
	}
	// The server names the files it left out and why; repeat its words rather than a count.
	if ((start.left_out_count ?? 0) > 0 && start.message) return { message: start.message };
	const queued = start.queued_count ?? 0;
	return { message: queuedMessage(queued > 0 ? queued : expectedCount) };
}
