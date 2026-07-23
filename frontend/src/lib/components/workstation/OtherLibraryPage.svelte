<script lang="ts">
	import { onMount } from 'svelte';

	import { fetchJson } from '$lib/api/client';
	import { initialOtherLibrary } from '$lib/api/placeholders';
	import type { OtherLibraryPayload } from '$lib/api/types';
	import { mergeOtherLibraryPayloads } from '$lib/other/library';
	import OtherLibraryView from './OtherLibraryView.svelte';

	let structure = $state<OtherLibraryPayload>(initialOtherLibrary);
	let details = $state<OtherLibraryPayload>(initialOtherLibrary);
	const payload = $derived(mergeOtherLibraryPayloads(structure, details));
	let structurePending = $state(true);
	let detailsPending = $state(true);
	let loadError = $state('');
	let detailsError = $state('');
	let disposed = false;

	onMount(() => {
		disposed = false;
		void hydrateStructure().finally(async () => {
			if (disposed) return;
			await hydrateDetails();
		});

		return () => {
			disposed = true;
		};
	});

	async function hydrateStructure() {
		structurePending = true;
		try {
			const response = await fetchJson<OtherLibraryPayload>('/api/dashboard/library/other');
			if (disposed) return;
			structure = response;
			loadError = '';
		} catch (error) {
			if (disposed) return;
			loadError =
				error instanceof Error ? error.message : 'The Other library index could not open.';
		} finally {
			if (!disposed) structurePending = false;
		}
	}

	async function hydrateDetails() {
		detailsPending = true;
		try {
			const response = await fetchJson<OtherLibraryPayload>('/api/dashboard/library/other/details');
			if (disposed) return;
			details = response;
			detailsError = '';
		} catch (error) {
			if (disposed) return;
			detailsError =
				error instanceof Error ? error.message : 'Other workflow details could not load.';
		} finally {
			if (!disposed) detailsPending = false;
		}
	}
</script>

<OtherLibraryView {payload} {structurePending} {detailsPending} {loadError} {detailsError} />
