import { fetchJson } from '$lib/api/client';
import type { HostsPayload, SettingsPayload } from '$lib/api/types';

type SettledPayload<T> = { data: T; error: null } | { data: null; error: string };

function errorMessage(error: unknown): string {
	return error instanceof Error ? error.message : 'Request failed.';
}

async function settle<T>(promise: Promise<T>): Promise<SettledPayload<T>> {
	try {
		return { data: await promise, error: null };
	} catch (error) {
		return { data: null, error: errorMessage(error) };
	}
}

export async function load({ fetch }: { fetch: typeof window.fetch }) {
	const [settings, hosts] = await Promise.all([
		settle(fetchJson<SettingsPayload>('/api/settings', fetch)),
		settle(fetchJson<HostsPayload>('/api/hosts?compact=1', fetch))
	]);
	const loadError = [settings.error, hosts.error].filter(Boolean).join(' · ') || null;

	return {
		settings: settings.data,
		hosts: hosts.data,
		loadError
	};
}
