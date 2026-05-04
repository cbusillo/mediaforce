<script lang="ts">
	let { data } = $props();

	const prefix = $derived(data.folder?.prefix ?? 'Current folder');
	const preservedContracts = [
		'Representative sample workflow',
		'Review pack and evidence flow',
		'Proposal approval or revision',
		'Host readiness and queue state',
		'Approved processing, retry, blocked, and completed states'
	];
</script>

<svelte:head>
	<title>{prefix} · Mediaforce UI Reset</title>
</svelte:head>

<main class="reset-stage">
	<p class="kicker">Folder Studio reset</p>
	<h1>{prefix}</h1>
	<p class="lede">
		The old Folder Studio implementation has been removed. The replacement must be designed around
		the canonical loop: sample, inspect evidence, approve or revise, then monitor processing.
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
