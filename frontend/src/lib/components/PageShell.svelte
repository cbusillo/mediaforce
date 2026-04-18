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
		padding: 0.9rem 0 4rem;
		display: grid;
		gap: 0.85rem;
		position: relative;
	}

	@media (max-width: 720px) {
		.page-shell {
			padding-top: 0.55rem;
		}
	}
</style>
