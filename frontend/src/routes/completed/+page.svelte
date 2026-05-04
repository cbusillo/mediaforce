<script lang="ts">
	const preservedContracts = [
		'Completed backup inventory',
		'Cleanup readiness',
		'Blocked cleanup reasons',
		'Size and reclaim totals',
		'Explicit cleanup actions'
	];
</script>

<svelte:head>
	<title>Completed Reset · Mediaforce</title>
</svelte:head>

<main class="reset-stage">
	<p class="kicker">Archive cleanup reset</p>
	<h1>Completed cleanup will be rebuilt from the workflow.</h1>
	<p class="lede">
		The prior completed-backup surface has been removed so cleanup decisions can be redesigned as an
		operator task instead of a page of inherited controls.
	</p>

	<section aria-labelledby="preserved-contracts">
		<h2 id="preserved-contracts">Contracts to rebuild against</h2>
		<ul>
			{#each preservedContracts as contract (contract)}
				<li>{contract}</li>
			{/each}
		</ul>
	</section>
</main>

<style>
	.reset-stage {
		display: grid;
		gap: 1.5rem;
		max-width: 760px;
		padding: 4rem max(1.25rem, calc((100vw - 760px) / 2));
	}

	.kicker {
		font-size: 0.78rem;
		font-weight: 800;
		letter-spacing: 0.12em;
		text-transform: uppercase;
		color: #9fb2c7;
	}

	h1 {
		font-size: clamp(2rem, 5vw, 4rem);
		line-height: 1;
	}

	.lede,
	li {
		color: #c6d0da;
		line-height: 1.65;
	}

	section {
		display: grid;
		gap: 0.75rem;
		padding-top: 1rem;
		border-top: 1px solid rgba(255, 255, 255, 0.18);
	}

	h2 {
		font-size: 0.95rem;
	}
</style>
