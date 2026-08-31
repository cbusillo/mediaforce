<script lang="ts">
	import type { Snippet } from 'svelte';

	import LibraryModeNav from './LibraryModeNav.svelte';
	import type {
		LibraryMetric,
		LibraryMode,
		LibraryNotice,
		LibraryWorkSegment
	} from './library-layout';

	let {
		active,
		title,
		metrics,
		workSegments = [],
		activeWorkSegment = '',
		onWorkSegmentSelect,
		notices = [],
		toolbarSummary = '',
		loading = false,
		toolbar,
		children
	}: {
		active: LibraryMode;
		title: string;
		metrics: LibraryMetric[];
		workSegments?: LibraryWorkSegment[];
		activeWorkSegment?: string;
		onWorkSegmentSelect?: (key: string) => void;
		notices?: LibraryNotice[];
		toolbarSummary?: string;
		loading?: boolean;
		toolbar: Snippet;
		children: Snippet;
	} = $props();

	const workTotal = $derived(workSegments.reduce((total, segment) => total + segment.count, 0));
</script>

<main class="library-layout">
	<LibraryModeNav {active} />
	<h1 class="sr-only">{title}</h1>

	<div class="status-band">
		<section class="metric-strip" aria-label={`${title} totals`}>
			{#each metrics as metric (metric.label)}
				<div>
					<span class="metric-strip__value">
						<strong class:metric-pending={metric.pending}>{metric.value}</strong>
						{#if metric.detail}<small>{metric.detail}</small>{/if}
					</span>
					<span class="metric-strip__label">{metric.label}</span>
				</div>
			{/each}
		</section>

		<section class="work-bar" aria-label="Current work summary">
			<div class="work-bar__total"><span>Current work</span><strong>{workTotal}</strong></div>
			<div class="work-bar__segments">
				{#if workSegments.length}
					{#each workSegments as segment (segment.key)}
						{#if onWorkSegmentSelect}
							<button
								class="work-segment"
								type="button"
								data-tone={segment.tone}
								data-count={segment.count}
								data-state={segment.key}
								aria-pressed={activeWorkSegment === segment.key}
								onclick={() => onWorkSegmentSelect(segment.key)}
							>
								<i aria-hidden="true"></i>{segment.count}
								{segment.label}
							</button>
						{:else}
							<span
								class="work-segment"
								data-tone={segment.tone}
								data-count={segment.count}
								data-state={segment.key}
							>
								<i aria-hidden="true"></i>{segment.count}
								{segment.label}
							</span>
						{/if}
					{/each}
				{:else}
					<span class="work-segment work-segment--empty">
						{loading ? 'Checking current work' : 'No active work'}
					</span>
				{/if}
			</div>
		</section>

		{#each notices as notice (`${notice.tone}-${notice.title}`)}
			<div
				class="notice"
				data-tone={notice.tone ?? 'idle'}
				role={notice.tone === 'fail' ? 'alert' : 'status'}
			>
				<strong>{notice.title}</strong><span>{notice.detail}</span>
			</div>
		{/each}
	</div>

	<section class="workspace" aria-busy={loading}>
		<header class="workspace__toolbar">
			{#if toolbarSummary}<span class="workspace__summary">{toolbarSummary}</span>{/if}
			<div class="workspace__controls">{@render toolbar()}</div>
		</header>
		{@render children()}
	</section>
</main>

<style>
	.library-layout {
		margin: 0 auto;
		max-width: var(--mf-shell-max);
		padding: var(--mf-space-8) var(--mf-shell-pad) var(--mf-space-11);
	}

	.sr-only {
		clip: rect(0 0 0 0);
		height: 1px;
		overflow: hidden;
		position: absolute;
		white-space: nowrap;
		width: 1px;
	}

	.status-band {
		margin-top: var(--mf-space-8);
	}

	.metric-strip {
		align-items: flex-start;
		display: grid;
		gap: var(--mf-space-6) var(--mf-space-9);
		grid-template-columns: repeat(4, minmax(0, 1fr));
	}

	.metric-strip > div {
		display: flex;
		flex-direction: column;
		min-width: 0;
	}

	.metric-strip__label {
		color: var(--mf-fg-tertiary);
		font-size: 10px;
		font-weight: var(--mf-weight-semibold);
		letter-spacing: 0.1em;
		order: -1;
		text-transform: uppercase;
	}

	.metric-strip__value {
		align-items: baseline;
		display: flex;
		gap: var(--mf-space-2);
		margin-top: var(--mf-space-2);
		min-width: 0;
	}

	.metric-strip__value strong {
		font-size: clamp(24px, 2.15vw, 31px);
		font-variant-numeric: tabular-nums;
		font-weight: var(--mf-weight-semibold);
		letter-spacing: -0.025em;
		line-height: 1.05;
		white-space: nowrap;
	}

	.metric-strip__value small {
		color: var(--mf-fg-tertiary);
		font-size: 11px;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.metric-strip__value small::before {
		content: '· ';
	}

	.metric-pending {
		color: var(--mf-fg-tertiary);
	}

	.work-bar {
		align-items: center;
		display: flex;
		gap: var(--mf-space-3);
		margin-top: var(--mf-space-5);
		min-height: 28px;
	}

	.work-bar__total {
		align-items: baseline;
		display: flex;
		gap: var(--mf-space-3);
		white-space: nowrap;
	}

	.work-bar__total span {
		color: var(--mf-fg-tertiary);
		font-size: 10px;
		font-weight: var(--mf-weight-semibold);
		letter-spacing: 0.1em;
		text-transform: uppercase;
	}

	.work-bar__total strong {
		font-size: var(--mf-text-md);
		font-variant-numeric: tabular-nums;
		font-weight: var(--mf-weight-semibold);
	}

	.work-bar__segments {
		align-items: center;
		display: flex;
		flex-wrap: wrap;
		gap: var(--mf-space-2);
		min-width: 0;
	}

	.work-segment {
		align-items: center;
		background: var(--mf-bg-panel-2);
		border-radius: var(--mf-radius-2);
		color: var(--mf-fg-secondary);
		display: inline-flex;
		font-size: var(--mf-text-xs);
		gap: var(--mf-space-2);
		min-height: 24px;
		padding: 0 var(--mf-space-3);
		white-space: nowrap;
	}

	button.work-segment {
		border: 0;
		cursor: pointer;
		font-family: inherit;
	}

	button.work-segment:hover,
	button.work-segment[aria-pressed='true'] {
		box-shadow: inset 0 0 0 1px var(--mf-line-strong);
		color: var(--mf-fg-primary);
	}

	.work-segment i {
		background: var(--mf-idle-fg);
		border-radius: 50%;
		height: 6px;
		width: 6px;
	}

	.work-segment[data-tone='active'] i {
		background: var(--mf-active-fg);
	}
	.work-segment[data-tone='ready'] i {
		background: var(--mf-ready-fg);
	}
	.work-segment[data-tone='wait'] i {
		background: var(--mf-wait-fg);
	}
	.work-segment[data-tone='fail'] i {
		background: var(--mf-fail-fg);
	}
	.work-segment--empty {
		color: var(--mf-fg-tertiary);
	}

	.notice {
		align-items: baseline;
		border-left: 3px solid var(--mf-idle-fg);
		display: flex;
		gap: var(--mf-space-5);
		margin-top: var(--mf-space-5);
		padding: var(--mf-space-4) var(--mf-space-5);
	}

	.notice[data-tone='fail'] {
		background: var(--mf-fail-bg);
		border-left-color: var(--mf-fail-fg);
		color: var(--mf-fail-fg);
	}

	.notice[data-tone='wait'] {
		background: var(--mf-wait-bg);
		border-left-color: var(--mf-wait-fg);
		color: var(--mf-wait-fg);
	}

	.notice[data-tone='idle'] {
		background: var(--mf-idle-bg);
		color: var(--mf-idle-fg);
	}

	.notice strong,
	.notice span {
		font-size: var(--mf-text-xs);
	}

	.notice strong {
		white-space: nowrap;
	}

	.workspace {
		background: transparent;
		min-width: 0;
	}

	.workspace__toolbar {
		align-items: end;
		border-bottom: 1px solid var(--mf-line-strong);
		display: grid;
		gap: var(--mf-space-5);
		grid-template-columns: minmax(0, 1fr) auto;
		margin-top: var(--mf-space-8);
		padding-bottom: var(--mf-space-4);
	}

	.workspace__summary {
		align-self: center;
		color: var(--mf-fg-secondary);
		font-size: var(--mf-text-xs);
		font-weight: var(--mf-weight-regular);
		grid-column: 2;
		grid-row: 1;
		white-space: nowrap;
	}

	.workspace__controls {
		align-items: end;
		display: grid;
		gap: var(--mf-space-3);
		grid-column: 1;
		grid-row: 1;
		grid-template-columns: minmax(240px, 320px) repeat(3, minmax(130px, max-content));
		min-width: 0;
	}

	.workspace__controls :global(label) {
		display: block;
		min-width: 0;
	}

	.workspace__controls :global(label > span:not(.sr-only)) {
		clip: rect(0 0 0 0);
		height: 1px;
		overflow: hidden;
		position: absolute;
		white-space: nowrap;
		width: 1px;
	}

	.workspace__controls :global(input),
	.workspace__controls :global(select) {
		background: var(--mf-bg-input);
		border: 1px solid transparent;
		border-radius: var(--mf-radius-2);
		color: var(--mf-fg-primary);
		font: inherit;
		font-size: var(--mf-text-xs);
		height: 36px;
		min-width: 0;
		padding: 0 var(--mf-space-4);
		width: 100%;
	}

	.workspace__controls :global(input:hover),
	.workspace__controls :global(select:hover) {
		border-color: var(--mf-line-strong);
	}

	.workspace__controls :global(input:focus-visible),
	.workspace__controls :global(select:focus-visible) {
		box-shadow: var(--mf-ring-focus);
		outline: none;
	}

	.workspace :global(.library-register) {
		background: var(--mf-bg-panel);
		box-shadow: 0 1px 2px color-mix(in srgb, var(--mf-fg-primary) 8%, transparent);
		min-width: 0;
	}

	.workspace :global(.library-register__header) {
		position: sticky;
		top: 0;
		z-index: 3;
	}

	.workspace :global(.library-workbench) {
		display: grid;
		grid-auto-flow: row;
		grid-template-columns: minmax(0, 1fr);
		min-width: 0;
	}

	.workspace :global(.library-index) {
		min-width: 0;
	}

	.workspace :global(.library-inspector) {
		background: var(--mf-bg-panel-2);
		min-width: 0;
	}

	@media (max-width: 1100px) {
		.library-layout {
			padding-inline: var(--mf-space-7);
		}

		.workspace__toolbar {
			grid-template-columns: minmax(0, 1fr);
		}

		.workspace__summary {
			grid-column: 1;
			grid-row: 2;
			justify-self: start;
		}

		.workspace__controls {
			grid-template-columns: minmax(200px, 1fr) repeat(3, minmax(110px, max-content));
		}
	}

	@media (max-width: 900px) {
		.metric-strip {
			gap: var(--mf-space-6) var(--mf-space-7);
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}
	}

	@media (max-width: 760px) {
		.library-layout {
			padding: var(--mf-space-6) var(--mf-space-5) var(--mf-space-10);
		}

		.status-band {
			margin-top: var(--mf-space-5);
		}

		.metric-strip {
			gap: var(--mf-space-4) var(--mf-space-5);
		}

		.metric-strip__value {
			gap: var(--mf-space-1);
			margin-top: var(--mf-space-1);
		}

		.metric-strip__value strong {
			font-size: 20px;
		}

		.work-bar {
			align-items: center;
			gap: var(--mf-space-2);
			margin-top: var(--mf-space-4);
			flex-wrap: wrap;
		}

		.work-bar__total {
			gap: var(--mf-space-2);
		}

		.work-bar__segments {
			gap: var(--mf-space-1);
		}

		.work-segment {
			min-height: 22px;
			padding-inline: var(--mf-space-2);
		}

		.notice {
			align-items: flex-start;
			flex-direction: column;
			gap: var(--mf-space-1);
		}

		.workspace__toolbar {
			display: flex;
			flex-wrap: wrap;
		}

		.workspace__controls {
			display: grid;
			flex: 1 0 100%;
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}

		.workspace__controls :global(label.search-field) {
			grid-column: 1 / -1;
		}

		.workspace__summary {
			order: 2;
			width: 100%;
		}
	}

	@media (max-width: 440px) {
		.workspace__controls {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}
	}
</style>
