<script lang="ts">
	import { resolve } from '$app/paths';

	let { active }: { active: 'tv' | 'movie' | 'other' } = $props();
</script>

<nav class="library-mode-nav" aria-label="Library type">
	<a
		href={resolve('/')}
		class:active={active === 'tv'}
		aria-current={active === 'tv' ? 'page' : undefined}>TV</a
	>
	<a
		href={resolve('/movies')}
		class:active={active === 'movie'}
		aria-current={active === 'movie' ? 'page' : undefined}>Movies</a
	>
	<a
		href={resolve('/other')}
		class:active={active === 'other'}
		aria-current={active === 'other' ? 'page' : undefined}>Other</a
	>
</nav>

<style>
	.library-mode-nav {
		align-items: center;
		border-bottom: 1px solid var(--mf-line);
		display: flex;
		gap: 22px;
	}

	a {
		border-bottom: 2px solid transparent;
		color: var(--mf-fg-secondary);
		font-size: 13px;
		font-weight: 700;
		letter-spacing: 0.02em;
		padding: 12px 1px 10px;
		text-decoration: none;
	}

	a:hover {
		color: var(--mf-fg-primary);
	}

	a.active {
		border-bottom-color: var(--mf-active-fg);
		color: var(--mf-active-fg);
	}
</style>
