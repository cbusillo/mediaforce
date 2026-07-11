import { describe, expect, it } from 'vitest';

import { nextTheme, resolvedTheme } from './theme';

describe('theme preference', () => {
	it('uses an explicit saved theme before the system preference', () => {
		expect(resolvedTheme('light', true)).toBe('light');
		expect(resolvedTheme('dark', false)).toBe('dark');
	});

	it('falls back to the system preference and toggles explicitly', () => {
		expect(resolvedTheme(null, true)).toBe('dark');
		expect(resolvedTheme(null, false)).toBe('light');
		expect(nextTheme('dark')).toBe('light');
	});
});
