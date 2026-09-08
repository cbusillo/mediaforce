import { describe, expect, it } from 'vitest';

import { summarizeWorkStates, type LibraryTone } from './library-layout';

type WorkItem = { key: string; label: string; tone: LibraryTone };

describe('summarizeWorkStates', () => {
	it('groups matching states and excludes idle work', () => {
		const items: WorkItem[] = [
			{ key: 'ready', label: 'Ready to act on', tone: 'ready' },
			{ key: 'ready', label: 'Ready to act on', tone: 'ready' },
			{ key: 'processing', label: 'Compressing', tone: 'active' },
			{ key: 'idle', label: 'No work', tone: 'idle' }
		];

		expect(summarizeWorkStates(items, (item) => item)).toEqual([
			{ key: 'ready', count: 2, label: 'Ready to act on', tone: 'ready' },
			{ key: 'processing', count: 1, label: 'Compressing', tone: 'active' }
		]);
	});

	it('keeps every reachable state group', () => {
		const items: WorkItem[] = [
			{ key: 'one', label: 'One', tone: 'active' },
			{ key: 'two', label: 'Two', tone: 'ready' },
			{ key: 'three', label: 'Three', tone: 'wait' },
			{ key: 'four', label: 'Four', tone: 'fail' },
			{ key: 'five', label: 'Five', tone: 'active' }
		];

		const segments = summarizeWorkStates(items, (item) => item);

		expect(segments).toHaveLength(5);
		expect(segments.reduce((total, segment) => total + segment.count, 0)).toBe(items.length);
		expect(segments.map((segment) => segment.key).sort()).toEqual([
			'five',
			'four',
			'one',
			'three',
			'two'
		]);
	});
});
