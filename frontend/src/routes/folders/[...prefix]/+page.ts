import { fetchJson } from '$lib/api/client';
import type { FolderPayload, FolderStatusPayload, HostsPayload } from '$lib/api/types';

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
	const encodedPrefix = encodePrefix(prefix);
	const [folder, status, hosts] = await Promise.all([
		fetchJson<FolderPayload>(`/api/folders/${encodedPrefix}`, fetch),
		fetchJson<FolderStatusPayload>(`/api/folders/${encodedPrefix}/status`, fetch),
		fetchJson<HostsPayload>('/api/hosts?compact=1', fetch)
	]);

	return { folder, status, hosts };
}
