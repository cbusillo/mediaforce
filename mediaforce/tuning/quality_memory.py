from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import math
from pathlib import PurePosixPath
from statistics import median, quantiles
from typing import Any, Literal

from sqlalchemy import func, select

from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import item_events, library_items, staged_artifacts
from mediaforce.core.evidence import stable_json_hash
from mediaforce.library.media_scopes import MediaScope
from mediaforce.tuning.compression_intent import LEGACY_COMPRESSION_INTENT_ID

QUALITY_MEMORY_SIGNATURE_VERSION = 2
QUALITY_MEMORY_MAX_AGE_DAYS = 180
MAX_HINT_IQR = 4.0
MAX_HINT_MAD = 2.0
TARGET_VIDEO_BITRATE_BUCKET = 1_000

QualityMemoryScope = Literal["item", "season", "series"]
QualityMemoryConfidence = Literal["none", "limited", "moderate", "high"]
QualitySearchObjective = Literal["quality", "target_size"]

_ALLOWED_ORIGINS = frozenset({"cli", "queue"})
_ENCODER_PARAMETER_OPTION = {
    "libaom-av1": "-aom-params",
    "librav1e": "-rav1e-params",
    "libsvtav1": "-svtav1-params",
    "libx264": "-x264-params",
    "libx265": "-x265-params",
}
_MIN_HINT_SAMPLES: dict[QualityMemoryScope, int] = {
    "item": 4,
    "season": 4,
    "series": 6,
}


@dataclass(frozen=True, slots=True)
class QualitySearchContext:
    metric: str
    target: float
    minimum_quality_score: float
    search_objective: QualitySearchObjective
    size_target_bytes: int | None
    target_video_bitrate: int | None
    source_codec: str
    output_width: int
    output_height: int
    encoder: str
    pixel_format: str
    preset: str
    encoder_parameters: str | None
    video_filter: str | None
    output_container: str
    compression_intent_id: str = LEGACY_COMPRESSION_INTENT_ID

    def __post_init__(self) -> None:
        target = _strict_number(self.target)
        if target is None or target < 0:
            raise ValueError("Quality-search target must be a finite non-negative number")
        minimum_quality_score = _strict_number(self.minimum_quality_score)
        if minimum_quality_score is None or not 0 <= minimum_quality_score <= target:
            raise ValueError("Minimum quality score must be finite, non-negative, and no greater than the target")
        if self.search_objective not in {"quality", "target_size"}:
            raise ValueError("Quality-search objective must be quality or target_size")
        size_target_bytes = _strict_positive_int(self.size_target_bytes)
        target_video_bitrate = _strict_positive_int(self.target_video_bitrate)
        if self.search_objective == "quality" and (size_target_bytes is not None or target_video_bitrate is not None):
            raise ValueError("Quality-only search cannot include a size target")
        if self.search_objective == "target_size" and (
                size_target_bytes is None or target_video_bitrate is None
        ):
            raise ValueError("Target-size search requires total target bytes and target video bitrate")
        if isinstance(self.output_width, bool) or not isinstance(self.output_width, int) or self.output_width <= 0:
            raise ValueError("Quality-search output width must be a positive integer")
        if isinstance(self.output_height, bool) or not isinstance(self.output_height, int) or self.output_height <= 0:
            raise ValueError("Quality-search output height must be a positive integer")

        object.__setattr__(self, "metric", _required_text(self.metric, "metric").upper())
        object.__setattr__(self, "target", round(target, 3))
        object.__setattr__(self, "minimum_quality_score", round(minimum_quality_score, 3))
        object.__setattr__(self, "size_target_bytes", size_target_bytes)
        object.__setattr__(self, "target_video_bitrate", target_video_bitrate)
        object.__setattr__(self, "source_codec", _required_text(self.source_codec, "source codec").casefold())
        object.__setattr__(self, "encoder", _required_text(self.encoder, "encoder").casefold())
        object.__setattr__(self, "pixel_format", _required_text(self.pixel_format, "pixel format").casefold())
        object.__setattr__(self, "preset", _required_text(self.preset, "preset").casefold())
        object.__setattr__(self, "encoder_parameters", _optional_text(self.encoder_parameters))
        object.__setattr__(self, "video_filter", _optional_text(self.video_filter))
        object.__setattr__(
            self,
            "output_container",
            _required_text(self.output_container, "output container").removeprefix(".").casefold(),
        )
        object.__setattr__(
            self,
            "compression_intent_id",
            _required_text(self.compression_intent_id, "compression intent identity"),
        )

    @property
    def signature_id(self) -> str:
        return f"qms2_{stable_json_hash(self.to_payload())[:32]}"

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": QUALITY_MEMORY_SIGNATURE_VERSION,
            "metric": self.metric,
            "target": self.target,
            "minimum_quality_score": self.minimum_quality_score,
            "search_objective": self.search_objective,
            "size_target_bytes": self.size_target_bytes,
            "target_video_bitrate": self.target_video_bitrate,
            "source_codec": self.source_codec,
            "output_width": self.output_width,
            "output_height": self.output_height,
            "encoder": self.encoder,
            "pixel_format": self.pixel_format,
            "preset": self.preset,
            "encoder_parameters": self.encoder_parameters,
            "video_filter": self.video_filter,
            "output_container": self.output_container,
            "compression_intent_id": self.compression_intent_id,
        }


@dataclass(frozen=True, slots=True)
class QualityMemoryExclusion:
    reason: str
    count: int


@dataclass(frozen=True, slots=True)
class QualityMemoryCohort:
    scope: QualityMemoryScope
    cohort_id: str
    evidence_count: int
    confidence: QualityMemoryConfidence
    median_crf: float | None
    minimum_crf: float | None
    maximum_crf: float | None
    iqr: float | None
    median_absolute_deviation: float | None
    central_crf: float | None
    reason: str


@dataclass(frozen=True, slots=True)
class QualityMemoryResult:
    signature_id: str
    cohorts: tuple[QualityMemoryCohort, ...]
    selected: QualityMemoryCohort | None
    global_metric_evidence_count: int
    exclusions: tuple[QualityMemoryExclusion, ...]
    reason: str

    @property
    def scope(self) -> QualityMemoryScope | None:
        return self.selected.scope if self.selected is not None else None

    @property
    def confidence(self) -> QualityMemoryConfidence:
        return self.selected.confidence if self.selected is not None else "none"

    @property
    def evidence_count(self) -> int:
        return self.selected.evidence_count if self.selected is not None else 0

    @property
    def central_crf(self) -> float | None:
        return self.selected.central_crf if self.selected is not None else None

    @property
    def dispersion(self) -> float | None:
        return self.selected.median_absolute_deviation if self.selected is not None else None


@dataclass(frozen=True, slots=True)
class AcceptedQualityOutcome:
    library_item_id: int
    rel_path: str
    source_fingerprint: str | None
    source_size_bytes: int | None
    source_duration_seconds: float | None
    source_video_codec: str
    chosen_crf: float
    quality_metric: str
    quality_target: float
    quality_score: float
    staging_size_bytes: int | None
    size_ratio: float | None
    encode_duration_seconds: float | None
    encode_completed_at: datetime
    promoted_at: datetime
    context: QualitySearchContext
    manifest_run_id: str | None
    item_index: int | None
    encode_origin: str | None
    encode_job_id: str | None
    encode_worker_id: str | None
    encode_host_key: str | None
    quality_search_run_id: str | None
    target_size_trace: dict[str, Any] | None


def load_quality_memory(
        connection: DBClient,
        *,
        exact_scope: MediaScope,
        season_scope: MediaScope | None,
        series_scope: MediaScope | None,
        expected_context: QualitySearchContext,
        current_source_fingerprint: str | None,
        as_of: datetime,
) -> QualityMemoryResult:
    _validate_scope_chain(exact_scope, season_scope, series_scope)
    current_time = _as_utc(as_of)
    observations, exclusion_counts = accepted_quality_outcomes(
        connection,
        metric=expected_context.metric,
        as_of=current_time,
    )

    global_metric_evidence_count = len(observations)
    matching_context: list[AcceptedQualityOutcome] = []
    for observation in observations:
        if observation.context.signature_id != expected_context.signature_id:
            exclusion_counts["search_signature_changed"] += 1
            continue
        matching_context.append(observation)

    scope_chain: list[tuple[QualityMemoryScope, MediaScope]] = [("item", exact_scope)]
    if season_scope is not None:
        scope_chain.append(("season", season_scope))
    if series_scope is not None:
        scope_chain.append(("series", series_scope))

    cohorts: list[QualityMemoryCohort] = []
    normalized_current_fingerprint = _optional_text(current_source_fingerprint)
    for scope_name, media_scope in scope_chain:
        scoped_observations = [
            observation
            for observation in matching_context
            if media_scope.includes(observation.rel_path)
        ]
        if scope_name == "item":
            matching_fingerprints: list[AcceptedQualityOutcome] = []
            for observation in scoped_observations:
                if normalized_current_fingerprint is None:
                    exclusion_counts["source_fingerprint_unavailable"] += 1
                elif observation.source_fingerprint is None:
                    exclusion_counts["historical_source_fingerprint_missing"] += 1
                elif observation.source_fingerprint != normalized_current_fingerprint:
                    exclusion_counts["source_fingerprint_changed"] += 1
                else:
                    matching_fingerprints.append(observation)
            scoped_observations = matching_fingerprints
        cohorts.append(
            _summarize_cohort(
                scope=scope_name,
                scope_prefix=media_scope.prefix,
                signature_id=expected_context.signature_id,
                observations=scoped_observations,
            )
        )

    selected = next((cohort for cohort in cohorts if cohort.central_crf is not None), None)
    reason = _result_reason(
        selected=selected,
        cohorts=cohorts,
        metric=expected_context.metric,
        global_metric_evidence_count=global_metric_evidence_count,
    )
    exclusions = tuple(
        QualityMemoryExclusion(reason=reason_code, count=count)
        for reason_code, count in sorted(exclusion_counts.items())
        if count > 0
    )
    return QualityMemoryResult(
        signature_id=expected_context.signature_id,
        cohorts=tuple(cohorts),
        selected=selected,
        global_metric_evidence_count=global_metric_evidence_count,
        exclusions=exclusions,
        reason=reason,
    )


def accepted_quality_outcomes(
        connection: DBClient,
        *,
        metric: str | None,
        as_of: datetime,
) -> tuple[list[AcceptedQualityOutcome], Counter[str]]:
    current_time = _as_utc(as_of)
    exclusion_counts: Counter[str] = Counter()
    outcomes: list[AcceptedQualityOutcome] = []
    for row in _quality_memory_rows(connection, metric):
        outcome, exclusion_reason = _accepted_observation(row, as_of=current_time)
        if outcome is None:
            exclusion_counts[exclusion_reason or "invalid_outcome"] += 1
            continue
        outcomes.append(outcome)
    return outcomes, exclusion_counts


def _quality_memory_rows(connection: DBClient, metric: str | None) -> list[Any]:
    completion_events = item_events.alias("quality_memory_completion_events")
    completion_event_json = (
        select(completion_events.c.details_json)
        .where(
            completion_events.c.library_item_id == staged_artifacts.c.library_item_id,
            completion_events.c.event_type == "encoding_completed",
            completion_events.c.created_at <= staged_artifacts.c.promoted_at,
        )
        .order_by(completion_events.c.created_at.desc(), completion_events.c.id.desc())
        .limit(1)
        .scalar_subquery()
        .label("completion_event_json")
    )
    return list(
        connection.execute(
            select(
                staged_artifacts.c.library_item_id,
                staged_artifacts.c.encode_origin,
                staged_artifacts.c.source_rel_path,
                staged_artifacts.c.source_size_bytes,
                staged_artifacts.c.source_duration_seconds,
                staged_artifacts.c.source_fingerprint,
                staged_artifacts.c.source_video_codec,
                staged_artifacts.c.chosen_crf,
                staged_artifacts.c.quality_metric,
                staged_artifacts.c.quality_target,
                staged_artifacts.c.quality_score,
                staged_artifacts.c.staging_size_bytes,
                staged_artifacts.c.size_ratio,
                staged_artifacts.c.encode_duration_seconds,
                staged_artifacts.c.manifest_run_id,
                staged_artifacts.c.item_index,
                staged_artifacts.c.encode_job_id,
                staged_artifacts.c.encode_worker_id,
                staged_artifacts.c.encode_host_key,
                staged_artifacts.c.encode_command_json,
                staged_artifacts.c.validation_json,
                staged_artifacts.c.encode_completed_at,
                staged_artifacts.c.staged_at,
                staged_artifacts.c.validated_at,
                staged_artifacts.c.promoted_at,
                staged_artifacts.c.updated_at.label("artifact_updated_at"),
                completion_event_json,
                library_items.c.rel_path,
                library_items.c.width.label("output_width"),
                library_items.c.height.label("output_height"),
                library_items.c.status,
                library_items.c.content_version_changed_at,
            )
            .select_from(
                staged_artifacts.join(
                    library_items,
                    library_items.c.id == staged_artifacts.c.library_item_id,
                )
            )
            .where(staged_artifacts.c.promoted_at.is_not(None))
            .where(
                func.upper(staged_artifacts.c.quality_metric) == metric.upper()
                if metric is not None
                else True
            )
            .order_by(
                staged_artifacts.c.promoted_at.desc(),
                staged_artifacts.c.library_item_id.asc(),
            )
        ).mappings()
    )


def _accepted_observation(row: Any, *, as_of: datetime) -> tuple[AcceptedQualityOutcome | None, str | None]:
    if str(row["encode_origin"] or "").strip().casefold() not in _ALLOWED_ORIGINS:
        return None, "unsupported_encode_origin"
    if str(row["status"] or "").strip().casefold() != "promoted":
        return None, "library_item_not_promoted"

    encode_completed_at = _parse_timestamp(row["encode_completed_at"])
    staged_at = _parse_timestamp(row["staged_at"])
    validated_at = _parse_timestamp(row["validated_at"])
    promoted_at = _parse_timestamp(row["promoted_at"])
    artifact_updated_at = _parse_timestamp(row["artifact_updated_at"])
    if any(
            value is None
            for value in (encode_completed_at, staged_at, validated_at, promoted_at, artifact_updated_at)
    ):
        return None, "acceptance_timestamp_missing"
    assert encode_completed_at is not None
    assert staged_at is not None
    assert validated_at is not None
    assert promoted_at is not None
    assert artifact_updated_at is not None
    if not encode_completed_at <= staged_at <= validated_at <= promoted_at <= as_of:
        return None, "acceptance_timestamp_order_invalid"
    if artifact_updated_at != promoted_at:
        return None, "artifact_changed_after_acceptance"
    if as_of - promoted_at > timedelta(days=QUALITY_MEMORY_MAX_AGE_DAYS):
        return None, "accepted_outcome_too_old"

    content_changed_at = _parse_timestamp(row["content_version_changed_at"])
    if row["content_version_changed_at"] and content_changed_at is None:
        return None, "content_change_timestamp_invalid"
    if content_changed_at is not None and content_changed_at > promoted_at:
        return None, "content_changed_after_acceptance"

    validation = _json_object(row["validation_json"])
    if validation is None or validation.get("passed") is not True:
        return None, "validation_not_passed"

    chosen_crf = _strict_number(row["chosen_crf"])
    quality_target = _strict_number(row["quality_target"])
    quality_score = _strict_number(row["quality_score"])
    output_width = _strict_positive_int(row["output_width"])
    output_height = _strict_positive_int(row["output_height"])
    if (
            chosen_crf is None
            or not 0 <= chosen_crf <= 63
            or quality_target is None
            or quality_target < 0
            or quality_score is None
            or quality_score < 0
            or output_width is None
            or output_height is None
    ):
        return None, "quality_measurement_invalid"

    rel_path = str(row["source_rel_path"] or row["rel_path"] or "").strip().strip("/")
    source_codec = str(row["source_video_codec"] or "").strip()
    command = _json_string_list(row["encode_command_json"])
    if not rel_path or not source_codec or command is None:
        return None, "encode_context_missing"
    completion_event = _json_object(row["completion_event_json"])
    if completion_event is None:
        return None, "completion_event_missing"
    warm_start = completion_event.get("quality_warm_start")
    if isinstance(warm_start, dict) and str(warm_start.get("status") or "") == "accepted":
        return None, "warm_start_selected"
    event_completed_at = _parse_timestamp(completion_event.get("encode_completed_at"))
    event_crf = _strict_number(completion_event.get("chosen_crf"))
    if (
            event_completed_at != encode_completed_at
            or event_crf is None
            or not math.isclose(event_crf, chosen_crf, rel_tol=0.0, abs_tol=1e-6)
    ):
        return None, "completion_event_mismatch"
    event_rel_path = str(completion_event.get("source_rel_path") or "").strip().strip("/")
    if event_rel_path and event_rel_path != rel_path:
        return None, "completion_event_mismatch"
    objective = _search_objective(
        completion_event,
        source_duration_seconds=row["source_duration_seconds"],
        quality_target=quality_target,
    )
    if objective is None:
        return None, "search_objective_invalid"
    search_objective, minimum_quality_score, size_target_bytes, target_video_bitrate = objective
    if quality_score < minimum_quality_score:
        return None, "quality_floor_not_met"
    try:
        context = quality_search_context_from_command(
            command,
            metric=str(row["quality_metric"] or ""),
            target=quality_target,
            minimum_quality_score=minimum_quality_score,
            search_objective=search_objective,
            size_target_bytes=size_target_bytes,
            target_video_bitrate=target_video_bitrate,
            source_codec=source_codec,
            output_width=output_width,
            output_height=output_height,
            compression_intent_id=str(
                completion_event.get("compression_intent_id") or LEGACY_COMPRESSION_INTENT_ID
            ),
        )
    except ValueError:
        return None, "encode_context_invalid"

    source_size_bytes = _strict_positive_int(row["source_size_bytes"])
    source_duration_seconds = _strict_number(row["source_duration_seconds"])
    staging_size_bytes = _strict_positive_int(row["staging_size_bytes"])
    size_ratio = _strict_number(row["size_ratio"])
    encode_duration_seconds = _strict_number(row["encode_duration_seconds"])
    return AcceptedQualityOutcome(
        library_item_id=int(row["library_item_id"]),
        rel_path=rel_path,
        source_fingerprint=_optional_text(row["source_fingerprint"]),
        source_size_bytes=source_size_bytes,
        source_duration_seconds=source_duration_seconds,
        source_video_codec=source_codec,
        chosen_crf=chosen_crf,
        quality_metric=context.metric,
        quality_target=quality_target,
        quality_score=quality_score,
        staging_size_bytes=staging_size_bytes,
        size_ratio=size_ratio,
        encode_duration_seconds=encode_duration_seconds,
        encode_completed_at=encode_completed_at,
        promoted_at=promoted_at,
        context=context,
        manifest_run_id=_optional_text(row["manifest_run_id"]),
        item_index=_strict_int(row["item_index"]),
        encode_origin=_optional_text(row["encode_origin"]),
        encode_job_id=_optional_text(row["encode_job_id"]),
        encode_worker_id=_optional_text(row["encode_worker_id"]),
        encode_host_key=_optional_text(row["encode_host_key"]),
        quality_search_run_id=_optional_text(completion_event.get("quality_search_run_id")),
        target_size_trace=(
            dict(target_size_trace)
            if isinstance((target_size_trace := completion_event.get("target_size_trace")), dict)
            else None
        ),
    ), None


def quality_search_context_from_command(
        command: list[str],
        *,
        metric: str,
        target: float,
        minimum_quality_score: float,
        search_objective: QualitySearchObjective,
        size_target_bytes: int | None,
        target_video_bitrate: int | None,
        source_codec: str,
        output_width: int,
        output_height: int,
        compression_intent_id: str = LEGACY_COMPRESSION_INTENT_ID,
) -> QualitySearchContext:
    output_container = PurePosixPath(command[-1]).suffix.removeprefix(".") if command else ""
    encoder = _command_option(command, "-c:v", "-codec:v")
    return QualitySearchContext(
        metric=metric,
        target=target,
        minimum_quality_score=minimum_quality_score,
        search_objective=search_objective,
        size_target_bytes=size_target_bytes,
        target_video_bitrate=target_video_bitrate,
        source_codec=source_codec,
        output_width=output_width,
        output_height=output_height,
        encoder=encoder,
        pixel_format=_command_option(command, "-pix_fmt"),
        preset=_command_option(command, "-preset"),
        encoder_parameters=_encoder_parameters(command, encoder=encoder),
        video_filter=_optional_command_option(command, "-vf", "-filter:v"),
        output_container=output_container,
        compression_intent_id=compression_intent_id,
    )


def _search_objective(
        completion_event: dict[str, Any],
        *,
        source_duration_seconds: Any,
        quality_target: float,
) -> tuple[QualitySearchObjective, float, int | None, int | None] | None:
    if "target_size_trace" not in completion_event or completion_event.get("target_size_trace") is None:
        return "quality", quality_target, None, None
    target_size_trace = completion_event.get("target_size_trace")
    if not isinstance(target_size_trace, dict):
        return None
    target = target_size_trace.get("target")
    quality_floor = target_size_trace.get("quality_floor")
    if not isinstance(target, dict) or not isinstance(quality_floor, dict):
        return None
    size_target_bytes = _strict_positive_int(target.get("total_target_bytes"))
    target_video_bytes = _strict_positive_int(target.get("target_video_bytes"))
    minimum_quality_score = _strict_number(quality_floor.get("minimum"))
    duration_seconds = _strict_number(source_duration_seconds)
    if (
            size_target_bytes is None
            or target_video_bytes is None
            or minimum_quality_score is None
            or duration_seconds is None
            or duration_seconds <= 0
    ):
        return None
    target_video_bitrate = rounded_target_video_bitrate(
        target_video_bytes=target_video_bytes,
        source_duration_seconds=duration_seconds,
    )
    return "target_size", minimum_quality_score, size_target_bytes, target_video_bitrate


def _summarize_cohort(
        *,
        scope: QualityMemoryScope,
        scope_prefix: str,
        signature_id: str,
        observations: list[AcceptedQualityOutcome],
) -> QualityMemoryCohort:
    crfs = sorted(observation.chosen_crf for observation in observations)
    evidence_count = len(crfs)
    cohort_id = f"qmc1_{stable_json_hash({
        'schema_version': QUALITY_MEMORY_SIGNATURE_VERSION,
        'scope': scope,
        'scope_prefix': scope_prefix,
        'signature_id': signature_id,
    })[:32]}"
    if not crfs:
        return QualityMemoryCohort(
            scope=scope,
            cohort_id=cohort_id,
            evidence_count=0,
            confidence="none",
            median_crf=None,
            minimum_crf=None,
            maximum_crf=None,
            iqr=None,
            median_absolute_deviation=None,
            central_crf=None,
            reason=f"No accepted {scope} outcomes matched the current quality-search signature.",
        )

    median_crf = float(median(crfs))
    absolute_deviations = [abs(value - median_crf) for value in crfs]
    median_absolute_deviation = float(median(absolute_deviations))
    iqr = None
    if evidence_count >= 4:
        first_quartile, _, third_quartile = quantiles(crfs, n=4, method="inclusive")
        iqr = float(third_quartile - first_quartile)
    confidence = _confidence(scope, evidence_count)
    minimum_samples = _MIN_HINT_SAMPLES[scope]
    central_crf = None
    if evidence_count < minimum_samples:
        reason = (
            f"{scope.title()} evidence has {evidence_count} matching accepted outcome(s); "
            f"at least {minimum_samples} are required for a CRF hint."
        )
    else:
        assert iqr is not None
    if evidence_count >= minimum_samples and (
            iqr > MAX_HINT_IQR or median_absolute_deviation > MAX_HINT_MAD
    ):
        reason = (
            f"{scope.title()} evidence is too dispersed for a CRF hint "
            f"(IQR {iqr:.2f}, MAD {median_absolute_deviation:.2f})."
        )
    elif evidence_count >= minimum_samples:
        central_crf = median_crf
        reason = (
            f"{scope.title()} hint uses median CRF {median_crf:.2f} from {evidence_count} "
            f"matching accepted outcomes ({confidence} confidence, IQR {iqr:.2f}, "
            f"MAD {median_absolute_deviation:.2f})."
        )

    return QualityMemoryCohort(
        scope=scope,
        cohort_id=cohort_id,
        evidence_count=evidence_count,
        confidence=confidence,
        median_crf=median_crf,
        minimum_crf=min(crfs),
        maximum_crf=max(crfs),
        iqr=iqr,
        median_absolute_deviation=median_absolute_deviation,
        central_crf=central_crf,
        reason=reason,
    )


def _confidence(scope: QualityMemoryScope, evidence_count: int) -> QualityMemoryConfidence:
    if evidence_count <= 0:
        return "none"
    if evidence_count < 4:
        return "limited"
    if evidence_count < 10 or scope == "series":
        return "moderate"
    return "high"


def _result_reason(
        *,
        selected: QualityMemoryCohort | None,
        cohorts: list[QualityMemoryCohort],
        metric: str,
        global_metric_evidence_count: int,
) -> str:
    if selected is not None:
        return selected.reason
    local_evidence = next((cohort for cohort in cohorts if cohort.evidence_count > 0), None)
    if local_evidence is not None:
        return f"No local cohort met the sample and dispersion rules. {local_evidence.reason}"
    if global_metric_evidence_count > 0:
        return (
            f"{global_metric_evidence_count} accepted {metric} outcome(s) exist, but global evidence is "
            "metrics-only and cannot guide CRF."
        )
    return f"No accepted {metric} outcomes are available for quality-memory analysis."


def _validate_scope_chain(
        exact_scope: MediaScope,
        season_scope: MediaScope | None,
        series_scope: MediaScope | None,
) -> None:
    if exact_scope.match != "exact_item":
        raise ValueError("Quality memory requires an exact-item scope")
    if season_scope is not None:
        if season_scope.kind != "tv_season" or season_scope.match != "descendants":
            raise ValueError("Quality-memory season scope must be a TV season descendant scope")
        if not season_scope.includes(exact_scope.prefix):
            raise ValueError("Quality-memory season scope does not contain the exact item")
    if series_scope is not None:
        if series_scope.kind != "tv_series" or series_scope.match != "descendants":
            raise ValueError("Quality-memory series scope must be a TV series descendant scope")
        if not series_scope.includes(exact_scope.prefix):
            raise ValueError("Quality-memory series scope does not contain the exact item")
        if season_scope is not None and not series_scope.includes(season_scope.prefix):
            raise ValueError("Quality-memory series scope does not contain the season scope")


def _command_option(command: list[str], *names: str) -> str:
    value = _optional_command_option(command, *names)
    if value is None:
        raise ValueError(f"Encode command is missing required option {names[0]}")
    return value


def _encoder_parameters(command: list[str], *, encoder: str) -> str | None:
    option = _ENCODER_PARAMETER_OPTION.get(encoder.casefold())
    return _optional_command_option(command, option) if option is not None else None


def _optional_command_option(command: list[str], *names: str) -> str | None:
    for index in range(len(command) - 2, -1, -1):
        if command[index] in names:
            return _optional_text(command[index + 1])
    return None


def rounded_target_video_bitrate(*, target_video_bytes: int, source_duration_seconds: float) -> int:
    bits_per_second = target_video_bytes * 8 / source_duration_seconds
    return max(
        TARGET_VIDEO_BITRATE_BUCKET,
        int(round(bits_per_second / TARGET_VIDEO_BITRATE_BUCKET)) * TARGET_VIDEO_BITRATE_BUCKET,
    )


def _json_object(value: Any) -> dict[str, Any] | None:
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _json_string_list(value: Any) -> list[str] | None:
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, list) or not parsed or not all(isinstance(item, str) for item in parsed):
        return None
    return parsed


def _strict_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _strict_positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _strict_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _required_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"Quality-search {label} is required")
    return text


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_utc(parsed)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
