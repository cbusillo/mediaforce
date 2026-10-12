<script lang="ts">
	import type { AudioPresetPayload } from '$lib/api/types';
	import { soundChoiceHelp } from '$lib/season/sound-choice';

	let {
		preset,
		saving = false,
		onChoose,
		onDismissSuggestion
	}: {
		preset: AudioPresetPayload;
		saving?: boolean;
		onChoose: (key: string) => void;
		onDismissSuggestion: () => void;
	} = $props();

	const helpId = $derived(`sound-choice-help-${preset.show_prefix.replace(/[^a-z0-9]+/gi, '-')}`);
</script>

<div class="sound-choice">
	<label>
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
	{#if preset.suggestion}
		{@const suggestion = preset.suggestion}
		<div class="sound-suggestion" role="status">
			<p><strong>{suggestion.label} may fit.</strong> {suggestion.reason}</p>
			<div>
				<button type="button" disabled={saving} onclick={() => onChoose(suggestion.preset)}
					>Use {suggestion.label}</button
				>
				<button type="button" class="quiet" disabled={saving} onclick={onDismissSuggestion}
					>Keep as is</button
				>
			</div>
		</div>
	{/if}
</div>

<style>
	.sound-choice {
		display: grid;
		gap: var(--mf-space-2);
		min-width: 0;
	}

	.sound-choice label {
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

	.sound-suggestion {
		background: var(--mf-wait-bg);
		border: 1px solid var(--mf-wait-line);
		border-radius: var(--mf-radius-2);
		display: grid;
		gap: var(--mf-space-2);
		padding: var(--mf-space-2) var(--mf-space-3);
	}

	.sound-suggestion p {
		color: var(--mf-fg-secondary);
		font-size: 12px;
		line-height: 1.4;
		margin: 0;
	}

	.sound-suggestion div {
		display: flex;
		flex-wrap: wrap;
		gap: var(--mf-space-2);
	}

	.sound-suggestion button {
		background: var(--mf-bg-panel);
		border: 1px solid var(--mf-line-strong);
		border-radius: var(--mf-radius-1);
		color: var(--mf-fg-primary);
		font: inherit;
		font-size: 12px;
		font-weight: 650;
		padding: 4px 10px;
	}

	.sound-suggestion button.quiet {
		background: transparent;
		border-color: transparent;
		color: var(--mf-fg-secondary);
	}

	/* On a phone the inspector stays short: one row, with the help kept for screen readers. */
	@media (max-width: 760px) {
		.sound-choice label {
			align-items: center;
			column-gap: var(--mf-space-3);
			grid-template-columns: auto minmax(0, 1fr);
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
