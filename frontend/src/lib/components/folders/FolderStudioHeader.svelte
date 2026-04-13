<script lang="ts">
	import { resolve } from '$app/paths';
	import Panel from '$lib/components/Panel.svelte';
	import Pill from '$lib/components/Pill.svelte';
	import SectionHead from '$lib/components/SectionHead.svelte';
	import type { BreadcrumbItem } from '$lib/folders/studio';

	type HeaderFactItem = {
		label: string;
		value: string;
	};

	type WorkflowStageCard = {
		key: string;
		label: string;
		status: string;
		detail: string;
	};

	type PreviewSubmission = {
		hostLabel?: string | null;
		note?: string | null;
	};

	let {
		breadcrumbItems,
		folderPrefix,
		metricStatusCopy,
		headerFactItems,
		actionState,
		previewSubmission,
		workflowStageCards,
		showFolderRefresh,
		folderRefreshSignal,
		folderRefreshMeta,
		showCalibrationStatus,
		calibrationSignal,
		calibrationMode,
		calibrationMeta
	}: {
		breadcrumbItems: BreadcrumbItem[];
		folderPrefix: string;
		metricStatusCopy: string;
		headerFactItems: HeaderFactItem[];
		actionState: string | null;
		previewSubmission: PreviewSubmission | null;
		workflowStageCards: WorkflowStageCard[];
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

<Panel class="folder-header" padding="1.35rem 1.45rem">
	<div class="folder-header-grid">
		<SectionHead
			eyebrow="Calibration Studio"
			heading={folderPrefix}
			lede="Tune this folder with hard-scene samples before you run the real batch."
			size="section"
		/>
		<div class="folder-header-side">
			<p class="lede-copy">{metricStatusCopy}</p>
			<div class="pill-row">
				{#each headerFactItems as item (item.label)}
					<Pill label={`${item.label}: ${item.value}`} variant="neutral" wide />
				{/each}
			</div>
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
	<div class="workflow-stage-strip" aria-label="Folder workflow stages">
		{#each workflowStageCards as stage (stage.key)}
			<div class={`workflow-stage-card ${stage.status}`.trim()}>
				<p class="workflow-stage-label">{stage.label}</p>
				<p class="workflow-stage-detail">{stage.detail}</p>
			</div>
		{/each}
	</div>
</Panel>

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
	.folder-header-grid {
		display: grid;
		grid-template-columns: minmax(0, 1.05fr) minmax(280px, 0.95fr);
		gap: var(--space-4);
		align-items: start;
	}

	.folder-header-side {
		display: grid;
		gap: var(--space-3);
	}

	.breadcrumb-row {
		display: flex;
		gap: 0.55rem;
		align-items: center;
		font-size: 0.92rem;
		color: rgba(148, 163, 184, 0.82);
		flex-wrap: wrap;
	}

	.breadcrumb-row a {
		color: #7dd3fc;
		font-weight: 700;
	}

	.workflow-stage-strip {
		display: grid;
		grid-template-columns: repeat(4, minmax(0, 1fr));
		gap: 0.75rem;
		margin-top: 1rem;
	}

	.workflow-stage-card {
		display: grid;
		gap: 0.2rem;
		padding: 0.85rem 0.95rem;
		border-radius: 0;
		background: rgba(15, 23, 42, 0.68);
		border: 1px solid rgba(148, 163, 184, 0.16);
		box-shadow: none;
	}

	.workflow-stage-card.current {
		background: rgba(8, 47, 73, 0.78);
		border-color: rgba(56, 189, 248, 0.28);
	}

	.workflow-stage-card.done {
		background: rgba(20, 83, 45, 0.64);
		border-color: rgba(74, 222, 128, 0.2);
	}

	.workflow-stage-card.pending {
		border-style: dashed;
		background: rgba(15, 20, 27, 0.72);
	}

	.workflow-stage-label,
	.workflow-stage-detail {
		margin: 0;
	}

	.workflow-stage-label {
		font-size: 0.74rem;
		font-weight: 800;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: rgba(125, 211, 252, 0.9);
	}

	.workflow-stage-detail {
		font-size: 0.94rem;
		font-weight: 700;
		line-height: 1.3;
		color: #f8fafc;
	}

	.status-strip-panel {
		background: rgba(15, 20, 27, 0.94);
	}

	.status-strip-panel.in-progress {
		position: relative;
		overflow: hidden;
	}

	.status-strip-panel.in-progress::after {
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

	.accent-strip {
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

	.pill-row,
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
		.folder-header-grid,
		.workflow-stage-strip {
			grid-template-columns: 1fr;
		}
	}

	@media (max-width: 720px) {
		.status-strip {
			align-items: start;
		}

		.status-strip-meta {
			white-space: normal;
		}
	}
</style>
