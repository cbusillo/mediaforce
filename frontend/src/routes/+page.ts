import { fetchJson } from '$lib/api/client';
import type {
	DashboardFoldersPayload,
	DashboardSummaryPayload,
	HostsPayload
} from '$lib/api/types';

export async function load({ fetch }: { fetch: typeof window.fetch }) {
	const [dashboard, hosts] = await Promise.all([
		fetchJson<DashboardSummaryPayload>('/api/dashboard', fetch),
		fetchJson<HostsPayload>('/api/hosts?compact=1', fetch)
	]);
	const foldersPayload: DashboardFoldersPayload = {
		folders: dashboard.folders_preview,
		folder_cache_key: dashboard.folder_cache_key,
		catalog_empty: dashboard.catalog_empty
	};

	return { dashboard, foldersPayload, hosts };
}
