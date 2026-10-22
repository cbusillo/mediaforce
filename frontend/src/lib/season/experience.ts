import { resolveMetricLabel } from '$lib/folders/studio';
import type {
	CalibrationJobPayload,
	CalibrationScopeActivityPayload,
	CalibrationTargetContract,
	DashboardScanJob,
	EncodeQueueJob,
	EncodeUnfinishedGroup,
	FolderPayload,
	FolderStatusPayload,
	MotionHeldFile,
	MotionHoldReason,
	MotionKeptFile,
	CompressionIntentOptionPayload,
	CompressionIntentRequestPayload,
	OperatorIntentRequestPayload,
	PageStatePayload,
	QualityRiskPayload,
	QualityRiskTag,
	SizeGoalMode,
	StagedIntegrityDisposition,
	StagedIntegrityRecord,
	TargetSizeProvenance
} from '$lib/api/types';
import { folderRoutePath } from '$lib/folder-display';
import { formatFileSize as formatOperatorFileSize, leftOutItemsCopy } from '$lib/format';
import type { HumanSeasonTone } from './page-state';

export interface SeasonPromotionIntegrityBlocker {
	code: string;
	count: number;
	label: string;
	nextAction: string;
}

export interface SeasonPromotionIntegrity {
	available: boolean;
	canFinish: boolean;
	error: string;
	reportComplete: boolean;
	readyCount: number;
	alreadyPlacedCount: number;
	unresolvedCount: number;
	totalCount: number;
	blockers: SeasonPromotionIntegrityBlocker[];
	records: StagedIntegrityRecord[];
}

const INTEGRITY_COPY: Record<StagedIntegrityDisposition, { label: string; nextAction: string }> = {
	promotable: {
		label: 'Ready to replace',
		nextAction: 'Ready to replace now.'
	},
	tracked: {
		label: 'Already in the library',
		nextAction: 'No action is needed for this file.'
	},
	unvalidated: {
		label: 'Needs a check',
		nextAction: 'Check this file. It can be replaced once it passes.'
	},
	validation_failed: {
		label: 'Check failed',
		nextAction: 'Inspect the failed file and compress a clean replacement.'
	},
	size_held: {
		label: 'Much smaller than expected',
		nextAction: 'Keep it, or make it again.'
	},
	missing: {
		label: 'Compressed file missing',
		nextAction: 'Compress this episode again.'
	},
	drifted: {
		label: 'Changed after checking',
		nextAction: 'Check the file again or compress a fresh replacement.'
	},
	orphaned: {
		label: 'Untracked working file',
		nextAction: 'Inspect the untracked file; Mediaforce will not adopt it automatically.'
	},
	partial_or_temporary: {
		label: 'Incomplete compressed file',
		nextAction: 'Wait for active work to finish or remove the abandoned temporary file.'
	},
	remote_only_or_unreachable: {
		label: 'File unavailable on the computer',
		nextAction: 'Restore access to the computer that has this file.'
	},
	not_started: {
		label: 'Not compressed yet',
		nextAction: 'It will be compressed later. Nothing else waits for it.'
	},
	retained: {
		label: 'Held aside for review',
		nextAction: 'Kept from an earlier incident. It does not affect replacing the originals.'
	}
};

const PROMOTION_BLOCKER_COPY: Record<string, { label: string; nextAction: string }> = {
	season_active_encode_job: {
		label: 'Compression still active',
		nextAction: 'Wait for the current compression job to finish.'
	},
	season_encode_job_attention: {
		label: 'Compression needs attention',
		nextAction: 'Resolve or retry the interrupted compression work.'
	},
	season_destination_conflict: {
		label: 'Library file conflict',
		nextAction: 'Resolve the existing destination file before replacing the season.'
	},
	season_policy_approval_missing: {
		label: 'Approved settings missing',
		nextAction: 'Review and approve one set of settings for this season.'
	},
	season_policy_provenance_missing: {
		label: 'Settings history missing',
		nextAction: 'Make the affected episodes again so their exact settings are recorded.'
	},
	season_policy_not_approved: {
		label: 'Output does not match approval',
		nextAction: 'Approve matching settings or make the affected episodes again.'
	},
	season_active_encode_unreadable: {
		label: 'Compression progress unknown',
		nextAction: 'Wait for the current compression job to finish, then check again.'
	},
	season_policy_gate_unavailable: {
		label: 'Approval check unavailable',
		nextAction: 'Restore the approved-settings check before finishing this season.'
	}
};

export function stagedIntegrityDispositionCopy(disposition: StagedIntegrityDisposition): {
	label: string;
	nextAction: string;
} {
	return (
		INTEGRITY_COPY[disposition] ?? {
			label: 'Needs attention',
			nextAction: 'Inspect this staged file before finishing the season.'
		}
	);
}

/** A held copy, or one whose only failed check is its size goal, can be kept as it is. */
export function canKeepStagedCopy(record: StagedIntegrityRecord): boolean {
	return (
		record.disposition === 'size_held' ||
		(record.remake?.reason === 'final_size' && !record.remake.pending)
	);
}

/** Unpublished files with a supported per-file keep or remake action. */
export function sizeHeldRecords(integrity: SeasonPromotionIntegrity): StagedIntegrityRecord[] {
	return integrity.records.filter(
		(record) =>
			record.item_id !== null &&
			(Boolean(record.remake) ||
				(record.disposition === 'size_held' && Boolean(record.size_prediction)))
	);
}

export function seasonPromotionIntegrity(status: FolderStatusPayload): SeasonPromotionIntegrity {
	const integrity = status.staged_integrity;
	if (!integrity) {
		return {
			available: false,
			canFinish: false,
			error: '',
			reportComplete: false,
			readyCount: 0,
			alreadyPlacedCount: 0,
			unresolvedCount: 0,
			totalCount: 0,
			blockers: [],
			records: []
		};
	}
	const records = integrity.records ?? [];
	const reportComplete = Boolean(
		integrity.discovery.requested &&
		!integrity.database_truncated &&
		!integrity.discovery.truncated &&
		integrity.records !== undefined &&
		integrity.next_offset == null
	);
	// Publishing is decided file by file; the server's count already leaves out files that must wait.
	const readyCount =
		integrity.promotion_readiness?.promotable_count ?? integrity.counts.promotable ?? 0;
	const alreadyPlacedCount = integrity.counts.tracked ?? 0;
	const exactItemScope = integrity.scope?.match === 'exact_item';
	const totalCount = Object.values(integrity.counts).reduce(
		(total, count) => total + (count ?? 0),
		0
	);
	const blockers = integrity.blockers.map((blocker) => {
		const disposition = blocker.code.replace('staged_integrity_', '') as StagedIntegrityDisposition;
		const copy = INTEGRITY_COPY[disposition];
		return {
			code: blocker.code,
			count: blocker.count,
			label: copy?.label ?? 'Needs attention',
			nextAction: copy?.nextAction ?? 'Inspect the season inventory before finishing.'
		};
	});
	for (const blocker of [
		...(integrity.promotion_readiness?.blockers ?? []),
		...(integrity.promotion_readiness?.waiting ?? [])
	]) {
		if (blocker.code.startsWith('season_staged_integrity_')) continue;
		const copy = PROMOTION_BLOCKER_COPY[blocker.code];
		blockers.push({
			code: blocker.code,
			count: blocker.count,
			label: copy?.label ?? 'Finish needs attention',
			nextAction: copy?.nextAction ?? 'Inspect the season before finishing.'
		});
	}
	if (integrity.database_truncated || integrity.discovery.truncated) {
		blockers.push({
			code: 'staged_integrity_incomplete_report',
			count: 1,
			label: 'Inventory incomplete',
			nextAction: 'Check the full file list before finishing this season.'
		});
	}

	return {
		available: integrity.discovery.requested,
		canFinish:
			reportComplete &&
			readyCount > 0 &&
			(exactItemScope || Boolean(integrity.promotion_readiness?.applicable)) &&
			Boolean(integrity.promotion_readiness?.can_promote),
		error: integrity.load_error ?? '',
		reportComplete,
		readyCount,
		alreadyPlacedCount,
		unresolvedCount: blockers.reduce((total, blocker) => total + blocker.count, 0),
		totalCount,
		blockers,
		records
	};
}

export interface ApprovalGuard {
	kind: 'high_impact' | 'size_tradeoff';
	title: string;
	detail: string;
	confirmHighImpact: boolean;
	confirmSizeTradeoff: boolean;
}

export interface SeasonIdentity {
	library: string;
	show: string;
	season: string;
	showPrefix: string;
}

export interface CatalogWarningNotice {
	title: string;
	detail: string;
}

export interface SizeGoal {
	key: string;
	title: string;
	megabytesPerEpisode: number;
	targetSizeBytes: number;
	detail: string;
	mode: SizeGoalMode;
	operatorIntent: OperatorIntentRequestPayload;
	requiresExplicitSelection: boolean;
}

export interface CompressionIntentContract {
	sizeLabel: string;
	sizeRule: string;
	searchLabel: string;
	searchRule: string;
	qualityLabel: string;
	qualityRule: string;
	finalHeadline: string;
	finalRule: string;
	announcement: string;
}

export type ReviewSizeAdjustmentDirection = 'smaller' | 'higher_quality';

export interface ReviewSizeAdjustment {
	direction: ReviewSizeAdjustmentDirection;
	goal: SizeGoal;
	compressionIntent: CompressionIntentRequestPayload;
}

export interface SizeTargetAnalysis {
	status: 'inside_target_band' | 'over_target' | 'under_target' | 'missing_prediction' | '';
	budgetBytes: number;
	predictedBytes: number;
	lowerBoundBytes: number;
	upperBoundBytes: number;
	predictedToBudgetRatio: number;
}

export interface ExpectedSizeChange {
	direction: 'smaller' | 'larger' | 'unchanged';
	bytes: number;
}

export interface ExactReviewSizeFacts {
	currentSizeBytes: number;
	estimatedOutputBytes: number | null;
	estimatedSpaceSavedBytes: number | null;
}

export interface CompareRiskSummary {
	verdict: string;
	blocked: boolean;
	requiresCadenceResolution: boolean;
	tone: HumanSeasonTone;
	title: string;
	detail: string;
	topRisk: string;
	topRiskLevel: string;
	topRiskDetail: string;
	authority: string;
	authorityDetail: string;
	hasSavedDecision: boolean;
	focusMoments: string[];
	picture: CompareRiskFact;
	sound: CompareRiskFact;
}

export interface CompareRiskFact {
	label: string;
	level: string;
	detail: string;
}

export interface ResolvedTargetSummary {
	targetBytes: number;
	sampleLowerBoundBytes: number;
	sampleUpperBoundBytes: number;
	finalLowerBoundBytes: number;
	finalUpperBoundBytes: number;
	sampleTolerancePercent: number;
	finalTolerancePercent: number;
	mode: SizeGoalMode | '';
	rationale: string;
	itemRuntimeSeconds: number;
	referenceSizeBytes: number;
	referenceRuntimeMinutes: number;
}

export interface TargetConstraintSummary {
	kind: 'arithmetic_infeasible' | 'bound_exhausted' | 'quality_conflict';
	title: string;
	detail: string;
	recoveryLabel: string;
	canReviewSmaller?: boolean;
}

export interface CalibrationEtaSummary {
	value: string;
	detail: string;
	tone: 'quiet' | 'attention';
}

export type QualityMemoryState =
	'empty' | 'sparse' | 'stale' | 'conflicting' | 'unavailable' | 'available' | 'high-confidence';

export interface QualityMemoryFact {
	label: string;
	value: string;
	detail: string;
}

export interface QualityMemoryView {
	state: QualityMemoryState;
	tone: HumanSeasonTone;
	badge: string;
	title: string;
	source: string;
	measured: QualityMemoryFact[];
	recommendation: QualityMemoryFact;
	evidence: string;
	dispersion: string;
	comparison: string;
	reason: string;
	policyCopy: string;
}

export interface ReviewConcern {
	tag: QualityRiskTag;
	label: string;
}

// Owner-facing wording on a show or sample page. Tests read it from here.
export const showCopy = {
	pastResults: 'Past results',
	noPastResults: 'No saved results to learn from yet',
	sizeGoal: 'Your size goal'
} as const;

export const REVIEW_CONCERNS: readonly ReviewConcern[] = [
	{ tag: 'softness_detail_loss', label: 'Picture looks soft' },
	{ tag: 'motion_breakup', label: 'Motion breaks up' },
	{ tag: 'banding_dark_scene_damage', label: 'Dark scenes or gradients look wrong' },
	{ tag: 'grain_noise_treatment', label: 'Grain or noise looks wrong' },
	{ tag: 'cadence_interlace_artifacts', label: 'Motion cadence looks uneven' },
	{ tag: 'audio_quality_layout', label: 'Sound quality or layout is wrong' },
	{ tag: 'other', label: 'Something else' }
];

const ACTIVE_JOB_STATUSES = new Set(['queued', 'running', 'starting', 'retry_backoff', 'stopping']);

export function catalogWarningNotice(
	scanJob: DashboardScanJob | null
): CatalogWarningNotice | null {
	const warnings = scanJob?.stats?.warnings ?? [];
	if (warnings.length === 0) return null;
	if (warnings.length > 1) {
		return {
			title: `${warnings.length} libraries need attention.`,
			detail:
				'Mediaforce could not safely reconcile these libraries, so their cached catalog state was preserved.'
		};
	}

	const warning = warnings[0];
	if (warning.code === 'source_unexpectedly_empty') {
		return {
			title: `${warning.label} returned no media.`,
			detail:
				'Cached catalog state was preserved. Disable or remove the library in Settings if it was intentionally cleared.'
		};
	}
	if (warning.code === 'source_scan_incomplete') {
		return {
			title: `${warning.label} could not be fully read.`,
			detail:
				'Cached catalog state was preserved because the scan did not finish reading this library.'
		};
	}
	if (warning.code === 'source_mass_disappearance') {
		return {
			title: `${warning.label} returned far fewer items than expected.`,
			detail: 'Cached catalog state was preserved so surviving workflow status remains unchanged.'
		};
	}
	if (warning.code === 'source_unavailable') {
		return {
			title: `${warning.label} storage is unavailable.`,
			detail:
				'Cached catalog state was preserved. Mediaforce will try this library again after storage returns.'
		};
	}
	return {
		title: `${warning.label} needs attention.`,
		detail:
			warning.message ||
			'Cached catalog state was preserved because this library could not be reconciled.'
	};
}

function record(value: unknown): Record<string, unknown> {
	return value && typeof value === 'object' && !Array.isArray(value)
		? (value as Record<string, unknown>)
		: {};
}

function records(value: unknown): Array<Record<string, unknown>> {
	return Array.isArray(value) ? value.map(record) : [];
}

function text(value: unknown): string {
	return typeof value === 'string' ? value.trim() : '';
}

function numberValue(value: unknown): number {
	const parsed = Number(value ?? 0);
	return Number.isFinite(parsed) ? parsed : 0;
}

function qualityRisk(folder: FolderPayload): QualityRiskPayload | null {
	const risk = record(folder.quality_risk);
	return Object.keys(risk).length > 0 ? (risk as QualityRiskPayload) : null;
}

function riskVerdictCopy(verdict: string | null | undefined): string {
	const normalized = text(verdict).toLowerCase();
	if (normalized === 'blocked') return 'Needs attention';
	if (normalized === 'request_comparison') return 'Review recommended';
	if (normalized === 'needs_operator_review') return 'Your review is needed';
	if (normalized === 'safe_to_sample') return 'Automated checks passed';
	return 'Review recommended';
}

function riskTone(risk: QualityRiskPayload | null): HumanSeasonTone {
	if (!risk) return 'quiet';
	if (risk.blocked) return 'attention';
	if (risk.request_comparison) return 'ready';
	return 'quiet';
}

function riskAuthority(risk: QualityRiskPayload | null): {
	value: string;
	detail: string;
	hasSavedDecision: boolean;
} {
	const decision = record(risk?.operator_decision);
	const status = text(decision.status).toLowerCase();
	if (status === 'rejected') {
		return {
			value: 'Not approved',
			detail: 'This exact sample was most recently marked as not acceptable.',
			hasSavedDecision: true
		};
	}
	if (status === 'approved') {
		return {
			value: 'Approved',
			detail: 'A decision has been saved for this exact sample.',
			hasSavedDecision: true
		};
	}
	return {
		value: 'Not decided yet',
		detail: 'No decision has been saved for this sample.',
		hasSavedDecision: false
	};
}

function plainRiskCopy(tag: QualityRiskTag): { label: string; detail: string } {
	const copy: Record<QualityRiskTag, { label: string; detail: string }> = {
		softness_detail_loss: {
			label: 'Fine detail',
			detail: 'Check faces, text, hair, and fine textures for softness or missing detail.'
		},
		motion_breakup: {
			label: 'Fast movement',
			detail: 'Watch movement and busy scenes for blocks, smearing, or distracting edges.'
		},
		banding_dark_scene_damage: {
			label: 'Dark scenes',
			detail: 'Check shadows and smooth color changes for visible steps or lost detail.'
		},
		grain_noise_treatment: {
			label: 'Texture and grain',
			detail: 'Check natural texture for waxiness, crawling noise, or an overly smooth look.'
		},
		cadence_interlace_artifacts: {
			label: 'Motion pattern',
			detail: 'Watch movement for combing, judder, repeated frames, or an uneven rhythm.'
		},
		audio_quality_layout: {
			label: 'Sound',
			detail: 'Listen for clear dialogue, balanced sound, and anything distracting.'
		},
		other: {
			label: 'Something to check',
			detail: 'Compare the selected moments closely before deciding.'
		}
	};
	return copy[tag];
}

function plainRiskLevel(level: unknown): string {
	const normalized = text(level).toLowerCase();
	if (normalized === 'high') return 'Check closely';
	if (normalized === 'medium') return 'Worth checking';
	return 'Review note';
}

function compareRiskFact(
	risks: NonNullable<QualityRiskPayload['typed_risks']>,
	predicate: (tag: QualityRiskTag) => boolean,
	fallbackLabel: string,
	fallbackDetail: string
): CompareRiskFact {
	const risk =
		risks.find((item) => predicate(item.tag) && text(item.level).toLowerCase() === 'high') ??
		risks.find((item) => predicate(item.tag) && text(item.level).toLowerCase() === 'medium') ??
		risks.find((item) => predicate(item.tag));
	if (!risk) {
		return { label: fallbackLabel, level: 'No specific warning', detail: fallbackDetail };
	}
	const copy = plainRiskCopy(risk.tag);
	return {
		label: copy.label,
		level: plainRiskLevel(risk.level),
		detail: copy.detail
	};
}

export function compareRiskSummary(folder: FolderPayload): CompareRiskSummary | null {
	const risk = qualityRisk(folder);
	if (!risk) return null;
	const typedRisks = Array.isArray(risk.typed_risks) ? risk.typed_risks : [];
	const topRisk =
		typedRisks.find((item) => text(item.level).toLowerCase() === 'high') ??
		typedRisks.find((item) => text(item.level).toLowerCase() === 'medium') ??
		typedRisks[0] ??
		null;
	const authority = riskAuthority(risk);
	const cadenceGate = text(risk.cadence_gate).toLowerCase();
	const cadenceTransform = text(risk.cadence_transform).toLowerCase();
	const legacyCadenceBlocker = (risk.blocking_reasons ?? []).some((reason) =>
		/\b(cadence|motion[ -]pattern)\b/i.test(reason)
	);
	const requiresCadenceResolution =
		Boolean(risk.blocked) &&
		(legacyCadenceBlocker ||
			cadenceGate === 'mixed' ||
			cadenceGate === 'unknown' ||
			(['tff', 'bff', 'telecine'].includes(cadenceGate) && !cadenceTransform));
	const picture = compareRiskFact(
		typedRisks,
		(tag) => tag !== 'audio_quality_layout',
		'No specific picture warning',
		'Check detail, movement, dark scenes, and natural texture in the selected moments.'
	);
	const sound = compareRiskFact(
		typedRisks,
		(tag) => tag === 'audio_quality_layout',
		'No specific sound warning',
		'Listen for clear dialogue, balanced sound, and anything distracting.'
	);
	const focusMoments = (risk.pre_test_instruction?.moments ?? [])
		.slice(0, 3)
		.map((moment) => `Moment ${moment.moment} needs the closest look.`);
	const topRiskCopy = topRisk ? plainRiskCopy(topRisk.tag) : null;
	return {
		verdict: riskVerdictCopy(risk.verdict),
		blocked: Boolean(risk.blocked),
		requiresCadenceResolution,
		tone: riskTone(risk),
		title: topRiskCopy?.label ?? riskVerdictCopy(risk.verdict),
		detail: risk.blocked
			? 'This sample needs attention before it can be approved.'
			: 'Compare the selected moments before deciding.',
		topRisk: topRiskCopy?.label ?? 'No specific warning',
		topRiskLevel: topRisk ? plainRiskLevel(topRisk.level) : 'Review note',
		topRiskDetail: topRiskCopy?.detail ?? 'Compare the selected moments before deciding.',
		authority: authority.value,
		authorityDetail: authority.detail,
		hasSavedDecision: authority.hasSavedDecision,
		focusMoments,
		picture,
		sound
	};
}

function jobStatus(job: unknown): string {
	return text(record(job).status).toLowerCase();
}

function isActiveJob(job: unknown): boolean {
	return ACTIVE_JOB_STATUSES.has(jobStatus(job));
}

export function seasonIdentity(prefix: string): SeasonIdentity {
	const segments = prefix.split('/').filter(Boolean);
	const library = segments[0] ?? '';
	const last = segments.at(-1) ?? prefix;
	const seasonIndex = segments.findIndex(
		(segment, index) => index >= 2 && /^(season\b|specials?$)/i.test(segment)
	);
	const showSegments = seasonIndex > 0 ? segments.slice(1, seasonIndex) : segments.slice(1);
	const show = showSegments.join(' / ') || last || 'Unknown show';
	const season = seasonIndex > 0 ? segments[seasonIndex] : 'Season';
	return {
		library,
		show,
		season,
		showPrefix: [library, ...showSegments].filter(Boolean).join('/')
	};
}

export function isSeriesPrefix(prefix: string): boolean {
	const segments = prefix.split('/').filter(Boolean);
	return segments[0]?.toLowerCase() === 'tv' && segments.length === 2;
}

export function seasonNumberLabel(season: string): string {
	const match = season.match(/(\d+)/);
	return match?.[1] ?? (season.toLowerCase().startsWith('special') ? 'S' : season.slice(0, 2));
}

export function episodeLabel(path: string | null | undefined): string {
	const value = text(path);
	const seasonEpisode = value.match(/S\d{1,3}E(\d{1,3})/i);
	if (seasonEpisode) return `Episode ${Number.parseInt(seasonEpisode[1], 10)}`;
	const alternate = value.match(/\b\d{1,3}x(\d{1,3})\b/i);
	if (alternate) return `Episode ${Number.parseInt(alternate[1], 10)}`;
	const fileName = value
		.split('/')
		.at(-1)
		?.replace(/\.[^.]+$/, '')
		.replaceAll(/[._-]+/g, ' ');
	return fileName?.trim() || 'A representative episode';
}

export interface SeasonEpisodeOption {
	itemId: number;
	label: string;
	statusLabel: string;
	relPath: string;
	href: `/folders/${string}`;
}

export function seasonEpisodeNavigationUnavailable(status: FolderStatusPayload): boolean {
	return Boolean(
		status.staged_integrity?.load_error || status.staged_integrity?.database_truncated
	);
}

export function seasonEpisodeOptions(status: FolderStatusPayload): SeasonEpisodeOption[] {
	const options = new Map<string, SeasonEpisodeOption>();
	for (const record of status.staged_integrity?.records ?? []) {
		const itemId = record.item_id;
		const relPath = record.rel_path?.trim();
		if (itemId === null || !relPath) continue;
		options.set(relPath, {
			itemId,
			label: episodeLabel(relPath),
			statusLabel: stagedIntegrityDispositionCopy(record.disposition).label,
			relPath,
			href: folderRoutePath(relPath)
		});
	}
	return [...options.values()].sort(
		(left, right) =>
			left.label.localeCompare(right.label, undefined, { numeric: true, sensitivity: 'base' }) ||
			left.itemId - right.itemId
	);
}

export function stagedEpisodeLinks(status: FolderStatusPayload): Array<{
	label: string;
	relPath: string;
	href: `/folders/${string}`;
}> {
	const records = status.staged_integrity?.records ?? [];
	const links = new Map<string, { label: string; relPath: string; href: `/folders/${string}` }>();
	for (const record of records) {
		const relPath = record.item_id !== null ? record.rel_path?.trim() : '';
		if (!relPath || ['not_started', 'tracked'].includes(record.disposition)) continue;
		links.set(relPath, {
			label: episodeLabel(relPath),
			relPath,
			href: folderRoutePath(relPath)
		});
	}
	return [...links.values()].sort((left, right) => left.relPath.localeCompare(right.relPath));
}

export function exactItemFilename(folder: FolderPayload): string {
	const prefix = folder.media_scope?.prefix || folder.prefix;
	return prefix.split('/').filter(Boolean).at(-1) ?? prefix;
}

export function folderHref(prefix: string): `/folders/${string}` {
	return `/folders/${prefix
		.split('/')
		.map((segment) => encodeURIComponent(segment))
		.join('/')}` as `/folders/${string}`;
}

export function formatFileSize(bytes: number | null | undefined): string {
	return formatOperatorFileSize(bytes, 'Size not available');
}

export function formatDecimalFileSize(bytes: number | null | undefined): string {
	return formatOperatorFileSize(bytes, '0 MB');
}

export function expectedSizeChange(
	currentBytes: number | null | undefined,
	expectedBytes: number | null | undefined
): ExpectedSizeChange {
	const deltaBytes = numberValue(currentBytes) - numberValue(expectedBytes);
	if (deltaBytes > 0) return { direction: 'smaller', bytes: deltaBytes };
	if (deltaBytes < 0) return { direction: 'larger', bytes: Math.abs(deltaBytes) };
	return { direction: 'unchanged', bytes: 0 };
}

export function exactReviewSizeFacts(
	currentBytes: number | null | undefined,
	estimatedOutputBytes: number | null | undefined
): ExactReviewSizeFacts {
	const currentSizeBytes = numberValue(currentBytes);
	const outputBytes = numberValue(estimatedOutputBytes);
	const hasEstimate = outputBytes > 0;
	return {
		currentSizeBytes,
		estimatedOutputBytes: hasEstimate ? outputBytes : null,
		estimatedSpaceSavedBytes: hasEstimate ? Math.max(0, currentSizeBytes - outputBytes) : null
	};
}

export interface SoundCheckView {
	status: 'passed' | 'failed' | 'unmeasured' | 'unchanged';
	factValue: string;
	headline: string;
	detail: string;
	problems: string[];
}

export function soundCheckView(sampleResult: unknown): SoundCheckView | null {
	const check = record(record(sampleResult).audio_quality);
	const status = text(check.status);
	const score = numberValue(check.sound_score);
	const ceiling = numberValue(check.max_sound_score);
	const reason = text(check.reason);
	if (status === 'passed') {
		return {
			status,
			factValue: `Passed · ${score.toFixed(2)} of ${ceiling.toFixed(2)}`,
			headline: 'The new sound passed its check.',
			detail: '',
			problems: []
		};
	}
	if (status === 'failed') {
		return {
			status,
			factValue: 'Did not pass',
			headline: 'The new sound did not pass its check.',
			detail: 'Listen to the Sample before approving.',
			problems: records(check.checks)
				.filter((item) => item.passed !== true)
				.map((item) => `${text(item.label)}: ${text(item.detail)}`)
		};
	}
	if (status === 'unmeasured') {
		return {
			status,
			factValue: 'Not measured',
			headline: 'The new sound could not be checked.',
			detail: reason || 'Create the sample again to check the sound.',
			problems: []
		};
	}
	if (status === 'unchanged') {
		return { status, factValue: 'Kept as is', headline: '', detail: reason, problems: [] };
	}
	return null;
}

function formatQualityMemoryNumber(value: number | null | undefined): string {
	if (value == null || !Number.isFinite(value)) return '—';
	return value.toLocaleString('en-US', {
		minimumFractionDigits: 1,
		maximumFractionDigits: 1
	});
}

function formatSignedQualityMemoryNumber(value: number | null | undefined): string {
	if (value == null || !Number.isFinite(value)) return '—';
	return `${value >= 0 ? '+' : '−'}${formatQualityMemoryNumber(Math.abs(value))}`;
}

function formatQualityMemoryDuration(value: number | null): string {
	if (value === null || !Number.isFinite(value) || value <= 0) return '';
	const totalSeconds = Math.round(value);
	const minutes = Math.floor(totalSeconds / 60);
	const seconds = totalSeconds % 60;
	if (minutes && seconds) return `${minutes}m ${seconds}s`;
	if (minutes) return `${minutes}m`;
	return `${seconds}s`;
}

function qualityMemorySizeDetail(value: number | null): string {
	if (value === null || !Number.isFinite(value)) return 'No saved size target for this run';
	if (Math.abs(value) < 0.05) return 'On saved size target';
	const percent = Math.abs(value).toLocaleString('en-US', { maximumFractionDigits: 1 });
	return `${percent}% ${value > 0 ? 'over' : 'under'} saved size target`;
}

function qualityMemorySavingsPercent(value: number | null | undefined): string {
	if (value == null || !Number.isFinite(value)) return '';
	return `${Math.round(value * 100).toLocaleString('en-US')}%`;
}

function qualityWarmStartFallbackCopy(reason: string | null | undefined): string {
	const copy: Record<string, string> = {
		candidate_rejected: 'the candidate did not satisfy the search',
		quality_target_miss: 'the candidate missed the strict quality target',
		size_cap_miss: 'the candidate exceeded the configured encoded-size cap',
		metric_mismatch: 'the candidate returned a different quality metric',
		invalid_measurement: 'the candidate measurement was invalid',
		quality_floor_miss: 'the candidate missed the minimum picture quality',
		source_cap_miss: 'the candidate exceeded the source-size cap',
		target_band_miss: 'the candidate missed the saved size band',
		final_size_miss: 'the speculative final output missed the saved size band',
		probe_error: 'the candidate probe could not be completed'
	};
	return copy[text(reason)] ?? 'the candidate did not satisfy every configured guard';
}

function qualityMemoryFallbackCopy(reason: string | null): string {
	const copy: Record<string, string> = {
		no_history: 'No compatible prior runs were available for this search.',
		stale_evidence: 'Matching runs are too old to use.',
		stale_signature: 'The saved settings have changed.',
		source_fingerprint_unavailable: 'The original file could not be verified.',
		source_fingerprint_changed: 'The original file changed, so earlier results cannot be reused.',
		sparse_cohort: 'There are not enough matching results yet.',
		high_dispersion: 'Earlier compression settings vary too much to suggest a starting point.',
		search_target_changed: 'Prior searches changed their quality target and were excluded.',
		non_monotonic_trace: 'Earlier measurements were inconsistent and were left out.',
		conflicting_quality_evidence: 'Earlier runs disagree about the minimum picture quality.',
		final_retry_terminal: 'Earlier results came from retries and cannot suggest where to start.',
		shadow_evaluation_error: 'Earlier results could not be checked; compression continued normally.'
	};
	return copy[reason ?? ''] ?? 'No starting suggestion was available.';
}

function qualityMemoryState(reason: string | null, confidence: string | null): QualityMemoryState {
	if (confidence === 'high') return 'high-confidence';
	if (confidence) return 'available';
	if (reason === 'no_history' || reason === 'sparse_cohort' || reason === 'final_retry_terminal') {
		return 'sparse';
	}
	if (reason === 'shadow_evaluation_error') return 'unavailable';
	if (
		reason === 'stale_evidence' ||
		reason === 'stale_signature' ||
		reason === 'source_fingerprint_unavailable' ||
		reason === 'source_fingerprint_changed'
	) {
		return 'stale';
	}
	return 'conflicting';
}

export function qualityMemoryView(folder: FolderPayload): QualityMemoryView {
	const memory = folder.quality_memory;
	const passivePolicyCopy =
		'Your quality limits and saved settings stayed unchanged. Earlier results were recorded for comparison; the normal search ran.';
	if (!memory) {
		return {
			state: 'empty',
			tone: 'quiet',
			badge: 'Nothing yet',
			title: showCopy.noPastResults,
			source: '',
			measured: [],
			recommendation: {
				label: 'Starting suggestion',
				value: 'Not available',
				detail: 'A finished episode will provide the first result.'
			},
			evidence: '0 results',
			dispersion: '—',
			comparison: '—',
			reason: 'Your size and quality settings stay as they are.',
			policyCopy: passivePolicyCopy
		};
	}

	const recommendation = memory.recommendation;
	const warmStart = memory.warm_start;
	const warmAttempted = memory.production_search_changed && Boolean(warmStart?.attempted);
	const warmAccepted = warmAttempted && warmStart?.status === 'accepted';
	const warmFallback = warmAttempted && Boolean(warmStart?.fallback_used);
	const policyCopy = warmAccepted
		? 'Your quality limits, size goal, picture changes, and saved settings stayed unchanged. The suggested starting setting passed every check, so the normal search was not needed.'
		: warmFallback
			? 'The suggested starting setting failed a check and was discarded. Your quality limits, size goal, picture changes, and saved settings stayed unchanged while the normal search ran.'
			: passivePolicyCopy;
	const state = qualityMemoryState(memory.fallback_reason, recommendation?.confidence ?? null);
	const withinOne = memory.comparison.within_one_crf;
	const tone: HumanSeasonTone = warmAccepted
		? 'success'
		: warmFallback
			? 'ready'
			: recommendation
				? withinOne === false
					? 'attention'
					: state === 'high-confidence'
						? 'success'
						: 'ready'
				: state === 'sparse'
					? 'quiet'
					: 'attention';
	const badge = warmAccepted
		? 'Starting suggestion worked'
		: warmFallback
			? 'Normal search used'
			: recommendation
				? state === 'high-confidence'
					? withinOne === false
						? 'High confidence · differed'
						: 'High confidence'
					: withinOne === false
						? 'Suggestion differed'
						: withinOne
							? 'Suggestion agreed'
							: 'Suggestion recorded'
				: state === 'sparse'
					? 'More results needed'
					: state === 'stale'
						? 'Earlier results out of date'
						: state === 'unavailable'
							? 'Earlier results unavailable'
							: 'Results disagree';
	const measured = memory.measured;
	const metric = resolveMetricLabel(measured.quality_metric, folder.metric_support);
	const qualityDetail = [
		measured.quality_target === null
			? ''
			: `target ${formatQualityMemoryNumber(measured.quality_target)}`,
		measured.quality_floor === null
			? ''
			: `minimum ${formatQualityMemoryNumber(measured.quality_floor)}`,
		measured.quality_margin === null
			? ''
			: `margin ${formatSignedQualityMemoryNumber(measured.quality_margin)}`
	]
		.filter(Boolean)
		.join(' · ');
	const searchDetail = [
		measured.candidate_count > 0
			? `${measured.candidate_count} candidate${measured.candidate_count === 1 ? '' : 's'}`
			: '',
		formatQualityMemoryDuration(measured.search_duration_seconds)
	]
		.filter(Boolean)
		.join(' · ');
	const evidence = recommendation
		? `${recommendation.sample_count.toLocaleString('en-US')} result${recommendation.sample_count === 1 ? '' : 's'} · ${recommendation.scope} · ${recommendation.confidence} confidence`
		: 'No matching results';
	const dispersion = recommendation
		? [
				recommendation.minimum_crf === null || recommendation.maximum_crf === null
					? ''
					: `Compression settings ${formatQualityMemoryNumber(recommendation.minimum_crf)}–${formatQualityMemoryNumber(recommendation.maximum_crf)}`,
				recommendation.iqr === null
					? ''
					: `middle spread ${formatQualityMemoryNumber(recommendation.iqr)}`,
				recommendation.median_absolute_deviation === null
					? ''
					: `typical difference ${formatQualityMemoryNumber(recommendation.median_absolute_deviation)}`
			]
				.filter(Boolean)
				.join(' · ') || 'Spread not recorded'
		: '—';
	const estimatedCandidateSavings = qualityMemorySavingsPercent(
		warmStart?.estimated_candidate_savings_rate
	);
	const displayedFirstCrf =
		warmAttempted && warmStart?.candidate_crf !== null && warmStart?.candidate_crf !== undefined
			? warmStart.candidate_crf
			: recommendation?.first_crf;
	const warmAdjustment =
		warmAttempted && warmStart?.adjusted && warmStart.requested_crf !== null
			? `adjusted from ${formatQualityMemoryNumber(warmStart.requested_crf)}`
			: '';
	const comparison = warmAccepted
		? [
				'Starting setting passed',
				estimatedCandidateSavings ? `estimated ${estimatedCandidateSavings} fewer attempts` : ''
			]
				.filter(Boolean)
				.join(' · ')
		: warmFallback
			? `Normal search used · ${measured.candidate_count.toLocaleString('en-US')} candidate${measured.candidate_count === 1 ? '' : 's'} total`
			: recommendation
				? memory.comparison.crf_delta === null
					? 'Production comparison unavailable'
					: `${withinOne ? 'Within 1 compression step' : 'More than 1 compression step apart'} · difference ${formatSignedQualityMemoryNumber(memory.comparison.crf_delta)}`
				: 'No recommendation to compare';
	return {
		state,
		tone,
		badge,
		title: warmAccepted
			? 'Earlier results helped this run'
			: warmFallback
				? 'Earlier results tried; normal search used'
				: recommendation
					? 'This run and its starting suggestion'
					: 'This run; earlier results not reused',
		source: memory.source_rel_path ? episodeLabel(memory.source_rel_path) : '',
		measured: [
			{
				label: 'Compression setting',
				value: formatQualityMemoryNumber(measured.selected_crf),
				detail: searchDetail || 'Production selection'
			},
			{
				label: `Measured ${metric}`,
				value: formatQualityMemoryNumber(measured.quality_score),
				detail: qualityDetail || 'No quality target details recorded'
			},
			{
				label: 'Final size',
				value: formatDecimalFileSize(measured.output_bytes),
				detail: qualityMemorySizeDetail(measured.size_error_percent)
			}
		],
		recommendation: warmAttempted
			? {
					label: 'First setting tried',
					value: formatQualityMemoryNumber(displayedFirstCrf),
					detail: warmAccepted
						? [
								recommendation ? evidence : 'Suggested starting setting',
								warmAdjustment,
								'passed every check'
							]
								.filter(Boolean)
								.join(' · ')
						: [
								recommendation ? evidence : 'Suggested starting setting',
								warmAdjustment,
								'normal search used'
							]
								.filter(Boolean)
								.join(' · ')
				}
			: recommendation
				? {
						label: 'Suggested first setting',
						value: formatQualityMemoryNumber(recommendation.first_crf),
						detail: evidence
					}
				: {
						label: 'Starting suggestion',
						value: 'Held back',
						detail: 'The normal search still ran.'
					},
		evidence,
		dispersion,
		comparison,
		reason: warmAccepted
			? 'The suggested starting setting passed the picture quality, size, and picture-change checks on this original.'
			: warmFallback
				? `Mediaforce discarded the suggested starting setting because ${qualityWarmStartFallbackCopy(warmStart?.fallback_reason)}, then ran the normal search unchanged.`
				: recommendation
					? 'The starting suggestion was recorded for comparison only.'
					: qualityMemoryFallbackCopy(memory.fallback_reason),
		policyCopy
	};
}

export function calibrationJobTargetContract(
	job: CalibrationJobPayload | null | undefined
): CalibrationTargetContract | null {
	if (!job) return null;
	if (job.target_contract) return job.target_contract;
	const video = record(record(job.policy).video);
	const explicitBytes = numberValue(video.target_size_bytes);
	const targetSizeBytes =
		explicitBytes > 0 ? explicitBytes : Math.round(numberValue(video.target_size_mb) * 1_000_000);
	if (targetSizeBytes <= 0) return null;
	const maxHeight = numberValue(video.max_height);
	return {
		schema_version: 1,
		target_size_bytes: targetSizeBytes,
		mode: text(video.size_goal_mode) || 'legacy',
		source: text(video.size_goal_source) || 'legacy',
		target_runtime_minutes: numberValue(video.target_runtime_minutes) || null,
		sample_tolerance_percent: numberValue(video.sample_projection_tolerance_percent) || null,
		final_tolerance_percent: numberValue(video.final_output_tolerance_percent) || null,
		resolution_mode:
			text(video.resolution_intent_mode) || (maxHeight > 0 ? 'max_height' : 'source'),
		max_height: maxHeight > 0 ? maxHeight : null
	};
}

export function calibrationTargetModeLabel(
	target: Pick<CalibrationTargetContract, 'mode'> | null | undefined
): string {
	if (target?.mode === 'absolute') return 'Absolute per episode';
	if (target?.mode === 'normalized') return 'Scaled by episode runtime';
	return 'Saved episode target';
}

export function calibrationResolutionLabel(
	target: CalibrationTargetContract | null | undefined
): string {
	if (!target) return 'Saved resolution';
	if (target.resolution_mode === 'source') return 'Keep source resolution';
	if (target.resolution_mode === 'max_height' && target.max_height) {
		return `Up to ${target.max_height}p`;
	}
	return 'Saved resolution';
}

export function overlappingCalibrationActivity(
	status: FolderStatusPayload,
	routePrefix: string
): CalibrationScopeActivityPayload | null {
	const activity = status.scope_activity ?? null;
	if (!activity || (activity.job.prefix ?? '') === routePrefix) return null;
	return activity;
}

export function shouldPrioritizeScopeActivity(
	activity: CalibrationScopeActivityPayload | null | undefined,
	hasOwnCalibration: boolean,
	hasPrimaryScopeWork = false
): boolean {
	if (!activity || hasPrimaryScopeWork) return false;
	const status = activity.job.status ?? 'idle';
	return ['queued', 'starting', 'running'].includes(status) || !hasOwnCalibration;
}

export function calibrationStageLabel(job: CalibrationJobPayload | null | undefined): string {
	const stage = text(job?.progress?.stage);
	const labels: Record<string, string> = {
		preparing_host: 'Starting the computer',
		preparing_source: 'Preparing the source',
		inspecting_source: 'Inspecting the picture',
		searching_target: 'Finding settings that hit the size target',
		measuring_quality: 'Measuring picture quality',
		selecting_review_moments: 'Choosing review moments',
		building_review: 'Building comparison clips',
		checking_sound: 'Checking the sound',
		encoding_full_calibration: 'Compressing the sample',
		saving_results: 'Saving sample results',
		completed: 'Complete'
	};
	if (labels[stage]) return labels[stage];
	if (job?.status === 'queued') return 'Waiting for a computer';
	if (job?.status === 'retry_backoff') return 'Waiting to retry the sample';
	if (job?.status === 'failed' || job?.status === 'stopped') return 'Sample needs retry';
	if (job?.status === 'completed' || job?.status === 'pending_review') return 'Ready to review';
	if (job?.status === 'running') return 'Creating comparison clips';
	return 'Preparing the sample';
}

export function calibrationWorkProgress(job: CalibrationJobPayload | null | undefined): {
	label: string;
	determinate: boolean;
} {
	const work = job?.progress?.work;
	if (!work || work.total <= 0) return { label: '', determinate: false };
	const completed = Math.max(0, Math.min(work.completed, work.total));
	if (job?.progress?.stage === 'searching_target') {
		return { label: 'Trying size settings', determinate: false };
	}
	if (job?.progress?.stage === 'building_review') {
		return {
			label: `${completed} of ${work.total} comparison clips built`,
			determinate: true
		};
	}
	return { label: 'Working', determinate: false };
}

export function calibrationWorkLabel(job: CalibrationJobPayload | null | undefined): string {
	return calibrationWorkProgress(job).label;
}

export function sampleSearchTechnicalDetail(job: CalibrationJobPayload | null | undefined): string {
	const work = job?.progress?.work;
	if (job?.progress?.stage !== 'searching_target' || !work || work.total <= 0) return '';
	const completed = Math.max(0, Math.min(work.completed, work.total));
	return `${completed} of up to ${work.total} size settings tried`;
}

export function calibrationLivenessLabel(job: CalibrationJobPayload | null | undefined): string {
	if (job?.status === 'completed' || job?.status === 'pending_review') return 'Sample finished';
	if (job?.status === 'failed' || job?.status === 'stopped') return 'Sample is not running';
	if (job?.status === 'retry_backoff') return 'Waiting before retry';
	const liveness = job?.progress?.liveness;
	if (liveness === 'reporting') return 'Computer is reporting normally';
	if (liveness === 'delayed') return 'Computer updates are arriving slowly';
	if (liveness === 'not_reporting') return 'Computer has stopped reporting progress';
	if (liveness === 'queued') return 'Waiting in the sample queue';
	return 'Waiting for the first progress update';
}

export function calibrationActivityStatusLabel(
	job: CalibrationJobPayload | null | undefined
): string {
	if (job?.status === 'queued' || job?.status === 'retry_backoff') return 'Sample waiting';
	if (job?.status === 'starting') {
		const stage = job.progress?.stage;
		if (!stage || /^(preparing_host|preparing_source)$/.test(stage)) return 'Sample starting';
		return 'Creating sample';
	}
	return 'Creating sample';
}

export function calibrationFreshnessLabel(value: unknown): string {
	if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return 'No update yet';
	if (value <= 5) return 'Updated just now';
	if (value < 60) return `Updated ${Math.floor(value)} sec ago`;
	return `Updated ${Math.floor(value / 60)} min ago`;
}

export function calibrationEtaSummary(
	job: CalibrationJobPayload | null | undefined
): CalibrationEtaSummary {
	if (job?.status === 'completed' || job?.status === 'pending_review') {
		return {
			value: 'Finished',
			detail: 'This sample is no longer running.',
			tone: 'quiet'
		};
	}
	if (job?.status === 'failed' || job?.status === 'stopped') {
		return {
			value: 'Stopped',
			detail: 'Open the owning scope to review the failure or retry the saved plan.',
			tone: 'attention'
		};
	}
	const progress = job?.progress;
	if (progress?.liveness === 'not_reporting') {
		return {
			value: 'Progress signal lost',
			detail: 'The job still exists, but its computer has not sent a recent heartbeat.',
			tone: 'attention'
		};
	}
	const estimate = progress?.estimate;
	if (!estimate) {
		return {
			value: 'No estimate yet',
			detail: 'A remaining-time range appears after three comparable samples have finished.',
			tone: progress?.liveness === 'delayed' ? 'attention' : 'quiet'
		};
	}
	if (estimate.longer_than_recent_runs) {
		return {
			value: 'Longer than recent samples',
			detail: `Comparable samples usually finished within ${coarseDuration(estimate.total_seconds_high)}. The computer can still be reporting normally.`,
			tone: 'attention'
		};
	}
	const low = coarseDuration(estimate.remaining_seconds_low);
	const high = coarseDuration(estimate.remaining_seconds_high);
	const value =
		estimate.remaining_seconds_low <= 0
			? `Up to ${high} left`
			: low === high
				? `About ${high} left`
				: `${low}–${high} left`;
	return {
		value,
		detail: `Based on ${estimate.sample_size} comparable completed samples; this range is not a guarantee.`,
		tone: progress?.liveness === 'delayed' ? 'attention' : 'quiet'
	};
}

function coarseDuration(seconds: number): string {
	if (!Number.isFinite(seconds) || seconds <= 0) return 'less than 1 min';
	const minutes = Math.max(1, Math.ceil(seconds / 60));
	if (minutes < 10) return `${minutes} min`;
	if (minutes < 60) return `${Math.ceil(minutes / 5) * 5} min`;
	const hours = Math.floor(minutes / 60);
	const remaining = Math.ceil((minutes % 60) / 15) * 15;
	return remaining >= 60
		? `${hours + 1} hr`
		: remaining > 0
			? `${hours} hr ${remaining} min`
			: `${hours} hr`;
}

export function formatDuration(seconds: number | null | undefined): string {
	const value = numberValue(seconds);
	if (value <= 0) return '';
	const minutes = Math.round(value / 60);
	if (minutes < 60) return `${minutes} min`;
	const hours = Math.floor(minutes / 60);
	const remaining = minutes % 60;
	return remaining ? `${hours} hr ${remaining} min` : `${hours} hr`;
}

export function normalizedSizePaceBytes(
	referenceSizeBytes: number | null | undefined,
	referenceRuntimeMinutes: number | null | undefined,
	paceMinutes = 30
): number {
	const sizeBytes = numberValue(referenceSizeBytes);
	const runtimeMinutes = numberValue(referenceRuntimeMinutes);
	if (sizeBytes <= 0 || runtimeMinutes <= 0 || !Number.isFinite(paceMinutes) || paceMinutes <= 0) {
		return 0;
	}
	return Math.round((sizeBytes * paceMinutes) / runtimeMinutes);
}

export function sizeGoals(folder: FolderPayload): SizeGoal[] {
	const options = folder.size_goal_options ?? [];
	return options.flatMap((option) => {
		const targetMegabytes = numberValue(option.resolved_size_goal.target_size_mb);
		const targetSizeBytes = numberValue(option.resolved_size_goal.target_size_bytes);
		const mode = option.operator_intent.size_goal.mode;
		if (
			targetMegabytes <= 0 ||
			targetSizeBytes <= 0 ||
			(mode !== 'normalized' && mode !== 'absolute')
		)
			return [];
		return [
			{
				key: option.key,
				title: option.title,
				megabytesPerEpisode: Math.round(targetMegabytes),
				targetSizeBytes,
				detail: option.detail || option.resolved_size_goal.rationale,
				mode,
				operatorIntent: option.operator_intent,
				requiresExplicitSelection: option.requires_explicit_selection
			}
		];
	});
}

export function compressionIntentContract(
	option: CompressionIntentOptionPayload
): CompressionIntentContract {
	const contract: Omit<CompressionIntentContract, 'announcement'> =
		option.key === 'reference'
			? {
					sizeLabel: 'Size goal',
					sizeRule: 'Keeps as much detail as fits near this size.',
					searchLabel: 'Most detail first',
					searchRule: 'Tries the most detailed versions first and only goes smaller to fit.',
					qualityLabel: 'Keep as much detail as fits',
					qualityRule: 'Detail matters more than extra savings.',
					finalHeadline: 'The finished file must land in the allowed size.',
					finalRule:
						'A file that comes out too big is tried again when that can be done safely; otherwise it waits for you. A file that comes out too small waits for you.'
				}
			: option.key === 'transparent' || option.key === 'perceptual_floor'
				? {
						sizeLabel: 'Size goal',
						sizeRule: 'Smaller is fine as long as it still looks and sounds right.',
						searchLabel: 'Smallest first',
						searchRule: 'Tries smaller sizes and keeps the smallest one that still looks right.',
						qualityLabel: 'Must still look and sound right',
						qualityRule: 'Picks the smallest file that still looks and sounds right.',
						finalHeadline: 'A smaller finished file is fine.',
						finalRule:
							'A smaller file is kept while it still looks right. A file that comes out too big is tried again when that can be done safely; otherwise it waits for you, as does a file that does not look right.'
					}
				: {
						sizeLabel: 'Size goal',
						sizeRule: 'Aims for this size and stays under the limit.',
						searchLabel: 'Closest to your goal',
						searchRule: 'Tries sizes near your goal and keeps the closest one that looks right.',
						qualityLabel: 'Must still look and sound right',
						qualityRule: 'Mediaforce checks picture and sound before keeping a file.',
						finalHeadline: 'The finished file must stay under the size limit.',
						finalRule:
							'A smaller file is kept once it looks right. A file that comes out too big is tried again when that can be done safely; otherwise it waits for you.'
					};
	return {
		...contract,
		announcement: `${option.title}. ${contract.sizeLabel}. ${contract.searchRule} ${contract.finalHeadline}`
	};
}

export function resolvedTargetSummary(folder: FolderPayload): ResolvedTargetSummary | null {
	const analysis = folderSizeTargetAnalysis(folder);
	const resolved = folder.resolved_operator_intent?.size_goal;
	const targetBytes = analysis.budgetBytes || numberValue(resolved?.target_size_bytes);
	if (targetBytes <= 0) return null;
	const sampleTolerancePercent = numberValue(resolved?.sample_projection_tolerance_percent) || 10;
	const finalTolerancePercent = numberValue(resolved?.final_output_tolerance_percent) || 5;
	const sampleRatio = sampleTolerancePercent / 100;
	const finalRatio = finalTolerancePercent / 100;
	return {
		targetBytes,
		sampleLowerBoundBytes:
			analysis.lowerBoundBytes ||
			numberValue(resolved?.sample_lower_bound_bytes) ||
			Math.round(targetBytes * (1 - sampleRatio)),
		sampleUpperBoundBytes:
			analysis.upperBoundBytes ||
			numberValue(resolved?.sample_upper_bound_bytes) ||
			Math.round(targetBytes * (1 + sampleRatio)),
		finalLowerBoundBytes:
			numberValue(resolved?.final_lower_bound_bytes) || Math.round(targetBytes * (1 - finalRatio)),
		finalUpperBoundBytes:
			numberValue(resolved?.final_upper_bound_bytes) || Math.round(targetBytes * (1 + finalRatio)),
		sampleTolerancePercent,
		finalTolerancePercent,
		mode: resolved?.mode === 'normalized' || resolved?.mode === 'absolute' ? resolved.mode : '',
		rationale: text(resolved?.rationale),
		itemRuntimeSeconds: numberValue(resolved?.item_runtime_seconds),
		referenceSizeBytes: numberValue(resolved?.reference_size_mb) * 1_000_000,
		referenceRuntimeMinutes: numberValue(resolved?.reference_runtime_minutes)
	};
}

export function targetProvenanceSummary(
	provenance: TargetSizeProvenance | null | undefined
): string | null {
	if (!provenance) return null;
	const source =
		provenance.source === 'exact_override'
			? 'Exact-item override'
			: provenance.source === 'ancestor_override'
				? 'Inherited folder override'
				: 'Configured default';
	const prefix = provenance.override_prefix ? ` (${provenance.override_prefix})` : '';
	return `${source}${prefix}`;
}

export function targetConstraintSummary(
	folder: FolderPayload,
	status?: FolderStatusPayload
): TargetConstraintSummary | null {
	const search = folder.failed_target_size_search ?? folder.quality_risk?.target_size_search;
	const feasibility = folder.stream_budget_ledger?.feasibility;
	const sourceCap = folder.stream_budget_ledger?.source_relative_cap;
	const sampleJob = record(status?.calibration_job ?? folder.calibration_job);
	const jobResult = record(sampleJob.result);
	const jobTrace = record(jobResult.target_size_trace);
	const searchStatus =
		text(search?.status) || text(jobResult.target_size_status) || text(jobTrace.status);
	const selectionReason = text(search?.selection_reason) || text(jobTrace.selection_reason);
	const legacyBoundExhaustion =
		searchStatus === 'infeasible' &&
		[
			'smallest_quality_safe_candidate_over_target_band',
			'largest_quality_safe_candidate_under_target_band',
			'target_lower_bound_exceeds_source_relative_cap',
			'source_relative_cap_consumed_by_non_video_budget'
		].includes(selectionReason);
	if (
		feasibility?.arithmetic_infeasible ||
		(searchStatus === 'infeasible' && !legacyBoundExhaustion)
	) {
		return {
			kind: 'arithmetic_infeasible',
			title: 'This size cannot fit the required streams.',
			detail:
				'Audio, subtitles, attachments, and container overhead leave no positive video budget at this size. Choose a roomier goal before creating another sample.',
			recoveryLabel: 'Choose a size that can fit'
		};
	}
	if (
		searchStatus === 'bound_exhausted' ||
		legacyBoundExhaustion ||
		sourceCap?.status === 'arithmetically_infeasible'
	) {
		const candidate = search?.under_target_review_candidate;
		if (candidate && currentOperatorIntent(folder)) {
			return {
				kind: 'bound_exhausted',
				title: 'A smaller result passed the measured quality checks.',
				detail: `The previous sample projected ${Math.round(candidate.predicted_whole_episode_bytes / 1_000_000)} MB with ${resolveMetricLabel(candidate.metric, folder.metric_support)} ${candidate.metric_score.toFixed(1)} (minimum ${candidate.minimum_metric_score}). It passed the source-size cap but fell below the target band. Allowing smaller results creates fresh comparison clips with current settings; the selected candidate may change. Review is still required before encoding.`,
				recoveryLabel: 'Allow smaller and create review sample',
				canReviewSmaller: true
			};
		}
		return {
			kind: 'bound_exhausted',
			title: 'The sample reached a configured limit.',
			detail:
				'The previous sample stopped at an inherited search or source-size limit before it could measure your requested size. Retrying that saved sample would repeat the same limit; create a fresh sample with updated settings instead.',
			recoveryLabel: 'Review size and settings'
		};
	}
	if (searchStatus === 'quality_conflict') {
		const metric = text(search?.selected_metric).toUpperCase();
		const minimum = numberValue(
			search?.minimum_metric_score || record(jobTrace.quality_floor).minimum
		);
		const floor =
			metric && minimum > 0
				? ` the minimum ${resolveMetricLabel(metric, folder.metric_support)} of ${minimum}`
				: ' the minimum picture quality';
		return {
			kind: 'quality_conflict',
			title: 'This size conflicts with the minimum picture quality.',
			detail: `Every measured candidate that fit the size fell below${floor}. Choose a roomier goal instead of silently lowering quality.`,
			recoveryLabel: 'Choose a roomier goal'
		};
	}
	return encodeTargetConstraint(folder, status?.page_state);
}

// A full encode can hit the minimum picture quality after its sample passed. Retrying repeats the
// same result, so it needs the same "choose a roomier goal" path as a failed sample. The server's page
// state says whether that stopped run is what the page leads with.
function encodeTargetConstraint(
	folder: FolderPayload,
	pageState: PageStatePayload | null | undefined
): TargetConstraintSummary | null {
	if (pageState?.recovery_kind !== 'season' || pageState.failure !== 'quality_conflict')
		return null;
	const encodeJob = folder.encode_job;
	const error =
		encodeJob && encodeJob.job_id === pageState.source_job?.id ? text(encodeJob.error) : '';
	const targetMb = Math.round(numberValue(error.match(/target=(\d+) bytes/)?.[1]) / 1_000_000);
	const reachableMb = Math.round(
		numberValue(error.match(/best_reachable=(\d+) bytes/)?.[1]) / 1_000_000
	);
	const measured =
		targetMb > 0 && reachableMb > 0
			? `Compressing the full episode could not reach ${targetMb} MB; the smallest result that kept the required quality was about ${reachableMb} MB.`
			: 'Compressing the full episode could not reach the saved size while keeping the required quality.';
	return {
		kind: 'quality_conflict',
		title: 'This size conflicts with the minimum picture quality.',
		detail: `${measured} Retrying would stop the same way. Choose a roomier goal instead of silently lowering quality.`,
		recoveryLabel: 'Choose a roomier goal'
	};
}

function normalizedOperatorText(value: string, limit = 600): string {
	return value.trim().replaceAll(/\s+/g, ' ').slice(0, limit);
}

export function testRequestWithInstructions(baseRequest: string, instructions: string): string {
	const detail = normalizedOperatorText(instructions);
	return detail
		? `${baseRequest.trim()} Additional operator priorities: ${detail}`
		: baseRequest.trim();
}

export function reviewFeedbackRequest(
	baseRequest: string,
	tags: readonly QualityRiskTag[],
	details: string,
	instructions = ''
): string {
	const selectedLabels = REVIEW_CONCERNS.filter((concern) => tags.includes(concern.tag)).map(
		(concern) => concern.label
	);
	const feedbackDetail = normalizedOperatorText(details);
	const feedback = [
		selectedLabels.length ? `Operator review concerns: ${selectedLabels.join(', ')}.` : '',
		feedbackDetail ? `Operator review detail: ${feedbackDetail}` : ''
	]
		.filter(Boolean)
		.join(' ');
	return testRequestWithInstructions(`${baseRequest.trim()} ${feedback}`.trim(), instructions);
}

export function reviewFeedbackIntent(
	intent: OperatorIntentRequestPayload | null,
	tags: readonly QualityRiskTag[],
	details: string
): OperatorIntentRequestPayload | null {
	if (!intent) return null;
	const feedbackDetail = normalizedOperatorText(details, 2000);
	const normalizedTags: QualityRiskTag[] = tags.length ? [...new Set(tags)] : ['other'];
	return {
		...intent,
		size_goal: { ...intent.size_goal },
		resolution: { ...intent.resolution },
		...(intent.quality ? { quality: { ...intent.quality } } : {}),
		...(intent.streams
			? {
					streams: {
						...(intent.streams.audio ? { audio: { ...intent.streams.audio } } : {}),
						...(intent.streams.subtitle ? { subtitle: { ...intent.streams.subtitle } } : {})
					}
				}
			: {}),
		quality_risk_tags: normalizedTags,
		quality_risk_details: feedbackDetail,
		evidence_authority: 'rejected_visual_result'
	};
}

export function isSizeGoalSelectionConfirmed(
	goals: readonly SizeGoal[],
	selectedGoalKey: SizeGoal['key'],
	selectedGoalPrefix: string,
	folderPrefix: string
): boolean {
	const requiresExplicitSelection = goals.some((goal) => goal.requiresExplicitSelection);
	return (
		!requiresExplicitSelection ||
		(selectedGoalPrefix === folderPrefix && goals.some((goal) => goal.key === selectedGoalKey))
	);
}

export function currentOperatorIntent(folder: FolderPayload): OperatorIntentRequestPayload | null {
	const request = folder.resolved_operator_intent?.request;
	if (!request || (request.schema_version !== 1 && request.schema_version !== 2)) return null;
	if (request.size_goal.mode !== 'normalized' && request.size_goal.mode !== 'absolute') return null;
	return request;
}

export function withCompressionIntent(
	intent: OperatorIntentRequestPayload,
	compressionIntent: CompressionIntentRequestPayload
): OperatorIntentRequestPayload {
	return {
		...intent,
		schema_version: 2,
		compression_intent: { ...compressionIntent, confirmed: true }
	};
}

export function reviewAdjustmentIntent(
	currentIntent: OperatorIntentRequestPayload,
	adjustment: ReviewSizeAdjustment
): OperatorIntentRequestPayload {
	const nextIntent = {
		...currentIntent,
		size_goal: { ...adjustment.goal.operatorIntent.size_goal },
		resolution: { ...currentIntent.resolution },
		...(currentIntent.quality ? { quality: { ...currentIntent.quality } } : {}),
		...(currentIntent.streams
			? {
					streams: {
						...(currentIntent.streams.audio ? { audio: { ...currentIntent.streams.audio } } : {}),
						...(currentIntent.streams.subtitle
							? { subtitle: { ...currentIntent.streams.subtitle } }
							: {})
					}
				}
			: {})
	};
	delete nextIntent.quality_risk_tags;
	delete nextIntent.quality_risk_details;
	delete nextIntent.evidence_authority;
	return withCompressionIntent(nextIntent, adjustment.compressionIntent);
}

export function reviewSizeAdjustment(
	goals: readonly SizeGoal[],
	compressionOptions: readonly CompressionIntentOptionPayload[],
	direction: ReviewSizeAdjustmentDirection,
	currentTargetBytes = 0,
	currentResultBytes = 0
): ReviewSizeAdjustment | null {
	const goalKey = direction === 'smaller' ? 'smaller' : 'roomier';
	const compressionLevel = direction === 'smaller' ? 'perceptual_floor' : 'reference';
	const goal = goals.find((candidate) => candidate.key === goalKey);
	const compressionOption = compressionOptions.find(
		(candidate) => candidate.key === compressionLevel
	);
	if (!goal || !compressionOption) return null;
	if (
		currentTargetBytes > 0 &&
		((direction === 'smaller' && goal.targetSizeBytes >= currentTargetBytes) ||
			(direction === 'higher_quality' && goal.targetSizeBytes <= currentTargetBytes))
	)
		return null;
	if (
		direction === 'smaller' &&
		currentResultBytes > 0 &&
		goal.targetSizeBytes >= currentResultBytes
	)
		return null;
	return {
		direction,
		goal,
		compressionIntent: compressionOption.compression_intent
	};
}

export interface SampleCoverageView {
	summary: string;
	hasUntestedGroups: boolean;
}

/**
 * Coverage recorded when the reviewed sample was chosen. The folder's current selection is not used:
 * once production starts it describes the files still left, not the file under review.
 */
export function reviewedSampleCoverage(folder: FolderPayload): SampleCoverageView | null {
	const calibration = record(folder.calibration);
	const selection = record(record(calibration.sample_item).representative_selection);
	const summary = text(selection.coverage_summary);
	if (!summary) return null;
	return {
		summary,
		hasUntestedGroups: records(record(selection.coverage).untested_groups).length > 0
	};
}

export function calibrationAcceptsUnderTargetResult(folder: FolderPayload): boolean | null {
	const calibration = record(folder.calibration);
	const sampleItem = record(calibration.sample_item);
	const compressionIntent = record(sampleItem.compression_intent);
	if (compressionIntent.schema_version !== 1 || compressionIntent.confirmed !== true) return null;
	if (typeof compressionIntent.accepts_under_target_result !== 'boolean') return null;
	return compressionIntent.accepts_under_target_result;
}

export function technicalVideoPolicy(folder: FolderPayload): Record<string, unknown> {
	const pendingProposal = record(folder.pending_proposal);
	const previewPolicy = record(pendingProposal.preview_policy);
	const previewVideo = record(previewPolicy.video);
	if (Object.keys(previewVideo).length > 0) return previewVideo;

	const calibrationJob = record(folder.calibration_job);
	const jobPolicy = record(calibrationJob.policy);
	if (isActiveJob(calibrationJob) && Object.keys(jobPolicy).length > 0)
		return record(jobPolicy.video);

	const policy = record(folder.policy ?? folder.summary?.resolved_policy);
	return record(policy.video);
}

export function goalRequest(goal: SizeGoal): string {
	const size = goal.operatorIntent.size_goal;
	const resolution =
		goal.operatorIntent.resolution.mode === 'source'
			? 'Keep the current resolution.'
			: `Limit the encoded height to ${goal.operatorIntent.resolution.max_height}p.`;
	if (goal.mode === 'normalized') {
		return `Use the ${size.value_mb} MB / ${size.reference_runtime_minutes} minute runtime-normalized goal, which resolves to about ${goal.megabytesPerEpisode} MB for this episode. ${resolution} Make a representative test so I can judge the picture and sound.`;
	}
	return `Use an absolute ${size.value_mb} MB per-episode target. ${resolution} Make a representative test so I can judge the picture and sound.`;
}

export function measuredFollowupRequest(
	analysis: SizeTargetAnalysis,
	acceptsUnderTarget = false
): string {
	const targetMegabytes = Math.round(analysis.budgetBytes / 1_000_000);
	if (targetMegabytes <= 0) return '';
	if (analysis.status === 'over_target' && analysis.predictedBytes > 0) {
		return `Measured follow-up: keep the ${targetMegabytes} MB per episode goal. The last representative test was ${analysis.predictedToBudgetRatio.toFixed(1)} times over that goal. Keep the current resolution and make the next representative test materially smaller toward the goal while preserving as much picture and sound quality as possible.`;
	}
	if (analysis.status === 'under_target' && analysis.predictedBytes > 0) {
		if (acceptsUnderTarget) {
			return `Measured follow-up: keep the ${targetMegabytes} MB per episode goal. The last representative test landed below that goal, which is acceptable for this compression goal. Preserve the smaller result unless the operator's picture or sound review requires another measured change; do not spend unused size automatically.`;
		}
		return `Measured follow-up: keep the ${targetMegabytes} MB per episode goal. The last representative test landed below that goal. Keep the current resolution and make the next representative test spend more of the available size on picture and sound quality.`;
	}
	if (analysis.status === 'inside_target_band' && analysis.predictedBytes > 0) {
		return `Measured revision: keep the ${targetMegabytes} MB per episode goal and current resolution. Make another representative test that addresses the operator's picture or sound concerns without changing the size target.`;
	}
	return `Aim for about ${targetMegabytes} MB per episode. Keep the current resolution and repeat the representative test because the previous run did not produce a usable full-episode size estimate.`;
}

export function approvalGuardFromMessage(
	message: string,
	confirmedHighImpact = false,
	confirmedSizeTradeoff = false
): ApprovalGuard | null {
	const normalized = message.trim().toLowerCase();
	if (normalized.includes('high-impact')) {
		return {
			kind: 'high_impact',
			title: 'This sample changed important picture settings.',
			detail:
				'The comparison you watched reflects those changes. Keep it only if the picture and sound still feel right.',
			confirmHighImpact: true,
			confirmSizeTradeoff: confirmedSizeTradeoff
		};
	}
	if (
		normalized.includes('target band') ||
		normalized.includes('predicted total size') ||
		normalized.includes('size tradeoff') ||
		normalized.includes('quality tradeoff')
	) {
		return {
			kind: 'size_tradeoff',
			title: 'This sample missed your size goal.',
			detail:
				'The result may be larger or smaller than the goal you chose. The expected size shown on this page is the result you would accept.',
			confirmHighImpact: confirmedHighImpact,
			confirmSizeTradeoff: true
		};
	}
	return null;
}

export interface WaitingReasons {
	needsYou: EncodeUnfinishedGroup[];
	waiting: EncodeUnfinishedGroup[];
	/** Files the owner already answered for, such as keeping an original. */
	yourChoices: EncodeUnfinishedGroup[];
	fileCount: number;
}

/** Every reason a folder's files are not finished: what needs the owner, what is only waiting, and what they chose. */
export function encodeWaitingReasons(job: EncodeQueueJob | null | undefined): WaitingReasons {
	const groups = (job?.progress?.unfinished_breakdown ?? []).filter(
		(group) => group.count > 0 && group.label.trim()
	);
	const open = groups.filter((group) => !group.owner_choice);
	const needsOwner = (group: EncodeUnfinishedGroup) =>
		group.needs_owner ?? !(group.reason === 'retrying' || group.reason.startsWith('waiting'));
	return {
		needsYou: open.filter(needsOwner),
		waiting: open.filter((group) => !needsOwner(group)),
		yourChoices: groups.filter((group) => group.owner_choice),
		fileCount: groups.reduce((sum, group) => sum + group.count, 0)
	};
}

/** Names the job a reason list belongs to when it is not the page's own scope. */
export function waitingScopeName(jobPrefix: string): string {
	const identity = seasonIdentity(jobPrefix);
	return identity.showPrefix === jobPrefix.replace(/\/+$/, '')
		? `all of ${identity.show}`
		: `${identity.show} ${identity.season}`;
}

export function predictedEpisodeSize(folder: FolderPayload): number {
	const calibration = record(folder.calibration);
	return numberValue(record(calibration.sample_result).predicted_total_size_bytes);
}

export function folderSizeTargetAnalysis(folder: FolderPayload): SizeTargetAnalysis {
	const analysis = record(folder.size_target_analysis);
	const rawStatus = text(analysis.status);
	const status = [
		'inside_target_band',
		'over_target',
		'under_target',
		'missing_prediction'
	].includes(rawStatus)
		? (rawStatus as SizeTargetAnalysis['status'])
		: '';
	return {
		status,
		budgetBytes: numberValue(analysis.budget_bytes),
		predictedBytes: numberValue(analysis.predicted_total_size_bytes),
		lowerBoundBytes: numberValue(analysis.lower_bound_bytes),
		upperBoundBytes: numberValue(analysis.upper_bound_bytes),
		predictedToBudgetRatio: numberValue(analysis.predicted_to_budget_ratio)
	};
}

export function currentEncodeProgress(job: EncodeQueueJob | null | undefined) {
	const progress = job?.progress;
	return {
		percent: Math.min(100, Math.max(0, numberValue(progress?.percent_complete))),
		completed: numberValue(progress?.completed_item_count),
		total: numberValue(progress?.total_item_count || job?.shard_count || record(job).item_count),
		currentEpisode: episodeLabel(progress?.current_item_rel_path),
		eta: text(progress?.eta_copy),
		// The estimate and, beside it, the episodes it does not count.
		etaDetail: [
			text(progress?.eta_copy),
			text(progress?.eta_copy) ? leftOutItemsCopy(progress) : ''
		]
			.filter(Boolean)
			.join(' · '),
		hosts: Array.isArray(record(progress).active_host_labels)
			? (record(progress).active_host_labels as unknown[]).map(String).filter(Boolean)
			: []
	};
}

export function scopedEncodeProgress(
	job: EncodeQueueJob | null | undefined,
	fallbackCompleted: number,
	fallbackTotal: number
) {
	const progress = currentEncodeProgress(job);
	const usesActiveJob = progress.total > 0;
	const total = usesActiveJob ? progress.total : Math.max(0, fallbackTotal);
	const completed = Math.min(
		total,
		Math.max(0, usesActiveJob ? progress.completed : fallbackCompleted)
	);
	const completedPercent = total > 0 ? (completed / total) * 100 : 0;
	return {
		...progress,
		total,
		completed,
		percent: Math.min(100, Math.max(progress.percent, completedPercent))
	};
}

export type ApprovalStartMode = 'scope' | 'older_seasons';

export interface ApprovalStartPlan {
	mode: ApprovalStartMode;
	episodeCount: number;
	label: string;
	detail: string;
}

/**
 * Approving a sample starts the encode only when exactly one start action exists and it
 * needs no further confirmation. Anything that bypasses a hold keeps its own explicit step, and
 * nothing starts when the queue would take no episode and start no motion-pattern check.
 */
export function approvalStartPlan(input: {
	scope: 'episode' | 'season' | 'series';
	admission: QueueAdmission;
	olderSeasons: { candidateCount: number; overriddenCount: number; seasonCount: number } | null;
}): ApprovalStartPlan | null {
	const { clearedCount, checkCount, protectedCount } = input.admission;
	const startable = clearedCount + checkCount;
	const plan = (mode: ApprovalStartMode, count: number, what: string): ApprovalStartPlan =>
		mode === 'scope' && checkCount > 0
			? {
					mode,
					episodeCount: count,
					label: `Keep, check, and compress ${what}`,
					detail: `Keeping this version starts compressing ${what}. ${checkCount === 1 ? 'An episode that needs' : 'Episodes that need'} a motion-pattern check ${checkCount === 1 ? 'gets it' : 'get it'} first and ${checkCount === 1 ? 'compresses' : 'compress'} once it passes. Originals stay in place until you replace them.`
				}
			: {
					mode,
					episodeCount: count,
					label: `Keep and compress ${what}`,
					detail: `Keeping this version starts compressing ${what} now. Originals stay in place until you replace them.`
				};
	const episodes = (count: number) => `${count} ${count === 1 ? 'episode' : 'episodes'}`;
	if (input.scope === 'episode') {
		if (protectedCount > 0 || startable <= 0) return null;
		return plan('scope', 1, 'this episode');
	}
	if (input.scope === 'season') {
		if (protectedCount > 0 || startable <= 0) return null;
		return plan('scope', startable, `${episodes(startable)} in this season`);
	}
	const older = input.olderSeasons;
	if (older && older.overriddenCount > 0) return null;
	if (startable > 0) {
		if (older && older.candidateCount !== clearedCount) return null;
		return plan('scope', startable, episodes(startable));
	}
	if (older && older.candidateCount > 0) {
		return plan(
			'older_seasons',
			older.candidateCount,
			`${episodes(older.candidateCount)} in ${older.seasonCount} older ${older.seasonCount === 1 ? 'season' : 'seasons'}`
		);
	}
	return null;
}

export interface ProductionScope {
	episodeCount: number;
	currentSizeBytes: number;
}

/**
 * What compressing this scope makes, from the selection queueing uses, so a season leaves out the
 * episodes already published. A show keeps its whole size, which its page labels as such; one
 * episode is itself.
 */
export function productionScope(folder: FolderPayload): ProductionScope {
	const wholeSize = folder.summary?.total_size_bytes ?? 0;
	if (folder.media_scope?.match === 'exact_item') {
		return { episodeCount: folder.summary?.item_count ?? 0, currentSizeBytes: wholeSize };
	}
	const episodeCount =
		folder.lifecycle?.eligible_candidate_count ?? folder.encode_candidate_count ?? 0;
	if (isSeriesPrefix(folder.prefix)) return { episodeCount, currentSizeBytes: wholeSize };
	return { episodeCount, currentSizeBytes: folder.lifecycle?.eligible_size_bytes ?? wholeSize };
}

/** What the queue action takes for a scope, split the way its motion-pattern gate splits it. */
export interface QueueAdmission {
	/** Episodes compressing takes now. */
	clearedCount: number;
	/** Episodes whose motion-pattern check compressing starts; each compresses once it passes. */
	checkCount: number;
	/** Protected episodes an override of the lifecycle hold would take or check. */
	protectedCount: number;
	/** Of those, the ones the override starts a motion-pattern check for instead of compressing now. */
	protectedCheckCount: number;
	/** Episodes that stay original for their motion pattern, protected or not. */
	motionHeldCount: number;
	/** Of those, how many the show's encode-as-is decision could accept. */
	asIsCount: number;
}

/** The scope's queue admission, from the lifecycle counts the server reads from the queue's own gate. */
export function queueAdmission(folder: FolderPayload): QueueAdmission {
	const lifecycle = folder.lifecycle;
	if (!lifecycle) {
		return {
			clearedCount: folder.encode_candidate_count ?? 0,
			checkCount: 0,
			protectedCount: 0,
			protectedCheckCount: 0,
			motionHeldCount: 0,
			asIsCount: 0
		};
	}
	const protectedMotionHeld = lifecycle.protected_motion_held_candidate_count ?? 0;
	return {
		clearedCount: lifecycle.eligible_candidate_count,
		checkCount: lifecycle.motion_check_candidate_count ?? 0,
		protectedCount: Math.max(0, lifecycle.held_candidate_count - protectedMotionHeld),
		protectedCheckCount: lifecycle.protected_motion_check_candidate_count ?? 0,
		motionHeldCount: (lifecycle.motion_held_candidate_count ?? 0) + protectedMotionHeld,
		asIsCount: lifecycle.motion_as_is_candidate_count ?? 0
	};
}

export interface MotionHolds {
	/** Episodes with a measured motion pattern Mediaforce cannot convert safely; they stay original. */
	heldFiles: string[];
	/** Episodes waiting for a motion-pattern check, which compressing the scope starts. */
	checkFiles: string[];
	/** How many held episodes the show's encode-as-is decision could accept. */
	asIsCount: number;
	/** The held episodes with their ids and reasons, which the owner can keep original. */
	heldItems: MotionHeldFile[];
	/** The episodes the owner kept original, which the owner can undo. */
	keptItems: MotionKeptFile[];
}

/** The episodes queueing leaves out of this scope for their motion pattern. */
export function motionHolds(folder: FolderPayload): MotionHolds {
	const files = folder.motion_hold_files;
	return {
		heldFiles: files?.held_files ?? [],
		checkFiles: files?.check_files ?? [],
		asIsCount: files?.as_is_count ?? 0,
		heldItems: files?.held ?? [],
		keptItems: files?.kept ?? []
	};
}

/** Whether a page lists any episode to keep original or to undo; it does in every page state. */
export function showsMotionKeepFiles(holds: MotionHolds): boolean {
	return holds.heldItems.length > 0 || holds.keptItems.length > 0;
}

const MOTION_HOLD_REASON_LABELS: Record<MotionHoldReason, string> = {
	mixed: 'Mixed motion pattern',
	partly_interlaced: 'Partly interlaced',
	unclear: 'Unclear motion pattern'
};

/** Why an episode is held for its motion pattern, in a few plain words. */
export function motionHoldReasonLabel(reason: MotionHoldReason | string): string {
	return MOTION_HOLD_REASON_LABELS[reason as MotionHoldReason] ?? MOTION_HOLD_REASON_LABELS.unclear;
}

/** What the finished page says about its episodes, counting the ones the owner kept original. */
export function finishedEpisodesNote(episodeCount: number, keptCount: number): string {
	if (keptCount <= 0) {
		return `All ${episodeCount} smaller episodes are in your library. The originals remain in the cleanup folder until you choose to delete them.`;
	}
	const published = Math.max(0, episodeCount - keptCount);
	const kept = `${episodeCountLabel(keptCount)} ${keptCount === 1 ? 'stays' : 'stay'} original by your choice.`;
	if (published === 0) return kept;
	const smaller =
		published === 1
			? '1 smaller episode is in your library, and its original remains in the cleanup folder.'
			: `${published} smaller episodes are in your library, and their originals remain in the cleanup folder.`;
	return `${smaller} ${kept}`;
}

function episodeCountLabel(count: number): string {
	return `${count} ${count === 1 ? 'episode' : 'episodes'}`;
}

/**
 * What the ready card adds about the episodes compressing leaves out for their motion pattern. The show
 * page shows its encode-as-is decision itself, so only a season or episode points to it.
 */
export function motionHoldNotes(holds: MotionHolds, onShowPage = false): string[] {
	const notes: string[] = [];
	const held = holds.heldFiles.length;
	const check = holds.checkFiles.length;
	if (held > 0) {
		notes.push(
			`${episodeCountLabel(held)} ${held === 1 ? 'stays' : 'stay'} original because Mediaforce can’t convert ${held === 1 ? 'its' : 'their'} motion pattern safely.`
		);
	}
	if (holds.asIsCount > 0 && !onShowPage) {
		notes.push(
			`The show page can compress ${holds.asIsCount === held ? (held === 1 ? 'it' : 'them') : holds.asIsCount} as-is.`
		);
	}
	if (check > 0) {
		notes.push(
			`${episodeCountLabel(check)} ${check === 1 ? 'needs' : 'need'} a motion-pattern check first. Compressing starts the check, and ${check === 1 ? 'it compresses' : 'each one compresses'} once it passes.`
		);
	}
	return notes;
}
