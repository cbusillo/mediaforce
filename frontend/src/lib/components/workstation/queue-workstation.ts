import type {
	DashboardFoldersPayload,
	DashboardSummaryPayload,
	FolderCard,
	WorkflowTone,
	HostsPayload
} from '$lib/api/types';
import type { FooterSignal, ShellTone, StatusTile } from './OperatorShell.svelte';
import { formatBytes, summarizeStatuses } from './folder-studio-view';

export function queueFolderTone(folder: FolderCard): ShellTone {
	if (folder.workflow_state?.tone) return workflowToneToShellTone(folder.workflow_state.tone);
	const explicit = String(folder.review_badge_tone ?? '').toLowerCase();
	if (explicit === 'fail' || explicit === 'blocked' || explicit === 'error') return 'fail';
	if (explicit === 'ready' || explicit === 'success') return 'ready';
	if (explicit === 'active' || explicit === 'running') return 'active';
	if (explicit === 'wait' || explicit === 'warning') return 'wait';
	if (folder.pending_count > 0) return 'wait';
	if (folder.known_saved_bytes > 0) return 'ready';
	return 'idle';
}

export function queueFolderState(folder: FolderCard): string {
	const workflowLabel = folder.workflow_state?.label?.trim();
	if (workflowLabel) return workflowLabel;
	const label = folder.review_badge_label?.trim();
	if (label) return queueStateLabel(label);
	if (folder.pending_count > 0) return 'Needs sample';
	if (folder.known_saved_bytes > 0) return 'Completed';
	return 'Cataloged';
}

export function workflowToneToShellTone(tone: WorkflowTone): ShellTone {
	if (tone === 'active') return 'active';
	if (tone === 'ready' || tone === 'success') return 'ready';
	if (tone === 'attention') return 'fail';
	return 'idle';
}

export function queueStateLabel(label: string): string {
	const normalized = label.trim().toLowerCase();
	if (normalized === 'ready to start') return 'Needs sample';
	if (normalized === 'no sample' || normalized === 'missing sample') return 'Needs sample';
	if (normalized === 'sample queued') return 'Sample waiting';
	if (normalized === 'sample running') return 'Sampling';
	if (normalized === 'sample failed retry') return 'Sample needs retry';
	if (normalized === 'review media') return 'Ready to review';
	if (normalized === 'proposal warning') return 'Review warning';
	if (normalized === 'proposal accepted') return 'Approved';
	if (normalized === 'encode queued' || normalized === 'encode running') return 'Processing';
	if (normalized === 'encode failed' || normalized === 'encode stopped') {
		return 'Processing needs attention';
	}
	return label.trim();
}

export function codecSummary(codecs: Record<string, number>): string {
	const entries = Object.entries(codecs).filter(([, count]) => count > 0);
	if (entries.length === 0) return '—';
	return entries
		.slice(0, 2)
		.map(([codec, count]) => `${count} ${codec.toUpperCase()}`)
		.join(' · ');
}

export function totalProjectedReclaim(folders: FolderCard[]): number {
	return folders.reduce(
		(total, folder) => total + Math.max(folder.projected_reclaim_bytes ?? 0, 0),
		0
	);
}

export function totalPendingItems(folders: FolderCard[]): number {
	return folders.reduce((total, folder) => total + workflowOpenItemCount(folder), 0);
}

export function isQueueActionableFolder(folder: FolderCard): boolean {
	const workflow = folder.workflow_state;
	if (!workflow) return Math.max(folder.pending_count ?? 0, 0) > 0;
	if (workflow.state === 'complete' || workflow.state === 'idle') return false;
	return workflowOpenItemCount(folder) > 0 || workflow.next_action.enabled;
}

export function queuePrimaryActionLabel(folder: FolderCard): string {
	const workflow = folder.workflow_state;
	if (!workflow) return 'Open Folder Studio';
	const counts = workflow.counts ?? {};
	if (workflow.next_action.kind === 'queue_encode') {
		const count = Math.max(counts.encode_candidates ?? 0, 0);
		return count > 0
			? `Queue ${count} encode${count === 1 ? '' : 's'}`
			: workflow.next_action.label;
	}
	if (workflow.next_action.kind === 'validate_outputs') {
		const count = Math.max(counts.ready_to_validate ?? 0, 0);
		return count > 0
			? `Validate ${count} output${count === 1 ? '' : 's'}`
			: workflow.next_action.label;
	}
	if (workflow.next_action.kind === 'promote_outputs') {
		const count = Math.max(counts.ready_to_promote ?? 0, 0);
		return count > 0
			? `Promote ${count} output${count === 1 ? '' : 's'}`
			: workflow.next_action.label;
	}
	if (workflow.next_action.enabled) return workflow.next_action.label;
	if (workflow.state === 'complete') return 'Review completed scope';
	return 'Open Folder Studio';
}

export function workflowOpenItemCount(folder: FolderCard): number {
	const counts = folder.workflow_state?.counts;
	if (!counts) return Math.max(folder.pending_count ?? 0, 0);
	return (
		Math.max(counts.encode_candidates ?? 0, 0) +
		Math.max(counts.ready_to_validate ?? 0, 0) +
		Math.max(counts.ready_to_promote ?? 0, 0) +
		Math.max(counts.processing ?? 0, 0) +
		Math.max(counts.blocked ?? 0, 0)
	);
}

export type WorkLaneKey =
	| 'attention'
	| 'processing'
	| 'validate'
	| 'promote'
	| 'encode'
	| 'sample'
	| 'other';

export type WorkLaneGroup = {
	key: WorkLaneKey;
	label: string;
	detail: string;
	tone: ShellTone;
	folders: FolderCard[];
	openWork: number;
};

const WORK_LANE_ORDER: WorkLaneKey[] = [
	'attention',
	'processing',
	'validate',
	'promote',
	'encode',
	'sample',
	'other'
];

const WORK_LANE_META: Record<WorkLaneKey, { label: string; detail: string; tone: ShellTone }> = {
	attention: {
		label: 'Needs attention',
		detail: 'Blocked, failed, or stopped work that needs operator action.',
		tone: 'fail'
	},
	processing: {
		label: 'Active processing',
		detail: 'Items currently running or waiting on encode workers.',
		tone: 'active'
	},
	validate: {
		label: 'Ready to validate',
		detail: 'Encoded outputs that need validation, not more encode queueing.',
		tone: 'ready'
	},
	promote: {
		label: 'Ready to promote',
		detail: 'Validated outputs waiting to be published into the library.',
		tone: 'ready'
	},
	encode: {
		label: 'Encode backlog',
		detail: 'Approved source items that still need encoded outputs.',
		tone: 'wait'
	},
	sample: {
		label: 'Sample and approval',
		detail: 'Folders that still need sampling, review evidence, or approval.',
		tone: 'wait'
	},
	other: {
		label: 'Other work',
		detail: 'Actionable folders that do not fit a primary pipeline lane.',
		tone: 'idle'
	}
};

export function workLaneKey(folder: FolderCard): WorkLaneKey {
	const workflow = folder.workflow_state;
	if (!workflow) return folder.pending_count > 0 ? 'sample' : 'other';
	if (workflow.tone === 'attention' || workflow.primary_lane === 'attention') return 'attention';
	if (workflow.primary_lane === 'blocked') return 'attention';
	if (workflow.primary_lane === 'processing') return 'processing';
	if (workflow.primary_lane === 'validate') return 'validate';
	if (workflow.primary_lane === 'promote') return 'promote';
	if (workflow.primary_lane === 'encode') return 'encode';
	return folder.pending_count > 0 ? 'sample' : 'other';
}

export function buildWorkLaneGroups(folders: FolderCard[]): WorkLaneGroup[] {
	const grouped = new Map<WorkLaneKey, FolderCard[]>();
	for (const folder of folders) {
		const key = workLaneKey(folder);
		grouped.set(key, [...(grouped.get(key) ?? []), folder]);
	}
	return WORK_LANE_ORDER.map((key) => {
		const laneFolders = grouped.get(key) ?? [];
		const meta = WORK_LANE_META[key];
		return {
			key,
			label: meta.label,
			detail: meta.detail,
			tone: meta.tone,
			folders: laneFolders,
			openWork: totalPendingItems(laneFolders)
		};
	}).filter((group) => group.folders.length > 0);
}

export function buildQueueStatusTiles(
	dashboard: DashboardSummaryPayload,
	foldersPayload: DashboardFoldersPayload,
	hosts: HostsPayload,
	scopeLabel = 'folders'
): StatusTile[] {
	const folders = foldersPayload.folders;
	const visibleScopeLabel = scopeLabel.trim().toLowerCase() || 'folders';
	const readyHosts = hosts.hosts.filter((host) => host.available).length;
	const encodeQueue = dashboard.encode_queue;
	const scanStatus = dashboard.scan_job?.status ?? 'idle';
	return [
		{
			label: 'Next work',
			value: `${folders.length} ${visibleScopeLabel}`,
			detail: `${totalPendingItems(folders).toLocaleString('en-US')} open work`,
			tone: folders.length ? 'wait' : 'idle'
		},
		{
			label: 'Projected reclaim',
			value: formatBytes(totalProjectedReclaim(folders)),
			detail: `estimated from visible ${visibleScopeLabel}`,
			tone: totalProjectedReclaim(folders) > 0 ? 'ready' : 'idle',
			mono: true
		},
		{
			label: 'Scan',
			value:
				scanStatus === 'running'
					? 'running'
					: scanStatus === 'queued'
						? 'queued'
						: scanStatus === 'failed'
							? 'needs attention'
							: 'idle',
			detail: dashboard.scan_job?.prefix ?? dashboard.scan_job?.scope ?? 'catalog scope',
			tone: ['queued', 'running'].includes(scanStatus)
				? 'active'
				: scanStatus === 'failed'
					? 'fail'
					: 'idle'
		},
		{
			label: 'Processing',
			value: `${encodeQueue.running_count} running · ${encodeQueue.queued_count} queued`,
			detail:
				encodeQueue.telemetry?.eta_copy ??
				encodeQueue.state.scheduler_summary ??
				'work window state',
			tone: encodeQueue.state.stop_requested
				? 'fail'
				: encodeQueue.state.is_paused
					? 'wait'
					: encodeQueue.running_count > 0
						? 'active'
						: 'idle'
		},
		{
			label: 'Workers',
			value: `${readyHosts} ready / ${hosts.hosts.length}`,
			detail: hosts.hosts.length ? 'capacity check complete' : 'worker status unavailable',
			tone: readyHosts > 0 ? 'ready' : hosts.hosts.length > 0 ? 'wait' : 'idle'
		}
	];
}

export function buildQueueFooterSignals(
	dashboard: DashboardSummaryPayload,
	foldersPayload: DashboardFoldersPayload,
	hosts: HostsPayload
): FooterSignal[] {
	return [
		{ label: 'Folders', value: String(foldersPayload.folders.length), tone: 'wait' },
		{
			label: 'Reclaim',
			value: formatBytes(totalProjectedReclaim(foldersPayload.folders)),
			tone: 'ready'
		},
		{
			label: 'Samples',
			value: `${dashboard.calibration_queue.active_count} active`,
			tone: dashboard.calibration_queue.active_count > 0 ? 'active' : 'idle'
		},
		{
			label: 'Workers',
			value: `${hosts.hosts.filter((host) => host.available).length}/${hosts.hosts.length}`
		}
	];
}

export function folderStatusCopy(folder: FolderCard): string {
	const workflowDetail = folder.workflow_state?.detail?.trim();
	if (workflowDetail) return workflowDetail;
	return folder.review_badge_detail || summarizeStatuses(folder.statuses);
}
