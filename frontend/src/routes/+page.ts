import { fetchJson } from '$lib/api/client';
import type { DashboardPayload, HostsPayload } from '$lib/api/types';

export async function load({ fetch }: { fetch: typeof window.fetch }) {
	const [dashboard, hosts] = await Promise.all([
		fetchJson<DashboardPayload>('/api/dashboard', fetch),
		fetchJson<HostsPayload>('/api/hosts?compact=1', fetch)
	]);

	return { dashboard, hosts };
}
