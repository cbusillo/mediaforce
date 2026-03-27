<script lang="ts">
	import { resolve } from '$app/paths';
	import type { FolderPayload, FolderStatusPayload, HostsPayload } from '$lib/api/types';
	import { invalidateAll } from '$app/navigation';
	import { postJson } from '$lib/api/client';
	import Button from '$lib/components/Button.svelte';
	import HostCard from '$lib/components/HostCard.svelte';
	import Panel from '$lib/components/Panel.svelte';
	import Pill from '$lib/components/Pill.svelte';
	import SectionHead from '$lib/components/SectionHead.svelte';
	import StatusBanner from '$lib/components/StatusBanner.svelte';
	import { formatCounts, formatGiB, formatTimestamp } from '$lib/format';
	import { toasts } from '$lib/stores/toasts';

	type FolderActionHost = { label?: string };
	type FolderCalibrationJob = { mode?: string; action?: string; host?: FolderActionHost };
	type FolderPolicy = {
		video?: { quality_metric?: string; default_grain?: number; max_encoded_percent?: number };
		audio?: { surround_5_1_opus_bitrate?: string };
	};
	type FolderItemPlan = {
		video?: { source_codec?: string; output_codec?: string; max_encoded_percent?: number };
		audio?: { source_codec?: string; output_codec?: string };
		subtitles?: { kept_track_count?: number };
	};
	type FolderQueueSample = { running_count?: number; queued_count?: number };
	type FolderCalibrationQueue = { sample?: FolderQueueSample };
	type SampleHostOption = { key?: string; label?: string; detail?: string; available?: boolean };
	type ReviewGate = { can_confirm_full?: boolean; message?: string };

	let {
		data
	}: {
		data: {
			folder: FolderPayload;
			status: FolderStatusPayload;
			hosts: HostsPayload;
		};
	} = $props();

	const folder = $derived(data.folder);
	const status = $derived(data.status);
	const hosts = $derived(data.hosts);
	const calibrationJob = $derived((status.calibration_job as FolderCalibrationJob | null) ?? null);
	const itemPlan = $derived((folder.item_plan as FolderItemPlan | undefined) ?? {});
	const policy = $derived((folder.policy as FolderPolicy | undefined) ?? {});
	const calibrationQueue = $derived(
		(folder.calibration_queue as FolderCalibrationQueue | undefined) ?? {}
	);
	const sampleHostOptions = $derived(
		(folder.sample_host_options as SampleHostOption[] | undefined) ?? []
	);
	const reviewGate = $derived((folder.review_gate as ReviewGate | undefined) ?? {});
	const apiPrefix = $derived(
		folder.prefix
			.split('/')
			.map((segment) => encodeURIComponent(segment))
			.join('/')
	);
	const selectedHostOption = $derived(
		sampleHostOptions.find((option) => String(option.key ?? '') === selectedHost) ?? null
	);
	const canRunSample = $derived(selectedHostOption?.available === true);
	let note = $state('');
	let selectedHost = $state('');
	let actionState = $state<string | null>(null);

	const factItems = $derived.by(() =>
		folder.summary
			? [
					{ label: 'Items', value: String(folder.summary.item_count) },
					{ label: 'Total Size', value: formatGiB(folder.summary.total_size_bytes, 2) },
					{ label: 'Statuses', value: formatCounts(folder.summary.statuses) },
					{ label: 'Video', value: formatCounts(folder.summary.video_codecs) },
					{ label: 'Audio', value: formatCounts(folder.summary.audio_codecs) }
				]
			: []
	);

	$effect(() => {
		const preferred = String(folder.sample_host_key || '').trim();
		const currentStillExists = sampleHostOptions.some(
			(option) => String(option.key ?? '') === selectedHost
		);
		if (currentStillExists) return;

		const nextHost =
			preferred ||
			String(sampleHostOptions.find((option) => option.available)?.key ?? '') ||
			String(sampleHostOptions[0]?.key ?? '');
		selectedHost = nextHost;
	});

	async function runSample() {
		actionState = 'sample';
		try {
			const response = await postJson<{ ok: boolean; message: string }>(
				`/api/folders/${apiPrefix}/ai-tune`,
				{
					note,
					host_key: selectedHost
				}
			);
			toasts.success('Sample queued', response.message);
			note = '';
			await invalidateAll();
		} catch (error) {
			toasts.error(
				'Could not queue sample',
				error instanceof Error ? error.message : 'Unexpected sample error'
			);
		} finally {
			actionState = null;
		}
	}

	async function saveProfile() {
		actionState = 'save';
		try {
			const response = await postJson<{ message: string }>(
				`/api/folders/${apiPrefix}/save-profile`,
				{}
			);
			toasts.success('Draft saved', response.message);
			await invalidateAll();
		} catch (error) {
			toasts.error(
				'Could not save draft',
				error instanceof Error ? error.message : 'Unexpected save error'
			);
		} finally {
			actionState = null;
		}
	}

	async function queueEncode() {
		actionState = 'encode';
		try {
			const response = await postJson<{ message: string }>(
				`/api/folders/${apiPrefix}/queue-encode`,
				{
					notes: note,
					bypass_schedule: false
				}
			);
			toasts.success('Folder encode queued', response.message);
			await invalidateAll();
		} catch (error) {
			toasts.error(
				'Could not queue folder encode',
				error instanceof Error ? error.message : 'Unexpected queue error'
			);
		} finally {
			actionState = null;
		}
	}
</script>

<svelte:head>
	<title>{folder.prefix} · Mediaforce</title>
</svelte:head>

<div class="page-stack">
	<nav class="breadcrumb-row">
		<a href={resolve('/')}>Folders</a>
		<span aria-hidden="true">›</span>
		<span>{folder.prefix}</span>
	</nav>

	<Panel class="folder-header" padding="1.35rem 1.45rem">
		<div class="folder-header-grid">
			<SectionHead
				eyebrow="Calibration Studio"
				heading={folder.prefix}
				lede="Tune this folder with hard-scene samples before you run the real batch."
				size="section"
			/>
			<div class="folder-header-side">
				<p class="lede-copy">{folder.metric_status_copy}</p>
				<div class="pill-row">
					{#each factItems.slice(0, 3) as item (item.label)}
						<Pill label={`${item.label}: ${item.value}`} variant="neutral" wide />
					{/each}
				</div>
			</div>
		</div>
	</Panel>

	{#if status.folder_scan_job && (status.folder_scan_status === 'queued' || status.folder_scan_status === 'running')}
		<StatusBanner
			eyebrow="Folder Refresh"
			heading="Refreshing this folder before you start a run"
			lede="The catalog snapshot is updating in the background so this folder view stays aligned with the latest media state."
			detail={`Started: ${String(status.folder_scan_job.started_at ?? status.folder_scan_job.created_at ?? '')}`}
		/>
	{/if}

	{#if calibrationJob && (status.calibration_status === 'queued' || status.calibration_status === 'running')}
		<StatusBanner
			eyebrow={calibrationJob.mode === 'full' ? 'Proof Encode' : 'Calibration'}
			heading={calibrationJob.mode === 'full'
				? 'Representative-file proof encode is active in the background'
				: 'Sample calibration is active in the background'}
			lede={calibrationJob.mode === 'full'
				? 'This full-file proof creates reviewable compare clips from a finished encode.'
				: 'This sampled run predicts full size quickly, then renders hotspot clips for review.'}
			detail={`Action: ${String(calibrationJob.action ?? '')} · Host: ${String(calibrationJob.host?.label ?? '')}`}
		/>
	{/if}

	<Panel>
		<div class="panel-stack">
			<SectionHead
				eyebrow="Calibration"
				heading="Tune with fast samples, then queue the folder encode"
				lede="Actions live up front so you can review the folder state and move immediately."
				size="section"
			/>
			<div class="pill-row">
				<Pill
					label={`Sample queue: ${String(calibrationQueue.sample?.running_count ?? 0)} running · ${String(calibrationQueue.sample?.queued_count ?? 0)} queued`}
					variant="neutral"
					wide
				/>
				<Pill
					label={`Encode queue: ${folder.encode_queue_summary ?? 'idle'}`}
					variant="ghost"
					wide
				/>
			</div>
			<div class="sample-host-grid">
				{#each sampleHostOptions as option (String(option.key ?? ''))}
					<button
						type="button"
						class:selected={selectedHost === String(option.key ?? '')}
						class:disabled={!option.available}
						class="sample-host-card"
						disabled={!option.available}
						onclick={() => (selectedHost = String(option.key ?? ''))}
					>
						<p class="eyebrow-copy">{option.available ? 'Ready for sample' : 'Unavailable'}</p>
						<p class="sample-host-label">{String(option.label ?? 'Unknown host')}</p>
						<p class="muted-copy">{String(option.detail ?? '')}</p>
					</button>
				{/each}
			</div>
			<p class="muted-copy">{selectedHostOption?.detail ?? folder.sample_host_help_text}</p>
			<label class="field-block">
				<span class="eyebrow-copy">Tuning note</span>
				<textarea
					bind:value={note}
					rows="3"
					placeholder="Describe what looks wrong, or leave blank for the first AI-guided sample."
				></textarea>
			</label>
			<div class="action-row">
				<Button loading={actionState === 'sample'} disabled={!canRunSample} onclick={runSample}
					>Start AI-Guided Sample</Button
				>
				<Button variant="secondary" loading={actionState === 'save'} onclick={saveProfile}
					>Save Draft</Button
				>
				<Button
					variant="ghost"
					loading={actionState === 'encode'}
					disabled={!reviewGate.can_confirm_full}
					onclick={queueEncode}>Queue Folder Encode</Button
				>
			</div>
			<div class="action-note">
				<p class="eyebrow-copy">Next step</p>
				<p class="lede-copy">
					{String(reviewGate.message ?? 'Run or review a calibration to continue.')}
				</p>
			</div>
		</div>
	</Panel>

	<div class="two-up">
		<Panel>
			<div class="panel-stack">
				<SectionHead eyebrow="Folder Summary" heading="What is in this folder" size="section" />
				<div class="fact-grid">
					{#each factItems as item (item.label)}
						<div class="fact-card">
							<p class="eyebrow-copy">{item.label}</p>
							<p class="fact-value">{item.value}</p>
						</div>
					{/each}
				</div>
			</div>
		</Panel>

		<Panel variant="accent">
			<div class="panel-stack">
				<SectionHead
					eyebrow="Representative File"
					heading={String(folder.sample_item?.rel_path ?? 'No representative file yet')}
					size="section"
				/>
				<ul class="detail-list muted-copy">
					<li>Source size: {formatGiB(Number(folder.sample_item?.source_size_bytes ?? 0), 2)}</li>
					<li>
						Video: {String(itemPlan.video?.source_codec ?? '')} to {String(
							itemPlan.video?.output_codec ?? ''
						)}
					</li>
					<li>
						Audio: {String(itemPlan.audio?.source_codec ?? '')} to {String(
							itemPlan.audio?.output_codec ?? ''
						)}
					</li>
					<li>Subtitles kept: {String(itemPlan.subtitles?.kept_track_count ?? '0')}</li>
				</ul>
			</div>
		</Panel>
	</div>

	<div class="two-up">
		<Panel>
			<div class="panel-stack">
				<SectionHead eyebrow="Current Draft" heading="Profile the app will test" size="section" />
				<div class="draft-grid">
					<div class="fact-card">
						<p class="eyebrow-copy">Quality Metric</p>
						<p class="fact-value">
							{String(policy.video?.quality_metric ?? 'auto')} → {folder.resolved_metric}
						</p>
					</div>
					<div class="fact-card">
						<p class="eyebrow-copy">Video Cap</p>
						<p class="fact-value">
							{String(
								itemPlan.video?.max_encoded_percent ?? policy.video?.max_encoded_percent ?? 'n/a'
							)}%
						</p>
					</div>
					<div class="fact-card">
						<p class="eyebrow-copy">Grain</p>
						<p class="fact-value">{String(policy.video?.default_grain ?? 'n/a')}</p>
					</div>
					<div class="fact-card">
						<p class="eyebrow-copy">5.1 Opus</p>
						<p class="fact-value">{String(policy.audio?.surround_5_1_opus_bitrate ?? 'n/a')}</p>
					</div>
				</div>
				<p class="muted-copy">{folder.metric_status_copy}</p>
				<div class="pill-row">
					{#each folder.hot_spots ?? [] as timestamp (timestamp)}
						<Pill label={formatTimestamp(timestamp)} variant="neutral" />
					{/each}
				</div>
			</div>
		</Panel>
	</div>

	<Panel>
		<div class="panel-stack">
			<SectionHead
				eyebrow="Host Status"
				heading="Where this calibration can run"
				lede="Readiness, queue pressure, and host issues stay together so you can pick the next machine quickly."
				size="section"
			/>
			<div class="host-grid">
				{#each hosts.hosts as host (host.key)}
					<HostCard {host} />
				{/each}
			</div>
		</div>
	</Panel>
</div>

<style>
	.page-stack,
	.panel-stack {
		display: grid;
		gap: var(--space-3);
	}

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
		color: var(--ink-soft);
	}

	.breadcrumb-row a {
		color: var(--accent-deep);
		font-weight: 700;
	}

	.two-up {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: var(--space-4);
	}

	.fact-grid,
	.draft-grid {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: var(--space-3);
	}

	.fact-card {
		display: grid;
		gap: var(--space-1);
		padding: 0.95rem 1rem;
		border-radius: var(--radius-md);
		background: var(--surface-2);
		border: 1px solid rgba(23, 35, 31, 0.08);
	}

	.fact-value {
		font-size: 1.05rem;
		font-weight: 700;
		line-height: 1.35;
	}

	.detail-list {
		display: grid;
		gap: var(--space-2);
	}

	.pill-row {
		display: flex;
		gap: var(--space-2);
		flex-wrap: wrap;
	}

	.host-grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
		gap: var(--space-3);
	}

	.action-note {
		display: grid;
		gap: var(--space-2);
		padding: 1rem 1.05rem;
		border-radius: var(--radius-md);
		background: var(--surface-3);
	}

	.sample-host-grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
		gap: var(--space-3);
	}

	.sample-host-card {
		display: grid;
		gap: var(--space-2);
		padding: 1rem;
		border-radius: var(--radius-md);
		border: 1px solid rgba(23, 35, 31, 0.1);
		background: rgba(255, 255, 255, 0.6);
		text-align: left;
	}

	.sample-host-card.selected {
		border-color: rgba(15, 118, 110, 0.34);
		background: rgba(15, 118, 110, 0.1);
		box-shadow: 0 10px 30px rgba(15, 118, 110, 0.12);
	}

	.sample-host-card.disabled {
		opacity: 0.7;
		cursor: not-allowed;
	}

	.sample-host-label {
		font-size: 1.05rem;
		font-weight: 700;
		line-height: 1.2;
	}

	.field-block {
		display: grid;
		gap: var(--space-2);
	}

	textarea {
		width: 100%;
		padding: 0.9rem 1rem;
		border-radius: var(--radius-md);
		border: 1px solid rgba(23, 35, 31, 0.12);
		background: var(--surface-2);
		color: var(--ink);
	}

	textarea {
		min-height: 9rem;
		resize: vertical;
	}

	.action-row {
		display: flex;
		gap: var(--space-2);
		flex-wrap: wrap;
	}

	@media (max-width: 900px) {
		.folder-header-grid,
		.two-up,
		.fact-grid,
		.draft-grid {
			grid-template-columns: 1fr;
		}
	}

	@media (max-width: 720px) {
		.host-grid,
		.sample-host-grid {
			grid-template-columns: 1fr;
		}
	}
</style>
