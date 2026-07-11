<script lang="ts">
	import type { Snippet } from 'svelte';
	import type { FooterSignal, ShellRouteId, StatusTile } from './shell-types';

	let props: {
		route: ShellRouteId;
		subject: string;
		crumb: string;
		statusTiles?: StatusTile[];
		footerSignals?: FooterSignal[];
		children: Snippet;
	} = $props();
</script>

<div
	class="operator-shell"
	data-route={props.route}
	data-subject={props.subject}
	data-crumb={props.crumb}
	data-status-count={props.statusTiles?.length ?? 0}
	data-footer-count={props.footerSignals?.length ?? 0}
>
	{@render props.children()}
</div>

<style>
	.operator-shell {
		background: var(--mf-bg-base);
		color: var(--mf-fg-primary);
		min-height: calc(100vh - 58px);
	}

	@media (max-width: 680px) {
		.operator-shell {
			min-height: calc(100vh - 62px);
		}
	}
</style>
