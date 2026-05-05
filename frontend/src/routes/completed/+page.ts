import { fetchJson } from '$lib/api/client';
import type { CompletedPayload } from '$lib/api/types';

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
	const completed = await settle(fetchJson<CompletedPayload>('/api/completed', fetch));

	return {
		completed: completed.data,
		loadError: completed.error
	};
}
