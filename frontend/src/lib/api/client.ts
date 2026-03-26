type FetchLike = typeof fetch;

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
		const message = await response.text();
		throw new Error(message || `Request failed with status ${response.status}`);
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
