import { fetchJson } from '$lib/api/client';
import type {
	DashboardFoldersPayload,
	DashboardSummaryPayload,
	HostsPayload
} from '$lib/api/types';

export async function load({ fetch }: { fetch: typeof window.fetch }) {
	const [dashboard, foldersPayload, hosts] = await Promise.all([
		fetchJson<DashboardSummaryPayload>('/api/dashboard', fetch),
		fetchJson<DashboardFoldersPayload>('/api/dashboard/folders', fetch),
		fetchJson<HostsPayload>('/api/hosts?compact=1', fetch)
	]);

	return { dashboard, foldersPayload, hosts };
}
