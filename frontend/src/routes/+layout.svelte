<script lang="ts">
	import { navigating } from '$app/stores';
	import favicon from '$lib/assets/favicon.svg';
	import '$lib/design/tokens.css';

	let { children } = $props();
	const routeLoading = $derived(Boolean($navigating?.to));
</script>

<svelte:head>
	<link rel="icon" href={favicon} />
	<title>Mediaforce</title>
</svelte:head>

{@render children()}

{#if routeLoading}
	<div class="route-loading" role="status" aria-live="polite" aria-label="Loading route data">
		<span class="route-loading__bar" aria-hidden="true"></span>
		<span>Loading workstation data</span>
	</div>
{/if}

<style>
	.route-loading {
		align-items: center;
		background: var(--mf-bg-shell);
		border: var(--mf-border-strong);
		bottom: var(--mf-space-5);
		box-shadow: var(--mf-shadow-popover);
		color: var(--mf-fg-primary);
		display: inline-flex;
		font-family: var(--mf-font-mono), monospace;
		font-size: var(--mf-text-xs);
		gap: var(--mf-space-4);
		left: var(--mf-space-5);
		min-height: 32px;
		padding: 0 var(--mf-space-5);
		position: fixed;
		text-transform: uppercase;
		z-index: 100;
	}

	.route-loading__bar {
		background: var(--mf-active-fg);
		display: inline-block;
		height: 14px;
		position: relative;
		width: 4px;
	}

	.route-loading__bar::after {
		animation: route-loading-pulse 900ms var(--mf-ease) infinite;
		background: var(--mf-active-fg-bright);
		content: '';
		height: 14px;
		left: 8px;
		position: absolute;
		top: 0;
		width: 4px;
	}

	@keyframes route-loading-pulse {
		0%,
		100% {
			opacity: 0.35;
		}
		50% {
			opacity: 1;
		}
	}
</style>
