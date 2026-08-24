export interface OperatorErrorView {
	summary: string;
	technicalDetail: string | null;
}

const DEFAULT_STATE_LABELS: Readonly<Record<string, string>> = {
	accepted: 'Approved',
	analyzing: 'Analyzing',
	blocked: 'Blocked',
	cancel_requested: 'Stopping',
	cancelled: 'Cancelled',
	complete: 'Complete',
	completed: 'Complete',
	completed_with_errors: 'Needs attention',
	encode_candidate: 'Ready to compress',
	encoding: 'Compressing',
	failed: 'Needs attention',
	idle: 'Idle',
	missing: 'Missing',
	missing_sample: 'Needs sample',
	needs_attention: 'Needs attention',
	needs_review: 'Needs review',
	not_started: 'Not started',
	paused: 'Paused',
	pending_review: 'Ready to review',
	planned: 'Ready to compress',
	promoted: 'Replaced',
	queued: 'Waiting',
	ready_to_promote: 'Ready to replace',
	ready_to_validate: 'Ready to check',
	retry_backoff: 'Retry scheduled',
	retry_wait: 'Retry scheduled',
	running: 'Working',
	source_unavailable: 'Source unavailable',
	starting: 'Starting',
	stopped: 'Stopped',
	stopping: 'Stopping',
	validated: 'Ready to replace',
	waiting_source: 'Source unavailable'
};

function normalizedText(value: unknown): string {
	return typeof value === 'string' ? value.trim() : '';
}

function isOperatorReadyCopy(value: string): boolean {
	if (!value || value.length > 280) return false;
	if (!/^[A-Z0-9]/.test(value)) return false;
	return !(
		/\b(?:[A-Za-z.]+)?(?:Error|Exception)\b|\b(?:Traceback|errno)\b/.test(value) ||
		/\b[a-z]+_[a-z_]+\b/.test(value) ||
		/[{}[\]]/.test(value) ||
		/(?:^|\s)\/[A-Za-z0-9._/-]+/.test(value)
	);
}

export function operatorStateCopy(
	value: unknown,
	overrides: Readonly<Record<string, string>> = {},
	fallback = 'Unknown state'
): string {
	const state = normalizedText(value).toLowerCase();
	if (!state) return fallback;
	return overrides[state] ?? DEFAULT_STATE_LABELS[state] ?? fallback;
}

export function operatorErrorView(value: unknown, fallback: string): OperatorErrorView {
	const errorObject = value instanceof Error;
	const detail = errorObject ? value.message.trim() : normalizedText(value);
	const summary = !errorObject && isOperatorReadyCopy(detail) ? detail : fallback;
	return {
		summary,
		technicalDetail: detail && detail !== summary ? detail : null
	};
}

export function safeOperatorErrorCopy(value: unknown, fallback: string): string {
	return operatorErrorView(value, fallback).summary;
}
