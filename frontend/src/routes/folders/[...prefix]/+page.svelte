<script lang="ts">
	import { resolve } from '$app/paths';
	import type {
		FolderPayload,
		FolderStatusPayload,
		HostRuntime,
		HostsPayload
	} from '$lib/api/types';
	import { invalidateAll } from '$app/navigation';
	import { postJson } from '$lib/api/client';
	import Button from '$lib/components/Button.svelte';
	import HostCard from '$lib/components/HostCard.svelte';
	import Panel from '$lib/components/Panel.svelte';
	import Pill from '$lib/components/Pill.svelte';
	import SectionHead from '$lib/components/SectionHead.svelte';
	import StatusBanner from '$lib/components/StatusBanner.svelte';
	import { folderLibraryLabel } from '$lib/folder-display';
	import { formatGiB, formatTimestamp, titleCase } from '$lib/format';
	import { toasts } from '$lib/stores/toasts';

	type FolderActionHost = { label?: string };
	type FolderCalibrationJob = { mode?: string; action?: string; host?: FolderActionHost };
	type FolderPolicy = {
		video?: {
			quality_metric?: string;
			default_grain?: number;
			max_encoded_percent?: number;
			target_vmaf?: number;
			min_target_vmaf?: number;
			target_xpsnr?: number;
			min_target_xpsnr?: number;
		};
		audio?: {
			surround_5_1_opus_bitrate?: string;
			stereo_opus_bitrate?: string;
			surround_7_1_opus_bitrate?: string;
		};
		subtitle?: { prefer_text?: boolean };
	};
	type FolderItemPlan = {
		video?: {
			source_codec?: string;
			output_codec?: string;
			quality_metric?: string;
			target?: number;
			min_target?: number;
			max_encoded_percent?: number;
			default_grain?: number;
		};
		audio?: {
			source_codec?: string;
			output_codec?: string;
			output_bitrate?: string | null;
			channels?: number;
			language?: string;
			action?: string;
			source_track_count?: number;
			kept_track_count?: number;
		};
		subtitles?: {
			kept_track_count?: number;
			source_track_count?: number;
			languages?: string[];
			codecs?: string[];
		};
	};
	type FolderQueueSample = { running_count?: number; queued_count?: number };
	type FolderCalibrationQueue = { sample?: FolderQueueSample };
	type FolderCompareClip = { timestamp_seconds?: number };
	type FolderCalibrationState = { compare_clips?: FolderCompareClip[] };
	type SampleHostOption = { key?: string; label?: string; detail?: string; available?: boolean };
	type ReviewGate = { can_confirm_full?: boolean; message?: string };
	type BreadcrumbHref = '/' | `/folders/${string}`;
	type BreadcrumbItem = { label: string; href: BreadcrumbHref | null };
	type SampleAudioTrack = {
		codec_name?: string;
		channels?: number;
		language?: string;
		default?: number | boolean;
		bit_rate?: number | string;
	};
	type SampleSubtitleTrack = {
		codec_name?: string;
		language?: string;
		default?: number | boolean;
		forced?: number | boolean;
	};
	type FolderSampleItem = {
		rel_path?: string;
		source_size_bytes?: number;
		video_codec?: string;
		container?: string;
		duration_seconds?: number;
		audio_summary?: SampleAudioTrack[];
		subtitle_summary?: SampleSubtitleTrack[];
		resolved_policy?: FolderPolicy;
	};
	type ComparisonValue = {
		headline: string;
		detail?: string;
	};
	type ComparisonRow = {
		label: string;
		current: ComparisonValue;
		draft: ComparisonValue;
		changed: boolean;
	};
	type SteadyComparisonRow = { label: string; value: string };
	type SampleHostCard = {
		key: string;
		label: string;
		detail: string;
		available: boolean;
		runtime: HostRuntime | null;
		preferred: boolean;
	};

	function formatStatusCountCopy(mapping: Record<string, number> | null | undefined): string {
		if (!mapping) return 'None';
		const entries = Object.entries(mapping);
		if (entries.length === 0) return 'None';
		return entries
			.map(([key, value]) => {
				const label =
					key === 'discovered'
						? 'discovered only'
						: key === 'promoted'
							? 'complete'
							: titleCase(key).toLowerCase();
				return `${value} ${label}`;
			})
			.join(' · ');
	}

	function codecLabel(codec: string | null | undefined): string {
		const key = String(codec ?? '')
			.trim()
			.toLowerCase();
		if (!key) return 'Unknown';
		const labels: Record<string, string> = {
			h264: 'H.264',
			h265: 'H.265',
			hevc: 'HEVC',
			av1: 'AV1',
			aac: 'AAC',
			ac3: 'AC-3',
			eac3: 'E-AC-3',
			dca: 'DTS',
			dts: 'DTS',
			truehd: 'TrueHD',
			flac: 'FLAC',
			opus: 'Opus',
			subrip: 'SRT',
			srt: 'SRT',
			ass: 'ASS',
			ssa: 'SSA',
			hdmv_pgs_subtitle: 'PGS',
			pgs: 'PGS'
		};
		return labels[key] ?? key.toUpperCase();
	}

	function channelLabel(channels: number | null | undefined): string | null {
		const value = Number(channels ?? 0);
		if (!Number.isFinite(value) || value <= 0) return null;
		if (value === 1) return 'mono';
		if (value === 2) return 'stereo';
		if (value === 6) return '5.1';
		if (value === 8) return '7.1';
		return `${value}ch`;
	}

	function formatBitrateCopy(value: number | string | null | undefined): string | null {
		if (value == null || value === '') return null;
		if (typeof value === 'number') {
			if (!Number.isFinite(value) || value <= 0) return null;
			return `${Math.round(value / 1000).toLocaleString('en-US')} kbps`;
		}
		const trimmed = String(value).trim();
		if (!trimmed) return null;
		if (/^\d+(?:\.\d+)?$/.test(trimmed)) {
			const numeric = Number(trimmed);
			if (!Number.isFinite(numeric) || numeric <= 0) return null;
			const kbps = numeric > 1000 ? Math.round(numeric / 1000) : numeric;
			return `${kbps.toLocaleString('en-US')} kbps`;
		}
		if (/^\d+(?:\.\d+)?k$/i.test(trimmed)) {
			return `${Number(trimmed.replace(/k$/i, '')).toLocaleString('en-US')} kbps`;
		}
		return trimmed;
	}

	function formatLanguageCopy(value: string | null | undefined): string | null {
		const trimmed = String(value ?? '').trim();
		if (!trimmed || trimmed === 'und') return null;
		return trimmed.toUpperCase();
	}

	function compactCopy(parts: Array<string | null | undefined>): string {
		return parts.filter(Boolean).join(' · ');
	}

	function comparisonValue(headline: string, detail?: string | null): ComparisonValue {
		return detail ? { headline, detail } : { headline };
	}

	function summarizeAudioTrack(track: SampleAudioTrack | null): ComparisonValue {
		if (!track) return comparisonValue('No audio track found');
		return comparisonValue(
			compactCopy([codecLabel(track.codec_name), formatBitrateCopy(track.bit_rate)]),
			compactCopy([
				channelLabel(track.channels),
				formatLanguageCopy(track.language),
				track.default ? 'default' : null
			])
		);
	}

	function summarizeAudioPlan(plan: FolderItemPlan['audio'] | undefined): ComparisonValue {
		if (!plan) return comparisonValue('No draft audio plan yet');
		const trackSummary =
			Number(plan.kept_track_count ?? 0) > 0 && Number(plan.source_track_count ?? 0) > 0
				? `keep ${plan.kept_track_count} of ${plan.source_track_count} tracks`
				: null;
		return comparisonValue(
			compactCopy([codecLabel(plan.output_codec), formatBitrateCopy(plan.output_bitrate)]),
			compactCopy([
				channelLabel(plan.channels),
				formatLanguageCopy(plan.language),
				plan.action === 'copy' ? 'copy current track' : trackSummary
			])
		);
	}

	function summarizeSubtitleSource(tracks: SampleSubtitleTrack[]): ComparisonValue {
		if (tracks.length === 0) return comparisonValue('No subtitle tracks');
		const grouped: Record<string, number> = {};
		tracks.forEach((track) => {
			const parts = [formatLanguageCopy(track.language), codecLabel(track.codec_name)].filter(
				Boolean
			);
			if (track.forced) parts.push('forced');
			const label = parts.join(' ');
			grouped[label] = (grouped[label] ?? 0) + 1;
		});
		const preview = Object.entries(grouped)
			.slice(0, 3)
			.map(([label, count]) => (count > 1 ? `${label} x${count}` : label));
		const suffix = tracks.length > 3 ? ` · +${tracks.length - 3} more` : '';
		return comparisonValue(
			`${tracks.length} subtitle track${tracks.length === 1 ? '' : 's'}`,
			`${preview.join(' · ')}${suffix}`
		);
	}

	function summarizeSubtitlePlan(
		plan: FolderItemPlan['subtitles'] | undefined,
		preferText: boolean
	): ComparisonValue {
		if (!plan) return comparisonValue('No subtitle draft yet');
		const kept = Number(plan.kept_track_count ?? 0);
		if (kept === 0) return comparisonValue('No subtitles kept');
		const languages = (plan.languages ?? [])
			.map((language) => formatLanguageCopy(language))
			.filter(Boolean);
		const codecs = (plan.codecs ?? []).map((codec) => codecLabel(codec));
		return comparisonValue(
			`Keep ${kept} subtitle track${kept === 1 ? '' : 's'}`,
			compactCopy([
				languages.length ? languages.join(', ') : null,
				codecs.length ? codecs.join(', ') : null,
				preferText ? 'prefer text' : 'allow image subtitles'
			])
		);
	}

	function metricPreferenceLabel(
		metric: string | null | undefined,
		resolvedMetric: string | null | undefined
	): string {
		const raw = String(metric ?? 'auto')
			.trim()
			.toLowerCase();
		if (raw === 'auto') {
			return resolvedMetric ? `Auto -> ${resolvedMetric}` : 'Auto';
		}
		return raw.toUpperCase();
	}

	function resolveMetricLabel(
		metric: string | null | undefined,
		metricSupport: FolderPayload['metric_support']
	): string {
		const raw = String(metric ?? 'auto')
			.trim()
			.toLowerCase();
		if (raw === 'auto') {
			return metricSupport.vmaf ? 'VMAF' : 'XPSNR';
		}
		return raw.toUpperCase();
	}

	function summarizeMetricPolicy(
		policyValue: FolderPolicy | undefined,
		metricSupport: FolderPayload['metric_support']
	): ComparisonValue {
		const video = policyValue?.video ?? {};
		const resolved = resolveMetricLabel(video.quality_metric, metricSupport);
		const target = resolved === 'VMAF' ? video.target_vmaf : video.target_xpsnr;
		const floor = resolved === 'VMAF' ? video.min_target_vmaf : video.min_target_xpsnr;
		return comparisonValue(
			metricPreferenceLabel(video.quality_metric, resolved),
			compactCopy([
				target != null ? `target ${target}` : null,
				floor != null ? `floor ${floor}` : null
			])
		);
	}

	function summarizeMetricPlan(plan: FolderItemPlan['video'] | undefined): ComparisonValue {
		if (!plan) return comparisonValue('No draft video metric yet');
		return comparisonValue(
			metricPreferenceLabel(plan.quality_metric, String(plan.quality_metric ?? '').toUpperCase()),
			compactCopy([
				plan.target != null ? `target ${plan.target}` : null,
				plan.min_target != null ? `floor ${plan.min_target}` : null
			])
		);
	}

	function compareValues(
		current: ComparisonValue | string,
		draft: ComparisonValue | string
	): boolean {
		const left =
			typeof current === 'string' ? current : `${current.headline} ${current.detail ?? ''}`;
		const right = typeof draft === 'string' ? draft : `${draft.headline} ${draft.detail ?? ''}`;
		return left.trim() !== right.trim();
	}

	function pathFilename(value: string | null | undefined): string {
		const trimmed = String(value ?? '').trim();
		if (!trimmed) return 'No representative file yet';
		return trimmed.split('/').at(-1) ?? trimmed;
	}

	function pathStem(value: string | null | undefined): string {
		const filename = pathFilename(value);
		const lastDot = filename.lastIndexOf('.');
		if (lastDot <= 0) return filename;
		return filename.slice(0, lastDot);
	}

	function pathExtension(value: string | null | undefined): string | null {
		const filename = pathFilename(value);
		const lastDot = filename.lastIndexOf('.');
		if (lastDot <= 0 || lastDot === filename.length - 1) return null;
		return filename.slice(lastDot + 1).toUpperCase();
	}

	function softWrapTokens(value: string): string[] {
		return value.split(/([._\-[\]()\s]+)/).filter(Boolean);
	}

	function formatCodecCountKey(key: string): string {
		const [codec, channelCount] = key.split(':');
		const channelCopy = channelCount ? channelLabel(Number(channelCount)) : null;
		return [codecLabel(codec), channelCopy].filter(Boolean).join(' ');
	}

	function formatCodecCountsCopy(mapping: Record<string, number> | null | undefined): string {
		if (!mapping) return 'None';
		const entries = Object.entries(mapping);
		if (entries.length === 0) return 'None';
		return entries.map(([key, value]) => `${value} ${formatCodecCountKey(key)}`).join(' · ');
	}

	function compactScheduleCopy(runtime: HostRuntime | null): string | null {
		if (!runtime) return null;
		if (!runtime.schedule_profile_label || runtime.schedule_profile_label === 'Always') {
			return 'Always';
		}
		return runtime.schedule_detail
			.replace(/^window\s+/i, '')
			.replace(/\s+in host local time$/i, ' local time');
	}

	function hostCapacityCopy(runtime: HostRuntime | null): string | null {
		if (!runtime) return null;
		const laneLabel = `lane${runtime.max_parallel_encodes === 1 ? '' : 's'}`;
		if (runtime.active_encode_count > 0) {
			return `${runtime.active_encode_count} of ${runtime.max_parallel_encodes} ${laneLabel} running`;
		}
		if (runtime.queue_active) {
			return `${runtime.max_parallel_encodes} ${laneLabel} available`;
		}
		if (runtime.schedule_open === false) {
			return `${runtime.max_parallel_encodes} ${laneLabel} waiting on schedule`;
		}
		if (runtime.active_reason === 'parallel encode slots are full') {
			return `${runtime.max_parallel_encodes} ${laneLabel} busy`;
		}
		return runtime.message;
	}

	function queueSummaryCopy(runningCount: number, queuedCount: number, label: string): string {
		if (runningCount === 0 && queuedCount === 0) {
			return `${label}: idle`;
		}
		return `${label}: ${runningCount} running · ${queuedCount} queued`;
	}

	function encodeQueueSummaryCopy(summary: string | undefined): string {
		const trimmed = String(summary ?? '').trim();
		if (!trimmed || trimmed.startsWith('0 running · 0 queued')) {
			return 'Encode queue: idle';
		}
		return `Encode queue: ${trimmed}`;
	}

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
	const rankedHosts = $derived.by(() =>
		[...hosts.hosts].sort(
			(left, right) =>
				Number(right.priority) - Number(left.priority) || left.label.localeCompare(right.label)
		)
	);
	const hostRuntimeByKey = $derived.by(
		() => new Map(rankedHosts.map((host) => [host.key, host] as const))
	);
	const calibrationJob = $derived((status.calibration_job as FolderCalibrationJob | null) ?? null);
	const sampleItem = $derived((folder.sample_item as FolderSampleItem | undefined) ?? {});
	const itemPlan = $derived((folder.item_plan as FolderItemPlan | undefined) ?? {});
	const policy = $derived((folder.policy as FolderPolicy | undefined) ?? {});
	const baselinePolicy = $derived((sampleItem.resolved_policy as FolderPolicy | undefined) ?? {});
	const calibrationQueue = $derived(
		(folder.calibration_queue as FolderCalibrationQueue | undefined) ?? {}
	);
	const sampleHostOptions = $derived(
		(folder.sample_host_options as SampleHostOption[] | undefined) ?? []
	);
	const calibration = $derived((folder.calibration as FolderCalibrationState | undefined) ?? {});
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
	const selectedHostRuntime = $derived.by(() => hostRuntimeByKey.get(selectedHost) ?? null);
	const canRunSample = $derived(selectedHostOption?.available === true);
	let note = $state('');
	let noteExpanded = $state(false);
	let selectedHost = $state('');
	let actionState = $state<string | null>(null);

	const breadcrumbItems = $derived.by(() => {
		const segments = folder.prefix.split('/').filter(Boolean);
		const items: BreadcrumbItem[] = [{ label: 'Folders', href: '/' }];
		const prefixParts: string[] = [];
		segments.forEach((segment, index) => {
			prefixParts.push(segment);
			items.push({
				label: index === 0 ? folderLibraryLabel(segment) : segment,
				href:
					index === segments.length - 1
						? null
						: `/folders/${prefixParts.map((part) => encodeURIComponent(part)).join('/')}`
			});
		});
		return items;
	});

	const summaryStatusCopy = $derived.by(() => formatStatusCountCopy(folder.summary?.statuses));

	const sampleHostCards = $derived.by(() =>
		sampleHostOptions
			.map((option) => {
				const key = String(option.key ?? '');
				const card: SampleHostCard = {
					key,
					label: String(option.label ?? 'Unknown host'),
					detail: String(option.detail ?? ''),
					available: option.available === true,
					runtime: hostRuntimeByKey.get(key) ?? null,
					preferred: key === String(folder.sample_host_key ?? '')
				};
				return card;
			})
			.sort(
				(left, right) =>
					Number(right.available) - Number(left.available) ||
					Number(right.runtime?.priority ?? -1) - Number(left.runtime?.priority ?? -1) ||
					left.label.localeCompare(right.label)
			)
	);

	const reviewMomentPills = $derived.by(() => {
		const labels = ['Early review', 'Midpoint review', 'Late review'];
		return (calibration.compare_clips ?? []).map((clip, index) => {
			const timestamp = Number(clip.timestamp_seconds ?? 0);
			return {
			key: `${index}-${timestamp}`,
			label: `${labels[index] ?? `Compare clip ${index + 1}`}: ${formatTimestamp(timestamp)}`
			};
		});
	});

	const factItems = $derived.by(() =>
		folder.summary
			? [
					{ label: 'Items', value: String(folder.summary.item_count) },
					{ label: 'Total Size', value: formatGiB(folder.summary.total_size_bytes, 2) },
					{ label: 'Statuses', value: summaryStatusCopy },
					{ label: 'Video', value: formatCodecCountsCopy(folder.summary.video_codecs) },
					{ label: 'Audio', value: formatCodecCountsCopy(folder.summary.audio_codecs) }
				]
			: []
	);
	const headerFactItems = $derived.by(() =>
		factItems.filter((item) => item.label !== 'Statuses').slice(0, 2)
	);
	const sampleQueueLabel = $derived.by(() =>
		queueSummaryCopy(
			Number(calibrationQueue.sample?.running_count ?? 0),
			Number(calibrationQueue.sample?.queued_count ?? 0),
			'Sample queue'
		)
	);
	const encodeQueueLabel = $derived.by(() => encodeQueueSummaryCopy(folder.encode_queue_summary));
	const folderSnapshotItems = $derived.by(() =>
		factItems.filter((item) => !['Items', 'Total Size'].includes(item.label))
	);
	const representativePath = $derived(String(sampleItem.rel_path ?? '').trim());
	const representativeFilenameStem = $derived(pathStem(representativePath));
	const representativeFilenameTokens = $derived.by(() =>
		softWrapTokens(representativeFilenameStem)
	);
	const representativeExtension = $derived(pathExtension(representativePath));
	const representativeAudioTrack = $derived.by(() => {
		const tracks = (sampleItem.audio_summary ?? []) as SampleAudioTrack[];
		return tracks.find((track) => Boolean(track.default)) ?? tracks[0] ?? null;
	});
	const streamComparisonRows = $derived.by(() => {
		const rows: ComparisonRow[] = [];
		const currentVideo = comparisonValue(
			codecLabel(sampleItem.video_codec),
			representativeExtension ?? undefined
		);
		const draftVideo = comparisonValue(
			codecLabel(itemPlan.video?.output_codec),
			representativeExtension ?? undefined
		);
		rows.push({
			label: 'Video stream',
			current: currentVideo,
			draft: draftVideo,
			changed: compareValues(currentVideo, draftVideo)
		});

		const currentAudio = summarizeAudioTrack(representativeAudioTrack);
		const draftAudio = summarizeAudioPlan(itemPlan.audio);
		rows.push({
			label: 'Primary audio',
			current: currentAudio,
			draft: draftAudio,
			changed: compareValues(currentAudio, draftAudio)
		});

		const currentSubs = summarizeSubtitleSource(
			(sampleItem.subtitle_summary ?? []) as SampleSubtitleTrack[]
		);
		const draftSubs = summarizeSubtitlePlan(
			itemPlan.subtitles,
			Boolean(policy.subtitle?.prefer_text ?? baselinePolicy.subtitle?.prefer_text ?? true)
		);
		rows.push({
			label: 'Subtitles',
			current: currentSubs,
			draft: draftSubs,
			changed: compareValues(currentSubs, draftSubs)
		});

		return rows;
	});
	const policyComparisonRows = $derived.by(() => {
		const rows: ComparisonRow[] = [];
		const currentMetric = summarizeMetricPolicy(baselinePolicy, folder.metric_support);
		const draftMetric = summarizeMetricPlan(itemPlan.video);
		rows.push({
			label: 'Quality guardrail',
			current: currentMetric,
			draft: draftMetric,
			changed: compareValues(currentMetric, draftMetric)
		});

		const currentCap = comparisonValue(
			`${String(baselinePolicy.video?.max_encoded_percent ?? 'n/a')}%`
		);
		const draftCap = comparisonValue(
			`${String(itemPlan.video?.max_encoded_percent ?? policy.video?.max_encoded_percent ?? 'n/a')}%`
		);
		rows.push({
			label: 'Size ceiling',
			current: currentCap,
			draft: draftCap,
			changed: compareValues(currentCap, draftCap)
		});

		const currentGrain = comparisonValue(String(baselinePolicy.video?.default_grain ?? 'n/a'));
		const draftGrain = comparisonValue(
			String(itemPlan.video?.default_grain ?? policy.video?.default_grain ?? 'n/a')
		);
		rows.push({
			label: 'Film grain',
			current: currentGrain,
			draft: draftGrain,
			changed: compareValues(currentGrain, draftGrain)
		});

		const currentSurround = comparisonValue(
			formatBitrateCopy(baselinePolicy.audio?.surround_5_1_opus_bitrate) ?? 'n/a'
		);
		const draftSurround = comparisonValue(
			formatBitrateCopy(
				policy.audio?.surround_5_1_opus_bitrate ?? itemPlan.audio?.output_bitrate
			) ?? 'n/a'
		);
		rows.push({
			label: '5.1 Opus budget',
			current: currentSurround,
			draft: draftSurround,
			changed: compareValues(currentSurround, draftSurround)
		});

		return rows;
	});
	const changedPolicyRows = $derived.by(() => policyComparisonRows.filter((row) => row.changed));
	const steadyPolicyRows = $derived.by<SteadyComparisonRow[]>(() =>
		policyComparisonRows
			.filter((row) => !row.changed)
			.map((row) => ({
				label: row.label,
				value: compactCopy([row.current.headline, row.current.detail ?? 'unchanged'])
			}))
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

	$effect(() => {
		if (note.trim()) {
			noteExpanded = true;
		}
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
			noteExpanded = false;
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
				heading={folder.prefix}
				lede="Tune this folder with hard-scene samples before you run the real batch."
				size="section"
			/>
			<div class="folder-header-side">
				<p class="lede-copy">{folder.metric_status_copy}</p>
				<div class="pill-row">
					{#each headerFactItems as item (item.label)}
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

	<div class="workflow-stack">
		<Panel>
			<div class="panel-stack">
				<SectionHead
					eyebrow="1. Run a calibration"
					heading="Choose a host and start the sample"
					lede="Pick the best machine, optionally leave a note, then run or save the draft without losing the encode gate context."
					size="section"
				/>
				<div class="pill-row">
					<Pill label={sampleQueueLabel} variant="neutral" wide />
					<Pill label={encodeQueueLabel} variant="ghost" wide />
				</div>
				<div class="run-layout">
					<div class="run-host-column">
						<div class="sample-host-grid">
							{#each sampleHostCards as hostCard (hostCard.key)}
								<button
									type="button"
									class:selected={selectedHost === hostCard.key}
									class:disabled={!hostCard.available}
									class:preferred={hostCard.preferred}
									class="sample-host-card"
									disabled={!hostCard.available}
									onclick={() => (selectedHost = hostCard.key)}
								>
									<div class="sample-host-topline">
										<p class="eyebrow-copy">
											{hostCard.available ? 'Ready for sample' : 'Unavailable'}
										</p>
										{#if hostCard.preferred}
											<span class="sample-host-badge">Recommended</span>
										{/if}
									</div>
									<p class="sample-host-label">{hostCard.label}</p>
									{#if hostCard.detail && hostCard.detail !== hostCard.label}
										<p class="muted-copy">{hostCard.detail}</p>
									{/if}
									<div class="sample-host-meta">
										{#if hostCard.runtime}
											<span>P{hostCard.runtime.priority}</span>
										{/if}
										{#if compactScheduleCopy(hostCard.runtime)}
											<span>{compactScheduleCopy(hostCard.runtime)}</span>
										{/if}
									</div>
									{#if hostCapacityCopy(hostCard.runtime)}
										<p class="sample-host-capacity">{hostCapacityCopy(hostCard.runtime)}</p>
									{/if}
								</button>
							{/each}
						</div>
						{#if !selectedHostRuntime && folder.sample_host_help_text}
							<p class="muted-copy host-selection-note">{folder.sample_host_help_text}</p>
						{/if}
					</div>
					<div class="run-action-column">
						<div class="action-row">
							<Button
								loading={actionState === 'sample'}
								disabled={!canRunSample}
								onclick={runSample}>Start AI-Guided Sample</Button
							>
							<Button
								variant="secondary"
								loading={actionState === 'save'}
								onclick={saveProfile}>Save Draft</Button
							>
							<div class="queue-action-block">
								<Button
									variant="ghost"
									loading={actionState === 'encode'}
									disabled={!reviewGate.can_confirm_full}
									onclick={queueEncode}>Queue Folder Encode</Button
								>
								{#if !reviewGate.can_confirm_full}
									<p class="inline-gate-copy">
										<span class="eyebrow-copy">Blocked</span>
										{String(reviewGate.message ?? 'Run or review a calibration to continue.')}
									</p>
								{/if}
							</div>
						</div>
						<div class="note-row">
							<button
								type="button"
								class="note-toggle"
								aria-expanded={noteExpanded}
								onclick={() => (noteExpanded = !noteExpanded)}
							>
								{noteExpanded ? 'Hide optional tuning note' : 'Add optional tuning note'}
							</button>
						</div>
						{#if noteExpanded}
							<label class="field-block note-panel">
								<span class="eyebrow-copy">Tuning note</span>
								<textarea
									bind:value={note}
									rows="3"
									placeholder="Describe what looks wrong, or leave blank for the first AI-guided sample."
								></textarea>
							</label>
						{/if}
					</div>
				</div>
			</div>
		</Panel>

		<Panel variant="accent">
			<div class="panel-stack">
				<SectionHead
					eyebrow="2. Review the draft"
					heading="Check what this run will test"
					lede="Keep the same decision detail, but scan it as a compact diff instead of a wall of cards."
					size="section"
				/>
				<div class="review-grid">
					<div class="review-block representative-block">
						<p class="eyebrow-copy">Representative file</p>
						<div
							class="representative-path-shell"
							title={representativePath || 'No representative file yet'}
						>
							<h3 class="review-block-title representative-file-name">
								{#each representativeFilenameTokens as token, index (`${index}-${token}`)}
									<span>{token}</span><wbr />
								{/each}
							</h3>
							<div class="representative-meta-row">
								{#if representativeExtension}
									<Pill label={representativeExtension} variant="neutral" />
								{/if}
								<Pill
									label={`Size ${formatGiB(Number(sampleItem.source_size_bytes ?? 0), 2)}`}
									variant="neutral"
								/>
								<Pill
									label={`Length ${formatTimestamp(Number(sampleItem.duration_seconds ?? 0))}`}
									variant="neutral"
								/>
							</div>
							<p class="muted-copy representative-support-copy">
								Representative pick for the preview clips and the draft diff below.
							</p>
						</div>
					</div>
					<div class="review-block">
						<p class="eyebrow-copy">Draft comparison</p>
						<div class="comparison-stack">
							<div class="comparison-group">
								<div class="comparison-group-head">
									<h3 class="comparison-group-title">Source media to draft output</h3>
								</div>
								<div class="comparison-table-head" aria-hidden="true">
									<span>Current</span>
									<span>Draft</span>
								</div>
								<div class="comparison-list compact-list">
									{#each streamComparisonRows as row (row.label)}
										<article class="comparison-row compact-row changed-row">
											<div class="comparison-row-head">
												<p class="comparison-label">{row.label}</p>
												<span class="comparison-status-pill changed-pill">Changed</span>
											</div>
											<div class="comparison-values">
												<div class="comparison-value-card">
													<p class="comparison-copy">{row.current.headline}</p>
													{#if row.current.detail}
														<p class="comparison-subcopy">{row.current.detail}</p>
													{/if}
												</div>
												<div class="comparison-arrow" aria-hidden="true">→</div>
												<div class="comparison-value-card draft-value-card">
													<p class="comparison-copy">{row.draft.headline}</p>
													{#if row.draft.detail}
														<p class="comparison-subcopy">{row.draft.detail}</p>
													{/if}
												</div>
											</div>
										</article>
									{/each}
								</div>
							</div>
							<div class="comparison-group">
								<div class="comparison-group-head">
									<h3 class="comparison-group-title">Current policy to draft policy</h3>
								</div>
								<div class="comparison-table-head" aria-hidden="true">
									<span>Current</span>
									<span>Draft</span>
								</div>
								<div class="comparison-list compact-list">
									{#each changedPolicyRows as row (row.label)}
										<article class="comparison-row compact-row changed-row">
											<div class="comparison-row-head">
												<p class="comparison-label">{row.label}</p>
												<span class="comparison-status-pill changed-pill">Changed</span>
											</div>
											<div class="comparison-values">
												<div class="comparison-value-card">
													<p class="comparison-copy">{row.current.headline}</p>
													{#if row.current.detail}
														<p class="comparison-subcopy">{row.current.detail}</p>
													{/if}
												</div>
												<div class="comparison-arrow" aria-hidden="true">→</div>
												<div class="comparison-value-card draft-value-card">
													<p class="comparison-copy">{row.draft.headline}</p>
													{#if row.draft.detail}
														<p class="comparison-subcopy">{row.draft.detail}</p>
													{/if}
												</div>
											</div>
										</article>
									{/each}
								</div>
								{#if steadyPolicyRows.length}
									<div class="steady-summary-block">
										<p class="eyebrow-copy">No other policy changes</p>
										<div class="steady-summary-list">
											{#each steadyPolicyRows as row (row.label)}
												<div class="steady-summary-row">
													<p class="steady-summary-label">{row.label}</p>
													<p class="steady-summary-value">{row.value}</p>
												</div>
											{/each}
										</div>
									</div>
								{/if}
							</div>
						</div>
					</div>
				</div>
				{#if reviewMomentPills.length}
					<div class="review-block hotspot-block">
						<p class="eyebrow-copy">Review moments</p>
						<p class="muted-copy">Captured compare clips from the latest sampled draft.</p>
						<div class="pill-row">
							{#each reviewMomentPills as pill (pill.key)}
								<Pill label={pill.label} variant="neutral" />
							{/each}
						</div>
					</div>
				{/if}
				<div class="review-block folder-snapshot-block">
					<p class="eyebrow-copy">Folder state</p>
					<div class="snapshot-grid">
						{#each folderSnapshotItems as item (item.label)}
							<div class="fact-card compact-fact-card">
								<p class="eyebrow-copy">{item.label}</p>
								<p class="fact-value">{item.value}</p>
							</div>
						{/each}
					</div>
				</div>
			</div>
		</Panel>
	</div>

	<Panel>
		<div class="panel-stack">
			<SectionHead
				eyebrow="3. Full host detail"
				heading="Compare every host lane"
				lede="Drop into the full host cards only when you need the complete queue, schedule, or issue breakdown behind the quick picker above."
				size="section"
			/>
			<div class="host-grid">
				{#each rankedHosts as host (host.key)}
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
		flex-wrap: wrap;
	}

	.breadcrumb-row a {
		color: var(--accent-deep);
		font-weight: 700;
	}

	.workflow-stack {
		display: grid;
		gap: var(--space-4);
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

	.pill-row {
		display: flex;
		gap: var(--space-2);
		flex-wrap: wrap;
	}

	.host-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
		gap: var(--space-3);
	}

	.run-layout {
		display: grid;
		grid-template-columns: minmax(240px, 320px) minmax(0, 1fr);
		gap: var(--space-4);
		align-items: start;
	}

	.run-action-column {
		display: grid;
		gap: var(--space-3);
		align-content: start;
	}

	.sample-host-grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(240px, 320px));
		gap: var(--space-3);
		justify-content: start;
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

	.sample-host-card.preferred {
		border-color: rgba(15, 118, 110, 0.22);
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

	.sample-host-topline {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-2);
	}

	.sample-host-label {
		font-size: 1.05rem;
		font-weight: 700;
		line-height: 1.2;
	}

	.sample-host-badge {
		display: inline-flex;
		align-items: center;
		padding: 0.25rem 0.5rem;
		border-radius: 999px;
		background: rgba(15, 118, 110, 0.12);
		color: var(--accent-deep);
		font-size: 0.72rem;
		font-weight: 700;
		letter-spacing: 0.08em;
		text-transform: uppercase;
	}

	.sample-host-meta {
		display: flex;
		gap: 0.45rem;
		flex-wrap: wrap;
		font-size: 0.75rem;
		font-weight: 700;
		color: var(--ink-soft);
		text-transform: uppercase;
		letter-spacing: 0.08em;
	}

	.sample-host-capacity {
		font-size: 0.92rem;
		font-weight: 600;
		color: var(--ink);
	}

	.host-selection-note {
		margin-top: -0.1rem;
	}

	.note-toggle {
		display: inline-flex;
		justify-self: start;
		align-items: center;
		padding: 0;
		border: 0;
		background: transparent;
		color: var(--accent-deep);
		font-size: 0.92rem;
		font-weight: 700;
	}

	.note-row {
		display: flex;
		align-items: center;
	}

	.note-panel {
		padding: 0.9rem 1rem 0;
		border-top: 1px solid rgba(23, 35, 31, 0.08);
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
		min-height: 9rem;
		resize: vertical;
	}

	.action-row {
		display: flex;
		gap: var(--space-2);
		flex-wrap: wrap;
		align-items: start;
	}

	.queue-action-block {
		display: grid;
		gap: 0.45rem;
		max-width: 24rem;
	}

	.inline-gate-copy {
		display: grid;
		gap: 0.15rem;
		font-size: 0.9rem;
		line-height: 1.45;
		color: var(--ink-soft);
	}

	.review-grid {
		display: grid;
		grid-template-columns: minmax(260px, 320px) minmax(0, 1fr);
		gap: var(--space-3);
		align-items: start;
	}

	.review-block {
		display: grid;
		gap: var(--space-2);
		padding: 1rem 1.05rem;
		border-radius: var(--radius-md);
		background: rgba(255, 255, 255, 0.52);
		border: 1px solid rgba(23, 35, 31, 0.08);
	}

	.review-block-title {
		font-size: 1.14rem;
		font-weight: 700;
		line-height: 1.25;
	}

	.representative-block {
		align-content: start;
	}

	.representative-path-shell {
		display: grid;
		gap: 0.75rem;
		padding: 0.95rem 1rem;
		border-radius: calc(var(--radius-md) - 0.1rem);
		background:
			linear-gradient(180deg, rgba(255, 255, 255, 0.78), rgba(255, 255, 255, 0.54)),
			radial-gradient(circle at top right, rgba(15, 118, 110, 0.09), transparent 52%);
		border: 1px solid rgba(15, 118, 110, 0.12);
	}

	.representative-file-name {
		font-size: 1.05rem;
		line-height: 1.32;
		overflow-wrap: anywhere;
	}

	.representative-meta-row {
		display: flex;
		gap: 0.5rem;
		flex-wrap: wrap;
	}

	.representative-support-copy {
		font-size: 0.88rem;
		line-height: 1.45;
	}

	.comparison-stack {
		display: grid;
		gap: var(--space-3);
	}

	.comparison-group {
		display: grid;
		gap: 0.7rem;
	}

	.comparison-group-head {
		display: grid;
	}

	.comparison-group-title {
		font-size: 0.98rem;
		font-weight: 700;
		line-height: 1.35;
	}

	.comparison-list {
		display: grid;
		gap: 0.6rem;
	}

	.comparison-table-head {
		display: grid;
		grid-template-columns: minmax(180px, 220px) minmax(0, 1fr) auto minmax(0, 1fr);
		gap: 0.75rem;
		padding: 0 0.2rem;
		font-size: 0.72rem;
		font-weight: 700;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--ink-soft);
	}

	.comparison-table-head span:first-child {
		grid-column: 2;
	}

	.comparison-table-head span:last-child {
		grid-column: 4;
	}

	.comparison-row {
		display: grid;
		gap: 0.5rem;
		padding: 0.8rem 0.9rem;
		border-radius: calc(var(--radius-md) - 0.12rem);
		background: rgba(255, 255, 255, 0.62);
		border: 1px solid rgba(23, 35, 31, 0.08);
	}

	.compact-row {
		grid-template-columns: minmax(180px, 220px) minmax(0, 1fr) auto minmax(0, 1fr);
		align-items: center;
	}

	.changed-row {
		background: linear-gradient(180deg, rgba(255, 255, 255, 0.86), rgba(255, 255, 255, 0.72));
	}

	.comparison-row-head {
		display: grid;
		gap: 0.4rem;
		align-items: start;
	}

	.comparison-label {
		font-size: 0.95rem;
		font-weight: 700;
		line-height: 1.3;
	}

	.comparison-status-pill {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		justify-self: start;
		padding: 0.22rem 0.58rem;
		border-radius: 999px;
		font-size: 0.72rem;
		font-weight: 700;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		white-space: nowrap;
	}

	.changed-pill {
		background: rgba(15, 118, 110, 0.12);
		color: var(--accent-deep);
	}

	.comparison-values {
		display: grid;
		grid-column: 2 / 5;
		grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
		gap: 0.75rem;
		align-items: stretch;
	}

	.comparison-value-card {
		display: grid;
		gap: 0.35rem;
		padding: 0.75rem 0.8rem;
		border-radius: calc(var(--radius-md) - 0.2rem);
		background: rgba(247, 245, 238, 0.88);
		border: 1px solid rgba(23, 35, 31, 0.08);
	}

	.draft-value-card {
		background: rgba(15, 118, 110, 0.1);
		border-color: rgba(15, 118, 110, 0.14);
	}

	.comparison-copy {
		font-size: 0.98rem;
		font-weight: 600;
		line-height: 1.35;
		word-break: break-word;
	}

	.comparison-subcopy {
		font-size: 0.82rem;
		line-height: 1.45;
		color: var(--ink-soft);
	}

	.comparison-arrow {
		display: grid;
		place-items: center;
		font-size: 1.15rem;
		font-weight: 800;
		color: var(--ink-soft);
	}

	.steady-summary-block {
		display: grid;
		gap: 0.55rem;
		padding-top: 0.2rem;
	}

	.steady-summary-list {
		display: grid;
		gap: 0.45rem;
	}

	.steady-summary-row {
		display: flex;
		justify-content: space-between;
		gap: var(--space-3);
		padding: 0.65rem 0.8rem;
		border-radius: calc(var(--radius-md) - 0.18rem);
		background: rgba(255, 255, 255, 0.48);
		border: 1px solid rgba(23, 35, 31, 0.06);
	}

	.steady-summary-label {
		font-size: 0.88rem;
		font-weight: 600;
		line-height: 1.35;
	}

	.steady-summary-value {
		font-size: 0.84rem;
		line-height: 1.4;
		color: var(--ink-soft);
		text-align: right;
	}

	.hotspot-block,
	.folder-snapshot-block {
		margin-top: 0;
	}

	.snapshot-grid {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: var(--space-3);
	}

	.compact-fact-card {
		background: rgba(255, 255, 255, 0.58);
	}

	@media (max-width: 900px) {
		.folder-header-grid,
		.run-layout,
		.review-grid,
		.snapshot-grid {
			grid-template-columns: 1fr;
		}

		.compact-row,
		.comparison-table-head {
			grid-template-columns: 1fr;
		}

		.comparison-values {
			grid-column: auto;
		}

		.comparison-table-head {
			display: none;
		}

		.comparison-values {
			grid-template-columns: 1fr;
		}

		.comparison-arrow {
			display: none;
		}

		.steady-summary-row {
			grid-template-columns: 1fr;
			display: grid;
		}

		.steady-summary-value {
			text-align: left;
		}
	}

	@media (max-width: 720px) {
		.host-grid,
		.sample-host-grid {
			grid-template-columns: 1fr;
		}
	}
</style>
