export interface StudioRefreshPlan {
	prefix: string;
	navigate: boolean;
}

export function studioRefreshPlan(
	currentPrefix: string,
	requestedPrefix?: string | null
): StudioRefreshPlan {
	const current = normalizePrefix(currentPrefix);
	const requested = normalizePrefix(requestedPrefix ?? '');
	const prefix = requested || current;
	return { prefix, navigate: prefix !== current };
}

function normalizePrefix(prefix: string): string {
	return prefix.trim().replace(/^\/+|\/+$/g, '');
}
