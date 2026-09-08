export type LibraryMode = 'tv' | 'movie' | 'other';
export type LibraryTone = 'active' | 'ready' | 'wait' | 'fail' | 'idle';

export type LibraryMetric = {
	value: string;
	label: string;
	detail?: string;
	pending?: boolean;
};

export type LibraryWorkSegment = {
	key: string;
	count: number;
	label: string;
	tone: LibraryTone;
};

export type LibraryWorkState = {
	key: string;
	label: string;
	tone: LibraryTone;
};

export type LibraryNotice = {
	title: string;
	detail: string;
	tone?: 'fail' | 'wait' | 'idle';
};

export function summarizeWorkStates<T>(
	items: T[],
	stateFor: (item: T) => LibraryWorkState | null
): LibraryWorkSegment[] {
	const counts: Record<string, LibraryWorkSegment> = {};
	for (const item of items) {
		const state = stateFor(item);
		if (!state || state.tone === 'idle') continue;
		counts[state.key] = {
			...state,
			count: (counts[state.key]?.count ?? 0) + 1
		};
	}
	return Object.values(counts).sort(
		(left, right) => right.count - left.count || left.label.localeCompare(right.label)
	);
}
