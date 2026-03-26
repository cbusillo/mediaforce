<script lang="ts">
	import favicon from '$lib/assets/favicon.svg';
	import '$lib/design/tokens.css';
	import Masthead from '$lib/components/Masthead.svelte';
	import PageShell from '$lib/components/PageShell.svelte';
	import ToastViewport from '$lib/components/ToastViewport.svelte';

	let { children } = $props();
</script>

<svelte:head>
	<link rel="icon" href={favicon} />
	<title>Mediaforce</title>
</svelte:head>

<PageShell>
	<Masthead />
	{@render children()}
</PageShell>

<ToastViewport />
