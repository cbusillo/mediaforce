<script lang="ts">
	import { resolve } from '$app/paths';
	import { page } from '$app/state';
	import Panel from '$lib/components/Panel.svelte';
</script>

<Panel class="masthead" padding="1rem 1.25rem">
	<div class="brand-block">
		<div class="brand-row">
			<a class="brand-link" href={resolve('/')}>
				<span class="brand-mark">Mediaforce</span>
				<span class="brand-name serif">AV1 calibration bench</span>
			</a>
		</div>
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
		gap: 0;
	}

	.brand-row {
		gap: 0.85rem;
		flex-wrap: wrap;
	}

	.brand-link {
		gap: 0.7rem;
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
		font-size: clamp(1.35rem, 2vw, 1.8rem);
		line-height: 1;
		color: var(--ink);
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
		min-height: 2.55rem;
		padding: 0.65rem 1rem;
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
