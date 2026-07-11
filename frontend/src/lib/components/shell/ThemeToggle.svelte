<script lang="ts">
	import { onMount } from 'svelte';

	import { applyTheme, nextTheme, THEME_STORAGE_KEY, type Theme } from '$lib/theme';

	let theme = $state<Theme>('light');

	onMount(() => {
		theme = document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light';
	});

	function toggleTheme() {
		theme = nextTheme(theme);
		applyTheme(theme);
		localStorage.setItem(THEME_STORAGE_KEY, theme);
	}
</script>

<button
	type="button"
	class="theme-toggle"
	onclick={toggleTheme}
	aria-label={theme === 'dark' ? 'Use light mode' : 'Use dark mode'}
	title={theme === 'dark' ? 'Use light mode' : 'Use dark mode'}
>
	{#if theme === 'dark'}
		<svg viewBox="0 0 20 20" aria-hidden="true">
			<circle cx="10" cy="10" r="3.25" />
			<path
				d="M10 2.5v2M10 15.5v2M2.5 10h2M15.5 10h2M4.7 4.7l1.4 1.4M13.9 13.9l1.4 1.4M15.3 4.7l-1.4 1.4M6.1 13.9l-1.4 1.4"
			/>
		</svg>
	{:else}
		<svg viewBox="0 0 20 20" aria-hidden="true">
			<path d="M15.8 12.7A6.4 6.4 0 0 1 7.3 4.2 6.4 6.4 0 1 0 15.8 12.7Z" />
		</svg>
	{/if}
</button>

<style>
	.theme-toggle {
		align-items: center;
		align-self: center;
		background: var(--mf-bg-panel-2);
		border: 1px solid var(--mf-line);
		border-radius: var(--mf-radius-2);
		color: var(--mf-fg-secondary);
		display: inline-flex;
		flex: 0 0 auto;
		height: 34px;
		justify-content: center;
		margin-left: auto;
		width: 34px;
	}

	.theme-toggle:hover {
		background: var(--mf-active-bg);
		border-color: var(--mf-active-line);
		color: var(--mf-active-fg);
	}

	.theme-toggle svg {
		fill: none;
		height: 17px;
		stroke: currentColor;
		stroke-linecap: round;
		stroke-linejoin: round;
		stroke-width: 1.5;
		width: 17px;
	}

	@media (max-width: 680px) {
		.theme-toggle {
			height: 32px;
			margin-left: 4px;
			width: 32px;
		}
	}
</style>
