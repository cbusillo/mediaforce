type FetchLike = typeof fetch;

function extractErrorMessage(raw: string, status: number): string {
	const trimmed = raw.trim();
	if (!trimmed) {
		return `Request failed with status ${status}`;
	}

	try {
		const parsed = JSON.parse(trimmed) as Record<string, unknown>;
		for (const key of ['message', 'detail', 'error']) {
			const value = parsed[key];
			if (typeof value === 'string' && value.trim()) {
				return value.trim();
			}
		}
	} catch {
		// Fall back to the raw response body when the server did not return JSON.
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
		const message = extractErrorMessage(await response.text(), response.status);
		throw new Error(message);
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
