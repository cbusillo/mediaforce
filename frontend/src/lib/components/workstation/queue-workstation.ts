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

export function buildQueueStatusTiles(
	dashboard: DashboardSummaryPayload,
	foldersPayload: DashboardFoldersPayload,
	hosts: HostsPayload
): StatusTile[] {
	const folders = foldersPayload.folders;
	const readyHosts = hosts.hosts.filter((host) => host.available).length;
	const encodeQueue = dashboard.encode_queue;
	const scanStatus = dashboard.scan_job?.status ?? 'idle';
	return [
		{
			label: 'Next work',
			value: `${folders.length} folders`,
			detail: `${totalPendingItems(folders).toLocaleString('en-US')} pending items`,
			tone: folders.length ? 'wait' : 'idle'
		},
		{
			label: 'Projected reclaim',
			value: formatBytes(totalProjectedReclaim(folders)),
			detail: 'estimated from visible folders',
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
