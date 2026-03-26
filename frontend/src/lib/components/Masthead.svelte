<script lang="ts">
	import { resolve } from '$app/paths';
	import { page } from '$app/state';
	import Panel from '$lib/components/Panel.svelte';

	function currentPageLabel(pathname: string) {
		if (pathname === '/settings') return 'Settings';
		if (pathname.startsWith('/folders/')) return 'Folder Studio';
		return 'Dashboard';
	}
</script>

<Panel class="masthead" padding="1rem 1.25rem">
	<div class="brand-block">
		<div class="brand-row">
			<a class="brand-link" href={resolve('/')}>
				<span class="brand-mark">Media Calibration Bench</span>
				<span class="brand-name serif">AV1 tuning studio</span>
			</a>
			<span class="route-pill">{currentPageLabel(page.url.pathname)}</span>
		</div>
		<p class="lede-copy brand-note">
			Hard-scene previews, queue controls, and profile tuning without fragment-driven layout jumps.
		</p>
	</div>

	<nav class="nav-row" aria-label="Primary">
		<a
			class:active={page.url.pathname === '/'}
			href={resolve('/')}
			aria-current={page.url.pathname === '/' ? 'page' : undefined}>Folders</a
		>
		<a
			class:active={page.url.pathname === '/settings'}
			href={resolve('/settings')}
			aria-current={page.url.pathname === '/settings' ? 'page' : undefined}>Settings</a
		>
	</nav>
</Panel>

<style>
	:global(.masthead) {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		gap: var(--space-4);
		align-items: center;
		box-shadow: var(--shadow-md);
		backdrop-filter: blur(18px);
	}

	.brand-block,
	.brand-row,
	.nav-row,
	.brand-link {
		display: flex;
		align-items: center;
	}

	.brand-block {
		gap: var(--space-2);
		flex-direction: column;
		align-items: flex-start;
	}

	.brand-row {
		gap: var(--space-3);
		flex-wrap: wrap;
	}

	.brand-link {
		gap: 0.8rem;
		flex-wrap: wrap;
	}

	.brand-mark {
		font-size: 0.76rem;
		font-weight: 800;
		text-transform: uppercase;
		letter-spacing: 0.18em;
		color: var(--accent-deep);
	}

	.brand-name {
		font-size: clamp(1.4rem, 2.2vw, 1.9rem);
		line-height: 1;
		color: var(--ink);
	}

	.brand-note {
		max-width: 58ch;
		font-size: 0.98rem;
	}

	.route-pill {
		display: inline-flex;
		align-items: center;
		min-height: 2rem;
		padding: 0.3rem 0.8rem;
		border-radius: var(--radius-pill);
		background: rgba(15, 118, 110, 0.1);
		border: 1px solid rgba(15, 118, 110, 0.14);
		color: var(--accent-deep);
		font-size: 0.82rem;
		font-weight: 700;
		letter-spacing: 0.04em;
		text-transform: uppercase;
	}

	.nav-row {
		gap: 0.65rem;
		justify-content: flex-end;
		flex-wrap: wrap;
	}

	.nav-row a {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-height: 2.8rem;
		padding: 0.72rem 1rem;
		border-radius: var(--radius-pill);
		border: 1px solid rgba(23, 35, 31, 0.08);
		background: rgba(255, 255, 255, 0.56);
		color: var(--ink-muted);
		font-size: 0.94rem;
		font-weight: 700;
		transition:
			transform 150ms ease,
			border-color 150ms ease,
			background-color 150ms ease,
			color 150ms ease;
	}

	.nav-row a:hover,
	.nav-row a.active {
		transform: translateY(-1px);
		border-color: rgba(15, 118, 110, 0.2);
		background: rgba(15, 118, 110, 0.11);
		color: var(--accent-deep);
	}

	@media (max-width: 860px) {
		:global(.masthead) {
			grid-template-columns: 1fr;
		}

		.nav-row {
			justify-content: flex-start;
		}
	}
</style>
