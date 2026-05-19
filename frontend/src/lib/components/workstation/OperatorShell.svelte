<script lang="ts">
	import { resolve } from '$app/paths';
	import type { Snippet } from 'svelte';

	export type ShellRouteId = 'queue' | 'folders' | 'ops' | 'completed' | 'settings' | 'studio';
	export type ShellTone = 'active' | 'ready' | 'wait' | 'fail' | 'idle';
	export type StatusTile = {
		label: string;
		value: string;
		detail?: string;
		tone?: ShellTone;
		mono?: boolean;
	};
	export type FooterSignal = {
		label: string;
		value: string;
		tone?: ShellTone;
	};

	const navItems: Array<{
		id: ShellRouteId;
		label: string;
		href: '/' | '/folders' | '/ops' | '/completed' | '/settings';
	}> = [
		{ id: 'queue', label: 'Queue', href: '/' },
		{ id: 'folders', label: 'Folders', href: '/folders' },
		{ id: 'ops', label: 'Ops', href: '/ops' },
		{ id: 'completed', label: 'Completed', href: '/completed' },
		{ id: 'settings', label: 'Settings', href: '/settings' }
	];

	let {
		route,
		subject,
		crumb,
		statusTiles = [],
		footerSignals = [],
		children
	}: {
		route: ShellRouteId;
		subject: string;
		crumb: string;
		statusTiles?: StatusTile[];
		footerSignals?: FooterSignal[];
		children: Snippet;
	} = $props();
</script>

<div class="operator-shell">
	<header class="masthead">
		<a class="brand" href={resolve('/')} aria-label="Mediaforce home">
			<span class="brand__mark" aria-hidden="true">[]</span>
			<span class="brand__name">Mediaforce</span>
		</a>
		<nav class="nav" aria-label="Primary">
			{#each navItems as item (item.id)}
				<a class:active={item.id === route} href={resolve(item.href)}>{item.label}</a>
			{/each}
		</nav>
		<div class="operator-context">
			<span class="operator-context__dot" aria-hidden="true"></span>
			<span>local workstation</span>
		</div>
	</header>

	<section class="status-strip" aria-label="System status">
		{#if statusTiles.length === 0}
			<div class="status-tile">
				<span class="status-tile__label">Runtime</span>
				<span class="status-tile__value">Loading</span>
				<span class="status-tile__detail">Waiting for route data</span>
			</div>
		{:else}
			{#each statusTiles as tile (tile.label)}
				<div class="status-tile status-tile--{tile.tone ?? 'idle'}">
					<span class="status-tile__label">{tile.label}</span>
					<span class:mono={tile.mono} class="status-tile__value">{tile.value}</span>
					{#if tile.detail}
						<span class="status-tile__detail">{tile.detail}</span>
					{/if}
				</div>
			{/each}
		{/if}
	</section>

	<div class="routebar">
		<span class="routebar__tab">
			<span class="routebar__dot" aria-hidden="true"></span>
			{subject}
		</span>
		<span class="routebar__crumb"
			><span>mediaforce</span><span>:</span><strong>{crumb}</strong></span
		>
	</div>

	{@render children()}

	<footer class="footer-strip" aria-label="Runtime telemetry">
		{#each footerSignals as signal (signal.label)}
			<span class="footer-strip__item footer-strip__item--{signal.tone ?? 'idle'}">
				<b>{signal.label}</b>
				{signal.value}
			</span>
		{/each}
	</footer>
</div>

<style>
	.operator-shell {
		background: var(--mf-bg-base);
		color: var(--mf-fg-primary);
		display: grid;
		min-height: 100vh;
		padding-bottom: 28px;
	}

	.masthead {
		align-items: stretch;
		background: var(--mf-bg-shell);
		border-bottom: var(--mf-border);
		display: grid;
		grid-template-columns: minmax(160px, 220px) 1fr auto;
		min-height: 44px;
		position: sticky;
		top: 0;
		z-index: 20;
	}

	.brand {
		align-items: center;
		border-right: var(--mf-border);
		display: inline-flex;
		gap: var(--mf-space-4);
		padding: 0 var(--mf-space-6);
		text-transform: uppercase;
	}

	.brand__mark {
		color: var(--mf-fg-primary);
		font-family: var(--mf-font-mono), monospace;
		font-size: var(--mf-text-sm);
		font-weight: var(--mf-weight-bold);
		letter-spacing: 0;
	}

	.brand__name {
		font-size: var(--mf-text-xs);
		font-weight: var(--mf-weight-bold);
		letter-spacing: 0.08em;
	}

	.nav {
		align-items: stretch;
		display: flex;
		min-width: 0;
	}

	.nav a {
		align-items: center;
		border-right: var(--mf-border);
		color: var(--mf-fg-secondary);
		display: inline-flex;
		font-size: var(--mf-text-sm);
		min-height: 44px;
		padding: 0 var(--mf-space-7);
		position: relative;
	}

	.nav a:hover {
		background: var(--mf-bg-panel);
		color: var(--mf-fg-primary);
	}

	.nav a.active {
		background: var(--mf-bg-panel);
		color: var(--mf-fg-primary);
	}

	.nav a.active::after {
		background: var(--mf-active-fg);
		bottom: 0;
		content: '';
		height: 2px;
		left: 0;
		position: absolute;
		right: 0;
	}

	.operator-context {
		align-items: center;
		border-left: var(--mf-border);
		color: var(--mf-fg-secondary);
		display: inline-flex;
		font-family: var(--mf-font-mono), monospace;
		font-size: var(--mf-text-xs);
		gap: var(--mf-space-4);
		padding: 0 var(--mf-space-6);
	}

	.operator-context__dot {
		background: var(--mf-ready-fg);
		border-radius: 50%;
		display: inline-block;
		height: 6px;
		width: 6px;
	}

	.status-strip {
		background: var(--mf-bg-strip);
		border-bottom: var(--mf-border);
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
		min-height: 72px;
	}

	.status-tile {
		border-right: var(--mf-border);
		display: grid;
		gap: var(--mf-space-2);
		padding: var(--mf-space-5) var(--mf-space-6);
		position: relative;
	}

	.status-tile::before {
		background: var(--tile-tone, var(--mf-line-strong));
		content: '';
		height: 2px;
		left: 0;
		position: absolute;
		right: 0;
		top: 0;
	}

	.status-tile--active {
		--tile-tone: var(--mf-active-fg);
	}

	.status-tile--ready {
		--tile-tone: var(--mf-ready-fg);
	}

	.status-tile--wait {
		--tile-tone: var(--mf-wait-fg);
	}

	.status-tile--fail {
		--tile-tone: var(--mf-fail-fg);
	}

	.status-tile__label {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-semibold);
		letter-spacing: 0.08em;
		line-height: 1;
		text-transform: uppercase;
	}

	.status-tile__value {
		color: var(--mf-fg-primary);
		font-size: var(--mf-text-md);
		font-weight: var(--mf-weight-semibold);
		line-height: var(--mf-leading-tight);
	}

	.status-tile__value.mono {
		font-family: var(--mf-font-mono), monospace;
		font-size: var(--mf-text-sm);
		font-weight: var(--mf-weight-medium);
	}

	.status-tile__detail {
		color: var(--mf-fg-tertiary);
		font-family: var(--mf-font-mono), monospace;
		font-size: var(--mf-text-xs);
		line-height: var(--mf-leading-snug);
	}

	.routebar {
		align-items: center;
		background: var(--mf-bg-panel);
		border-bottom: var(--mf-border);
		display: flex;
		min-height: 34px;
		overflow: hidden;
	}

	.routebar__tab {
		align-items: center;
		align-self: stretch;
		border-right: var(--mf-border);
		color: var(--mf-fg-primary);
		display: inline-flex;
		font-size: var(--mf-text-sm);
		gap: var(--mf-space-4);
		padding: 0 var(--mf-space-6);
		position: relative;
	}

	.routebar__tab::after {
		background: var(--mf-active-fg);
		bottom: 0;
		content: '';
		height: 2px;
		left: 0;
		position: absolute;
		right: 0;
	}

	.routebar__dot {
		background: var(--mf-active-fg);
		border-radius: 50%;
		display: inline-block;
		height: 6px;
		width: 6px;
	}

	.routebar__crumb {
		align-items: center;
		color: var(--mf-fg-tertiary);
		display: inline-flex;
		font-family: var(--mf-font-mono), monospace;
		font-size: var(--mf-text-xs);
		gap: var(--mf-space-3);
		margin-left: auto;
		min-width: 0;
		overflow: hidden;
		padding: 0 var(--mf-space-6);
		white-space: nowrap;
	}

	.routebar__crumb strong {
		color: var(--mf-active-fg);
		font-weight: var(--mf-weight-medium);
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.footer-strip {
		align-items: center;
		background: var(--mf-bg-strip);
		border-top: var(--mf-border);
		bottom: 0;
		color: var(--mf-fg-tertiary);
		display: flex;
		font-family: var(--mf-font-mono), monospace;
		font-size: var(--mf-text-2xs);
		gap: var(--mf-space-7);
		left: 0;
		min-height: 28px;
		overflow: hidden;
		padding: 0 var(--mf-space-6);
		position: fixed;
		right: 0;
		white-space: nowrap;
		z-index: 20;
	}

	.footer-strip__item {
		align-items: center;
		display: inline-flex;
		gap: var(--mf-space-3);
	}

	.footer-strip__item b {
		color: var(--mf-fg-primary);
		font-family: var(--mf-font-sans), sans-serif;
		font-weight: var(--mf-weight-semibold);
		text-transform: uppercase;
	}

	.footer-strip__item--active {
		color: var(--mf-active-fg);
	}

	.footer-strip__item--ready {
		color: var(--mf-ready-fg);
	}

	.footer-strip__item--wait {
		color: var(--mf-wait-fg);
	}

	.footer-strip__item--fail {
		color: var(--mf-fail-fg);
	}

	@media (max-width: 760px) {
		.masthead {
			grid-template-columns: 1fr;
			position: static;
		}

		.brand {
			border-right: 0;
			min-height: 40px;
			padding: 0 var(--mf-space-4);
		}

		.operator-context {
			display: none;
		}

		.nav {
			border-top: var(--mf-border);
			overflow-x: auto;
		}

		.nav a {
			flex: 1 0 auto;
			justify-content: center;
			min-height: 38px;
			padding: 0 var(--mf-space-4);
		}

		.status-strip {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}

		.routebar__crumb {
			display: none;
		}

		.footer-strip {
			gap: var(--mf-space-5);
			overflow-x: auto;
			padding: 0 var(--mf-space-4);
			scrollbar-width: none;
		}

		.footer-strip::-webkit-scrollbar {
			display: none;
		}

		.footer-strip__item {
			flex: 0 0 auto;
		}
	}
</style>
