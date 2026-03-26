<script lang="ts">
	let { children } = $props();
</script>

<div class="page-shell">
	{@render children()}
</div>

<style>
	.page-shell {
		width: var(--shell-width);
		margin: 0 auto;
		padding: 1.5rem 0 4rem;
		display: grid;
		gap: var(--space-4);
	}
</style>
