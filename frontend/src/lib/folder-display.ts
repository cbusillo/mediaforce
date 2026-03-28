import { titleCase } from '$lib/format';

export type FolderLibraryTheme = {
	base: string;
	surface: string;
	border: string;
	text: string;
	glow: string;
	softText: string;
};

const DEFAULT_LIBRARY_COLOR = '#0f766e';

export function folderLibraryKey(prefix: string): string {
	return prefix.split('/')[0] ?? prefix;
}

export function folderLibraryLabel(key: string): string {
	return key.toLowerCase() === 'tv' ? 'TV' : titleCase(key);
}

export function folderScopeLabel(prefix: string, scopeLabel: string): string {
	const key = folderLibraryKey(prefix).toLowerCase();
	if (key === 'movies' && scopeLabel.toLowerCase() === 'folder') {
		return 'Movie';
	}
	return scopeLabel;
}

function normalizeLibraryColor(value?: string | null): string {
	if (value && /^#[0-9a-fA-F]{6}$/.test(value)) {
		return value.toLowerCase();
	}
	return DEFAULT_LIBRARY_COLOR;
}

function hexToRgb(hex: string): [number, number, number] {
	const normalized = normalizeLibraryColor(hex).slice(1);
	return [0, 2, 4].map((index) => Number.parseInt(normalized.slice(index, index + 2), 16)) as [
		number,
		number,
		number,
	];
}

function rgba(hex: string, alpha: number): string {
	const [red, green, blue] = hexToRgb(hex);
	return `rgba(${red}, ${green}, ${blue}, ${alpha})`;
}

function mixChannel(value: number, target: number, weight: number): number {
	return Math.round(value * (1 - weight) + target * weight);
}

function mixHex(hex: string, target: number, weight: number): string {
	const [red, green, blue] = hexToRgb(hex);
	const mixed = [red, green, blue].map((channel) => mixChannel(channel, target, weight));
	return `#${mixed.map((channel) => channel.toString(16).padStart(2, '0')).join('')}`;
}

export function folderLibraryTheme(color?: string | null): FolderLibraryTheme {
	const base = normalizeLibraryColor(color);
	return {
		base,
		surface: rgba(base, 0.11),
		border: rgba(base, 0.2),
		text: mixHex(base, 0, 0.2),
		glow: rgba(base, 0.16),
		softText: mixHex(base, 255, 0.32),
	};
}

export function folderLibraryThemeStyle(color?: string | null): string {
	const theme = folderLibraryTheme(color);
	return [
		`--library-base:${theme.base}`,
		`--library-surface:${theme.surface}`,
		`--library-border:${theme.border}`,
		`--library-text:${theme.text}`,
		`--library-glow:${theme.glow}`,
		`--library-soft-text:${theme.softText}`
	].join(';');
}
