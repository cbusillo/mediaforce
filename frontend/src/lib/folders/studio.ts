import type { FolderPayload } from '$lib/api/types';
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
	started_at?: string | null;
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
		target_size_mb?: number;
		target_runtime_minutes?: number;
		decision_model?: string;
		quality_engine?: string;
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
export type EncodeStatusTone = 'live' | 'queued' | 'warning' | 'blocked' | 'neutral';
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

export function calibrationFailureHeading(
	mode: string | null | undefined,
	status: string | null | undefined
) {
	const stopped = String(status ?? '').trim() === 'stopped';
	if (String(mode ?? '').trim() === 'full') {
		return stopped
			? 'Representative-file proof stopped before review clips were ready'
			: 'Representative-file proof failed before review clips were ready';
	}
	return stopped
		? 'Sample run stopped before review clips were ready'
		: 'Sample run failed before review clips were ready';
}

export function calibrationFailureLede(
	mode: string | null | undefined,
	status: string | null | undefined
) {
	const fullMode = String(mode ?? '').trim() === 'full';
	if (String(status ?? '').trim() === 'stopped') {
		if (fullMode) {
			return 'The previous proof encode ended before review clips were produced. If you stopped it intentionally, rerun the proof when ready; otherwise check the host log.';
		}
		return 'The previous run ended before review clips were produced. If you stopped it intentionally, retry when ready; otherwise check the host log.';
	}
	if (fullMode) {
		return 'The previous proof encode ended before review clips were produced. Check the detail below, then rerun the proof.';
	}
	return 'The previous run ended before review clips were produced. Check the detail below, then retry the sample.';
}

export function summarizeCalibrationFailureDetail(
	error: string | null | undefined,
	hostLabel: string | null | undefined,
	status: string | null | undefined
): string {
	const host = String(hostLabel ?? '').trim();
	const rawError = String(error ?? '').trim();
	const detail = rawError
		? summarizeCalibrationError(rawError, status)
		: 'No failure detail was recorded for this run.';
	return host ? `Host: ${host} · ${detail}` : detail;
}

function summarizeCalibrationError(rawError: string, status: string | null | undefined): string {
	const lines = rawError
		.split('\n')
		.map((line) => line.trim())
		.filter((line) => line.length > 0 && !line.startsWith('export '));
	const diagnosticLines = lines.filter((line) => !isCleanupWarningLine(line));
	const primaryLines = diagnosticLines.length > 0 ? diagnosticLines : lines;
	const actionableLine = primaryLines.find(isActionableFailureLine);
	if (actionableLine) {
		const crfSearchSummary = summarizeFailedCrfSearch(primaryLines);
		return crfSearchSummary ?? actionableLine;
	}
	const progressLine = primaryLines.find(parseAbAv1SampleProgress);
	if (progressLine) {
		const progress = parseAbAv1SampleProgress(progressLine);
		const stopped = String(status ?? '').trim() === 'stopped';
		const prefix = stopped
			? 'Stopped while ab-av1 was encoding'
			: 'ab-av1 was still encoding when the run ended';
		return `${prefix} ${progress}. No specific error line was recorded.`;
	}
	return primaryLines[0] ?? rawError;
}

function isCleanupWarningLine(line: string): boolean {
	return /^cleanup warning:/i.test(line);
}

function summarizeFailedCrfSearch(lines: string[]): string | null {
	if (!lines.some((line) => /failed to find a suitable crf/i.test(line))) return null;
	const probes = lines.map(parseCrfSearchProbe).filter((probe): probe is string => Boolean(probe));
	const uniqueProbes = Array.from(new Set(probes));
	if (uniqueProbes.length === 0) {
		return 'No CRF satisfied both the quality target and size limit. Try relaxing the target, raising the size cap, or adjusting the sample note.';
	}
	return `No CRF satisfied both the quality target and size limit. Search tried ${uniqueProbes.join(
		' and '
	)}.`;
}

function parseCrfSearchProbe(line: string): string | null {
	const match = line.match(
		/\bcrf\s+([\d.]+)\s+([A-Z0-9]+)\s+([\d.]+)(?:\s+predicted\b.*?)?\s+\((\d+(?:\.\d+)?)%\)/i
	);
	if (!match) return null;
	return `CRF ${match[1]} at ${match[2].toUpperCase()} ${match[3]} / ${match[4]}%`;
}

function isActionableFailureLine(line: string): boolean {
	return /\b(error|failed|failure|denied|timed out|timeout|no such|not found|unable|invalid|panic|killed|exited|permission)\b/i.test(
		line
	);
}

function parseAbAv1SampleProgress(line: string): string | null {
	const match = line.match(/encoding sample\s+(\d+)\/(\d+)\s+crf\s+([\d.]+)/i);
	if (!match) return null;
	return `sample ${match[1]}/${match[2]} at CRF ${match[3]}`;
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
		quality_target?: number;
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
	budget_bytes?: number;
	budget_label?: string;
	scale_height?: number;
	scale_label?: string;
	black_bar_handling?: string;
	crop?: string;
	feasibility?: string;
	requires_confirmation?: boolean;
	estimated_source_percent?: number;
	estimated_video_bitrate_kbps?: number;
	target_video_bitrate_kbps?: number;
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
export type BudgetEnforcement = {
	status?: string;
	size_target_analysis?: Record<string, unknown>;
	applied_policy?: FolderPolicy;
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
	latest_failed_sample_job?: Record<string, unknown> | null;
	self_check?: ProposalSelfCheck;
	budget_enforcement?: BudgetEnforcement | null;
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
		latest_failed_sample_job?: Record<string, unknown> | null;
	} | null;
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
export type ReviewGate = {
	can_confirm_full?: boolean;
	message?: string;
	status?: string;
	accepted_at?: string;
	next_action_label?: string;
};
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

export function encodeStatusTone(status: string): EncodeStatusTone {
	if (status === 'running') return 'live';
	if (status === 'queued' || status === 'retry_backoff') return 'queued';
	if (status === 'needs_attention' || status === 'failed' || status === 'stopped') return 'blocked';
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
			buttonLabel: 'Approved'
		};
	}
	const requiresConfirmation = highImpactPolicyCount > 0;
	const confirmationArmed = requiresConfirmation && armed;
	return {
		requiresConfirmation,
		armed: confirmationArmed,
		buttonLabel: 'Approve'
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
