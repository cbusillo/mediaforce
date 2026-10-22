import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';

import type { LibraryAvailability, LibraryType, SettingsLibrary } from '$lib/api/types';
import LibraryRootsPanel from './LibraryRootsPanel.svelte';

function library(libraryType: LibraryType, availability: LibraryAvailability): SettingsLibrary {
	return {
		index: '0',
		key: `${libraryType}_library`,
		label: 'Library',
		path: '/Volumes/Library',
		color: '#4e6fa6',
		library_type: libraryType,
		availability,
		default_profile: '',
		plex_path: '',
		policy: {},
		readiness: { state: 'ready', label: 'Ready', detail: '' },
		type_change_confirmation: ''
	};
}

function visibleText(selected: SettingsLibrary): string {
	const html = render(LibraryRootsPanel, {
		props: {
			libraries: [selected],
			typeOptions: [
				{ key: 'tv', label: 'TV' },
				{ key: 'movie', label: 'Movies' },
				{ key: 'spatial', label: '3D / VR' },
				{ key: 'other', label: 'Other' }
			],
			profileOptions: { tv: [], movie: [], spatial: [], other: [] },
			audioPresetOptions: [],
			plexReady: false,
			plexTokenEnv: '',
			onLibrariesChange: () => {},
			onAdd: () => {},
			onRemove: () => {},
			onMove: () => {},
			onTypeChange: () => {}
		}
	}).body;
	// Option values and other attributes are internal keys; only rendered text reaches the screen.
	return html.replace(/<[^>]*>/g, ' ');
}

describe('LibraryRootsPanel', () => {
	it.each([
		['spatial', 'browse_only'],
		['other', 'production'],
		['movie', 'disabled'],
		['tv', 'production']
	] as const)('shows a %s library (%s) without setup jargon', (libraryType, availability) => {
		const text = visibleText(library(libraryType, availability));

		expect(text).not.toMatch(/\broots?\b|production|browse only|policy|#\d+\b|work unit|profile/i);
	});
});
