<script lang="ts">
	import { navigating } from '$app/stores';
	import favicon from '$lib/assets/favicon.svg';
	import AppShell from '$lib/components/shell/AppShell.svelte';
	import '$lib/design/tokens.css';

	let { children } = $props();
	const routeLoading = $derived(Boolean($navigating?.to));
</script>

<svelte:head>
	<link rel="icon" href={favicon} />
	<title>Mediaforce</title>
</svelte:head>

<AppShell>
	{@render children()}
</AppShell>

{#if routeLoading}
	<div class="route-loading" role="status" aria-live="polite" aria-label="Opening page">
		<span class="route-loading__bar" aria-hidden="true"></span>
		<span>Opening page</span>
	</div>
{/if}

<style>
	.route-loading {
		align-items: center;
		background: var(--mf-fg-primary);
		border: 1px solid var(--mf-line-strong);
		border-radius: 999px;
		bottom: 18px;
		box-shadow: var(--mf-shadow-modal);
		color: var(--mf-fg-on-accent);
		display: inline-flex;
		font-family: var(--mf-font-sans);
		font-size: 12px;
		font-weight: 700;
		gap: 10px;
		left: 18px;
		min-height: 42px;
		padding: 0 17px;
		position: fixed;
		z-index: 100;
	}

	.route-loading__bar {
		animation: route-loading-pulse 900ms ease-in-out infinite;
		background: #78d0be;
		border-radius: 50%;
		display: inline-block;
		height: 7px;
		width: 7px;
	}

	@keyframes route-loading-pulse {
		0%,
		100% {
			opacity: 0.35;
		}
		50% {
			opacity: 1;
			transform: scale(1.15);
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.route-loading__bar {
			animation: none;
		}
	}
</style>
