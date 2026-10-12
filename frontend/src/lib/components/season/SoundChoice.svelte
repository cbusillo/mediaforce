<script lang="ts">
	import type { AudioPresetPayload } from '$lib/api/types';
	import { soundChoiceHelp } from '$lib/season/sound-choice';

	let {
		preset,
		saving = false,
		onChoose
	}: {
		preset: AudioPresetPayload;
		saving?: boolean;
		onChoose: (key: string) => void;
	} = $props();

	const helpId = $derived(`sound-choice-help-${preset.show_prefix.replace(/[^a-z0-9]+/gi, '-')}`);
</script>

<label class="sound-choice">
	<span>Sound</span>
	<select
		value={preset.current}
		disabled={saving}
		aria-describedby={helpId}
		onchange={(event) => onChoose((event.currentTarget as HTMLSelectElement).value)}
	>
		{#each preset.options as option (option.key)}
			<option value={option.key}>{option.label}</option>
		{/each}
	</select>
	<small id={helpId}>{saving ? 'Saving the sound choice…' : soundChoiceHelp(preset)}</small>
</label>

<style>
	.sound-choice {
		display: grid;
		gap: var(--mf-space-1);
		min-width: 0;
	}

	.sound-choice span {
		font-size: 11px;
	}

	.sound-choice select {
		background: var(--mf-bg-panel-2);
		border: 1px solid var(--mf-line-strong);
		border-radius: var(--mf-radius-1);
		color: var(--mf-fg-primary);
		font: inherit;
		min-height: 30px;
		padding: 3px 28px 3px 8px;
		width: 100%;
	}

	.sound-choice small {
		color: var(--mf-fg-tertiary);
		font-size: 11px;
		line-height: 1.35;
		max-width: 360px;
	}

	/* On a phone the inspector stays short: one row, with the help kept for screen readers. */
	@media (max-width: 760px) {
		.sound-choice {
			align-items: center;
			grid-template-columns: auto minmax(0, 1fr);
			column-gap: var(--mf-space-3);
		}

		.sound-choice small {
			clip-path: inset(50%);
			height: 1px;
			overflow: hidden;
			position: absolute;
			white-space: nowrap;
			width: 1px;
		}
	}
</style>
