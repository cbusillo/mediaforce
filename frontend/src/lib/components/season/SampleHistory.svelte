<script lang="ts">
	import { fetchJson } from '$lib/api/client';

	let { prefix }: { prefix: string } = $props();
	type Sample = {
		job_id: string;
		finished_at: string;
		file_name: string;
		selection: { coverage_summary?: string | null };
	};
	type History = { offset: number; limit: number; total: number; samples: Sample[] };
	let history = $state<History | null>(null);
	let busy = $state(false);
	let failed = $state(false);

	async function load(offset = 0) {
		if (busy) return;
		busy = true;
		failed = false;
		try {
			const encoded = prefix.split('/').map(encodeURIComponent).join('/');
			history = await fetchJson<History>(`/api/folders/${encoded}/sample-history?offset=${offset}`);
		} catch {
			failed = true;
		} finally {
			busy = false;
		}
	}
</script>

<details
	class="sample-history"
	ontoggle={(event) => {
		if (event.currentTarget.open && !history) void load();
	}}
>
	<summary>Past samples</summary>
	{#if busy}<p role="status">Loading samples…</p>{/if}
	{#if failed}
		<p role="alert">Past samples could not be loaded.</p>
		<button type="button" onclick={() => load(history?.offset)} disabled={busy}>Try again</button>
	{:else if history}
		{#if history.total === 0}
			<p>No saved samples for this folder.</p>
		{:else}
			<p>Saved coverage for each sample. Every file is still checked before it is published.</p>
			<ul aria-label="Past samples">
				{#each history.samples as sample (sample.job_id)}
					<li>
						<time datetime={sample.finished_at}
							>{new Date(sample.finished_at).toLocaleString()}</time
						>
						<strong>{sample.file_name || 'Sample file not recorded'}</strong>
						<p>
							{sample.selection.coverage_summary || 'Coverage was not recorded for this sample.'}
						</p>
					</li>
				{/each}
			</ul>
			<nav aria-label="Sample history pages">
				<span
					>{history.offset + 1}–{history.offset + history.samples.length} of {history.total}</span
				>
				<button
					type="button"
					disabled={busy || history.offset === 0}
					onclick={() => load(Math.max(0, history!.offset - history!.limit))}>Newer samples</button
				>
				<button
					type="button"
					disabled={busy || history.offset + history.samples.length >= history.total}
					onclick={() => load(history!.offset + history!.limit)}>Older samples</button
				>
			</nav>
		{/if}
	{/if}
</details>

<style>
	.sample-history {
		border-top: 1px solid var(--line);
		padding-top: 1rem;
	}
	summary {
		cursor: pointer;
		font-weight: 600;
	}
	ul {
		list-style: none;
		padding: 0;
	}
	li {
		border-bottom: 1px solid var(--line);
		padding: 1rem 0;
		overflow-wrap: anywhere;
	}
	time,
	strong {
		display: block;
	}
	time {
		color: var(--muted);
		font-size: 0.85rem;
		margin-bottom: 0.4rem;
	}
	nav {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: 0.75rem;
	}
</style>
