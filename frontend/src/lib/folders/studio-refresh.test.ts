import { describe, expect, it } from 'vitest';

import { studioRefreshPlan } from './studio-refresh';

describe('studioRefreshPlan', () => {
	it('navigates to the surviving title after an exact movie file is replaced', () => {
		expect(
			studioRefreshPlan(
				'movies/2101 (2014)/2101 (2014) [WEBRip] [1080p] [YTS.AM].mp4',
				'movies/2101 (2014)'
			)
		).toEqual({ prefix: 'movies/2101 (2014)', navigate: true });
	});

	it('navigates to the promoted path when a root-level movie file changes extension', () => {
		expect(
			studioRefreshPlan('movies/Loose Feature (2024).mp4', 'movies/Loose Feature (2024).mkv')
		).toEqual({ prefix: 'movies/Loose Feature (2024).mkv', navigate: true });
	});

	it('refreshes in place when the action target is already the current scope', () => {
		expect(studioRefreshPlan('movies/2101 (2014)', '/movies/2101 (2014)/')).toEqual({
			prefix: 'movies/2101 (2014)',
			navigate: false
		});
	});

	it('falls back to the current scope when the action has no target', () => {
		expect(studioRefreshPlan('movies/2101 (2014)')).toEqual({
			prefix: 'movies/2101 (2014)',
			navigate: false
		});
	});
});
