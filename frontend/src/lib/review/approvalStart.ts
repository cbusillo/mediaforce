/** What the approval request reports about the encode it was asked to start. */
export type ApprovalStartResponse = {
	ok?: boolean;
	message?: string;
	already_active?: boolean;
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
 * The approval is saved before the encode is queued, so a queue failure is reported as
 * needing attention beside the saved approval; the approved page then offers the retry.
 */
export function approvalStartOutcome(
	start: ApprovalStartResponse | undefined,
	startedMessage: string
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
	return { message: startedMessage };
}
