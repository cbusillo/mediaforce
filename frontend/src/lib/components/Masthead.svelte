<script lang="ts">
	import { resolve } from '$app/paths';
	import { page } from '$app/state';
</script>

<header class="masthead">
	<div class="brand-block">
		<a class="brand-link" href={resolve('/')}>
			<div class="brand-stack">
				<span class="brand-mark">Mediaforce</span>
				<span class="brand-name">AV1 ops</span>
			</div>
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
		gap: 1rem;
		align-items: center;
		position: sticky;
		top: 0.75rem;
		z-index: 10;
		padding: 0.65rem 0.85rem;
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
		justify-content: flex-start;
		gap: 0.8rem;
		flex-wrap: wrap;
		width: 100%;
	}

	.brand-stack {
		display: flex;
		align-items: baseline;
		gap: 0.55rem;
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
		font-size: clamp(1rem, 1.3vw, 1.18rem);
		font-weight: 700;
		line-height: 1;
		color: var(--ink);
	}

	.brand-link:hover .brand-name {
		color: var(--accent-deep);
	}

	.nav-row {
		gap: 0.15rem;
		justify-content: flex-end;
		flex-wrap: wrap;
		padding-left: 0.8rem;
		border-left: 1px solid rgba(23, 35, 31, 0.08);
	}

	.nav-row a {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-height: 2rem;
		padding: 0.42rem 0.68rem;
		border-bottom: 2px solid transparent;
		color: var(--ink-muted);
		font-size: 0.84rem;
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

	@media (max-width: 1100px) {
		.masthead {
			gap: 0.75rem;
			padding: 0.5rem 0.7rem;
			top: 0.5rem;
		}

		.brand-mark {
			font-size: 0.72rem;
			letter-spacing: 0.16em;
		}

		.brand-name {
			font-size: 1rem;
		}

		.nav-row {
			padding-left: 0.65rem;
		}

		.nav-row a {
			min-height: 1.8rem;
			padding: 0.32rem 0.56rem;
			font-size: 0.8rem;
		}
	}

	@media (max-width: 560px) {
		.brand-stack {
			gap: 0.4rem;
		}

		.brand-mark {
			font-size: 0.68rem;
			letter-spacing: 0.16em;
		}

		.brand-name {
			font-size: 0.92rem;
		}

		.nav-row {
			display: grid;
			grid-template-columns: repeat(2, minmax(0, 1fr));
			gap: 0.3rem 0.6rem;
			width: 100%;
			padding-top: 0.55rem;
		}

		.nav-row a {
			justify-content: flex-start;
			padding-inline: 0;
			min-height: 1.95rem;
			font-size: 0.84rem;
		}
	}
</style>
