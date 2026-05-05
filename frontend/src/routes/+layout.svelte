<script lang="ts">
	import favicon from '$lib/assets/favicon.svg';
	import '$lib/design/tokens.css';

	let { children } = $props();
</script>

<svelte:head>
	<link rel="icon" href={favicon} />
	<title>Mediaforce</title>
</svelte:head>

{@render children()}
