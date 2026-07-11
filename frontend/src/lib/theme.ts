export const THEME_STORAGE_KEY = 'mediaforce-theme';

export type Theme = 'light' | 'dark';

export function resolvedTheme(storedTheme: string | null, prefersDark: boolean): Theme {
	if (storedTheme === 'light' || storedTheme === 'dark') return storedTheme;
	return prefersDark ? 'dark' : 'light';
}

export function nextTheme(theme: Theme): Theme {
	return theme === 'dark' ? 'light' : 'dark';
}

export function applyTheme(theme: Theme, root: HTMLElement = document.documentElement): void {
	root.dataset.theme = theme;
	root.style.colorScheme = theme;
}
