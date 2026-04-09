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
		padding: 1.25rem 0 4.5rem;
		display: grid;
		gap: var(--space-4);
	}

	@media (max-width: 720px) {
		.page-shell {
			padding-top: 0.75rem;
		}
	}
</style>
