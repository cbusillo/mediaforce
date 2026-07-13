<script lang="ts">
	import { resolve } from '$app/paths';
	import { page } from '$app/state';
	import type { Snippet } from 'svelte';
	import ThemeToggle from './ThemeToggle.svelte';

	let { children }: { children: Snippet } = $props();

	const navigation = [
		{ label: 'Library', href: '/', icon: 'seasons' },
		{ label: 'Activity', href: '/ops', icon: 'activity' },
		{ label: 'Finished', href: '/completed', icon: 'finished' },
		{ label: 'Settings', href: '/settings', icon: 'settings' }
	] as const;

	function isActive(href: string): boolean {
		const pathname = page.url.pathname;
		if (href === '/')
			return (
				pathname === '/' ||
				pathname === '/movies' ||
				pathname.startsWith('/movies/') ||
				pathname === '/folders' ||
				pathname.startsWith('/folders/')
			);
		return pathname === href || pathname.startsWith(`${href}/`);
	}
</script>

<div class="app-shell">
	<header class="app-header">
		<div class="app-header__inner">
			<a class="brand" href={resolve('/')} aria-label="Mediaforce library">
				<span class="brand-mark" aria-hidden="true"><i></i><i></i></span>
				<span class="brand-name">Mediaforce</span>
			</a>

			<nav class="app-nav" aria-label="Mediaforce">
				{#each navigation as item (item.href)}
					<a
						class:active={isActive(item.href)}
						href={resolve(item.href)}
						aria-current={isActive(item.href) ? 'page' : undefined}
					>
						{#if item.icon === 'seasons'}
							<svg viewBox="0 0 20 20" aria-hidden="true">
								<path d="M4 4.5h12v11H4zM7 4.5v11M4 8h3M4 12h3" />
							</svg>
						{:else if item.icon === 'activity'}
							<svg viewBox="0 0 20 20" aria-hidden="true">
								<path d="M3 10h3l2-4 3.5 8 2.5-5h3" />
							</svg>
						{:else if item.icon === 'finished'}
							<svg viewBox="0 0 20 20" aria-hidden="true">
								<circle cx="10" cy="10" r="7" /><path d="m6.5 10 2.2 2.2 4.8-5" />
							</svg>
						{:else}
							<svg viewBox="0 0 20 20" aria-hidden="true">
								<circle cx="10" cy="10" r="2.5" /><path
									d="M10 3v2M10 15v2M3 10h2M15 10h2M5 5l1.5 1.5M13.5 13.5 15 15M15 5l-1.5 1.5M6.5 13.5 5 15"
								/>
							</svg>
						{/if}
						<span>{item.label}</span>
					</a>
				{/each}
			</nav>
			<ThemeToggle />
		</div>
	</header>

	<div class="app-content">
		{@render children()}
	</div>
</div>

<style>
	.app-shell {
		min-height: 100vh;
	}

	.app-header {
		background: var(--mf-bg-shell-translucent);
		border-bottom: 1px solid var(--mf-line);
		position: relative;
		z-index: 50;
	}

	.app-header__inner {
		align-items: stretch;
		display: flex;
		height: 58px;
		margin: 0 auto;
		max-width: 1120px;
		padding: 0 24px;
	}

	.brand {
		align-items: center;
		color: var(--mf-fg-primary);
		display: inline-flex;
		font-size: 15px;
		font-weight: 600;
		gap: 10px;
		margin-right: 38px;
		text-decoration: none;
	}

	.brand:hover {
		color: var(--mf-fg-primary);
	}

	.brand-mark {
		align-items: center;
		background: var(--mf-brand-bg);
		border-radius: 8px;
		display: inline-flex;
		gap: 3px;
		height: 28px;
		justify-content: center;
		width: 28px;
	}

	.brand-mark i {
		background: var(--mf-brand-fg);
		border-radius: 2px;
		display: block;
		height: 10px;
		width: 2px;
	}

	.brand-mark i:last-child {
		height: 6px;
	}

	.app-nav {
		align-items: stretch;
		display: flex;
		gap: 4px;
	}

	.app-nav a {
		align-items: center;
		border-bottom: 2px solid transparent;
		color: var(--mf-fg-secondary);
		display: inline-flex;
		font-size: 14px;
		font-weight: 500;
		gap: 7px;
		padding: 2px 14px 0;
		text-decoration: none;
	}

	.app-nav a:hover {
		background: var(--mf-active-bg);
		color: var(--mf-active-fg);
	}

	.app-nav a.active {
		border-bottom-color: var(--mf-active-fg);
		color: var(--mf-active-fg);
		font-weight: 600;
	}

	.app-nav svg {
		display: none;
		fill: none;
		height: 18px;
		stroke: currentColor;
		stroke-linecap: round;
		stroke-linejoin: round;
		stroke-width: 1.5;
		width: 18px;
	}

	.app-content {
		min-height: calc(100vh - 58px);
	}

	@media (max-width: 680px) {
		.app-header__inner {
			height: 62px;
			padding: 0 10px;
		}

		.brand {
			margin-right: 4px;
		}

		.brand-name {
			display: none;
		}

		.brand-mark {
			border-radius: 7px;
			height: 30px;
			width: 30px;
		}

		.app-nav {
			flex: 1;
			justify-content: space-between;
		}

		.app-nav a {
			flex: 1;
			flex-direction: column;
			font-size: 10px;
			gap: 2px;
			justify-content: center;
			min-width: 0;
			padding: 3px 2px 1px;
		}

		.app-nav svg {
			display: block;
		}

		.app-content {
			min-height: calc(100vh - 62px);
		}
	}
</style>
