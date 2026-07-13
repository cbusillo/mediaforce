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
		let refreshTimer: number | undefined;
		void hydrateStructure().finally(() => {
			if (disposed) return;
			void hydrateDetails();
			refreshTimer = window.setInterval(() => void hydrateDetails(true), 7000);
		});

		return () => {
			disposed = true;
			if (refreshTimer) window.clearInterval(refreshTimer);
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

	async function hydrateDetails(background = false) {
		if (!background) detailsPending = true;
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
