import type { FolderPayload, HostRuntime } from '$lib/api/types';
import { titleCase } from '$lib/format';

export type FolderActionHost = { key?: string; label?: string };
export type FolderCalibrationJob = {
	job_id?: string;
	status?: string;
	mode?: string;
	action?: string;
	host?: FolderActionHost;
	notes?: string;
	error?: string | null;
	created_at?: string | null;
	finished_at?: string | null;
};
export type FolderPolicy = {
	video?: {
		encoder?: string;
		pixel_format?: string;
		preset?: number;
		crf_search?: boolean;
		quality_metric?: string;
		target_vmaf?: number;
		target_xpsnr?: number;
		min_target_vmaf?: number;
		min_target_xpsnr?: number;
		target_relax_step_vmaf?: number;
		target_relax_step_xpsnr?: number;
		sample_every?: string;
		sample_duration?: string;
		min_crf?: number;
		max_crf?: number;
		max_encoded_percent?: number;
		max_height?: number;
		downsample_algorithm?: string;
		black_bar_handling?: string;
		black_bar_detect_samples?: number;
		black_bar_detect_seconds?: number;
		black_bar_detect_limit?: number;
		black_bar_detect_round?: number;
		black_bar_detect_start_seconds?: number;
		crop?: string;
		default_grain?: number;
		grain_denoise?: number;
		thorough?: boolean;
	};
	audio?: {
		keep_languages?: string[];
		copy_codecs?: string[];
		convert_to_opus_codecs?: string[];
		surround_5_1_opus_bitrate?: string;
		stereo_opus_bitrate?: string;
		surround_7_1_opus_bitrate?: string;
	};
	subtitle?: {
		keep_languages?: string[];
		prefer_text?: boolean;
		keep_forced?: boolean;
		default_mode?: string;
	};
};
export type FolderItemPlan = {
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
export type FolderQueueSample = { running_count?: number; queued_count?: number };
export type FolderCalibrationQueue = { sample?: FolderQueueSample };
export type EncodeStatusTone = 'live' | 'queued' | 'warning' | 'neutral';
export type HighImpactApprovalGate = {
	requiresConfirmation: boolean;
	armed: boolean;
	buttonLabel: string;
};

export function resolveBenchDraftNote(note: string, noteOverride?: unknown): string {
	return typeof noteOverride === 'string' ? noteOverride.trim() : note.trim();
}

export function buildCalibrationThreadScrollSignature(
	session: CalibrationThreadSession,
	threadCount: number
): string {
	return [
		session.key,
		session.note,
		session.requestResponse ?? '',
		session.requestDisposition ?? '',
		session.summary,
		session.diagnosis ?? '',
		session.feasibilityNote ?? '',
		session.confidence ?? '',
		session.suggestedFollowUp ?? '',
		session.runSummary ?? '',
		session.runNextStep ?? '',
		session.runOutcome ?? '',
		session.runConfidence ?? '',
		String(session.isCurrent),
		String(threadCount)
	].join('\u001f');
}

export type FolderReviewClip = {
	path?: string;
	timestamp_seconds?: number;
	duration_seconds?: number;
	size_bytes?: number;
};
export type FolderReviewPair = {
	timestamp_seconds?: number;
	duration_seconds?: number;
	source_clip?: FolderReviewClip;
	preview_clip?: FolderReviewClip;
	compare_clip?: FolderReviewClip | null;
};
export type FolderCalibrationState = {
	job_id?: string;
	draft_hash?: string;
	accepted_draft_hash?: string;
	compare_clips?: FolderReviewClip[];
	preview_clips?: FolderReviewClip[];
	source_clips?: FolderReviewClip[];
	review_pairs?: FolderReviewPair[];
	sample_result?: {
		chosen_crf?: number;
		quality_metric?: string;
		quality_score?: number;
		predicted_total_size_bytes?: number;
		predicted_encode_percent?: number;
		predicted_encode_seconds?: number;
	};
	browser_review_ready?: boolean;
	review_media_ready?: boolean;
	compare_clips_purged?: boolean;
	preview_clips_purged?: boolean;
	source_clips_purged?: boolean;
	advice?: FolderAdviceState;
};
export type FolderOperatorRequest = {
	request_type?: string;
	metric?: string;
	target?: number;
	budget_label?: string;
	scale_height?: number;
	scale_label?: string;
	feasibility?: string;
	requires_confirmation?: boolean;
	estimated_source_percent?: number;
	estimated_video_bitrate_kbps?: number;
	request_text?: string;
};
export type FolderRunVerdict = {
	summary?: string;
	outcome?: string;
	confidence?: string;
	next_step?: string | null;
};
export type FolderMultimodalReviewArtifact = {
	kind?: string | null;
	label?: string | null;
	detail?: string | null;
	image_url?: string | null;
};
export type VisibleReviewArtifact = {
	kind: string;
	label: string;
	detail: string;
	imageUrl: string;
	category: 'audio' | 'visual';
};
export type FolderMultimodalReviewPack = {
	artifact_count?: number;
	artifacts?: FolderMultimodalReviewArtifact[];
	audio_plan?: {
		action?: string | null;
		summary?: string | null;
		target_bitrate?: string | null;
		primary_track?: {
			codec_name?: string | null;
			channels?: number | null;
			language?: string | null;
		} | null;
	} | null;
};

export function normalizeReviewArtifacts(
	reviewPack: FolderMultimodalReviewPack | null | undefined
): VisibleReviewArtifact[] {
	return ((reviewPack?.artifacts as FolderMultimodalReviewArtifact[] | undefined) ?? [])
		.map((artifact) => {
			const kind = String(artifact.kind ?? '').trim();
			const category: VisibleReviewArtifact['category'] = /audio|spectrogram/i.test(kind)
				? 'audio'
				: 'visual';
			return {
				kind,
				label: String(artifact.label ?? '').trim(),
				detail: String(artifact.detail ?? '').trim(),
				imageUrl: String(artifact.image_url ?? '').trim(),
				category
			};
		})
		.filter((artifact) => artifact.label || artifact.detail || artifact.imageUrl);
}

export type FolderAdviceState = {
	summary?: string;
	diagnosis?: string | null;
	confidence?: string | null;
	suggested_follow_up?: string | null;
	request_disposition?: string | null;
	request_response?: string | null;
	feasibility_note?: string | null;
	operator_note?: string | null;
	operator_request?: FolderOperatorRequest;
	run_verdict?: FolderRunVerdict;
	multimodal_review_pack?: FolderMultimodalReviewPack | null;
	operator_approved_at?: string | null;
};
export type ProposalSelfCheck = {
	status?: string;
	summary?: string;
	issues?: string[];
};
export type PendingSampleProposal = {
	proposal_id?: string;
	status?: string;
	kind?: string;
	action?: string;
	created_at?: string | null;
	can_queue?: boolean;
	message?: string;
	operator_note?: string | null;
	operator_request?: FolderOperatorRequest | null;
	request_disposition?: string | null;
	request_response?: string | null;
	feasibility_note?: string | null;
	summary?: string;
	diagnosis?: string | null;
	confidence?: string | null;
	suggested_follow_up?: string | null;
	applied_policy?: FolderPolicy;
	preview_policy?: FolderPolicy;
	current_policy?: FolderPolicy;
	host?: { key?: string; label?: string };
	self_check?: ProposalSelfCheck;
	operator_signal?: string | null;
	evidence_checked?: string[];
	multimodal_review_pack?: FolderMultimodalReviewPack | null;
	trace?: ProposalTrace | null;
};
export type ProposalTrace = {
	prompt_version?: string | null;
	raw_response?: string | null;
	proposed_policy?: FolderPolicy | null;
	context?: {
		goal?: string;
		current_policy?: FolderPolicy | null;
		sample_item?: FolderSampleItem | null;
		runtime_toolbelt?: Record<string, unknown> | null;
		retrieved_memory?: Array<Record<string, unknown>> | null;
		recent_calibration?: Record<string, unknown> | null;
		folder_summary?: Record<string, unknown> | null;
		metric_support?: Record<string, unknown> | null;
		requested_experiment?: FolderOperatorRequest | null;
		multimodal_review_pack?: FolderMultimodalReviewPack | null;
	} | null;
};
export type TuningSessionSummary = {
	session_id?: string;
	note?: string;
	summary?: string;
	diagnosis?: string | null;
	confidence?: string | null;
	suggested_follow_up?: string | null;
	request_disposition?: string | null;
	request_response?: string | null;
	feasibility_note?: string | null;
	created_at?: string | null;
};
export type CalibrationThreadSession = {
	key: string;
	note: string;
	requestResponse?: string | null;
	requestDisposition?: string | null;
	summary: string;
	diagnosis?: string | null;
	feasibilityNote?: string | null;
	confidence?: string | null;
	suggestedFollowUp?: string | null;
	runSummary?: string | null;
	runNextStep?: string | null;
	runOutcome?: string;
	runConfidence?: string | null;
	isCurrent: boolean;
};
export type SampleHostOption = {
	key?: string;
	label?: string;
	detail?: string;
	available?: boolean;
};
export type ReviewGate = {
	can_confirm_full?: boolean;
	message?: string;
	status?: string;
	accepted_at?: string;
	next_action_label?: string;
};
export type ApprovedSeasonShortcut = {
	root_prefix?: string;
	root_label?: string;
	count?: number;
	season_labels?: string[];
	season_prefixes?: string[];
	suggested_note?: string;
};
export type BreadcrumbHref = '/' | `/folders/${string}`;
export type BreadcrumbItem = { label: string; href: BreadcrumbHref | null };
export type SampleAudioTrack = {
	codec_name?: string;
	channels?: number;
	language?: string;
	default?: number | boolean;
	bit_rate?: number | string;
};
export type SampleSubtitleTrack = {
	codec_name?: string;
	language?: string;
	default?: number | boolean;
	forced?: number | boolean;
};
export type FolderSampleItem = {
	rel_path?: string;
	source_size_bytes?: number;
	video_codec?: string;
	video_bitrate?: number;
	width?: number;
	height?: number;
	container?: string;
	duration_seconds?: number;
	audio_summary?: SampleAudioTrack[];
	subtitle_summary?: SampleSubtitleTrack[];
	resolved_policy?: FolderPolicy;
};
export type SnapshotItem = { label: string; value: string; detail?: string };
export type ComparisonValue = {
	headline: string;
	detail?: string;
};
export type ComparisonRow = {
	label: string;
	current: ComparisonValue;
	draft: ComparisonValue;
	changed: boolean;
};
export type PolicyWorkbenchRow = {
	path: string;
	section: string;
	label: string;
	current: string;
	draft: string;
	changed: boolean;
};
export type PolicyWorkbenchSection = { title: string; rows: PolicyWorkbenchRow[] };
export type WorkbenchStat = { label: string; value: string; detail?: string };
export type SteadyComparisonRow = { label: string; value: string };
export type SampleHostCard = {
	key: string;
	label: string;
	detail: string;
	available: boolean;
	runtime: HostRuntime | null;
	searchSummary?: {
		label: string;
		detail: string;
	} | null;
	preferred: boolean;
};

export function formatStatusCountCopy(mapping: Record<string, number> | null | undefined): string {
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

export function codecLabel(codec: string | null | undefined): string {
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

export function channelLabel(channels: number | null | undefined): string | null {
	const value = Number(channels ?? 0);
	if (!Number.isFinite(value) || value <= 0) return null;
	if (value === 1) return 'mono';
	if (value === 2) return 'stereo';
	if (value === 6) return '5.1';
	if (value === 8) return '7.1';
	return `${value}ch`;
}

export function formatBitrateCopy(value: number | string | null | undefined): string | null {
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

export function formatPercentCopy(value: number | string | null | undefined): string {
	if (value == null || value === '') return 'n/a';
	if (typeof value === 'number') {
		if (!Number.isFinite(value)) return 'n/a';
		return `${value.toLocaleString('en-US', {
			maximumFractionDigits: value >= 100 ? 0 : 1,
			minimumFractionDigits: value < 100 && Math.abs(value % 1) > 0.04 ? 1 : 0
		})}%`;
	}
	const trimmed = String(value).trim();
	if (!trimmed || trimmed.toLowerCase() === 'n/a') return 'n/a';
	if (/^-?\d+(?:\.\d+)?$/.test(trimmed)) {
		const numeric = Number(trimmed);
		if (!Number.isFinite(numeric)) return 'n/a';
		return `${numeric.toLocaleString('en-US', {
			maximumFractionDigits: numeric >= 100 ? 0 : 1,
			minimumFractionDigits: numeric < 100 && Math.abs(numeric % 1) > 0.04 ? 1 : 0
		})}%`;
	}
	return `${trimmed}%`;
}

export function formatResolutionCopy(
	width: number | string | null | undefined,
	height: number | string | null | undefined
): string | null {
	const parsedWidth = Number(width ?? 0);
	const parsedHeight = Number(height ?? 0);
	if (!Number.isFinite(parsedWidth) || !Number.isFinite(parsedHeight)) return null;
	if (parsedWidth <= 0 || parsedHeight <= 0) return null;
	return `${parsedWidth.toLocaleString('en-US')}x${parsedHeight.toLocaleString('en-US')}`;
}

export function inferResolutionFromPath(value: string | null | undefined): string | null {
	const match = String(value ?? '').match(
		/(?:^|[.\s_-])(4320p|2160p|1440p|1080p|720p|576p|480p)(?:$|[.\s_-])/i
	);
	return match ? match[1].toLowerCase() : null;
}

export function formatLanguageCopy(value: string | null | undefined): string | null {
	const trimmed = String(value ?? '').trim();
	if (!trimmed || trimmed === 'und') return null;
	return trimmed.toUpperCase();
}

export function formatBooleanCopy(value: boolean | null | undefined): string {
	if (value == null) return 'n/a';
	return value ? 'On' : 'Off';
}

export function compactCopy(parts: Array<string | null | undefined>): string {
	return parts.filter(Boolean).join(' · ');
}

export function comparisonValue(headline: string, detail?: string | null): ComparisonValue {
	return detail ? { headline, detail } : { headline };
}

export function summarizeAudioTrack(track: SampleAudioTrack | null): ComparisonValue {
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

export function summarizeAudioPlan(plan: FolderItemPlan['audio'] | undefined): ComparisonValue {
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

export function summarizeSubtitleSource(tracks: SampleSubtitleTrack[]): ComparisonValue {
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

export function summarizeSubtitlePlan(
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

export function resolveMetricLabel(
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

export function summarizeMetricPolicy(
	policyValue: FolderPolicy | undefined,
	metricSupport: FolderPayload['metric_support']
): ComparisonValue {
	const video = policyValue?.video ?? {};
	const resolved = resolveMetricLabel(video.quality_metric, metricSupport);
	const target = resolved === 'VMAF' ? video.target_vmaf : video.target_xpsnr;
	const floor = resolved === 'VMAF' ? video.min_target_vmaf : video.min_target_xpsnr;
	return comparisonValue(
		resolved,
		compactCopy([
			target != null ? `target ${target}` : null,
			floor != null ? `floor ${floor}` : null
		])
	);
}

export function summarizeMetricPlan(plan: FolderItemPlan['video'] | undefined): ComparisonValue {
	if (!plan) return comparisonValue('No draft video metric yet');
	return comparisonValue(
		String(plan.quality_metric ?? '').toUpperCase() || 'n/a',
		compactCopy([
			plan.target != null ? `target ${plan.target}` : null,
			plan.min_target != null ? `floor ${plan.min_target}` : null
		])
	);
}

function summarizeBlackBarHandling(value: string | null | undefined): string | null {
	const handling = String(value ?? '')
		.trim()
		.toLowerCase();
	if (!handling || handling === 'off') return null;
	if (handling === 'smart') return 'smart black-bar detect';
	if (handling === 'auto') return 'auto black-bar detect';
	return `${handling} black-bar mode`;
}

export function summarizeVideoTransformPolicy(
	policyValue: FolderPolicy | null | undefined
): ComparisonValue {
	const video = policyValue?.video ?? {};
	const maxHeight = Number(video.max_height ?? 0);
	const crop = String(video.crop ?? '').trim();
	const headlineParts = [
		crop ? 'manual crop' : summarizeBlackBarHandling(video.black_bar_handling),
		Number.isFinite(maxHeight) && maxHeight > 0 ? `max ${maxHeight}p` : null
	].filter(Boolean) as string[];
	const detail = compactCopy([
		Number.isFinite(maxHeight) && maxHeight > 0 && video.downsample_algorithm
			? String(video.downsample_algorithm)
			: null,
		crop || null
	]);
	return comparisonValue(
		headlineParts.length ? headlineParts.join(' + ') : 'No crop or scale',
		detail
	);
}

export function compareValues(
	current: ComparisonValue | string,
	draft: ComparisonValue | string
): boolean {
	const left =
		typeof current === 'string' ? current : `${current.headline} ${current.detail ?? ''}`;
	const right = typeof draft === 'string' ? draft : `${draft.headline} ${draft.detail ?? ''}`;
	return left.trim() !== right.trim();
}

export function flattenPolicy(
	policyValue: FolderPolicy | null | undefined
): Record<string, unknown> {
	const flattened: Record<string, unknown> = {};
	if (!policyValue) return flattened;
	for (const section of ['video', 'audio', 'subtitle'] as const) {
		const rawSection = policyValue[section];
		if (!rawSection) continue;
		for (const [key, value] of Object.entries(rawSection)) {
			flattened[`${section}.${key}`] = value;
		}
	}
	return flattened;
}

export function policyRowLabel(path: string): string {
	const labels: Record<string, string> = {
		'video.encoder': 'Video encoder',
		'video.pixel_format': 'Pixel format',
		'video.preset': 'Preset',
		'video.crf_search': 'CRF search',
		'video.quality_metric': 'Quality metric',
		'video.target_vmaf': 'Target VMAF',
		'video.min_target_vmaf': 'VMAF floor',
		'video.target_xpsnr': 'Target XPSNR',
		'video.min_target_xpsnr': 'XPSNR floor',
		'video.target_relax_step_vmaf': 'VMAF relax step',
		'video.target_relax_step_xpsnr': 'XPSNR relax step',
		'video.sample_every': 'Sample cadence',
		'video.sample_duration': 'Sample duration',
		'video.min_crf': 'Minimum CRF',
		'video.max_crf': 'Maximum CRF',
		'video.max_encoded_percent': 'Size ceiling',
		'video.max_height': 'Output height cap',
		'video.downsample_algorithm': 'Downsample filter',
		'video.black_bar_handling': 'Black-bar handling',
		'video.black_bar_detect_samples': 'Black-bar samples',
		'video.black_bar_detect_seconds': 'Black-bar window',
		'video.black_bar_detect_limit': 'Black-bar threshold',
		'video.black_bar_detect_round': 'Black-bar rounding',
		'video.black_bar_detect_start_seconds': 'Black-bar start',
		'video.crop': 'Manual crop',
		'video.default_grain': 'Film grain',
		'video.grain_denoise': 'Film-grain denoise',
		'video.thorough': 'Thorough search',
		'audio.keep_languages': 'Audio languages',
		'audio.copy_codecs': 'Audio codecs to copy',
		'audio.convert_to_opus_codecs': 'Audio codecs to convert',
		'audio.stereo_opus_bitrate': '2.0 Opus budget',
		'audio.surround_5_1_opus_bitrate': '5.1 Opus budget',
		'audio.surround_7_1_opus_bitrate': '7.1 Opus budget',
		'subtitle.keep_languages': 'Subtitle languages',
		'subtitle.prefer_text': 'Prefer text subtitles',
		'subtitle.keep_forced': 'Keep forced subtitles',
		'subtitle.default_mode': 'Subtitle default mode'
	};
	if (labels[path]) return labels[path];
	return titleCase(path.split('.').at(-1)?.replace(/_/g, ' ') ?? path);
}

export function formatPolicyValue(
	path: string,
	value: unknown,
	metricSupport: FolderPayload['metric_support']
): string {
	if (value == null) return 'n/a';
	if (Array.isArray(value)) {
		const items = value
			.map((item) => String(item ?? '').trim())
			.filter(Boolean)
			.map((item) => (item.length <= 4 ? item.toUpperCase() : item));
		return items.length ? items.join(', ') : 'none';
	}
	if (typeof value === 'boolean') return formatBooleanCopy(value);
	if (path.includes('bitrate')) return formatBitrateCopy(value as number | string) ?? 'n/a';
	if (path === 'video.max_encoded_percent') return formatPercentCopy(value as number | string);
	if (path === 'video.max_height') {
		const height = Number(value);
		return Number.isFinite(height) && height > 0 ? `max ${height}p` : 'off';
	}
	if (path === 'video.black_bar_handling') {
		return summarizeBlackBarHandling(String(value)) ?? 'off';
	}
	if (path === 'video.crop') return String(value).trim() || 'off';
	if (
		path === 'video.black_bar_detect_seconds' ||
		path === 'video.black_bar_detect_start_seconds'
	) {
		return `${value}s`;
	}
	if (typeof value === 'number') {
		return Number.isInteger(value)
			? value.toLocaleString('en-US')
			: value.toLocaleString('en-US', { maximumFractionDigits: 2 });
	}
	if (path === 'video.quality_metric') return resolveMetricLabel(String(value), metricSupport);
	return String(value);
}

export function workbenchSection(path: string): string {
	if (path.startsWith('video.')) return 'Video';
	if (path.startsWith('audio.')) return 'Audio';
	if (path.startsWith('subtitle.')) return 'Subtitles';
	return 'Other';
}

export function pathFilename(value: string | null | undefined): string {
	const trimmed = String(value ?? '').trim();
	if (!trimmed) return 'No representative file yet';
	return trimmed.split('/').at(-1) ?? trimmed;
}

export function pathStem(value: string | null | undefined): string {
	const filename = pathFilename(value);
	const lastDot = filename.lastIndexOf('.');
	if (lastDot <= 0) return filename;
	return filename.slice(0, lastDot);
}

export function pathExtension(value: string | null | undefined): string | null {
	const filename = pathFilename(value);
	const lastDot = filename.lastIndexOf('.');
	if (lastDot <= 0 || lastDot === filename.length - 1) return null;
	return filename.slice(lastDot + 1).toUpperCase();
}

export function softWrapTokens(value: string): string[] {
	return value.split(/([._\-[\]()\s]+)/).filter(Boolean);
}

export function formatCodecCountKey(key: string): string {
	const [codec, channelCount] = key.split(':');
	const channelCopy = channelCount ? channelLabel(Number(channelCount)) : null;
	return [codecLabel(codec), channelCopy].filter(Boolean).join(' ');
}

export function formatCodecCountsCopy(mapping: Record<string, number> | null | undefined): string {
	if (!mapping) return 'None';
	const entries = Object.entries(mapping);
	if (entries.length === 0) return 'None';
	return entries.map(([key, value]) => `${value} ${formatCodecCountKey(key)}`).join(' · ');
}

export function compactScheduleCopy(runtime: HostRuntime | null): string | null {
	if (!runtime) return null;
	if (!runtime.schedule_profile_label || runtime.schedule_profile_label === 'Always') {
		return 'Always';
	}
	const detail = runtime.schedule_detail.trim();
	const rangeMatch = detail.match(/(\d{2}:\d{2}).*?(\d{2}:\d{2})(.*)$/i);
	if (rangeMatch) {
		const suffix = String(rangeMatch[3] ?? '').trim();
		if (!suffix) {
			return `Window ${rangeMatch[1]}-${rangeMatch[2]}`;
		}
		if (/^in\s+host local time$/i.test(suffix)) {
			return `Window ${rangeMatch[1]}-${rangeMatch[2]} local`;
		}
		return `Window ${rangeMatch[1]}-${rangeMatch[2]} ${suffix}`;
	}
	return detail.replace(/^window\s+/i, '');
}

export function hostCapacityCopy(runtime: HostRuntime | null): string | null {
	if (!runtime) return null;
	const laneLabel = `lane${runtime.max_parallel_encodes === 1 ? '' : 's'}`;
	if (runtime.active_encode_count > 0) {
		return `${runtime.active_encode_count}/${runtime.max_parallel_encodes} ${laneLabel} running`;
	}
	if (runtime.queue_active) {
		return `${runtime.max_parallel_encodes} ${laneLabel} free`;
	}
	if (runtime.schedule_open === false) {
		return `${runtime.max_parallel_encodes} ${laneLabel} scheduled`;
	}
	if (runtime.active_reason === 'parallel encode slots are full') {
		return `${runtime.max_parallel_encodes} ${laneLabel} busy`;
	}
	return runtime.message;
}

export function queueSummaryCopy(runningCount: number, queuedCount: number, label: string): string {
	if (runningCount === 0 && queuedCount === 0) {
		return `${label}: idle`;
	}
	return `${label}: ${runningCount} running · ${queuedCount} queued`;
}

export function encodeQueueSummaryCopy(summary: string | undefined): string {
	const trimmed = String(summary ?? '').trim();
	if (!trimmed || trimmed.startsWith('0 running · 0 queued')) {
		return 'Encode queue: idle';
	}
	if (/waiting for a host schedule window/i.test(trimmed)) {
		return `Encode queue: ${trimmed.replace(/\s*·\s*waiting for a host schedule window/i, ' · next worker window')}`;
	}
	return `Encode queue: ${trimmed}`;
}

export function encodeStatusTone(status: string): EncodeStatusTone {
	if (status === 'running') return 'live';
	if (status === 'queued' || status === 'retry_backoff') return 'queued';
	if (status === 'needs_attention' || status === 'failed' || status === 'stopped') return 'warning';
	return 'neutral';
}

export function formatDateTimeCopy(value: string | null | undefined): string {
	const trimmed = String(value ?? '').trim();
	if (!trimmed) return '';
	const parsed = new Date(trimmed);
	if (Number.isNaN(parsed.getTime())) return '';
	return parsed.toLocaleString('en-US', {
		month: 'short',
		day: 'numeric',
		hour: 'numeric',
		minute: '2-digit'
	});
}

export function describeHighImpactApprovalGate({
	reviewGateStatus,
	highImpactPolicyCount,
	armed
}: {
	reviewGateStatus: string | null | undefined;
	highImpactPolicyCount: number;
	armed: boolean;
}): HighImpactApprovalGate {
	if (reviewGateStatus === 'accepted') {
		return {
			requiresConfirmation: false,
			armed: false,
			buttonLabel: 'Draft already approved'
		};
	}
	const requiresConfirmation = highImpactPolicyCount > 0;
	const confirmationArmed = requiresConfirmation && armed;
	return {
		requiresConfirmation,
		armed: confirmationArmed,
		buttonLabel: confirmationArmed ? 'Confirm High-Impact Approval' : 'Approve Draft + Queue Folder'
	};
}

export function approvalReviewSignature(rows: ComparisonRow[]): string {
	return rows
		.map(
			(row) =>
				`${row.label}:${row.current.headline}:${row.current.detail ?? ''}:${row.draft.headline}:${row.draft.detail ?? ''}:${row.changed ? 'changed' : 'steady'}`
		)
		.join('|');
}
