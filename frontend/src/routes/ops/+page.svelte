<script lang="ts">
	const preservedContracts = [
		'Encode queue control',
		'Calibration queue control',
		'Host readiness and capabilities',
		'Schedule windows and off-window reasons',
		'Blocked, failed, retry, pause, and stop states'
	];
</script>

<svelte:head>
	<title>Ops Reset · Mediaforce</title>
</svelte:head>

<main class="reset-stage">
	<p class="kicker">Fleet console reset</p>
	<h1>Ops is waiting for the new workstation design.</h1>
	<p class="lede">
		The old queue and host modules were removed to prevent the next fleet console from inheriting
		the failed dashboard composition.
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
