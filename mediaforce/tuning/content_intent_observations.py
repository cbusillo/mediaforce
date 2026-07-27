from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
import json
import logging
import math
from pathlib import PurePosixPath
from statistics import median
from typing import Any, Literal, Mapping, Sequence, cast

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import content_intent_boundary_observations
from mediaforce.core.evidence import canonical_json_text, stable_json_hash, stable_policy_hash, stable_source_id
from mediaforce.core.type_defs import float_value, int_value, object_dict, object_list
from mediaforce.encoding.helpers import build_svt_params
from mediaforce.tuning.compression_intent import CompressionIntentV1, compression_intent_from_item
from mediaforce.tuning.stream_budget import StreamBudgetIdentityError, StreamBudgetLedger


LOGGER = logging.getLogger(__name__)


CONTENT_INTENT_OBSERVATION_SCHEMA_VERSION = 1
CONTENT_INTENT_COMPATIBILITY_SCHEMA_VERSION = 1
CONTENT_PROFILE_SCHEMA_VERSION = 1
CONTENT_VERSION_FINGERPRINT_KIND = "mediaforce_content_version_v1"
BOUNDARY_ASSESSMENT_CONTRACT = "operator_visual_v1"
MAX_CORRECTION_REBASE_ATTEMPTS = 8
VISUAL_BOUNDARY_CONCERN_TAGS = frozenset({
    "softness_detail_loss",
    "motion_breakup",
    "banding_dark_scene_damage",
    "grain_noise_treatment",
    "cadence_interlace_artifacts",
    "other",
})

BoundaryAuthority = Literal["runtime_native", "correction"]
BoundaryDisposition = Literal["active", "withdrawn"]
BoundaryObservationKind = Literal["visual_approval", "visual_rejection"]
BoundaryVerdict = Literal["acceptable", "unacceptable"]
BoundaryKind = Literal["upper_bound", "lower_bound"]
BoundaryDirection = Literal["smaller", "same", "larger"]
BoundaryMeasurementBasis = Literal["sample_projection", "full_output"]
BoundarySummaryStatus = Literal[
    "empty",
    "acceptable_only",
    "unacceptable_only",
    "bounded",
    "conflicting",
]
BoundaryCohortScope = Literal["item", "folder", "content_class", "operator"]
BoundaryCohortConfidence = Literal["none", "limited", "moderate", "high"]


class ContentIntentObservationConflictError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ContentIntentBoundaryCompatibilityV1:
    encoder: str
    encoder_version: str
    encoder_runtime_version: str
    encoder_runtime_signature_id: str
    quality_tool: str
    quality_tool_version: str
    metric_runtime_signature_id: str
    preset: int
    pixel_format: str
    encoder_parameters: tuple[str, ...]
    output_width: int
    output_height: int
    frame_rate: str
    cadence_transform: str
    video_filter: str | None
    output_container: str
    stream_plan_id: str
    measurement_basis: BoundaryMeasurementBasis
    assessment_contract: str
    quality_metric: str
    quality_target: float
    minimum_quality_score: float

    def __post_init__(self) -> None:
        if self.measurement_basis not in {"sample_projection", "full_output"}:
            raise ValueError("Boundary compatibility requires a supported measurement basis")
        if self.preset < 0:
            raise ValueError("Boundary compatibility preset must be non-negative")
        if self.output_width <= 0 or self.output_height <= 0:
            raise ValueError("Boundary compatibility requires positive output dimensions")
        if self.quality_target < 0 or not math.isfinite(self.quality_target):
            raise ValueError("Boundary compatibility quality target must be finite and non-negative")
        if (
                self.minimum_quality_score < 0
                or self.minimum_quality_score > self.quality_target
                or not math.isfinite(self.minimum_quality_score)
        ):
            raise ValueError("Boundary compatibility minimum quality must fit the quality target")
        for value, label in (
            (self.encoder, "encoder"),
            (self.encoder_version, "encoder version"),
            (self.encoder_runtime_version, "encoder runtime version"),
            (self.encoder_runtime_signature_id, "encoder runtime signature"),
            (self.quality_tool, "quality tool"),
            (self.quality_tool_version, "quality tool version"),
            (self.metric_runtime_signature_id, "metric runtime signature"),
            (self.pixel_format, "pixel format"),
            (self.frame_rate, "frame rate"),
            (self.cadence_transform, "cadence transform"),
            (self.output_container, "output container"),
            (self.stream_plan_id, "stream plan identity"),
            (self.assessment_contract, "assessment contract"),
            (self.quality_metric, "quality metric"),
        ):
            if not value.strip():
                raise ValueError(f"Boundary compatibility requires {label}")

    @property
    def compatibility_key(self) -> str:
        return f"cibc1_{stable_json_hash(self.to_payload())[:32]}"

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": CONTENT_INTENT_COMPATIBILITY_SCHEMA_VERSION,
            "encoder": self.encoder,
            "encoder_version": self.encoder_version,
            "encoder_runtime_version": self.encoder_runtime_version,
            "encoder_runtime_signature_id": self.encoder_runtime_signature_id,
            "quality_tool": self.quality_tool,
            "quality_tool_version": self.quality_tool_version,
            "metric_runtime_signature_id": self.metric_runtime_signature_id,
            "preset": self.preset,
            "pixel_format": self.pixel_format,
            "encoder_parameters": list(self.encoder_parameters),
            "output_width": self.output_width,
            "output_height": self.output_height,
            "frame_rate": self.frame_rate,
            "cadence_transform": self.cadence_transform,
            "video_filter": self.video_filter,
            "output_container": self.output_container,
            "stream_plan_id": self.stream_plan_id,
            "measurement_basis": self.measurement_basis,
            "assessment_contract": self.assessment_contract,
            "quality_metric": self.quality_metric,
            "quality_target": self.quality_target,
            "minimum_quality_score": self.minimum_quality_score,
        }


@dataclass(frozen=True, slots=True)
class ContentIntentBoundaryObservation:
    observation_id: str
    series_id: str
    boundary_group_id: str
    schema_version: int
    revision: int
    supersedes_observation_id: str | None
    supersession_reason: str | None
    authority: BoundaryAuthority
    disposition: BoundaryDisposition
    personalization_eligible: bool
    exclusion_reason: str | None
    library_item_id: int
    prefix: str
    source_rel_path: str
    source_id: str
    source_fingerprint: str | None
    content_fingerprint_kind: str
    content_fingerprint: str
    content_id: str
    content_profile_id: str
    content_traits_json: str
    intent_semantic_id: str
    intent_snapshot_id: str
    intent_level: str
    compatibility_key: str
    compatibility_json: str
    policy_hash: str
    source_event_kind: str
    source_event_id: str
    job_id: str
    artifact_fingerprint: str
    source_evidence_ids_json: str
    observation_kind: BoundaryObservationKind
    verdict: BoundaryVerdict
    boundary_kind: BoundaryKind
    authoritative_anchor_bytes: int
    boundary_size_bytes: int
    actual_output_bytes: int | None
    sampled_clip_bytes: int | None
    duration_seconds: float
    boundary_bitrate_bps: int
    direction: BoundaryDirection
    quality_metric: str
    quality_target: float
    minimum_quality_score: float
    measured_quality_score: float
    quality_floor_met: bool
    assessment_json: str
    provenance_json: str
    payload_sha256: str
    recorded_at: str

    def values(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "personalization_eligible": int(self.personalization_eligible),
            "quality_floor_met": int(self.quality_floor_met),
        }


@dataclass(frozen=True, slots=True)
class ContentIntentObservationBuildResult:
    observation: ContentIntentBoundaryObservation | None
    exclusion_reason: str | None


@dataclass(frozen=True, slots=True)
class ContentIntentObservationRecordResult:
    status: Literal["inserted", "existing", "corrected", "excluded"]
    observation_id: str | None
    exclusion_reason: str | None


@dataclass(frozen=True, slots=True)
class ContentIntentBoundarySummary:
    status: BoundarySummaryStatus
    acceptable_upper_bound_bytes: int | None
    unacceptable_lower_bound_bytes: int | None
    acceptable_observation_ids: tuple[str, ...]
    unacceptable_observation_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ContentIntentBoundaryCohort:
    scope: BoundaryCohortScope
    cohort_id: str
    boundary_status: BoundarySummaryStatus
    observation_count: int
    source_count: int
    confidence: BoundaryCohortConfidence
    acceptable_count: int
    unacceptable_count: int
    median_acceptable_bitrate_bps: int | None
    acceptable_bitrate_mad: int | None
    minimum_acceptable_bitrate_bps: int | None
    maximum_acceptable_bitrate_bps: int | None
    median_acceptable_crf: float | None
    acceptable_crf_mad: float | None
    minimum_acceptable_crf: float | None
    maximum_acceptable_crf: float | None
    evidence_snapshot_id: str | None
    actionable: bool
    reason: str


@dataclass(frozen=True, slots=True)
class ContentIntentPersonalizationState:
    model_compatibility_id: str
    intent_semantic_id: str
    compatibility_key: str
    item_boundary: ContentIntentBoundarySummary
    cohorts: tuple[ContentIntentBoundaryCohort, ...]


@dataclass(frozen=True, slots=True)
class ContentIntentReplayContext:
    source_id: str
    content_id: str
    content_profile_id: str
    content_traits: tuple[str, ...]


def build_content_intent_boundary_compatibility(
        *,
        encoder: str,
        encoder_version: str,
        encoder_runtime_version: str,
        encoder_runtime_signature_id: str,
        quality_tool: str,
        quality_tool_version: str,
        metric_runtime_signature_id: str,
        preset: int,
        pixel_format: str,
        encoder_parameters: Sequence[str],
        output_width: int,
        output_height: int,
        frame_rate: str,
        cadence_transform: str,
        video_filter: str | None,
        output_container: str,
        stream_plan_id: str,
        measurement_basis: BoundaryMeasurementBasis,
        quality_metric: str,
        quality_target: float,
        minimum_quality_score: float,
        assessment_contract: str = BOUNDARY_ASSESSMENT_CONTRACT,
) -> ContentIntentBoundaryCompatibilityV1:
    return ContentIntentBoundaryCompatibilityV1(
        encoder=_required_text(encoder, "encoder").casefold(),
        encoder_version=_required_text(encoder_version, "encoder version"),
        encoder_runtime_version=_required_text(encoder_runtime_version, "encoder runtime version"),
        encoder_runtime_signature_id=_required_text(
            encoder_runtime_signature_id,
            "encoder runtime signature",
        ),
        quality_tool=_required_text(quality_tool, "quality tool").casefold(),
        quality_tool_version=_required_text(quality_tool_version, "quality tool version"),
        metric_runtime_signature_id=_required_text(
            metric_runtime_signature_id,
            "metric runtime signature",
        ),
        preset=preset,
        pixel_format=_required_text(pixel_format, "pixel format").casefold(),
        encoder_parameters=tuple(sorted(_required_text(value, "encoder parameter") for value in encoder_parameters)),
        output_width=output_width,
        output_height=output_height,
        frame_rate=_required_text(frame_rate, "frame rate"),
        cadence_transform=_required_text(cadence_transform, "cadence transform").casefold(),
        video_filter=_optional_text(video_filter),
        output_container=_required_text(output_container, "output container").removeprefix(".").casefold(),
        stream_plan_id=_required_text(stream_plan_id, "stream plan identity"),
        measurement_basis=measurement_basis,
        assessment_contract=_required_text(assessment_contract, "assessment contract"),
        quality_metric=_required_text(quality_metric, "quality metric").upper(),
        quality_target=round(float(quality_target), 3),
        minimum_quality_score=round(float(minimum_quality_score), 3),
    )


def content_intent_boundary_compatibility_from_payload(
        payload: Mapping[str, Any] | None,
) -> ContentIntentBoundaryCompatibilityV1:
    value = object_dict(payload)
    if int_value(value.get("schema_version")) != CONTENT_INTENT_COMPATIBILITY_SCHEMA_VERSION:
        raise ValueError("Boundary compatibility uses an unsupported schema version")
    parameters = tuple(
        str(parameter).strip()
        for parameter in object_list(value.get("encoder_parameters"))
        if str(parameter).strip()
    )
    return build_content_intent_boundary_compatibility(
        encoder=str(value.get("encoder") or ""),
        encoder_version=str(value.get("encoder_version") or ""),
        encoder_runtime_version=str(value.get("encoder_runtime_version") or ""),
        encoder_runtime_signature_id=str(value.get("encoder_runtime_signature_id") or ""),
        quality_tool=str(value.get("quality_tool") or ""),
        quality_tool_version=str(value.get("quality_tool_version") or ""),
        metric_runtime_signature_id=str(value.get("metric_runtime_signature_id") or ""),
        preset=int_value(value.get("preset")),
        pixel_format=str(value.get("pixel_format") or ""),
        encoder_parameters=parameters,
        output_width=int_value(value.get("output_width")),
        output_height=int_value(value.get("output_height")),
        frame_rate=str(value.get("frame_rate") or ""),
        cadence_transform=str(value.get("cadence_transform") or ""),
        video_filter=_optional_text(value.get("video_filter")),
        output_container=str(value.get("output_container") or ""),
        stream_plan_id=str(value.get("stream_plan_id") or ""),
        measurement_basis=cast(
            BoundaryMeasurementBasis,
            str(value.get("measurement_basis") or ""),
        ),
        assessment_contract=str(value.get("assessment_contract") or ""),
        quality_metric=str(value.get("quality_metric") or ""),
        quality_target=float_value(value.get("quality_target")),
        minimum_quality_score=float_value(value.get("minimum_quality_score")),
    )


def content_intent_stream_plan_id(stream_budget: Mapping[str, Any] | None) -> str | None:
    payload = object_dict(stream_budget)
    stream_plan = object_dict(payload.get("stream_plan"))
    if not stream_plan:
        return None
    compatible_streams = []
    for raw_stream in object_list(stream_plan.get("streams")):
        stream = object_dict(raw_stream)
        compatible_streams.append(
            {
                "kind": str(stream.get("kind") or "").strip().casefold(),
                "source_codec": str(stream.get("source_codec") or "").strip().casefold(),
                "action": str(stream.get("action") or "").strip().casefold(),
                "output_codec": _optional_text(stream.get("output_codec")),
                "codec_argument": _optional_text(stream.get("codec_argument")),
                "output_bitrate_bps": stream.get("output_bitrate_bps"),
                "output_bitrate_text": _optional_text(stream.get("output_bitrate_text")),
                "channels": stream.get("channels"),
                "language": _optional_text(stream.get("language")),
                "default": bool(stream.get("default")),
                "forced": bool(stream.get("forced")),
                "mime_type": _optional_text(stream.get("mime_type")),
            }
        )
    compatible_streams.sort(key=stable_json_hash)
    compatibility_plan = {
        "schema_version": int_value(stream_plan.get("schema_version")),
        "output_container": str(stream_plan.get("output_container") or "").strip().removeprefix(".").casefold(),
        "attachments_known": bool(stream_plan.get("attachments_known")),
        "copy_unknown_attachments": bool(stream_plan.get("copy_unknown_attachments")),
        "streams": compatible_streams,
    }
    return f"cisp1_{stable_json_hash({'schema_version': 1, 'stream_plan': compatibility_plan})[:32]}"


def content_intent_frame_rate(sample_item: Mapping[str, Any] | None) -> str | None:
    cadence_summary = object_dict(object_dict(sample_item).get("cadence_summary"))
    probe = object_dict(cadence_summary.get("probe"))
    frame_rate = str(probe.get("average_frame_rate") or probe.get("nominal_frame_rate") or "").strip()
    return frame_rate or None


def build_visual_content_intent_observation(
        *,
        prefix: str,
        sample_item: Mapping[str, Any],
        calibration: Mapping[str, Any],
        verdict: Literal["approved", "rejected"],
        concern_tags: Sequence[str],
        evidence_ids: Sequence[str],
        moment_indexes: Sequence[int],
        recorded_at: str,
) -> ContentIntentObservationBuildResult:
    normalized_recorded_at = _timestamp(recorded_at, "recorded timestamp")
    if verdict not in {"approved", "rejected"}:
        raise ValueError("Visual boundary observations require an approved or rejected verdict")
    normalized_concerns = tuple(
        sorted({str(value).strip().lower() for value in concern_tags if str(value).strip()})
    )
    if verdict == "rejected" and not VISUAL_BOUNDARY_CONCERN_TAGS.intersection(normalized_concerns):
        return ContentIntentObservationBuildResult(None, "visual_rejection_reason_missing")
    item = object_dict(sample_item)
    calibration_payload = object_dict(calibration)
    policy = object_dict(calibration_payload.get("policy"))
    sample_result = object_dict(calibration_payload.get("sample_result"))
    compatibility_payload = object_dict(sample_result.get("content_intent_compatibility"))
    calibration_mode = str(calibration_payload.get("mode") or "").strip().lower()
    review_media_ready = calibration_payload.get("review_media_ready") is True
    boundary_review_media_ready = calibration_payload.get("boundary_review_media_ready") is True
    normalized_prefix = _normalized_path(prefix)
    source_rel_path = _normalized_path(str(item.get("rel_path") or ""))
    library_item_id = int_value(item.get("library_item_id"))
    content_fingerprint = str(item.get("content_version_fingerprint") or "").strip()
    source_fingerprint = _optional_text(item.get("source_fingerprint"))
    expected_source_id = stable_source_id(item)
    recorded_source_id = str(item.get("representative_source_id") or "").strip()
    if recorded_source_id and recorded_source_id != expected_source_id:
        return ContentIntentObservationBuildResult(None, "source_identity_mismatch")
    source_id = expected_source_id
    job_id = str(calibration_payload.get("job_id") or "").strip()
    artifact_fingerprint = str(calibration_payload.get("review_artifact_fingerprint") or "").strip()
    current_artifact_fingerprint = str(
        calibration_payload.get("current_review_artifact_fingerprint") or ""
    ).strip()
    duration_seconds = float_value(item.get("duration_seconds"))
    stream_budget = object_dict(item.get("stream_budget_ledger"))
    try:
        stream_budget_ledger = StreamBudgetLedger.from_payload(stream_budget)
        stream_budget_ledger.validate_item(item)
    except (StreamBudgetIdentityError, ValueError):
        return ContentIntentObservationBuildResult(None, "stream_budget_identity_invalid")
    authoritative_anchor_bytes = stream_budget_ledger.total_target_bytes or 0
    boundary_size_bytes = int_value(sample_result.get("predicted_total_size_bytes"))
    predicted_video_size_bytes = int_value(sample_result.get("predicted_video_size_bytes"))
    sampled_clip_bytes = int_value(sample_result.get("sampled_clip_bytes")) or None
    quality_metric = str(sample_result.get("quality_metric") or "").strip().upper()
    quality_target = float_value(sample_result.get("quality_target"))
    measured_quality_score = float_value(sample_result.get("quality_score"))
    media_fingerprint_decision = object_dict(item.get("media_fingerprint_decision"))
    media_fingerprint_evidence_id = str(media_fingerprint_decision.get("evidence_id") or "").strip()
    target_trace = object_dict(sample_result.get("target_size_trace"))
    minimum_quality_score = float_value(object_dict(target_trace.get("quality_floor")).get("minimum"))
    if minimum_quality_score <= 0:
        video_policy = object_dict(policy.get("video"))
        minimum_quality_score = float_value(
            video_policy.get(f"min_target_{quality_metric.casefold()}")
        ) or quality_target

    exclusion_reason = _visual_observation_exclusion_reason(
        calibration_mode=calibration_mode,
        review_media_ready=review_media_ready,
        boundary_review_media_ready=boundary_review_media_ready,
        normalized_prefix=normalized_prefix,
        source_rel_path=source_rel_path,
        library_item_id=library_item_id,
        content_fingerprint=content_fingerprint,
        source_id=source_id,
        job_id=job_id,
        artifact_fingerprint=artifact_fingerprint,
        current_artifact_fingerprint=current_artifact_fingerprint,
        policy=policy,
        duration_seconds=duration_seconds,
        authoritative_anchor_bytes=authoritative_anchor_bytes,
        boundary_size_bytes=boundary_size_bytes,
        predicted_video_size_bytes=predicted_video_size_bytes,
        sampled_clip_bytes=sampled_clip_bytes,
        quality_metric=quality_metric,
        quality_target=quality_target,
        minimum_quality_score=minimum_quality_score,
        measured_quality_score=measured_quality_score,
        media_fingerprint_evidence_id=media_fingerprint_evidence_id,
        compatibility_payload=compatibility_payload,
    )
    if exclusion_reason is not None:
        return ContentIntentObservationBuildResult(None, exclusion_reason)

    try:
        compatibility = content_intent_boundary_compatibility_from_payload(compatibility_payload)
    except ValueError:
        return ContentIntentObservationBuildResult(None, "compatibility_invalid")
    compatibility_exclusion = _compatibility_exclusion_reason(
        compatibility,
        sample_item=item,
        policy=policy,
        quality_metric=quality_metric,
        quality_target=quality_target,
        minimum_quality_score=minimum_quality_score,
    )
    if compatibility_exclusion is not None:
        return ContentIntentObservationBuildResult(None, compatibility_exclusion)
    intent = compression_intent_from_item(item)
    if intent.requires_confirmation:
        return ContentIntentObservationBuildResult(None, "compression_intent_unconfirmed")
    content_traits = _content_traits(item)
    if not content_traits:
        return ContentIntentObservationBuildResult(None, "content_profile_missing")
    if str(media_fingerprint_decision.get("status") or "") != "measured":
        return ContentIntentObservationBuildResult(None, "content_fingerprint_unmeasured")

    policy_hash = stable_policy_hash(policy)
    stream_policy_hash = str(stream_budget.get("policy_hash") or "").strip()
    if stream_policy_hash and stream_policy_hash != policy_hash:
        return ContentIntentObservationBuildResult(None, "stream_budget_policy_mismatch")
    compatibility_stream_plan_id = str(compatibility.stream_plan_id)
    stream_plan_id = content_intent_stream_plan_id(stream_budget)
    if not stream_plan_id or compatibility_stream_plan_id != stream_plan_id:
        return ContentIntentObservationBuildResult(None, "stream_plan_mismatch")

    content_id = _content_id(content_fingerprint)
    content_profile_id = _content_profile_id(content_traits)
    boundary_group_id = _boundary_group_id(content_id, intent, compatibility)
    source_event_kind = "post_test_review"
    source_event_id = job_id
    series_id = _series_id(
        boundary_group_id=boundary_group_id,
        source_event_kind=source_event_kind,
        source_event_id=source_event_id,
        source_id=source_id,
        job_id=job_id,
        artifact_fingerprint=artifact_fingerprint,
    )
    observation_kind: BoundaryObservationKind = (
        "visual_approval" if verdict == "approved" else "visual_rejection"
    )
    normalized_verdict: BoundaryVerdict = "acceptable" if verdict == "approved" else "unacceptable"
    boundary_kind: BoundaryKind = "upper_bound" if verdict == "approved" else "lower_bound"
    direction = _direction(authoritative_anchor_bytes, boundary_size_bytes)
    boundary_bitrate_bps = round((predicted_video_size_bytes * 8) / duration_seconds)
    cadence_evidence_id = str(object_dict(item.get("cadence_decision")).get("evidence_id") or "").strip()
    normalized_evidence_ids = tuple(
        sorted(
            {
                *(str(value).strip() for value in evidence_ids if str(value).strip()),
                media_fingerprint_evidence_id,
                *(value for value in (cadence_evidence_id,) if value),
            }
        )
    )
    normalized_moment_indexes = tuple(sorted({int(value) for value in moment_indexes if int(value) > 0}))
    assessment = {
        "schema_version": CONTENT_INTENT_OBSERVATION_SCHEMA_VERSION,
        "measurement_basis": compatibility.measurement_basis,
        "predicted_total_size_bytes": boundary_size_bytes,
        "predicted_video_size_bytes": predicted_video_size_bytes,
        "sampled_clip_bytes": sampled_clip_bytes,
        "review_artifact_size_bytes": int_value(sample_result.get("review_artifact_size_bytes")) or None,
        "chosen_crf": float_value(sample_result.get("chosen_crf")) or None,
        "quality_floor_met": measured_quality_score >= minimum_quality_score,
        "content_traits": list(content_traits),
        "content_fingerprint_evidence_id": str(media_fingerprint_decision.get("evidence_id") or "") or None,
        "concern_tags": list(normalized_concerns),
        "moment_indexes": list(normalized_moment_indexes),
        "evidence_ids": list(normalized_evidence_ids),
    }
    provenance = {
        "schema_version": CONTENT_INTENT_OBSERVATION_SCHEMA_VERSION,
        "source": "operator_visual_review",
        "calibration_action": str(calibration_payload.get("action") or "sample"),
        "sample_job_id": job_id,
        "policy_hash": policy_hash,
        "intent_snapshot_id": intent.snapshot_id,
        "content_fingerprint_kind": CONTENT_VERSION_FINGERPRINT_KIND,
        "artifact_fingerprint": artifact_fingerprint,
        "recorded_at": normalized_recorded_at,
    }
    return ContentIntentObservationBuildResult(
        _build_observation(
            series_id=series_id,
            boundary_group_id=boundary_group_id,
            revision=0,
            supersedes_observation_id=None,
            supersession_reason=None,
            authority="runtime_native",
            disposition="active",
            personalization_eligible=True,
            exclusion_reason=None,
            library_item_id=library_item_id,
            prefix=normalized_prefix,
            source_rel_path=source_rel_path,
            source_id=source_id,
            source_fingerprint=source_fingerprint,
            content_fingerprint=content_fingerprint,
            content_id=content_id,
            content_profile_id=content_profile_id,
            content_traits=content_traits,
            intent_semantic_id=intent.semantic_id,
            intent_snapshot_id=intent.snapshot_id,
            intent_level=intent.level,
            compatibility=compatibility,
            policy_hash=policy_hash,
            source_event_kind=source_event_kind,
            source_event_id=source_event_id,
            job_id=job_id,
            artifact_fingerprint=artifact_fingerprint,
            source_evidence_ids=normalized_evidence_ids,
            observation_kind=observation_kind,
            verdict=normalized_verdict,
            boundary_kind=boundary_kind,
            authoritative_anchor_bytes=authoritative_anchor_bytes,
            boundary_size_bytes=boundary_size_bytes,
            actual_output_bytes=None,
            sampled_clip_bytes=sampled_clip_bytes,
            duration_seconds=duration_seconds,
            boundary_bitrate_bps=boundary_bitrate_bps,
            direction=direction,
            quality_metric=quality_metric,
            quality_target=quality_target,
            minimum_quality_score=minimum_quality_score,
            measured_quality_score=measured_quality_score,
            quality_floor_met=measured_quality_score >= minimum_quality_score,
            assessment=assessment,
            provenance=provenance,
            recorded_at=normalized_recorded_at,
        ),
        None,
    )


def record_visual_content_intent_observation(
        connection: DBClient,
        *,
        prefix: str,
        sample_item: Mapping[str, Any],
        calibration: Mapping[str, Any],
        verdict: Literal["approved", "rejected"],
        concern_tags: Sequence[str],
        evidence_ids: Sequence[str],
        moment_indexes: Sequence[int],
        recorded_at: str,
) -> ContentIntentObservationRecordResult:
    result = build_visual_content_intent_observation(
        prefix=prefix,
        sample_item=sample_item,
        calibration=calibration,
        verdict=verdict,
        concern_tags=concern_tags,
        evidence_ids=evidence_ids,
        moment_indexes=moment_indexes,
        recorded_at=recorded_at,
    )
    if result.observation is None:
        LOGGER.info(
            "Content-intent boundary observation excluded: %s",
            result.exclusion_reason or "unknown_reason",
        )
        return ContentIntentObservationRecordResult("excluded", None, result.exclusion_reason)
    try:
        inserted = append_content_intent_boundary_observation(connection, result.observation)
    except ContentIntentObservationConflictError:
        inserted = False
    if inserted:
        return ContentIntentObservationRecordResult(
            "inserted",
            result.observation.observation_id,
            None,
        )
    candidate_assessment = object_dict(json.loads(result.observation.assessment_json))
    for _attempt in range(MAX_CORRECTION_REBASE_ATTEMPTS):
        current = connection.execute(
            select(content_intent_boundary_observations)
            .where(content_intent_boundary_observations.c.series_id == result.observation.series_id)
            .order_by(content_intent_boundary_observations.c.revision.desc())
            .limit(1)
        ).mappings().one_or_none()
        if current is None:
            raise ContentIntentObservationConflictError(
                f"Content-intent boundary series {result.observation.series_id} conflicted without a current row"
            )
        if not _observation_integrity_valid(current):
            raise ContentIntentObservationConflictError(
                f"Content-intent boundary series {result.observation.series_id} failed integrity validation"
            )
        if str(current.get("disposition") or "") == "withdrawn":
            return ContentIntentObservationRecordResult(
                "excluded",
                str(current.get("observation_id") or "") or None,
                "review_series_withdrawn",
            )
        if _same_visual_review(current, result.observation):
            return ContentIntentObservationRecordResult(
                "existing",
                str(current.get("observation_id") or "") or None,
                None,
            )
        if str(current.get("source_evidence_ids_json") or "") != result.observation.source_evidence_ids_json:
            raise ContentIntentObservationConflictError(
                f"Content-intent boundary series {result.observation.series_id} changed immutable source evidence"
            )
        previous = _rehash_observation(current)
        corrected = correct_content_intent_boundary_observation(
            previous,
            verdict=result.observation.verdict,
            personalization_eligible=True,
            exclusion_reason=None,
            reason_code="operator_reassessed_same_review",
            recorded_at=_monotonic_successor_timestamp(previous.recorded_at, recorded_at),
            concern_tags=object_list(candidate_assessment.get("concern_tags")),
            moment_indexes=[
                int_value(value)
                for value in object_list(candidate_assessment.get("moment_indexes"))
                if int_value(value) > 0
            ],
        )
        try:
            if append_content_intent_boundary_observation(connection, corrected):
                return ContentIntentObservationRecordResult("corrected", corrected.observation_id, None)
        except ContentIntentObservationConflictError:
            continue
    raise ContentIntentObservationConflictError(
        f"Content-intent boundary series {result.observation.series_id} exceeded correction rebase retries"
    )


def correct_content_intent_boundary_observation(
        previous: ContentIntentBoundaryObservation,
        *,
        verdict: BoundaryVerdict,
        personalization_eligible: bool,
        exclusion_reason: str | None,
        reason_code: str,
        recorded_at: str,
        concern_tags: Sequence[object] | None = None,
        moment_indexes: Sequence[int] | None = None,
) -> ContentIntentBoundaryObservation:
    if previous.disposition == "withdrawn":
        raise ValueError("Withdrawn boundary evidence cannot be corrected")
    successor_recorded_at = _successor_timestamp(previous.recorded_at, recorded_at)
    observation_kind: BoundaryObservationKind = (
        "visual_approval" if verdict == "acceptable" else "visual_rejection"
    )
    boundary_kind: BoundaryKind = "upper_bound" if verdict == "acceptable" else "lower_bound"
    corrected_assessment = object_dict(json.loads(previous.assessment_json))
    if concern_tags is not None:
        corrected_assessment["concern_tags"] = sorted(
            {str(value).strip().lower() for value in concern_tags if str(value).strip()}
        )
    if moment_indexes is not None:
        corrected_assessment["moment_indexes"] = sorted(
            {int(value) for value in moment_indexes if int(value) > 0}
        )
    return _build_observation(
        series_id=previous.series_id,
        boundary_group_id=previous.boundary_group_id,
        revision=previous.revision + 1,
        supersedes_observation_id=previous.observation_id,
        supersession_reason=_required_text(reason_code, "correction reason"),
        authority="correction",
        disposition="active",
        personalization_eligible=personalization_eligible,
        exclusion_reason=_optional_text(exclusion_reason),
        library_item_id=previous.library_item_id,
        prefix=previous.prefix,
        source_rel_path=previous.source_rel_path,
        source_id=previous.source_id,
        source_fingerprint=previous.source_fingerprint,
        content_fingerprint=previous.content_fingerprint,
        content_id=previous.content_id,
        content_profile_id=previous.content_profile_id,
        content_traits=tuple(json.loads(previous.content_traits_json)),
        intent_semantic_id=previous.intent_semantic_id,
        intent_snapshot_id=previous.intent_snapshot_id,
        intent_level=previous.intent_level,
        compatibility=content_intent_boundary_compatibility_from_payload(
            object_dict(json.loads(previous.compatibility_json))
        ),
        policy_hash=previous.policy_hash,
        source_event_kind=previous.source_event_kind,
        source_event_id=previous.source_event_id,
        job_id=previous.job_id,
        artifact_fingerprint=previous.artifact_fingerprint,
        source_evidence_ids=tuple(json.loads(previous.source_evidence_ids_json)),
        observation_kind=observation_kind,
        verdict=verdict,
        boundary_kind=boundary_kind,
        authoritative_anchor_bytes=previous.authoritative_anchor_bytes,
        boundary_size_bytes=previous.boundary_size_bytes,
        actual_output_bytes=previous.actual_output_bytes,
        sampled_clip_bytes=previous.sampled_clip_bytes,
        duration_seconds=previous.duration_seconds,
        boundary_bitrate_bps=previous.boundary_bitrate_bps,
        direction=previous.direction,
        quality_metric=previous.quality_metric,
        quality_target=previous.quality_target,
        minimum_quality_score=previous.minimum_quality_score,
        measured_quality_score=previous.measured_quality_score,
        quality_floor_met=previous.quality_floor_met,
        assessment=corrected_assessment,
        provenance={
            **object_dict(json.loads(previous.provenance_json)),
            "correction_reason": reason_code,
            "recorded_at": successor_recorded_at,
        },
        recorded_at=successor_recorded_at,
    )


def withdraw_content_intent_boundary_observation(
        previous: ContentIntentBoundaryObservation,
        *,
        reason_code: str,
        recorded_at: str,
) -> ContentIntentBoundaryObservation:
    correction = correct_content_intent_boundary_observation(
        previous,
        verdict=previous.verdict,
        personalization_eligible=False,
        exclusion_reason=_required_text(reason_code, "withdrawal reason"),
        reason_code=reason_code,
        recorded_at=recorded_at,
    )
    values = asdict(correction)
    values["disposition"] = "withdrawn"
    return _rehash_observation(values)


def append_content_intent_boundary_observation(
        connection: DBClient,
        observation: ContentIntentBoundaryObservation,
) -> bool:
    result = connection.execute(
        sqlite_insert(content_intent_boundary_observations)
        .values(**observation.values())
        .on_conflict_do_nothing()
    )
    if result.rowcount:
        return True
    existing_hash = connection.execute(
        select(content_intent_boundary_observations.c.payload_sha256)
        .where(content_intent_boundary_observations.c.observation_id == observation.observation_id)
    ).scalar_one_or_none()
    if existing_hash == observation.payload_sha256:
        return False
    conflicting_hash = connection.execute(
        select(content_intent_boundary_observations.c.payload_sha256)
        .where(content_intent_boundary_observations.c.series_id == observation.series_id)
        .where(content_intent_boundary_observations.c.revision == observation.revision)
    ).scalar_one_or_none()
    if conflicting_hash == observation.payload_sha256:
        return False
    raise ContentIntentObservationConflictError(
        f"Conflicting content-intent boundary observation for {observation.series_id} "
        f"revision={observation.revision}"
    )


def load_current_content_intent_boundary_observations(
        connection: DBClient,
        *,
        recorded_before: str | None = None,
        intent_semantic_id: str | None = None,
        compatibility_key: str | None = None,
        include_withdrawn: bool = False,
) -> list[Mapping[str, Any]]:
    ranked_query = select(
        *content_intent_boundary_observations.c,
        func.row_number().over(
            partition_by=content_intent_boundary_observations.c.series_id,
            order_by=content_intent_boundary_observations.c.revision.desc(),
        ).label("observation_rank"),
    )
    if recorded_before is not None:
        ranked_query = ranked_query.where(
            content_intent_boundary_observations.c.recorded_at
            < _timestamp(recorded_before, "replay cutoff timestamp")
        )
    if intent_semantic_id is not None:
        ranked_query = ranked_query.where(
            content_intent_boundary_observations.c.intent_semantic_id == intent_semantic_id
        )
    if compatibility_key is not None:
        ranked_query = ranked_query.where(
            content_intent_boundary_observations.c.compatibility_key == compatibility_key
        )
    ranked = ranked_query.subquery()
    query = select(ranked).where(ranked.c.observation_rank == 1)
    if not include_withdrawn:
        query = query.where(ranked.c.disposition == "active")
    return list(connection.execute(query).mappings())


def summarize_content_intent_boundary(
        observations: Sequence[Mapping[str, Any]],
) -> ContentIntentBoundarySummary:
    eligible = [
        observation
        for observation in observations
        if bool(observation.get("personalization_eligible")) and str(observation.get("disposition")) == "active"
    ]
    acceptable = [
        observation
        for observation in eligible
        if str(observation.get("verdict")) == "acceptable"
    ]
    unacceptable = [
        observation
        for observation in eligible
        if str(observation.get("verdict")) == "unacceptable"
    ]
    acceptable_upper_bound = min(
        (int_value(observation.get("boundary_size_bytes")) for observation in acceptable),
        default=0,
    ) or None
    unacceptable_lower_bound = max(
        (int_value(observation.get("boundary_size_bytes")) for observation in unacceptable),
        default=0,
    ) or None
    if acceptable_upper_bound is None and unacceptable_lower_bound is None:
        status: BoundarySummaryStatus = "empty"
    elif acceptable_upper_bound is None:
        status = "unacceptable_only"
    elif unacceptable_lower_bound is None:
        status = "acceptable_only"
    elif unacceptable_lower_bound >= acceptable_upper_bound:
        status = "conflicting"
    else:
        status = "bounded"
    return ContentIntentBoundarySummary(
        status=status,
        acceptable_upper_bound_bytes=acceptable_upper_bound,
        unacceptable_lower_bound_bytes=unacceptable_lower_bound,
        acceptable_observation_ids=tuple(sorted(str(value.get("observation_id")) for value in acceptable)),
        unacceptable_observation_ids=tuple(sorted(str(value.get("observation_id")) for value in unacceptable)),
    )


def replay_content_intent_personalization(
        observations: Sequence[Mapping[str, Any]],
        *,
        source_id: str,
        content_id: str,
        prefix: str,
        content_profile_id: str,
        intent_semantic_id: str,
        compatibility_key: str,
) -> ContentIntentPersonalizationState:
    scoped_rows = _content_intent_replay_scope_rows(
        observations,
        source_id=source_id,
        content_id=content_id,
        prefix=prefix,
        content_profile_id=content_profile_id,
        intent_semantic_id=intent_semantic_id,
        compatibility_key=compatibility_key,
    )
    cohorts = (
        _cohort("item", scoped_rows["item"], f"item:{content_id}"),
        _cohort(
            "folder",
            scoped_rows["folder"],
            f"folder:{stable_json_hash({'prefix': _normalized_path(prefix)})[:24]}",
        ),
        _cohort(
            "content_class",
            scoped_rows["content_class"],
            f"content:{content_profile_id}",
        ),
        _cohort("operator", scoped_rows["operator"], "operator:local"),
    )
    return ContentIntentPersonalizationState(
        model_compatibility_id=_model_compatibility_id(
            content_profile_id=content_profile_id,
            intent_semantic_id=intent_semantic_id,
            compatibility_key=compatibility_key,
        ),
        intent_semantic_id=intent_semantic_id,
        compatibility_key=compatibility_key,
        item_boundary=summarize_content_intent_boundary(scoped_rows["item"]),
        cohorts=cohorts,
    )


def content_intent_replay_scope_rows(
        observations: Sequence[Mapping[str, Any]],
        *,
        source_id: str,
        content_id: str,
        prefix: str,
        content_profile_id: str,
        intent_semantic_id: str,
        compatibility_key: str,
        scope: BoundaryCohortScope,
) -> tuple[Mapping[str, Any], ...]:
    return tuple(
        _content_intent_replay_scope_rows(
            observations,
            source_id=source_id,
            content_id=content_id,
            prefix=prefix,
            content_profile_id=content_profile_id,
            intent_semantic_id=intent_semantic_id,
            compatibility_key=compatibility_key,
        )[scope]
    )


def _content_intent_replay_scope_rows(
        observations: Sequence[Mapping[str, Any]],
        *,
        source_id: str,
        content_id: str,
        prefix: str,
        content_profile_id: str,
        intent_semantic_id: str,
        compatibility_key: str,
) -> dict[BoundaryCohortScope, list[Mapping[str, Any]]]:
    current_observations = _current_revision_rows(observations)
    intent_technical_compatible = [
        observation
        for observation in current_observations
        if str(observation.get("intent_semantic_id")) == intent_semantic_id
        and str(observation.get("compatibility_key")) == compatibility_key
        and str(observation.get("disposition")) == "active"
        and bool(observation.get("personalization_eligible"))
        and _observation_integrity_valid(observation)
    ]
    exact = [
        observation
        for observation in intent_technical_compatible
        if str(observation.get("source_id")) == source_id
        and str(observation.get("content_id")) == content_id
    ]
    profile_compatible = [
        observation
        for observation in intent_technical_compatible
        if str(observation.get("content_profile_id")) == content_profile_id
    ]
    normalized_prefix = _normalized_path(prefix)
    return {
        "item": exact,
        "folder": [
            observation
            for observation in profile_compatible
            if str(observation.get("prefix")) == normalized_prefix
        ],
        "content_class": [
            observation
            for observation in profile_compatible
            if str(observation.get("prefix")) != normalized_prefix
        ],
        "operator": profile_compatible,
    }


def content_intent_replay_context(
        sample_item: Mapping[str, Any],
) -> ContentIntentReplayContext:
    item = object_dict(sample_item)
    content_fingerprint = str(item.get("content_version_fingerprint") or "").strip()
    content_traits = _content_traits(item)
    if not content_fingerprint:
        raise ValueError("Content-intent replay requires a content-version fingerprint")
    if not content_traits:
        raise ValueError("Content-intent replay requires measured content traits")
    return ContentIntentReplayContext(
        source_id=stable_source_id(item),
        content_id=_content_id(content_fingerprint),
        content_profile_id=_content_profile_id(content_traits),
        content_traits=content_traits,
    )


def _build_observation(
        *,
        series_id: str,
        boundary_group_id: str,
        revision: int,
        supersedes_observation_id: str | None,
        supersession_reason: str | None,
        authority: BoundaryAuthority,
        disposition: BoundaryDisposition,
        personalization_eligible: bool,
        exclusion_reason: str | None,
        library_item_id: int,
        prefix: str,
        source_rel_path: str,
        source_id: str,
        source_fingerprint: str | None,
        content_fingerprint: str,
        content_id: str,
        content_profile_id: str,
        content_traits: Sequence[str],
        intent_semantic_id: str,
        intent_snapshot_id: str,
        intent_level: str,
        compatibility: ContentIntentBoundaryCompatibilityV1,
        policy_hash: str,
        source_event_kind: str,
        source_event_id: str,
        job_id: str,
        artifact_fingerprint: str,
        source_evidence_ids: Sequence[str],
        observation_kind: BoundaryObservationKind,
        verdict: BoundaryVerdict,
        boundary_kind: BoundaryKind,
        authoritative_anchor_bytes: int,
        boundary_size_bytes: int,
        actual_output_bytes: int | None,
        sampled_clip_bytes: int | None,
        duration_seconds: float,
        boundary_bitrate_bps: int,
        direction: BoundaryDirection,
        quality_metric: str,
        quality_target: float,
        minimum_quality_score: float,
        measured_quality_score: float,
        quality_floor_met: bool,
        assessment: Mapping[str, Any],
        provenance: Mapping[str, Any],
        recorded_at: str,
) -> ContentIntentBoundaryObservation:
    normalized_recorded_at = _timestamp(recorded_at, "recorded timestamp")
    if personalization_eligible and exclusion_reason is not None:
        raise ValueError("Eligible boundary evidence cannot carry an exclusion reason")
    if not personalization_eligible and exclusion_reason is None:
        raise ValueError("Ineligible boundary evidence requires an exclusion reason")
    semantic_payload = {
        "schema_version": CONTENT_INTENT_OBSERVATION_SCHEMA_VERSION,
        "series_id": series_id,
        "boundary_group_id": boundary_group_id,
        "revision": revision,
        "supersedes_observation_id": supersedes_observation_id,
        "supersession_reason": supersession_reason,
        "authority": authority,
        "disposition": disposition,
        "personalization_eligible": personalization_eligible,
        "exclusion_reason": exclusion_reason,
        "library_item_id": library_item_id,
        "prefix": prefix,
        "source_rel_path": source_rel_path,
        "source_id": source_id,
        "source_fingerprint": source_fingerprint,
        "content_fingerprint_kind": CONTENT_VERSION_FINGERPRINT_KIND,
        "content_fingerprint": content_fingerprint,
        "content_id": content_id,
        "content_profile_id": content_profile_id,
        "content_traits": list(content_traits),
        "intent_semantic_id": intent_semantic_id,
        "intent_snapshot_id": intent_snapshot_id,
        "intent_level": intent_level,
        "compatibility_key": compatibility.compatibility_key,
        "compatibility": compatibility.to_payload(),
        "policy_hash": policy_hash,
        "source_event_kind": source_event_kind,
        "source_event_id": source_event_id,
        "job_id": job_id,
        "artifact_fingerprint": artifact_fingerprint,
        "source_evidence_ids": list(source_evidence_ids),
        "observation_kind": observation_kind,
        "verdict": verdict,
        "boundary_kind": boundary_kind,
        "authoritative_anchor_bytes": authoritative_anchor_bytes,
        "boundary_size_bytes": boundary_size_bytes,
        "actual_output_bytes": actual_output_bytes,
        "sampled_clip_bytes": sampled_clip_bytes,
        "duration_seconds": round(duration_seconds, 3),
        "boundary_bitrate_bps": boundary_bitrate_bps,
        "direction": direction,
        "quality_metric": quality_metric,
        "quality_target": round(quality_target, 3),
        "minimum_quality_score": round(minimum_quality_score, 3),
        "measured_quality_score": round(measured_quality_score, 3),
        "quality_floor_met": quality_floor_met,
        "assessment": dict(assessment),
        "provenance": dict(provenance),
    }
    payload_sha256 = stable_json_hash(semantic_payload)
    return ContentIntentBoundaryObservation(
        observation_id=f"cibo1_{payload_sha256[:32]}",
        series_id=series_id,
        boundary_group_id=boundary_group_id,
        schema_version=CONTENT_INTENT_OBSERVATION_SCHEMA_VERSION,
        revision=revision,
        supersedes_observation_id=supersedes_observation_id,
        supersession_reason=supersession_reason,
        authority=authority,
        disposition=disposition,
        personalization_eligible=personalization_eligible,
        exclusion_reason=exclusion_reason,
        library_item_id=library_item_id,
        prefix=prefix,
        source_rel_path=source_rel_path,
        source_id=source_id,
        source_fingerprint=source_fingerprint,
        content_fingerprint_kind=CONTENT_VERSION_FINGERPRINT_KIND,
        content_fingerprint=content_fingerprint,
        content_id=content_id,
        content_profile_id=content_profile_id,
        content_traits_json=_json(content_traits),
        intent_semantic_id=intent_semantic_id,
        intent_snapshot_id=intent_snapshot_id,
        intent_level=intent_level,
        compatibility_key=compatibility.compatibility_key,
        compatibility_json=_json(compatibility.to_payload()),
        policy_hash=policy_hash,
        source_event_kind=source_event_kind,
        source_event_id=source_event_id,
        job_id=job_id,
        artifact_fingerprint=artifact_fingerprint,
        source_evidence_ids_json=_json(source_evidence_ids),
        observation_kind=observation_kind,
        verdict=verdict,
        boundary_kind=boundary_kind,
        authoritative_anchor_bytes=authoritative_anchor_bytes,
        boundary_size_bytes=boundary_size_bytes,
        actual_output_bytes=actual_output_bytes,
        sampled_clip_bytes=sampled_clip_bytes,
        duration_seconds=round(duration_seconds, 3),
        boundary_bitrate_bps=boundary_bitrate_bps,
        direction=direction,
        quality_metric=quality_metric,
        quality_target=round(quality_target, 3),
        minimum_quality_score=round(minimum_quality_score, 3),
        measured_quality_score=round(measured_quality_score, 3),
        quality_floor_met=quality_floor_met,
        assessment_json=_json(assessment),
        provenance_json=_json(provenance),
        payload_sha256=payload_sha256,
        recorded_at=normalized_recorded_at,
    )


def _rehash_observation(values: Mapping[str, Any]) -> ContentIntentBoundaryObservation:
    compatibility = content_intent_boundary_compatibility_from_payload(
        object_dict(json.loads(str(values["compatibility_json"])))
    )
    return _build_observation(
        series_id=str(values["series_id"]),
        boundary_group_id=str(values["boundary_group_id"]),
        revision=int(values["revision"]),
        supersedes_observation_id=_optional_text(values.get("supersedes_observation_id")),
        supersession_reason=_optional_text(values.get("supersession_reason")),
        authority=cast(BoundaryAuthority, str(values["authority"])),
        disposition=cast(BoundaryDisposition, str(values["disposition"])),
        personalization_eligible=bool(values["personalization_eligible"]),
        exclusion_reason=_optional_text(values.get("exclusion_reason")),
        library_item_id=int(values["library_item_id"]),
        prefix=str(values["prefix"]),
        source_rel_path=str(values["source_rel_path"]),
        source_id=str(values["source_id"]),
        source_fingerprint=_optional_text(values.get("source_fingerprint")),
        content_fingerprint=str(values["content_fingerprint"]),
        content_id=str(values["content_id"]),
        content_profile_id=str(values["content_profile_id"]),
        content_traits=tuple(json.loads(str(values["content_traits_json"]))),
        intent_semantic_id=str(values["intent_semantic_id"]),
        intent_snapshot_id=str(values["intent_snapshot_id"]),
        intent_level=str(values["intent_level"]),
        compatibility=compatibility,
        policy_hash=str(values["policy_hash"]),
        source_event_kind=str(values["source_event_kind"]),
        source_event_id=str(values["source_event_id"]),
        job_id=str(values["job_id"]),
        artifact_fingerprint=str(values["artifact_fingerprint"]),
        source_evidence_ids=tuple(json.loads(str(values["source_evidence_ids_json"]))),
        observation_kind=cast(BoundaryObservationKind, str(values["observation_kind"])),
        verdict=cast(BoundaryVerdict, str(values["verdict"])),
        boundary_kind=cast(BoundaryKind, str(values["boundary_kind"])),
        authoritative_anchor_bytes=int(values["authoritative_anchor_bytes"]),
        boundary_size_bytes=int(values["boundary_size_bytes"]),
        actual_output_bytes=(int(values["actual_output_bytes"]) if values.get("actual_output_bytes") else None),
        sampled_clip_bytes=(int(values["sampled_clip_bytes"]) if values.get("sampled_clip_bytes") else None),
        duration_seconds=float(values["duration_seconds"]),
        boundary_bitrate_bps=int(values["boundary_bitrate_bps"]),
        direction=cast(BoundaryDirection, str(values["direction"])),
        quality_metric=str(values["quality_metric"]),
        quality_target=float(values["quality_target"]),
        minimum_quality_score=float(values["minimum_quality_score"]),
        measured_quality_score=float(values["measured_quality_score"]),
        quality_floor_met=bool(values["quality_floor_met"]),
        assessment=object_dict(json.loads(str(values["assessment_json"]))),
        provenance=object_dict(json.loads(str(values["provenance_json"]))),
        recorded_at=str(values["recorded_at"]),
    )


def _visual_observation_exclusion_reason(
        *,
        calibration_mode: str,
        review_media_ready: bool,
        boundary_review_media_ready: bool,
        normalized_prefix: str,
        source_rel_path: str,
        library_item_id: int,
        content_fingerprint: str,
        source_id: str,
        job_id: str,
        artifact_fingerprint: str,
        current_artifact_fingerprint: str,
        policy: Mapping[str, Any],
        duration_seconds: float,
        authoritative_anchor_bytes: int,
        boundary_size_bytes: int,
        predicted_video_size_bytes: int,
        sampled_clip_bytes: int | None,
        quality_metric: str,
        quality_target: float,
        minimum_quality_score: float,
        measured_quality_score: float,
        media_fingerprint_evidence_id: str,
        compatibility_payload: Mapping[str, Any],
) -> str | None:
    checks = (
        (calibration_mode == "sample", "unsupported_calibration_mode"),
        (review_media_ready, "review_media_unavailable"),
        (boundary_review_media_ready, "encoded_review_media_unavailable"),
        (bool(normalized_prefix), "prefix_missing"),
        (bool(source_rel_path), "source_path_missing"),
        (library_item_id > 0, "library_item_missing"),
        (bool(content_fingerprint), "content_version_fingerprint_missing"),
        (bool(source_id), "source_identity_missing"),
        (bool(job_id), "sample_job_missing"),
        (bool(artifact_fingerprint), "review_artifact_missing"),
        (current_artifact_fingerprint == artifact_fingerprint, "review_artifact_stale"),
        (bool(policy), "policy_missing"),
        (duration_seconds > 0 and math.isfinite(duration_seconds), "duration_invalid"),
        (authoritative_anchor_bytes > 0, "size_anchor_missing"),
        (boundary_size_bytes > 0, "boundary_size_missing"),
        (predicted_video_size_bytes > 0, "video_boundary_size_missing"),
        (sampled_clip_bytes is not None and sampled_clip_bytes > 0, "sample_measurement_missing"),
        (bool(quality_metric), "quality_metric_missing"),
        (quality_target > 0 and math.isfinite(quality_target), "quality_target_invalid"),
        (
            minimum_quality_score > 0
            and minimum_quality_score <= quality_target
            and math.isfinite(minimum_quality_score),
            "quality_floor_invalid",
        ),
        (measured_quality_score > 0 and math.isfinite(measured_quality_score), "quality_score_invalid"),
        (bool(media_fingerprint_evidence_id), "content_fingerprint_evidence_missing"),
        (bool(compatibility_payload), "compatibility_missing"),
    )
    return next((reason for valid, reason in checks if not valid), None)


def _compatibility_exclusion_reason(
        compatibility: ContentIntentBoundaryCompatibilityV1,
        *,
        sample_item: Mapping[str, Any],
        policy: Mapping[str, Any],
        quality_metric: str,
        quality_target: float,
        minimum_quality_score: float,
) -> str | None:
    if compatibility.measurement_basis != "sample_projection":
        return "compatibility_measurement_basis_mismatch"
    if compatibility.assessment_contract != BOUNDARY_ASSESSMENT_CONTRACT:
        return "compatibility_assessment_contract_mismatch"
    video_policy = object_dict(object_dict(policy).get("video"))
    if compatibility.encoder != str(video_policy.get("encoder") or "").strip().casefold():
        return "compatibility_encoder_mismatch"
    if compatibility.pixel_format != str(video_policy.get("pixel_format") or "").strip().casefold():
        return "compatibility_pixel_format_mismatch"
    if compatibility.encoder_parameters != tuple(sorted(build_svt_params(video_policy))):
        return "compatibility_encoder_parameters_mismatch"
    if compatibility.quality_metric != quality_metric:
        return "compatibility_quality_mismatch"
    if not math.isclose(compatibility.quality_target, quality_target, rel_tol=0.0, abs_tol=0.001):
        return "compatibility_quality_mismatch"
    if not math.isclose(
            compatibility.minimum_quality_score,
            minimum_quality_score,
            rel_tol=0.0,
            abs_tol=0.001,
    ):
        return "compatibility_quality_mismatch"
    output_container = str(object_dict(sample_item).get("output_container") or "").removeprefix(".").casefold()
    if compatibility.output_container != output_container:
        return "compatibility_container_mismatch"
    frame_rate = content_intent_frame_rate(sample_item)
    if frame_rate is None or compatibility.frame_rate != frame_rate:
        return "compatibility_frame_rate_mismatch"
    cadence_transform = str(
        object_dict(object_dict(sample_item).get("cadence_decision")).get("transform") or "none"
    ).strip().casefold()
    if compatibility.cadence_transform != cadence_transform:
        return "compatibility_cadence_mismatch"
    return None


def _current_revision_rows(
        observations: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    current: dict[str, Mapping[str, Any]] = {}
    for observation in observations:
        series_id = str(observation.get("series_id") or observation.get("observation_id") or "")
        if not series_id:
            continue
        previous = current.get(series_id)
        if previous is None or int_value(observation.get("revision")) > int_value(previous.get("revision")):
            current[series_id] = observation
    return list(current.values())


def _observation_integrity_valid(observation: Mapping[str, Any]) -> bool:
    try:
        rebuilt = _rehash_observation(observation)
        _timestamp(str(observation.get("recorded_at") or ""), "recorded timestamp")
    except (KeyError, TypeError, ValueError):
        return False
    return (
        rebuilt.observation_id == str(observation.get("observation_id") or "")
        and rebuilt.payload_sha256 == str(observation.get("payload_sha256") or "")
    )


def _same_visual_review(
        current: Mapping[str, Any],
        candidate: ContentIntentBoundaryObservation,
) -> bool:
    return (
        str(current.get("verdict") or "") == candidate.verdict
        and bool(current.get("personalization_eligible")) == candidate.personalization_eligible
        and str(current.get("assessment_json") or "") == candidate.assessment_json
        and str(current.get("source_evidence_ids_json") or "") == candidate.source_evidence_ids_json
    )


def _content_traits(item: Mapping[str, Any]) -> tuple[str, ...]:
    decision = object_dict(item.get("media_fingerprint_decision"))
    traits = [str(value).strip().lower() for value in object_list(decision.get("traits")) if str(value).strip()]
    if not traits:
        traits = [
            str(finding.get("id") or "").strip().lower()
            for finding in (object_dict(value) for value in object_list(decision.get("findings")))
            if str(finding.get("id") or "").strip()
        ]
    if not traits and str(decision.get("status") or "") == "measured":
        traits = ["typical"]
    return tuple(sorted(set(traits)))


def _content_id(content_fingerprint: str) -> str:
    payload = {
        "schema_version": CONTENT_INTENT_OBSERVATION_SCHEMA_VERSION,
        "content_fingerprint_kind": CONTENT_VERSION_FINGERPRINT_KIND,
        "content_fingerprint": content_fingerprint,
    }
    return f"cic1_{stable_json_hash(payload)[:32]}"


def _content_profile_id(content_traits: Sequence[str]) -> str:
    payload = {
        "schema_version": CONTENT_PROFILE_SCHEMA_VERSION,
        "traits": sorted(content_traits),
    }
    return f"cip1_{stable_json_hash(payload)[:32]}"


def _boundary_group_id(
        content_id: str,
        intent: CompressionIntentV1,
        compatibility: ContentIntentBoundaryCompatibilityV1,
) -> str:
    payload = {
        "content_id": content_id,
        "intent_semantic_id": intent.semantic_id,
        "compatibility_key": compatibility.compatibility_key,
    }
    return f"cibg1_{stable_json_hash(payload)[:32]}"


def _model_compatibility_id(
        *,
        content_profile_id: str,
        intent_semantic_id: str,
        compatibility_key: str,
) -> str:
    payload = {
        "schema_version": CONTENT_INTENT_COMPATIBILITY_SCHEMA_VERSION,
        "content_profile_id": content_profile_id,
        "intent_semantic_id": intent_semantic_id,
        "technical_compatibility_key": compatibility_key,
    }
    return f"cimc1_{stable_json_hash(payload)[:32]}"


def _series_id(
        *,
        boundary_group_id: str,
        source_event_kind: str,
        source_event_id: str,
        source_id: str,
        job_id: str,
        artifact_fingerprint: str,
) -> str:
    payload = {
        "boundary_group_id": boundary_group_id,
        "source_event_kind": source_event_kind,
        "source_event_id": source_event_id,
        "source_id": source_id,
        "job_id": job_id,
        "artifact_fingerprint": artifact_fingerprint,
    }
    return f"cibs1_{stable_json_hash(payload)[:32]}"


def _cohort(
        scope: BoundaryCohortScope,
        observations: Sequence[Mapping[str, Any]],
        cohort_id: str,
) -> ContentIntentBoundaryCohort:
    acceptable = [
        int_value(observation.get("boundary_bitrate_bps"))
        for observation in observations
        if str(observation.get("verdict")) == "acceptable"
        and int_value(observation.get("boundary_bitrate_bps")) > 0
    ]
    unacceptable_count = sum(
        1 for observation in observations if str(observation.get("verdict")) == "unacceptable"
    )
    acceptable_crfs = [
        value
        for observation in observations
        if str(observation.get("verdict")) == "acceptable"
        for value in (_observation_chosen_crf(observation),)
        if value is not None
    ]
    acceptable_source_count = len({
        str(observation.get("source_id"))
        for observation in observations
        if str(observation.get("verdict")) == "acceptable"
        and int_value(observation.get("boundary_bitrate_bps")) > 0
    })
    central = round(median(acceptable)) if acceptable else None
    mad = round(median(abs(value - central) for value in acceptable)) if acceptable and central is not None else None
    central_crf = round(median(acceptable_crfs), 3) if acceptable_crfs else None
    crf_mad = (
        round(median(abs(value - central_crf) for value in acceptable_crfs), 3)
        if acceptable_crfs and central_crf is not None
        else None
    )
    confidence = _cohort_confidence(acceptable_source_count, central, mad)
    boundary = summarize_content_intent_boundary(observations)
    actionable = (
        boundary.status not in {"empty", "conflicting"}
        and (
            scope == "item"
            or confidence in {"moderate", "high"}
        )
    )
    if not observations:
        reason = "No compatible local boundary evidence is available."
    elif boundary.status == "conflicting":
        reason = "Compatible local evidence conflicts and cannot personalize a starting point."
    elif actionable and scope == "item":
        reason = "An exact content-version boundary is available as an item-local exception."
    elif actionable:
        reason = "Independent compatible sources support a bounded local prior."
    elif confidence == "limited":
        reason = "Local evidence is preserved, but more independent sources are required before prediction."
    else:
        reason = "Compatible local evidence is ready for a later bounded-prior predictor."
    return ContentIntentBoundaryCohort(
        scope=scope,
        cohort_id=cohort_id,
        boundary_status=boundary.status,
        observation_count=len(observations),
        source_count=acceptable_source_count,
        confidence=confidence,
        acceptable_count=len(acceptable),
        unacceptable_count=unacceptable_count,
        median_acceptable_bitrate_bps=central,
        acceptable_bitrate_mad=mad,
        minimum_acceptable_bitrate_bps=min(acceptable) if acceptable else None,
        maximum_acceptable_bitrate_bps=max(acceptable) if acceptable else None,
        median_acceptable_crf=central_crf,
        acceptable_crf_mad=crf_mad,
        minimum_acceptable_crf=min(acceptable_crfs) if acceptable_crfs else None,
        maximum_acceptable_crf=max(acceptable_crfs) if acceptable_crfs else None,
        evidence_snapshot_id=(
            f"cics1_{stable_json_hash(sorted(
                str(observation.get('payload_sha256') or '')
                for observation in observations
            ))[:32]}"
            if observations
            else None
        ),
        actionable=actionable,
        reason=reason,
    )


def _observation_chosen_crf(observation: Mapping[str, Any]) -> float | None:
    try:
        assessment = object_dict(json.loads(str(observation.get("assessment_json") or "{}")))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    value = assessment.get("chosen_crf")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    normalized = float(value)
    if not math.isfinite(normalized) or not 0 <= normalized <= 63:
        return None
    return round(normalized, 3)


def _cohort_confidence(
        source_count: int,
        central: int | None,
        mad: int | None,
) -> BoundaryCohortConfidence:
    if source_count <= 0:
        return "none"
    if source_count < 3 or central is None or mad is None:
        return "limited"
    relative_mad = mad / central if central > 0 else math.inf
    if source_count >= 6 and relative_mad <= 0.10:
        return "high"
    if relative_mad <= 0.25:
        return "moderate"
    return "limited"


def _direction(anchor_bytes: int, boundary_bytes: int) -> BoundaryDirection:
    if boundary_bytes < anchor_bytes:
        return "smaller"
    if boundary_bytes > anchor_bytes:
        return "larger"
    return "same"


def _normalized_path(value: str) -> str:
    normalized = str(PurePosixPath(str(value).strip().strip("/")))
    return "" if normalized == "." else normalized


def _required_text(value: object, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"Boundary observation requires {label}")
    return normalized


def _timestamp(value: object, label: str) -> str:
    normalized = _required_text(value, label)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Boundary observation requires a valid {label}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Boundary observation requires a timezone-aware {label}")
    return parsed.astimezone(UTC).isoformat()


def _successor_timestamp(previous: str, successor: str) -> str:
    normalized_previous = _timestamp(previous, "previous recorded timestamp")
    normalized_successor = _timestamp(successor, "recorded timestamp")
    previous_value = datetime.fromisoformat(normalized_previous.replace("Z", "+00:00"))
    successor_value = datetime.fromisoformat(normalized_successor.replace("Z", "+00:00"))
    if successor_value <= previous_value:
        raise ValueError("Boundary correction timestamps must advance")
    return normalized_successor


def _monotonic_successor_timestamp(previous: str, requested: str) -> str:
    normalized_previous = _timestamp(previous, "previous recorded timestamp")
    normalized_requested = _timestamp(requested, "recorded timestamp")
    previous_value = datetime.fromisoformat(normalized_previous)
    requested_value = datetime.fromisoformat(normalized_requested)
    if requested_value > previous_value:
        return normalized_requested
    return (previous_value + timedelta(seconds=1)).isoformat()


def _optional_text(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _json(value: object) -> str:
    return canonical_json_text(value)
