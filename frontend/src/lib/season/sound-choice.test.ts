import { describe, expect, it } from 'vitest';

import type { AudioPresetPayload } from '$lib/api/types';
import { soundChoiceHelp } from './sound-choice';

const preset: AudioPresetPayload = {
	show_prefix: 'tv/Big Brother (US)',
	current: 'speech_stereo',
	source: 'show',
	library_default: null,
	options: [
		{ key: 'keep_surround', label: 'Film', summary: 'Keeps surround sound.' },
		{ key: 'speech_stereo', label: 'Speech-heavy', summary: 'Stereo at 96 kbps.' }
	]
};

describe('soundChoiceHelp', () => {
	it('says what the choice does and where it came from', () => {
		expect(soundChoiceHelp(preset)).toBe(
			'Stereo at 96 kbps. Chosen for this show. A change needs a new sample.'
		);
		expect(soundChoiceHelp({ ...preset, current: 'keep_surround', source: 'library' })).toBe(
			'Keeps surround sound. The TV library default. A change needs a new sample.'
		);
		expect(soundChoiceHelp({ ...preset, current: 'keep_surround', source: 'default' })).toBe(
			'Keeps surround sound. The standard choice. A change needs a new sample.'
		);
	});
});
