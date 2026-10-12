import type { AudioPresetPayload } from '$lib/api/types';

export function soundChoiceHelp(preset: AudioPresetPayload): string {
	const current = preset.options.find((option) => option.key === preset.current);
	const source =
		preset.source === 'show'
			? 'Chosen for this show.'
			: preset.source === 'library'
				? 'The TV library default.'
				: 'The standard choice.';
	return [current?.summary, source, 'A change needs a new sample.'].filter(Boolean).join(' ');
}
