import { fetchJson } from '$lib/api/client';
import type {
	DashboardFoldersPayload,
	DashboardSummaryPayload,
	FolderPayload,
	FolderStatusPayload,
	HostsPayload
} from '$lib/api/types';

function encodePrefix(prefix: string): string {
	return prefix
		.split('/')
		.map((segment) => encodeURIComponent(segment))
		.join('/');
}

export async function load({
	fetch,
	params
}: {
	fetch: typeof window.fetch;
	params: { prefix?: string };
}) {
	const prefix = params.prefix ?? '';
	if (!prefix) {
		const [dashboard, foldersPayload, hosts] = await Promise.all([
			fetchJson<DashboardSummaryPayload>('/api/dashboard', fetch),
			fetchJson<DashboardFoldersPayload>('/api/dashboard/folders', fetch),
			fetchJson<HostsPayload>('/api/hosts?compact=1', fetch)
		]);

		return { mode: 'directory' as const, dashboard, foldersPayload, hosts };
	}

	const encodedPrefix = encodePrefix(prefix);
	const [folder, status, hosts] = await Promise.all([
		fetchJson<FolderPayload>(`/api/folders/${encodedPrefix}`, fetch),
		fetchJson<FolderStatusPayload>(`/api/folders/${encodedPrefix}/status`, fetch),
		fetchJson<HostsPayload>('/api/hosts?compact=1', fetch)
	]);

	return { mode: 'studio' as const, folder, status, hosts };
}
