<script lang="ts">
	import OperatorShell from './OperatorShell.svelte';
	import type { ShellRouteId } from './shell-types';

	let {
		route,
		subject,
		crumb,
		message = 'Loading workstation data',
		error = ''
	}: {
		route: ShellRouteId;
		subject: string;
		crumb: string;
		message?: string;
		error?: string;
	} = $props();
</script>

<OperatorShell
	{route}
	{subject}
	{crumb}
	statusTiles={[
		{
			label: 'Route',
			value: error ? 'Issue' : 'Loading',
			detail: error || 'Fetching current payload',
			tone: error ? 'fail' : 'active'
		},
		{
			label: 'UI',
			value: 'Ready',
			detail: 'Navigation accepted',
			tone: 'ready'
		},
		{
			label: 'Data',
			value: error ? 'Failed' : 'Pending',
			detail: error ? 'Showing error state' : 'Waiting for API response',
			tone: error ? 'fail' : 'wait'
		}
	]}
	footerSignals={[{ label: 'Navigation', value: message, tone: error ? 'fail' : 'active' }]}
>
	<main class="route-loading-workspace" aria-busy={!error} aria-live="polite">
		<section class="route-loading-panel">
			<div>
				<span>{subject}</span>
				<h1>{error ? 'Route data failed' : message}</h1>
				<p>{error || 'The route is visible while Mediaforce waits on the API.'}</p>
			</div>
			{#if !error}
				<div class="meter" aria-hidden="true"><i></i></div>
			{/if}
		</section>
	</main>
</OperatorShell>

<style>
	.route-loading-workspace {
		display: grid;
		padding: var(--mf-space-6);
	}

	.route-loading-panel {
		align-items: end;
		background: var(--mf-bg-panel);
		border: var(--mf-border);
		display: grid;
		gap: var(--mf-space-6);
		grid-template-columns: minmax(0, 1fr) minmax(160px, 280px);
		min-height: 180px;
		padding: var(--mf-space-6);
	}

	span {
		color: var(--mf-active-fg-bright);
		font-family: var(--mf-font-mono), monospace;
		font-size: var(--mf-text-xs);
		text-transform: uppercase;
	}

	h1,
	p {
		margin: 0;
	}

	h1 {
		font-size: var(--mf-text-xl);
		line-height: var(--mf-leading-tight);
		margin-top: var(--mf-space-3);
	}

	p {
		color: var(--mf-fg-secondary);
		margin-top: var(--mf-space-3);
	}

	.meter {
		background: var(--mf-bg-strip);
		border: var(--mf-border);
		height: 10px;
		overflow: hidden;
	}

	.meter i {
		animation: loading-meter 1.1s var(--mf-ease) infinite;
		background: var(--mf-active-fg);
		display: block;
		height: 100%;
		width: 38%;
	}

	@keyframes loading-meter {
		0% {
			transform: translateX(-100%);
		}

		100% {
			transform: translateX(280%);
		}
	}

	@media (max-width: 760px) {
		.route-loading-panel {
			grid-template-columns: 1fr;
		}
	}
</style>
