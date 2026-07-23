<script lang="ts">
	import { onMount } from 'svelte';

	import { fetchJson } from '$lib/api/client';
	import { initialMovieLibrary } from '$lib/api/placeholders';
	import type { MovieLibraryPayload } from '$lib/api/types';
	import { mergeMovieLibraryPayloads } from '$lib/movies/library';
	import MovieLibraryView from './MovieLibraryView.svelte';

	let structure = $state<MovieLibraryPayload>(initialMovieLibrary);
	let details = $state<MovieLibraryPayload>(initialMovieLibrary);
	let payload = $state<MovieLibraryPayload>(initialMovieLibrary);
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
		try {
			const response = await fetchJson<MovieLibraryPayload>('/api/dashboard/library/movies');
			if (disposed) return;
			structure = response;
			loadError = '';
			syncPayload();
		} catch (error) {
			if (disposed) return;
			loadError = error instanceof Error ? error.message : 'The movie title index could not open.';
		} finally {
			if (!disposed) structurePending = false;
		}
	}

	async function hydrateDetails() {
		detailsPending = true;
		try {
			const response = await fetchJson<MovieLibraryPayload>(
				'/api/dashboard/library/movies/details'
			);
			if (disposed) return;
			details = response;
			detailsError = '';
			syncPayload();
		} catch (error) {
			if (disposed) return;
			detailsError =
				error instanceof Error ? error.message : 'Movie workflow details could not load.';
		} finally {
			if (!disposed) detailsPending = false;
		}
	}

	function syncPayload() {
		payload = mergeMovieLibraryPayloads(structure, details);
	}
</script>

<MovieLibraryView {payload} {structurePending} {detailsPending} {loadError} {detailsError} />
