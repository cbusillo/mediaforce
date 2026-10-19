import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';

import MotionKeepFiles from './MotionKeepFiles.svelte';

const held = [
	{
		library_item_id: 1,
		rel_path: 'tv/Show/Season 19/E01.mkv',
		reason: 'mixed' as const,
		as_is: false
	},
	{
		library_item_id: 2,
		rel_path: 'tv/Show/Season 19/E02.mkv',
		reason: 'unclear' as const,
		as_is: true
	}
];
const kept = [
	{
		library_item_id: 3,
		rel_path: 'tv/Show/Season 19/E03.mkv',
		reason: 'partly_interlaced' as const,
		kept_at: null
	}
];

function body(props: Record<string, unknown>): string {
	return render(MotionKeepFiles, {
		props: { onKeep: () => {}, onUndo: () => {}, ...props }
	}).body;
}

describe('MotionKeepFiles', () => {
	it('offers to keep each held episode original, and all of them at once', () => {
		const html = body({ held });

		expect(html).toContain('Keep E01.mkv original');
		expect(html).toContain('Keep E02.mkv original');
		expect(html).toContain(`Keep all ${held.length} original`);
		expect(html).not.toContain('Kept original');
	});

	it('lists kept episodes with their reason and an undo', () => {
		const html = body({ kept });

		expect(html).toContain('E03.mkv');
		expect(html).toContain('Partly interlaced');
		expect(html).toContain('Undo keeping E03.mkv original');
		expect(html).not.toContain('Keep original');
	});

	it('renders nothing when no episode is held or kept', () => {
		expect(
			body({})
				.replace(/<!--.*?-->/g, '')
				.trim()
		).toBe('');
	});
});
