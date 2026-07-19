<script lang="ts">
	import { onMount } from 'svelte';
	import { SvelteURLSearchParams } from 'svelte/reactivity';

	import { fetchJson } from '$lib/api/client';
	import { initialDashboard, initialHosts } from '$lib/api/placeholders';
	import type { DashboardSummaryPayload, HostsPayload, OperatorWorkPayload } from '$lib/api/types';
	import OpsWorkstationView from '$lib/components/workstation/OpsWorkstationView.svelte';
	import type { OperatorWorkQuery } from '$lib/components/workstation/operator-work';

	let dashboard = $state<DashboardSummaryPayload | null>(initialDashboard);
	let hosts = $state<HostsPayload | null>(initialHosts);
	let operatorWork = $state<OperatorWorkPayload | null>(null);
	let loadError = $state('');
	let refreshGeneration = 0;
	let operatorRefreshGeneration = 0;
	let operatorQuery = $state<OperatorWorkQuery>({ limit: 25 });

	function operatorWorkUrl(query: OperatorWorkQuery): string {
		const params = new SvelteURLSearchParams();
		if (query.offset) params.set('offset', String(query.offset));
		if (query.limit) params.set('limit', String(query.limit));
		if (query.evidenceKind) params.set('evidence_kind', query.evidenceKind);
		if (query.state) params.set('state', query.state);
		if (query.mediaRoot) params.set('media_root', query.mediaRoot);
		if (query.workStatus) params.set('work_status', query.workStatus);
		const queryString = params.toString();
		return `/api/operator-work${queryString ? `?${queryString}` : ''}`;
	}

	async function refreshOperatorWork(nextQuery: OperatorWorkQuery = operatorQuery) {
		operatorQuery = { ...nextQuery };
		const generation = ++operatorRefreshGeneration;
		try {
			const payload = await fetchJson<OperatorWorkPayload>(operatorWorkUrl(operatorQuery));
			if (generation !== operatorRefreshGeneration) return;
			operatorWork = payload;
			loadError = '';
		} catch (error) {
			if (generation !== operatorRefreshGeneration) return;
			loadError = error instanceof Error ? error.message : 'Operator work state failed to load.';
			throw error instanceof Error ? error : new Error(loadError);
		}
	}

	async function refreshOpsData() {
		const generation = ++refreshGeneration;
		const operatorGeneration = ++operatorRefreshGeneration;
		try {
			const [dashboardPayload, hostsPayload, operatorPayload] = await Promise.all([
				fetchJson<DashboardSummaryPayload>('/api/dashboard?preview_limit=0'),
				fetchJson<HostsPayload>('/api/hosts?compact=1'),
				fetchJson<OperatorWorkPayload>(operatorWorkUrl(operatorQuery))
			]);
			if (generation !== refreshGeneration || operatorGeneration !== operatorRefreshGeneration)
				return;
			dashboard = dashboardPayload;
			hosts = hostsPayload;
			operatorWork = operatorPayload;
			loadError = '';
		} catch (error) {
			if (generation !== refreshGeneration) return;
			loadError = error instanceof Error ? error.message : 'Ops route failed to load.';
			throw error instanceof Error ? error : new Error(loadError);
		}
	}

	onMount(() => {
		void refreshOpsData().catch(() => undefined);
		return () => {
			refreshGeneration += 1;
			operatorRefreshGeneration += 1;
		};
	});
</script>

<svelte:head>
	<title>Activity · Mediaforce</title>
</svelte:head>

<OpsWorkstationView
	{dashboard}
	{hosts}
	{operatorWork}
	loadError={loadError || null}
	onRefresh={refreshOpsData}
	onOperatorRefresh={refreshOperatorWork}
/>
