type FetchLike = typeof fetch;

export class ApiError extends Error {
	readonly status: number;
	readonly payload: Record<string, unknown> | null;

	constructor(message: string, status: number, payload: Record<string, unknown> | null) {
		super(message);
		this.name = 'ApiError';
		this.status = status;
		this.payload = payload;
	}
}

export function apiDownloadHref(path: string): string {
	if (typeof window === 'undefined') return path;
	const normalizedPath = path.startsWith('/') ? path : `/${path}`;
	if (window.location.port === '4173') {
		const backendUrl = new URL(normalizedPath, window.location.origin);
		backendUrl.port = '8777';
		return backendUrl.pathname + backendUrl.search + backendUrl.hash;
	}
	return normalizedPath;
}

function parseErrorPayload(raw: string): Record<string, unknown> | null {
	try {
		const parsed = JSON.parse(raw) as unknown;
		return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
			? (parsed as Record<string, unknown>)
			: null;
	} catch {
		return null;
	}
}

function extractErrorMessage(
	raw: string,
	status: number,
	payload: Record<string, unknown> | null
): string {
	const trimmed = raw.trim();
	if (!trimmed) {
		return `Request failed with status ${status}`;
	}

	if (payload) {
		for (const key of ['message', 'detail', 'error']) {
			const value = payload[key];
			if (typeof value === 'string' && value.trim()) {
				return value.trim();
			}
		}
	}

	return trimmed;
}

export async function fetchJson<T>(
	input: RequestInfo | URL,
	fetcher: FetchLike = fetch,
	init?: RequestInit
): Promise<T> {
	const response = await fetcher(input, {
		headers: {
			Accept: 'application/json',
			...(init?.headers ?? {})
		},
		...init
	});

	if (!response.ok) {
		const raw = await response.text();
		const payload = parseErrorPayload(raw);
		throw new ApiError(
			extractErrorMessage(raw, response.status, payload),
			response.status,
			payload
		);
	}

	return (await response.json()) as T;
}

export async function postJson<T>(
	input: RequestInfo | URL,
	body: unknown,
	fetcher: FetchLike = fetch,
	init?: Omit<RequestInit, 'body' | 'method'>
): Promise<T> {
	return fetchJson<T>(input, fetcher, {
		method: 'POST',
		body: JSON.stringify(body),
		headers: {
			'Content-Type': 'application/json',
			...(init?.headers ?? {})
		},
		...init
	});
}
