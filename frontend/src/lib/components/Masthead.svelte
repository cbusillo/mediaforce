<script lang="ts">
	import { resolve } from '$app/paths';
	import { page } from '$app/state';
</script>

<header class="masthead">
	<div class="brand-block">
		<a class="brand-link" href={resolve('/')}>
			<div class="brand-stack">
				<span class="brand-mark">Mediaforce</span>
				<span class="brand-name">AV1 operations console</span>
			</div>
			<span class="brand-context">Operator workstation</span>
		</a>
	</div>

	<nav class="nav-row" aria-label="Primary">
		<a
			class:active={page.url.pathname === '/'}
			href={resolve('/')}
			aria-current={page.url.pathname === '/' ? 'page' : undefined}>Folders</a
		>
		<a
			class:active={page.url.pathname === '/ops'}
			href={resolve('/ops')}
			aria-current={page.url.pathname === '/ops' ? 'page' : undefined}>Ops</a
		>
		<a
			class:active={page.url.pathname === '/completed'}
			href={resolve('/completed')}
			aria-current={page.url.pathname === '/completed' ? 'page' : undefined}>Completed</a
		>
		<a
			class:active={page.url.pathname === '/settings'}
			href={resolve('/settings')}
			aria-current={page.url.pathname === '/settings' ? 'page' : undefined}>Settings</a
		>
	</nav>
</header>

<style>
	.masthead {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		gap: 1.25rem;
		align-items: center;
		position: sticky;
		top: 0.75rem;
		z-index: 10;
		padding: 0.9rem 1.1rem;
		border: 1px solid rgba(23, 35, 31, 0.1);
		background: rgba(255, 252, 246, 0.9);
		backdrop-filter: blur(18px);
		box-shadow: var(--shadow-md);
	}

	.brand-block,
	.nav-row,
	.brand-link {
		display: flex;
		align-items: center;
	}

	.brand-block {
		min-width: 0;
	}

	.brand-link {
		justify-content: space-between;
		gap: 1rem;
		flex-wrap: wrap;
		width: 100%;
	}

	.brand-stack {
		display: grid;
		gap: 0.18rem;
	}

	.brand-mark,
	.brand-context {
		font-size: 0.76rem;
		font-weight: 800;
		text-transform: uppercase;
		letter-spacing: 0.18em;
	}

	.brand-mark {
		color: var(--accent-deep);
	}

	.brand-name {
		font-size: clamp(1.15rem, 1.75vw, 1.45rem);
		font-weight: 700;
		line-height: 1;
		color: var(--ink);
	}

	.brand-context {
		color: var(--ink-soft);
		white-space: nowrap;
	}

	.brand-link:hover .brand-name {
		color: var(--accent-deep);
	}

	.nav-row {
		gap: 0.3rem;
		justify-content: flex-end;
		flex-wrap: wrap;
		padding-left: 1rem;
		border-left: 1px solid rgba(23, 35, 31, 0.08);
	}

	.nav-row a {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-height: 2.35rem;
		padding: 0.58rem 0.82rem;
		border-bottom: 2px solid transparent;
		color: var(--ink-muted);
		font-size: 0.9rem;
		font-weight: 700;
		transition:
			transform 150ms ease,
			border-color 150ms ease,
			color 150ms ease;
	}

	.nav-row a:hover,
	.nav-row a.active {
		transform: translateY(-1px);
		border-color: rgba(15, 118, 110, 0.7);
		color: var(--accent-deep);
	}

	@media (max-width: 860px) {
		.masthead {
			grid-template-columns: 1fr;
			top: 0.5rem;
		}

		.nav-row {
			padding-left: 0;
			border-left: 0;
			border-top: 1px solid rgba(23, 35, 31, 0.08);
			padding-top: 0.75rem;
		}

		.nav-row {
			justify-content: flex-start;
		}
	}

	@media (max-width: 560px) {
		.nav-row {
			display: grid;
			grid-template-columns: repeat(2, minmax(0, 1fr));
			gap: 0.45rem 0.75rem;
			width: 100%;
		}

		.nav-row a {
			justify-content: flex-start;
			padding-inline: 0;
		}
	}
</style>
