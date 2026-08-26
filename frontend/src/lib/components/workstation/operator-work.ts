import type {
	OperatorCatalogState,
	OperatorEvidenceBacklogRow,
	OperatorEvidenceState,
	OperatorWorkPayload
} from '$lib/api/types';
import { operatorStateCopy, safeOperatorErrorCopy } from '$lib/operator-copy';

export type OperatorStateTone = 'active' | 'ready' | 'wait' | 'fail' | 'idle';

export interface OperatorStateView {
	label: string;
	tone: OperatorStateTone;
	detail: string;
}

export interface OperatorWorkQuery {
	offset?: number;
	limit?: number;
	evidenceKind?: string;
	state?: string;
	mediaRoot?: string;
	workStatus?: string;
}

export function catalogStateView(
	catalog: OperatorCatalogState | null | undefined
): OperatorStateView {
	if (!catalog) {
		return { label: 'Unknown', tone: 'idle', detail: 'Catalog status has not loaded yet.' };
	}
	if (catalog.status === 'running') {
		return {
			label: 'Refreshing',
			tone: 'active',
			detail: 'Reading changed files and preserving remembered results while the refresh runs.'
		};
	}
	if (catalog.status === 'queued') {
		return {
			label: 'Queued',
			tone: 'wait',
			detail: 'The catalog refresh is waiting to start.'
		};
	}
	if (catalog.status === 'failed') {
		return {
			label: 'Refresh failed',
			tone: 'fail',
			detail: friendlyFailureCopy(
				catalog.job?.error,
				'The catalog refresh stopped before it finished.'
			)
		};
	}
	if (catalog.status === 'paused') {
		return {
			label: 'Paused',
			tone: 'wait',
			detail:
				'Background work was paused before this refresh began. Remembered catalog facts were unchanged.'
		};
	}
	if (catalog.freshness === 'current') {
		return {
			label: 'Current',
			tone: 'ready',
			detail: 'Remembered file facts match the current catalog policy.'
		};
	}
	if (catalog.freshness === 'stale') {
		return {
			label: 'Refresh suggested',
			tone: 'wait',
			detail: 'The remembered catalog is old enough to check again.'
		};
	}
	return {
		label: 'Needs a check',
		tone: 'idle',
		detail: 'Mediaforce is keeping the last known catalog until a safe refresh completes.'
	};
}

export function evidenceStateView(
	evidence: OperatorEvidenceState | null | undefined
): OperatorStateView {
	if (!evidence) {
		return { label: 'Unknown', tone: 'idle', detail: 'Analysis status has not loaded yet.' };
	}
	const queue = evidence.queue;
	if (queue.status === 'cancel_requested') {
		return {
			label: 'Cancelling',
			tone: 'wait',
			detail: 'Stopping the active analyzer and cancelling every item that has not started.'
		};
	}
	if (evidence.runner_active || queue.status === 'running') {
		return {
			label: 'Analyzing',
			tone: 'active',
			detail: 'Expensive media analysis is running for the prepared batch.'
		};
	}
	if (queue.status === 'paused') {
		const started = evidence.progress.done_count > 0 || Boolean(queue.started_at);
		return {
			label: started ? 'Paused' : 'Prepared',
			tone: 'wait',
			detail: started
				? 'No new analyzer will start until you resume this batch.'
				: 'The scope is prepared, but no media will be opened until you start analysis.'
		};
	}
	if (queue.status === 'queued') {
		return {
			label: 'Ready to continue',
			tone: 'wait',
			detail: 'Prepared work is waiting for an explicit start.'
		};
	}
	if (queue.status === 'completed_with_errors') {
		return {
			label: 'Needs attention',
			tone: 'fail',
			detail: 'Some analysis could not be completed.'
		};
	}
	if (queue.status === 'cancelled') {
		return {
			label: 'Cancelled',
			tone: 'idle',
			detail: 'The last prepared batch was cancelled.'
		};
	}
	if (queue.status === 'completed' && queue.batch_id) {
		return {
			label: 'Complete',
			tone: 'ready',
			detail: 'The last prepared batch finished.'
		};
	}
	return {
		label: 'Quiet',
		tone: 'idle',
		detail: 'No expensive analysis is running. Mediaforce is using remembered evidence.'
	};
}

export function evidenceKindLabel(kind: string): string {
	if (kind === 'cadence_analysis') return 'Motion pattern';
	if (kind === 'media_fingerprint') return 'Media fingerprint';
	return 'Other analysis';
}

export function evidenceStateLabel(state: string, decisionStatus: string | null = null): string {
	if (state === 'current' && decisionStatus && !['resolved', 'measured'].includes(decisionStatus)) {
		return 'Needs decision';
	}
	if (state === 'current') return 'Current';
	if (state === 'analysis_required') return 'Needs analysis';
	if (state === 'classification_required') return 'Needs update';
	return operatorStateCopy(state);
}

export function evidenceStateTone(
	state: string,
	decisionStatus: string | null = null
): OperatorStateTone {
	if (state === 'current' && decisionStatus && !['resolved', 'measured'].includes(decisionStatus)) {
		return 'wait';
	}
	if (state === 'current') return 'ready';
	if (state === 'analysis_required') return 'wait';
	if (state === 'classification_required') return 'active';
	return 'idle';
}

export function evidenceReasonCopy(
	reason: string | null,
	state: string | null = null,
	decisionStatus: string | null = null
): string {
	if (state === 'current' && decisionStatus && !['resolved', 'measured'].includes(decisionStatus)) {
		return 'Needs your review before conversion.';
	}
	if (!reason) return 'Status could not be confirmed.';
	if (reason === 'missing') return 'No saved result.';
	if (reason === 'malformed') return 'Saved result could not be read.';
	if (reason === 'retry_required') return 'Analysis should be tried again.';
	if (reason === 'schema_changed') return 'Saved result uses an older format.';
	if (reason === 'tool_changed') return 'Analysis tools changed.';
	if (reason === 'source_changed') return 'The media file changed.';
	if (reason === 'policy_changed') return 'Saved results need a new decision.';
	return 'Status could not be confirmed.';
}

export interface EvidenceWorkReasonView {
	label: string;
	detail: string;
}

export function evidenceWorkReasonView(reason: string | null): EvidenceWorkReasonView | null {
	if (!reason) return null;
	if (reason === 'sample_safety') {
		return { label: 'Needed for sample', detail: 'Complete this before starting the sample.' };
	}
	if (reason === 'encode_safety') {
		return {
			label: 'Needed for production',
			detail: 'Complete this before starting production work.'
		};
	}
	if (reason === 'policy_reclassification') {
		return {
			label: 'No media read',
			detail: 'Reinterprets stored measurements without opening the media.'
		};
	}
	if (reason === 'representative_technical_coverage') {
		return {
			label: 'Representative check',
			detail: 'Chosen from codec, resolution, cadence, audio layout, and runtime.'
		};
	}
	if (reason === 'representative_uncertainty') {
		return {
			label: 'Bounded follow-up',
			detail: 'Checks one nearby file because measured representative coverage is uncertain.'
		};
	}
	if (reason === 'operator_scope') {
		return { label: 'Operator request', detail: 'Prepared from the scope you selected.' };
	}
	return { label: 'Priority work', detail: 'Prepared for a decision that needs current evidence.' };
}

export function evidenceWorkStatusView(row: OperatorEvidenceBacklogRow): OperatorStateView {
	const status = row.work_status;
	if (!status) {
		return { label: 'Not prepared', tone: 'idle', detail: 'This item is not in the active batch.' };
	}
	if (status === 'queued') {
		return { label: 'Prepared', tone: 'wait', detail: 'Waiting for an explicit start.' };
	}
	if (status === 'running') {
		return { label: 'Running', tone: 'active', detail: 'The analyzer owns this item now.' };
	}
	if (status === 'retry_wait') {
		return {
			label: 'Retry scheduled',
			tone: 'wait',
			detail: 'A retry delay is protecting the source.'
		};
	}
	if (status === 'waiting_source') {
		return {
			label: 'Source unavailable',
			tone: 'wait',
			detail: 'The library must return before retrying.'
		};
	}
	if (status === 'failed') {
		return {
			label: 'Failed',
			tone: 'fail',
			detail: friendlyFailureCopy(row.last_error, 'Analysis could not finish.')
		};
	}
	if (status === 'cancelled') {
		return { label: 'Cancelled', tone: 'idle', detail: 'This prepared item was cancelled.' };
	}
	if (status === 'completed') {
		return { label: 'Complete', tone: 'ready', detail: 'Saved evidence is current.' };
	}
	return { label: operatorStateCopy(status), tone: 'idle', detail: 'Stored work state.' };
}

export function operatorRefreshInterval(
	work: OperatorWorkPayload | null | undefined
): number | null {
	if (!work || work.refresh.mode !== 'active') return null;
	return work.refresh.interval_ms ?? 2_500;
}

export function scopeLabel(evidence: OperatorEvidenceState | null | undefined): string {
	const scope = evidence?.queue.scope;
	return scope?.scope_label || scope?.title || scope?.prefix || 'No scope prepared';
}

export function formatCount(value: number): string {
	return new Intl.NumberFormat().format(value);
}

export function formatTimestamp(value: string | null | undefined): string {
	if (!value) return 'Never';
	const parsed = new Date(value);
	if (Number.isNaN(parsed.getTime())) return 'Unknown';
	return new Intl.DateTimeFormat(undefined, {
		month: 'short',
		day: 'numeric',
		hour: 'numeric',
		minute: '2-digit'
	}).format(parsed);
}

function friendlyFailureCopy(value: string | null | undefined, fallback: string): string {
	const detail = value?.trim();
	if (!detail) return fallback;
	if (/cancel(?:led|ed)|processcancellederror/i.test(detail)) {
		return 'The operation was stopped before it finished.';
	}
	if (/non-current result:\s*unknown/i.test(detail)) {
		return 'Analysis did not produce a usable result.';
	}
	return safeOperatorErrorCopy(detail, fallback);
}
