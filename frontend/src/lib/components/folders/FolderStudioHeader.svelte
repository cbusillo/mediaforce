<script lang="ts">
	import { resolve } from '$app/paths';
	import Panel from '$lib/components/Panel.svelte';
	import type { BreadcrumbItem } from '$lib/folders/studio';

	type HeaderFactItem = {
		label: string;
		value: string;
	};

	type PreviewSubmission = {
		hostLabel?: string | null;
		note?: string | null;
	};

	let {
		breadcrumbItems,
		folderTitle,
		headerFactItems,
		actionState,
		previewSubmission,
		showFolderRefresh,
		folderRefreshSignal,
		folderRefreshMeta,
		showCalibrationStatus,
		calibrationSignal,
		calibrationMode,
		calibrationMeta
	}: {
		breadcrumbItems: BreadcrumbItem[];
		folderTitle: string;
		headerFactItems: HeaderFactItem[];
		actionState: string | null;
		previewSubmission: PreviewSubmission | null;
		showFolderRefresh: boolean;
		folderRefreshSignal: string;
		folderRefreshMeta: string;
		showCalibrationStatus: boolean;
		calibrationSignal: string;
		calibrationMode: 'full' | 'sample';
		calibrationMeta: string;
	} = $props();
</script>

<nav class="breadcrumb-row">
	{#each breadcrumbItems as item, index (`${item.label}-${index}`)}
		{#if index > 0}
			<span aria-hidden="true">›</span>
		{/if}
		{#if item.href}
			<a href={resolve(item.href)}>{item.label}</a>
		{:else}
			<span>{item.label}</span>
		{/if}
	{/each}
</nav>

<section class="folder-header">
	<div class="folder-header-bar">
		<div class="folder-title-block">
			<h1>{folderTitle}</h1>
		</div>
		<div class="fact-row">
			{#each headerFactItems as item (item.label)}
				<span>{item.label}: {item.value}</span>
			{/each}
		</div>
	</div>
	{#if actionState === 'preview' && previewSubmission}
		<div class="status-strip internal-status-strip">
			<div class="section-copy-block">
				<div class="status-strip-signal" aria-live="polite">
					<span class="status-strip-beacon" aria-hidden="true"></span>
					<span>Drafting bench reply</span>
				</div>
				<p class="eyebrow-copy">Bench request</p>
				<p class="status-strip-title">The bench is preparing the next sample draft now</p>
				<p class="muted-copy">
					{previewSubmission.hostLabel
						? `Mediaforce is reading the latest note and review context for ${previewSubmission.hostLabel}. The updated draft card will appear below when the reply lands.`
						: 'Mediaforce is reading the latest note and review context. The updated draft card will appear below when the reply lands.'}
				</p>
			</div>
			<p class="status-strip-meta">
				{previewSubmission.note
					? `Latest note: ${previewSubmission.note}`
					: 'Using the current host and saved review context'}
			</p>
		</div>
	{/if}
</section>

{#if showFolderRefresh}
	<Panel class="status-strip-panel in-progress" padding="0.95rem 1rem">
		<div class="status-strip">
			<div class="section-copy-block">
				<div class="status-strip-signal" aria-live="polite">
					<span class="status-strip-beacon" aria-hidden="true"></span>
					<span>{folderRefreshSignal}</span>
				</div>
				<p class="eyebrow-copy">Folder refresh</p>
				<p class="status-strip-title">Refreshing in the background before you start a run</p>
				<p class="muted-copy">
					The catalog snapshot is updating so this view stays aligned with the latest media state.
				</p>
			</div>
			<p class="status-strip-meta">{folderRefreshMeta}</p>
		</div>
	</Panel>
{/if}

{#if showCalibrationStatus}
	<Panel class="status-strip-panel accent-strip in-progress" padding="0.95rem 1rem">
		<div class="status-strip">
			<div class="section-copy-block">
				<div class="status-strip-signal" aria-live="polite">
					<span class="status-strip-beacon" aria-hidden="true"></span>
					<span>{calibrationSignal}</span>
				</div>
				<p class="eyebrow-copy">{calibrationMode === 'full' ? 'Proof encode' : 'Calibration'}</p>
				<p class="status-strip-title">
					{calibrationMode === 'full'
						? 'Representative-file proof encode is running'
						: 'Sample calibration is running'}
				</p>
				<p class="muted-copy">
					{calibrationMode === 'full'
						? 'This full-file proof creates reviewable compare clips from a finished encode.'
						: 'This sampled run predicts full size quickly, then renders hotspot clips for review.'}
				</p>
			</div>
			<p class="status-strip-meta">{calibrationMeta}</p>
		</div>
	</Panel>
{/if}

<style>
	.folder-header-bar {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		gap: 0.85rem;
		align-items: end;
	}

	.folder-header {
		display: grid;
		gap: 0.35rem;
		padding: 0.05rem 0 0.45rem;
		border-bottom: 1px solid rgba(148, 163, 184, 0.14);
	}

	.folder-title-block {
		display: grid;
		gap: 0.15rem;
		min-width: 0;
	}

	.folder-title-block h1 {
		margin: 0;
		font-size: clamp(1.1rem, 1.8vw, 1.4rem);
		line-height: 1.05;
		overflow-wrap: break-word;
	}

	.breadcrumb-row {
		display: flex;
		gap: 0.45rem;
		align-items: center;
		font-size: 0.82rem;
		color: rgba(148, 163, 184, 0.82);
		flex-wrap: wrap;
	}

	.breadcrumb-row a {
		color: #7dd3fc;
		font-weight: 600;
	}

	.fact-row {
		display: flex;
		justify-content: flex-end;
		gap: 0.9rem;
		flex-wrap: wrap;
		font-size: 0.82rem;
		font-weight: 700;
		color: rgba(226, 232, 240, 0.78);
	}

	@media (max-width: 1100px) {
		.breadcrumb-row {
			gap: 0.35rem;
			font-size: 0.76rem;
		}

		.folder-header {
			gap: 0.22rem;
			padding: 0 0 0.3rem;
		}

		.folder-header-bar {
			gap: 0.6rem;
		}

		.folder-title-block h1 {
			font-size: 1rem;
		}

		.fact-row {
			gap: 0.65rem;
			font-size: 0.76rem;
		}
	}

	:global(.status-strip-panel) {
		background: rgba(15, 20, 27, 0.94);
	}

	:global(.status-strip-panel.in-progress) {
		position: relative;
		overflow: hidden;
	}

	:global(.status-strip-panel.in-progress)::after {
		content: '';
		position: absolute;
		inset: 0;
		pointer-events: none;
		background: linear-gradient(
			110deg,
			rgba(125, 211, 252, 0) 0%,
			rgba(125, 211, 252, 0.12) 18%,
			rgba(125, 211, 252, 0) 36%
		);
		transform: translateX(-150%);
		animation: status-strip-sheen 2.8s ease-in-out infinite;
	}

	:global(.accent-strip) {
		background: rgba(8, 47, 73, 0.86);
	}

	.status-strip {
		display: flex;
		justify-content: space-between;
		gap: 1rem;
		align-items: end;
		flex-wrap: wrap;
	}

	.status-strip-title {
		margin: 0;
		font-size: 1rem;
		font-weight: 700;
		line-height: 1.35;
	}

	.status-strip-signal {
		display: inline-flex;
		align-items: center;
		gap: 0.45rem;
		margin-bottom: 0.35rem;
		padding: 0.28rem 0.62rem;
		border-radius: 999px;
		background: rgba(30, 41, 59, 0.82);
		color: #e2e8f0;
		font-size: 0.72rem;
		font-weight: 700;
		letter-spacing: 0.08em;
		text-transform: uppercase;
	}

	.status-strip-beacon {
		position: relative;
		width: 0.62rem;
		height: 0.62rem;
		border-radius: 999px;
		background: #e4572e;
		box-shadow: 0 0 0 0 rgba(228, 87, 46, 0.4);
		animation: status-strip-pulse 1.35s ease-out infinite;
	}

	.status-strip-meta {
		margin: 0;
		font-size: 0.82rem;
		line-height: 1.45;
		color: var(--ink-soft);
		white-space: nowrap;
	}

	.fact-row {
		display: flex;
		gap: 1rem;
		flex-wrap: wrap;
		justify-content: flex-end;
		font-size: 0.84rem;
		font-weight: 700;
		color: rgba(203, 213, 225, 0.82);
	}

	.section-copy-block {
		display: grid;
		gap: 0.3rem;
		min-width: 0;
	}

	@keyframes status-strip-pulse {
		0% {
			box-shadow: 0 0 0 0 rgba(228, 87, 46, 0.38);
		}

		70% {
			box-shadow: 0 0 0 0.68rem rgba(228, 87, 46, 0);
		}

		100% {
			box-shadow: 0 0 0 0 rgba(228, 87, 46, 0);
		}
	}

	@keyframes status-strip-sheen {
		0% {
			transform: translateX(-150%);
		}

		100% {
			transform: translateX(150%);
		}
	}

	@media (max-width: 900px) {
		.folder-header-bar {
			grid-template-columns: 1fr;
		}

		.fact-row {
			justify-content: flex-start;
		}
	}

	@media (max-width: 720px) {
		.folder-header-bar {
			grid-template-columns: 1fr;
			align-items: start;
		}

		.fact-row {
			justify-content: flex-start;
		}

		.status-strip {
			align-items: start;
		}

		.status-strip-meta {
			white-space: normal;
		}
	}
</style>
