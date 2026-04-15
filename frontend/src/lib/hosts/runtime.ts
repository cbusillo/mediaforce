import type { HostRuntime, HostsPayload } from '$lib/api/types';

export const HOST_STATUS_PENDING_MESSAGE = 'Checking host status...';

type QualitySearchMode = 'worker-local' | 'fully-remote' | 'local-assist';

type QualitySearchSummary = {
	mode: QualitySearchMode;
	label: string;
	detail: string;
};

function hostHasAnySourceRoots(runtime: HostRuntime | null): boolean {
	if (!runtime?.source_roots) return false;
	return Object.values(runtime.source_roots).some((value) => String(value ?? '').trim().length > 0);
}

export function qualitySearchSummary(runtime: HostRuntime | null): QualitySearchSummary | null {
	if (!runtime) return null;
	const mediaAccess = String(runtime.media_access ?? '')
		.trim()
		.toLowerCase();
	if (mediaAccess === 'stream') {
		if (hostHasAnySourceRoots(runtime)) {
			return {
				mode: 'fully-remote',
				label: 'Search runs on worker',
				detail: 'Mapped source paths let this worker handle the quality search remotely.'
			};
		}
		return {
			mode: 'local-assist',
			label: 'Search starts here',
			detail: 'Quality search samples on this machine first, then the encode streams to the worker.'
		};
	}
	return {
		mode: 'worker-local',
		label: 'Search runs on worker',
		detail: 'This worker has mounted media access, so search and encode stay on the worker.'
	};
}

export function folderAwareQualitySearchSummary(
	runtime: HostRuntime | null,
	folderPrefix: string | null | undefined
): QualitySearchSummary | null {
	if (!runtime) return null;
	const mediaAccess = String(runtime.media_access ?? '')
		.trim()
		.toLowerCase();
	const mediaRoot = String(folderPrefix ?? '')
		.split('/')
		.filter(Boolean)[0]
		?.trim();
	if (mediaAccess !== 'stream') {
		return {
			mode: 'worker-local',
			label: 'Search runs on worker',
			detail:
				'This host can search and encode the representative file without using your machine first.'
		};
	}
	const mappedRoot = mediaRoot ? String(runtime.source_roots?.[mediaRoot] ?? '').trim() : '';
	if (mappedRoot) {
		return {
			mode: 'fully-remote',
			label: 'Search runs on worker',
			detail: `This folder maps to ${mappedRoot}, so the quality search stays on ${runtime.label}.`
		};
	}
	if (hostHasAnySourceRoots(runtime)) {
		return {
			mode: 'local-assist',
			label: 'Search starts here',
			detail:
				'This host has remote roots for other libraries, but this folder still needs local quality-search help first.'
		};
	}
	return {
		mode: 'local-assist',
		label: 'Search starts here',
		detail: 'This host streams the encode, but quality search still starts on this machine first.'
	};
}

export function hostsStatusPending(payload: HostsPayload | null | undefined): boolean {
	return (payload?.hosts ?? []).some(
		(host) => isPendingHostRuntime(host) && Array.isArray(host.issues) && host.issues.length === 0
	);
}

export function isPendingHostRuntime(runtime: HostRuntime | null | undefined): boolean {
	return String(runtime?.message ?? '').trim() === HOST_STATUS_PENDING_MESSAGE;
}
