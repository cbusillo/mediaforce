import type {
	DashboardSummaryPayload,
	EncodeQueueJob,
	FolderCard,
	FolderPayload,
	FolderStatusPayload,
	OperatorIntentRequestPayload,
	QualityRiskPayload,
	QualityRiskTag,
	SizeGoalMode
} from '$lib/api/types';

export type HumanSeasonStateKey =
	| 'needs_test'
	| 'making_test'
	| 'ready_to_compare'
	| 'ready_to_make'
	| 'making_season'
	| 'ready_to_check'
	| 'ready_to_finish'
	| 'finished'
	| 'needs_help';

export type HumanSeasonTone = 'quiet' | 'active' | 'ready' | 'success' | 'attention';

export interface HumanSeasonState {
	key: HumanSeasonStateKey;
	label: string;
	detail: string;
	tone: HumanSeasonTone;
	recoveryKind?: 'test' | 'season';
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

export interface SizeTargetAnalysis {
	status: 'inside_target_band' | 'over_target' | 'under_target' | 'missing_prediction' | '';
	budgetBytes: number;
	predictedBytes: number;
	lowerBoundBytes: number;
	upperBoundBytes: number;
	predictedToBudgetRatio: number;
}

export interface ReviewClip {
	path: string;
	timestampSeconds: number;
	durationSeconds: number;
	sizeBytes: number;
}

export interface ReviewPair {
	source: ReviewClip;
	preview: ReviewClip;
	comparePath: string;
}

export interface CompareRiskSummary {
	verdict: string;
	blocked: boolean;
	tone: HumanSeasonTone;
	title: string;
	detail: string;
	topRisk: string;
	topRiskLevel: string;
	topRiskDetail: string;
	authority: string;
	authorityDetail: string;
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
	kind: 'arithmetic_infeasible' | 'quality_conflict';
	title: string;
	detail: string;
	recoveryLabel: string;
}

export interface ReviewConcern {
	tag: QualityRiskTag;
	label: string;
}

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
const FAILURE_JOB_STATUSES = new Set(['failed', 'stopped', 'needs_attention']);

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
	if (normalized === 'blocked') return 'Blocked';
	if (normalized === 'request_comparison') return 'Request comparison';
	if (normalized === 'needs_operator_review') return 'Needs operator review';
	if (normalized === 'safe_to_sample') return 'Safe to sample';
	return 'Review evidence';
}

function riskTone(risk: QualityRiskPayload | null): HumanSeasonTone {
	if (!risk) return 'quiet';
	if (risk.blocked) return 'attention';
	if (risk.request_comparison) return 'ready';
	return 'quiet';
}

function riskAuthority(risk: QualityRiskPayload | null): { value: string; detail: string } {
	const decision = record(risk?.operator_decision);
	const status = text(decision.status).toLowerCase();
	if (status === 'rejected') {
		return {
			value: 'Current rejection',
			detail: 'The latest matching rejection outranks any older approval.'
		};
	}
	if (status === 'approved') {
		return {
			value: 'Current approval',
			detail: 'The current sample, source, and policy hash have an authoritative approval.'
		};
	}
	return {
		value: 'No current decision',
		detail: 'Older, stale, or sibling evidence is not treated as authority here.'
	};
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
	return {
		label: risk.label,
		level: `${risk.level} risk`,
		detail: risk.rationale
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
	const picture = compareRiskFact(
		typedRisks,
		(tag) => tag !== 'audio_quality_layout',
		'No specific picture warning',
		'Judge detail, motion, dark scenes, grain, and cadence in the selected moments.'
	);
	const sound = compareRiskFact(
		typedRisks,
		(tag) => tag === 'audio_quality_layout',
		'No specific sound warning',
		'Listen for clarity, channel layout, balance, and anything distracting.'
	);
	const focusMoments = (risk.pre_test_instruction?.moments ?? []).slice(0, 3).map((moment) => {
		const labels = Array.isArray(moment.risk_tags) ? moment.risk_tags.join(', ') : 'review risk';
		return `Moment ${moment.moment} · ${labels}`;
	});
	return {
		verdict: riskVerdictCopy(risk.verdict),
		blocked: Boolean(risk.blocked),
		tone: riskTone(risk),
		title: topRisk ? topRisk.label : riskVerdictCopy(risk.verdict),
		detail:
			(risk.blocking_reasons && risk.blocking_reasons[0]) ||
			risk.comparison_reason ||
			text(risk.interpretation?.summary) ||
			'Measured evidence and review authority define the current risk state.',
		topRisk: topRisk?.label ?? 'No elevated typed risk',
		topRiskLevel: topRisk?.level ? `${topRisk.level} risk` : 'unknown risk',
		topRiskDetail: topRisk?.rationale ?? 'No typed review focus was published for this sample.',
		authority: authority.value,
		authorityDetail: authority.detail,
		focusMoments,
		picture,
		sound
	};
}

function booleanValue(value: unknown): boolean {
	return value === true;
}

function jobStatus(job: unknown): string {
	return text(record(job).status).toLowerCase();
}

function isActiveJob(job: unknown): boolean {
	return ACTIVE_JOB_STATUSES.has(jobStatus(job));
}

function isFailedJob(job: unknown): boolean {
	return FAILURE_JOB_STATUSES.has(jobStatus(job));
}

function matchesPrefix(job: Record<string, unknown>, prefix: string): boolean {
	return text(job.prefix) === prefix;
}

function dashboardJobs(dashboard: DashboardSummaryPayload, kind: 'sample' | 'encode') {
	if (kind === 'sample') {
		const lane = dashboard.calibration_queue.sample;
		return {
			running: records(lane.running),
			queued: records(lane.queued),
			ready: records(lane.pending_review),
			recent: records(lane.recent_failed)
		};
	}
	return {
		running: dashboard.encode_queue.running.map(record),
		queued: dashboard.encode_queue.queued.map(record),
		ready: [] as Array<Record<string, unknown>>,
		recent: (dashboard.encode_queue.recent ?? []).map(record)
	};
}

export function seasonIdentity(prefix: string): SeasonIdentity {
	const segments = prefix.split('/').filter(Boolean);
	const library = segments[0] ?? '';
	const last = segments.at(-1) ?? prefix;
	const hasSeasonSuffix = /^(season\b|specials?$)/i.test(last);
	const showSegments = hasSeasonSuffix ? segments.slice(1, -1) : segments.slice(1);
	const show = showSegments.join(' / ') || last || 'Unknown show';
	const season = hasSeasonSuffix ? last : 'Season';
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

export function folderHref(prefix: string): `/folders/${string}` {
	return `/folders/${prefix
		.split('/')
		.map((segment) => encodeURIComponent(segment))
		.join('/')}` as `/folders/${string}`;
}

export function formatFileSize(bytes: number | null | undefined): string {
	const value = numberValue(bytes);
	if (value <= 0) return 'Size not available';
	const units = [
		{ threshold: 1024 ** 3, suffix: 'GB' },
		{ threshold: 1024 ** 2, suffix: 'MB' },
		{ threshold: 1024, suffix: 'KB' }
	];
	for (const unit of units) {
		if (value >= unit.threshold) {
			const scaled = value / unit.threshold;
			const digits = scaled >= 10 ? 0 : 1;
			return `${scaled.toFixed(digits)} ${unit.suffix}`;
		}
	}
	return `${Math.round(value)} bytes`;
}

export function formatDecimalFileSize(bytes: number | null | undefined): string {
	const value = numberValue(bytes);
	if (value <= 0) return '0 MB';
	for (const unit of [
		{ threshold: 1_000_000_000_000, suffix: 'TB' },
		{ threshold: 1_000_000_000, suffix: 'GB' },
		{ threshold: 1_000_000, suffix: 'MB' }
	]) {
		if (value >= unit.threshold) {
			const scaled = value / unit.threshold;
			const digits = Number.isInteger(scaled) ? 0 : scaled >= 10 ? 1 : 2;
			return `${scaled.toFixed(digits)} ${unit.suffix}`;
		}
	}
	return `${Math.round(value / 1_000)} KB`;
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

export function targetConstraintSummary(folder: FolderPayload): TargetConstraintSummary | null {
	const search = folder.quality_risk?.target_size_search;
	const feasibility = folder.stream_budget_ledger?.feasibility;
	if (feasibility?.arithmetic_infeasible || search?.status === 'infeasible') {
		return {
			kind: 'arithmetic_infeasible',
			title: 'This size cannot fit the required streams.',
			detail:
				'Audio, subtitles, attachments, and container overhead leave no positive video budget at this size. Choose a roomier goal before making another test.',
			recoveryLabel: 'Choose a size that can fit'
		};
	}
	if (search?.status === 'quality_conflict') {
		const metric = text(search.selected_metric).toUpperCase();
		const minimum = numberValue(search.minimum_metric_score);
		const floor =
			metric && minimum > 0 ? ` the ${metric} floor of ${minimum}` : ' the quality floor';
		return {
			kind: 'quality_conflict',
			title: 'This size conflicts with the quality floor.',
			detail: `Every measured candidate that fit the size fell below${floor}. Choose a roomier goal instead of silently lowering quality.`,
			recoveryLabel: 'Choose a roomier goal'
		};
	}
	return null;
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
	if (!request || request.schema_version !== 1) return null;
	if (request.size_goal.mode !== 'normalized' && request.size_goal.mode !== 'absolute') return null;
	return request;
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

export function measuredFollowupRequest(analysis: SizeTargetAnalysis): string {
	const targetMegabytes = Math.round(analysis.budgetBytes / 1_000_000);
	if (targetMegabytes <= 0) return '';
	if (analysis.status === 'over_target' && analysis.predictedBytes > 0) {
		return `Measured follow-up: keep the ${targetMegabytes} MB per episode goal. The last representative test was ${analysis.predictedToBudgetRatio.toFixed(1)} times over that goal. Keep the current resolution and make the next representative test materially smaller toward the goal while preserving as much picture and sound quality as possible.`;
	}
	if (analysis.status === 'under_target' && analysis.predictedBytes > 0) {
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
			title: 'This test changed important picture settings.',
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
			title: 'This test missed your size goal.',
			detail:
				'The result may be larger or smaller than the goal you chose. The expected size shown on this page is the result you would accept.',
			confirmHighImpact: confirmedHighImpact,
			confirmSizeTradeoff: true
		};
	}
	return null;
}

export function librarySeasonState(
	card: FolderCard,
	dashboard: DashboardSummaryPayload
): HumanSeasonState {
	const sampleJobs = dashboardJobs(dashboard, 'sample');
	const encodeJobs = dashboardJobs(dashboard, 'encode');
	if (
		[...encodeJobs.running, ...encodeJobs.queued].some((job) => matchesPrefix(job, card.prefix))
	) {
		return {
			key: 'making_season',
			label: 'Making the season',
			detail: 'The smaller episodes are being made now.',
			tone: 'active'
		};
	}
	if (
		[...sampleJobs.running, ...sampleJobs.queued].some((job) => matchesPrefix(job, card.prefix))
	) {
		return {
			key: 'making_test',
			label: 'Making a test',
			detail: 'One episode is being tested now.',
			tone: 'active'
		};
	}
	if (sampleJobs.ready.some((job) => matchesPrefix(job, card.prefix))) {
		return {
			key: 'ready_to_compare',
			label: 'Test ready',
			detail: 'Compare the original and new version.',
			tone: 'ready'
		};
	}

	const promoted = numberValue(card.statuses.promoted);
	if (
		card.workflow_state?.primary_lane === 'complete' ||
		(card.item_count > 0 && promoted >= card.item_count)
	) {
		return {
			key: 'finished',
			label: 'Finished',
			detail: 'Every episode is in place.',
			tone: 'success'
		};
	}
	if (card.workflow_state?.primary_lane === 'attention') {
		return {
			key: 'needs_help',
			label: 'Needs a little help',
			detail: 'Open the season for one clear recovery step.',
			tone: 'attention',
			recoveryKind: 'season'
		};
	}
	if (card.workflow_state?.primary_lane === 'validate') {
		return {
			key: 'ready_to_check',
			label: 'Ready to check',
			detail: 'The new episodes are ready for a safety check.',
			tone: 'ready'
		};
	}
	if (card.workflow_state?.primary_lane === 'promote') {
		return {
			key: 'ready_to_finish',
			label: 'Ready to finish',
			detail: 'The smaller episodes passed their checks.',
			tone: 'ready'
		};
	}
	if (card.workflow_state?.primary_lane === 'processing') {
		return {
			key: 'making_season',
			label: 'Making the season',
			detail: 'The smaller episodes are being made now.',
			tone: 'active'
		};
	}

	const badge = text(card.review_badge_label).toLowerCase();
	if (badge.includes('ready to review')) {
		return {
			key: 'ready_to_compare',
			label: 'Test ready',
			detail: 'Compare the original and new version.',
			tone: 'ready'
		};
	}
	if (badge.includes('approved')) {
		return {
			key: 'ready_to_make',
			label: 'Test approved',
			detail: 'The rest of the season can be made.',
			tone: 'ready'
		};
	}
	if (badge.includes('validate')) {
		return {
			key: 'ready_to_check',
			label: 'Ready to check',
			detail: 'The new episodes are ready for a safety check.',
			tone: 'ready'
		};
	}
	if (badge.includes('rerun') || badge.includes('attention')) {
		return {
			key: 'needs_help',
			label: 'Needs a little help',
			detail: 'Open the season for one clear recovery step.',
			tone: 'attention',
			recoveryKind: badge.includes('rerun') ? 'test' : 'season'
		};
	}
	return {
		key: 'needs_test',
		label: 'Make a test',
		detail: 'Choose a size, then compare one episode first.',
		tone: 'quiet'
	};
}

export function detailSeasonState(
	folder: FolderPayload,
	status: FolderStatusPayload
): HumanSeasonState {
	const encodeJob = folder.encode_job;
	const workflow = status.workflow_state ?? folder.workflow_state;
	const sampleJob = status.calibration_job ?? folder.calibration_job;
	const retryableSample = status.retryable_sample_job;
	const calibration = record(folder.calibration);
	const reviewGate = record(folder.review_gate);
	const draftHash = text(calibration.draft_hash);
	const acceptedHash = text(calibration.accepted_draft_hash);
	const priorTestJobId = text(calibration.job_id);
	const reviewGateStatus = text(reviewGate.status);

	if (isActiveJob(encodeJob) || workflow?.primary_lane === 'processing') {
		return {
			key: 'making_season',
			label: 'Making the season',
			detail: 'The smaller episodes are being made now.',
			tone: 'active'
		};
	}
	if (isFailedJob(encodeJob) || workflow?.primary_lane === 'attention') {
		return {
			key: 'needs_help',
			label: 'The season stopped',
			detail: 'Completed episodes are safe. Try the unfinished work again.',
			tone: 'attention',
			recoveryKind: 'season'
		};
	}
	if (workflow?.primary_lane === 'validate') {
		return {
			key: 'ready_to_check',
			label: 'Ready to check',
			detail: 'The new episodes are ready for a safety check.',
			tone: 'ready'
		};
	}
	if (workflow?.primary_lane === 'promote') {
		return {
			key: 'ready_to_finish',
			label: 'Ready to finish',
			detail: 'The smaller episodes passed their checks.',
			tone: 'ready'
		};
	}
	if (workflow?.primary_lane === 'complete' || workflow?.state === 'complete') {
		return {
			key: 'finished',
			label: 'Finished',
			detail: 'Every episode is in place.',
			tone: 'success'
		};
	}
	if (isActiveJob(sampleJob) || ['queued', 'running'].includes(status.calibration_status)) {
		return {
			key: 'making_test',
			label: 'Making a test',
			detail: 'One representative episode is being tested now.',
			tone: 'active'
		};
	}
	if (retryableSample || isFailedJob(sampleJob)) {
		return {
			key: 'needs_help',
			label: 'The test stopped',
			detail: 'Nothing was replaced. You can try the same test again.',
			tone: 'attention',
			recoveryKind: 'test'
		};
	}
	const reviewReady =
		booleanValue(calibration.browser_review_ready) || booleanValue(calibration.review_media_ready);
	const currentRisk = qualityRisk(folder);
	const currentRiskStatus = text(record(currentRisk?.operator_decision).status).toLowerCase();
	const currentRiskBlocksApproval =
		Boolean(currentRisk?.blocked) ||
		['rejected', 'needs_operator_review'].includes(currentRiskStatus);
	const currentDraftIsApproved =
		!currentRiskBlocksApproval &&
		(booleanValue(reviewGate.can_confirm_full) || (draftHash && draftHash === acceptedHash));
	if (
		reviewGateStatus === 'missing_review_media' ||
		(priorTestJobId && !draftHash && !reviewReady)
	) {
		return {
			key: 'needs_help',
			label: 'The test needs another try',
			detail: 'The previous test ended before the comparison was ready.',
			tone: 'attention',
			recoveryKind: 'test'
		};
	}
	if (reviewReady && draftHash && !currentDraftIsApproved) {
		return {
			key: 'ready_to_compare',
			label: 'Test ready',
			detail: 'Compare the original and new version.',
			tone: 'ready'
		};
	}
	if (currentDraftIsApproved) {
		return {
			key: 'ready_to_make',
			label: 'Test approved',
			detail: 'The rest of the season can be made.',
			tone: 'ready'
		};
	}
	return {
		key: 'needs_test',
		label: 'Make a test',
		detail: 'Choose a size, then compare one episode first.',
		tone: 'quiet'
	};
}

export function normalizeReviewPairs(folder: FolderPayload): ReviewPair[] {
	const calibration = record(folder.calibration);
	return records(calibration.review_pairs)
		.map((pair) => {
			const source = record(pair.source_clip);
			const preview = record(pair.preview_clip);
			const compare = record(pair.compare_clip);
			return {
				source: {
					path: text(source.path),
					timestampSeconds: numberValue(source.timestamp_seconds),
					durationSeconds: numberValue(source.duration_seconds),
					sizeBytes: numberValue(source.size_bytes)
				},
				preview: {
					path: text(preview.path),
					timestampSeconds: numberValue(preview.timestamp_seconds),
					durationSeconds: numberValue(preview.duration_seconds),
					sizeBytes: numberValue(preview.size_bytes)
				},
				comparePath: text(compare.path)
			};
		})
		.filter((pair) => pair.source.path && pair.preview.path);
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

export function reviewSampleSizes(folder: FolderPayload): {
	original: number;
	smaller: number;
	durationSeconds: number;
	ratioPercent: number;
} {
	const totals = normalizeReviewPairs(folder).reduce(
		(total, pair) => ({
			original: total.original + pair.source.sizeBytes,
			smaller: total.smaller + pair.preview.sizeBytes,
			durationSeconds: total.durationSeconds + pair.source.durationSeconds
		}),
		{ original: 0, smaller: 0, durationSeconds: 0 }
	);
	return {
		...totals,
		ratioPercent: totals.original > 0 ? Math.round((totals.smaller / totals.original) * 100) : 0
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
		hosts: Array.isArray(record(progress).active_host_labels)
			? (record(progress).active_host_labels as unknown[]).map(String).filter(Boolean)
			: []
	};
}

export function plainFailureMessage(folder: FolderPayload, status: FolderStatusPayload): string {
	const retryable = record(status.retryable_sample_job);
	const sampleJob = record(status.calibration_job ?? folder.calibration_job);
	const encodeJob = record(folder.encode_job);
	const calibration = record(folder.calibration);
	const reviewGate = record(folder.review_gate);
	const raw = text(retryable.error) || text(sampleJob.error) || text(encodeJob.error);
	const normalized = raw.toLowerCase();
	if (
		text(reviewGate.status) === 'missing_review_media' ||
		(text(calibration.job_id) && !text(calibration.draft_hash))
	) {
		return 'The previous test ended before the comparison was ready. Nothing was replaced.';
	}
	if (!raw) return 'The work stopped before it finished. Your completed files are still safe.';
	if (normalized.includes('restart') || normalized.includes('interrupted')) {
		return 'The work stopped when Mediaforce restarted. Your completed files are still safe.';
	}
	if (normalized.includes('permission') || normalized.includes('denied')) {
		return 'The selected computer could not reach one of the files. Check its connection, then try again.';
	}
	if (normalized.includes('suitable crf') || normalized.includes('quality target')) {
		return 'That size goal was too small for this episode. Try a roomier goal.';
	}
	if (normalized.includes('stopped')) {
		return 'The work was stopped before it finished. Nothing was replaced.';
	}
	return 'The work stopped before it finished. Nothing was replaced, and you can try again.';
}

export function activeSeasonCards(
	cards: FolderCard[],
	dashboard: DashboardSummaryPayload
): Array<{ card: FolderCard; state: HumanSeasonState }> {
	return cards
		.map((card) => ({ card, state: librarySeasonState(card, dashboard) }))
		.filter(({ state }) =>
			[
				'making_test',
				'ready_to_compare',
				'ready_to_make',
				'making_season',
				'ready_to_check',
				'ready_to_finish',
				'needs_help'
			].includes(state.key)
		)
		.sort((left, right) => {
			const order: Record<HumanSeasonStateKey, number> = {
				making_test: 0,
				making_season: 1,
				ready_to_compare: 2,
				needs_help: 3,
				ready_to_make: 4,
				ready_to_check: 5,
				ready_to_finish: 6,
				finished: 7,
				needs_test: 8
			};
			return order[left.state.key] - order[right.state.key];
		});
}
