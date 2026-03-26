import { fetchJson } from '$lib/api/client';
import type { HostsPayload, SettingsPayload } from '$lib/api/types';

export async function load({ fetch }: { fetch: typeof window.fetch }) {
	const [settings, hosts] = await Promise.all([
		fetchJson<SettingsPayload>('/api/settings', fetch),
		fetchJson<HostsPayload>('/api/hosts', fetch)
	]);

	return { settings, hosts };
}
