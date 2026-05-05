import { fetchJson } from '$lib/api/client';
import type { DashboardSummaryPayload, HostsPayload } from '$lib/api/types';

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
	const [dashboard, hosts] = await Promise.all([
		settle(fetchJson<DashboardSummaryPayload>('/api/dashboard', fetch)),
		settle(fetchJson<HostsPayload>('/api/hosts?compact=1', fetch))
	]);
	const loadError = [dashboard.error, hosts.error].filter(Boolean).join(' · ') || null;

	return {
		dashboard: dashboard.data,
		hosts: hosts.data,
		loadError
	};
}
