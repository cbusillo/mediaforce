from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from importlib import resources
import json
import math
import re
from typing import Any, Literal, Mapping, Sequence, cast

from mediaforce.core.db import DBClient
from mediaforce.core.evidence import canonical_json_text, stable_json_hash
from mediaforce.core.type_defs import int_value, object_dict, object_list
from mediaforce.encoding.quality import QualitySearchWarmStart
from mediaforce.tuning.compression_intent import (
    COMPRESSION_INTENT_LEVELS,
    CompressionIntentLevel,
    compression_intent_from_item,
)
from mediaforce.tuning.content_intent_observations import (
    BOUNDARY_ASSESSMENT_CONTRACT,
    BoundaryCohortConfidence,
    ContentIntentBoundaryCohort,
    ContentIntentBoundaryCompatibilityV1,
    ContentIntentPersonalizationState,
    ContentIntentReplayContext,
    content_intent_boundary_compatibility_from_payload,
    content_intent_replay_context,
    content_intent_replay_scope_rows,
    load_current_content_intent_boundary_observations,
    replay_content_intent_personalization,
)


AV1_COLD_START_BUNDLE_SCHEMA = "mediaforce.av1_cold_start_priors"
AV1_COLD_START_BUNDLE_SCHEMA_VERSION = 1
AV1_COLD_START_PREDICTOR_VERSION = "acsp1"
AV1_COLD_START_ARTIFACT_NAME = "av1_cold_start_priors_v1.json"
AV1_COLD_START_ARTIFACT_PACKAGE = "mediaforce.tuning.data"
AV1_COLD_START_LOCAL_MAX_AGE_DAYS = 180
AV1_COLD_START_MIN_CONFIDENCE_SCORE = 0.65
AV1_COLD_START_MIN_RANGE_HIT_RATE = 0.5
AV1_COLD_START_MIN_HELD_OUT_COUNT = 3
AV1_COLD_START_MAX_LOCAL_CRF_MAD = 2.0
AV1_COLD_START_MAX_LOCAL_CRF_RANGE = 6.0
AV1_COLD_START_PROBE_SUPPORTED_INTENTS = frozenset({"balanced"})
AV1_COLD_START_REQUEST_FALLBACK_CODES = frozenset({
    "compression_intent_unconfirmed",
    "content_evidence_missing",
    "target_video_bitrate_missing",
})
AV1_COLD_START_BUNDLE_FALLBACK_CODES = frozenset({
    "public_bundle_invalid",
    "public_bundle_missing",
    "public_bundle_unavailable",
})

AV1ColdStartBundleState = Literal["validation_pending", "active", "retired"]
AV1ColdStartConfidence = Literal["none", "limited", "moderate", "high"]
AV1ColdStartPredictionStatus = Literal["recommended", "no_recommendation"]
AV1ColdStartPredictionSource = Literal["none", "public", "local", "combined"]
AV1ColdStartTrait = Literal[
    "animation",
    "darkness",
    "motion",
    "grain_noise",
    "texture_detail",
    "low_motion_dialogue",
    "mixed",
    "typical",
    "unknown",
]

AV1_COLD_START_TRAITS = frozenset({
    "animation",
    "darkness",
    "motion",
    "grain_noise",
    "texture_detail",
    "low_motion_dialogue",
    "mixed",
    "typical",
    "unknown",
})
AV1_COLD_START_REVIEW_RISKS = frozenset({
    "flat_regions_and_edges",
    "banding_dark_scene_damage",
    "motion_breakup",
    "grain_noise_treatment",
    "softness_detail_loss",
    "dialogue_facial_detail",
    "mixed_or_unknown_content",
})
AV1_COLD_START_INTENT_OBJECTIVES: dict[str, str] = {
    "reference": "maximize_measured_fidelity_within_authoritative_size_limit",
    "transparent": "minimize_size_subject_to_source_indistinguishability",
    "balanced": "minimize_target_distance_subject_to_measured_quality_floor",
    "perceptual_floor": "minimize_size_subject_to_explicit_visual_acceptability",
}

AV1_COLD_START_PRIVATE_PUBLIC_KEYS = frozenset({
    "artifact_fingerprint",
    "artifact_path",
    "content_fingerprint",
    "content_id",
    "content_profile_id",
    "job_id",
    "library_item_id",
    "manifest_path",
    "observation_id",
    "operator_notes",
    "policy_hash",
    "prefix",
    "rel_path",
    "review_media",
    "source_fingerprint",
    "source_id",
    "source_path",
})
AV1_COLD_START_PRIVATE_PUBLIC_TEXT_RE = re.compile(
    r"(?:/Users/|/Volumes/|/home/|/private/|/tmp(?:/|\b)|/var/|/etc/|/opt/|/usr/|"
    r"/Library/|/Applications/|/mnt/|/srv/|/data/|~/|"
    r"[A-Za-z]:\\|\\\\[^\\\s]+\\|smb://|nfs://|review-media|"
    r"\.sqlite(?:3)?\b|\.db\b)",
    re.IGNORECASE,
)
_VERSION_RE = re.compile(r"(?<!\d)(\d+)\.(\d+)(?:\.(\d+))?")


class AV1ColdStartContractError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AV1ColdStartRuntimeCompatibilityV1:
    encoder: str
    encoder_version: tuple[int, int, int]
    public_version_contract_supported: bool
    encoder_runtime_major: int
    quality_tool: str
    quality_tool_major: int
    measurement_basis: str
    assessment_contract: str
    preset: int
    pixel_format: str
    bit_depth: int
    output_width: int
    output_height: int
    frame_rate: str
    cadence_transform: str
    video_filter: str | None
    encoder_parameters: tuple[str, ...]
    grain_strength: int
    grain_denoise: int
    quality_metric: str
    quality_target: float
    minimum_quality_score: float
    output_container: str
    target_video_bitrate_bps: int

    def __post_init__(self) -> None:
        if self.quality_tool != "ab-av1":
            raise AV1ColdStartContractError("AV1 cold-start quality tool is unsupported")
        if self.measurement_basis != "sample_projection":
            raise AV1ColdStartContractError("AV1 cold-start measurement basis is unsupported")
        if self.assessment_contract != BOUNDARY_ASSESSMENT_CONTRACT:
            raise AV1ColdStartContractError("AV1 cold-start assessment contract is unsupported")

    @property
    def compatibility_id(self) -> str:
        return f"acsrc1_{stable_json_hash(self.to_payload())[:32]}"

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "encoder": self.encoder,
            "encoder_version": _format_version(self.encoder_version),
            "public_version_contract_supported": self.public_version_contract_supported,
            "encoder_runtime_major": self.encoder_runtime_major,
            "quality_tool": self.quality_tool,
            "quality_tool_major": self.quality_tool_major,
            "measurement_basis": self.measurement_basis,
            "assessment_contract": self.assessment_contract,
            "preset": self.preset,
            "pixel_format": self.pixel_format,
            "bit_depth": self.bit_depth,
            "output_width": self.output_width,
            "output_height": self.output_height,
            "frame_rate": self.frame_rate,
            "cadence_transform": self.cadence_transform,
            "video_filter": self.video_filter,
            "encoder_parameters": list(self.encoder_parameters),
            "grain_strength": self.grain_strength,
            "grain_denoise": self.grain_denoise,
            "quality_metric": self.quality_metric,
            "quality_target": self.quality_target,
            "minimum_quality_score": self.minimum_quality_score,
            "output_container": self.output_container,
            "target_video_bitrate_bps": self.target_video_bitrate_bps,
        }


@dataclass(frozen=True, slots=True)
class AV1ColdStartCellCompatibilityV1:
    encoder: str
    encoder_version_min: tuple[int, int, int]
    encoder_version_max_exclusive: tuple[int, int, int]
    encoder_runtime_major: int
    quality_tool: str
    quality_tool_major: int
    measurement_basis: str
    assessment_contract: str
    preset: int
    pixel_format: str
    bit_depth: int
    output_width: int
    output_height: int
    frame_rate: str
    cadence_transform: str
    video_filter: str | None
    encoder_parameters: tuple[str, ...]
    grain_strength: int
    grain_denoise: int
    quality_metric: str
    quality_target: float
    minimum_quality_score: float
    output_container: str
    target_video_bitrate_min_bps: int
    target_video_bitrate_max_bps: int

    def __post_init__(self) -> None:
        if self.encoder_version_min >= self.encoder_version_max_exclusive:
            raise AV1ColdStartContractError("AV1 prior encoder version range must be increasing")
        if self.bit_depth not in {8, 10, 12}:
            raise AV1ColdStartContractError("AV1 prior bit depth must be 8, 10, or 12")
        if self.preset < 0 or self.output_width <= 0 or self.output_height <= 0:
            raise AV1ColdStartContractError("AV1 prior compatibility dimensions must be positive")
        if self.encoder_runtime_major < 0 or self.quality_tool_major < 0:
            raise AV1ColdStartContractError("AV1 prior tool major versions must be non-negative")
        if self.quality_tool != "ab-av1":
            raise AV1ColdStartContractError("AV1 prior quality tool is unsupported")
        if self.measurement_basis != "sample_projection":
            raise AV1ColdStartContractError("AV1 prior measurement basis is unsupported")
        if self.assessment_contract != BOUNDARY_ASSESSMENT_CONTRACT:
            raise AV1ColdStartContractError("AV1 prior assessment contract is unsupported")
        if not 0 <= self.grain_strength <= 50 or self.grain_denoise not in {0, 1}:
            raise AV1ColdStartContractError("AV1 prior grain settings are invalid")
        if not 0 <= self.minimum_quality_score <= self.quality_target:
            raise AV1ColdStartContractError("AV1 prior quality floor must fit the target")
        if (
                self.target_video_bitrate_min_bps <= 0
                or self.target_video_bitrate_max_bps < self.target_video_bitrate_min_bps
        ):
            raise AV1ColdStartContractError("AV1 prior target-video bitrate range is invalid")

    def matches(self, runtime: AV1ColdStartRuntimeCompatibilityV1) -> bool:
        return (
            runtime.public_version_contract_supported
            and self.encoder == runtime.encoder
            and self.encoder_version_min <= runtime.encoder_version < self.encoder_version_max_exclusive
            and self.encoder_runtime_major == runtime.encoder_runtime_major
            and self.quality_tool == runtime.quality_tool
            and self.quality_tool_major == runtime.quality_tool_major
            and self.measurement_basis == runtime.measurement_basis
            and self.assessment_contract == runtime.assessment_contract
            and self.preset == runtime.preset
            and self.pixel_format == runtime.pixel_format
            and self.bit_depth == runtime.bit_depth
            and self.output_width == runtime.output_width
            and self.output_height == runtime.output_height
            and self.frame_rate == runtime.frame_rate
            and self.cadence_transform == runtime.cadence_transform
            and self.video_filter == runtime.video_filter
            and self.encoder_parameters == runtime.encoder_parameters
            and self.grain_strength == runtime.grain_strength
            and self.grain_denoise == runtime.grain_denoise
            and self.quality_metric == runtime.quality_metric
            and math.isclose(self.quality_target, runtime.quality_target, abs_tol=0.001)
            and math.isclose(self.minimum_quality_score, runtime.minimum_quality_score, abs_tol=0.001)
            and self.output_container == runtime.output_container
            and self.target_video_bitrate_min_bps
            <= runtime.target_video_bitrate_bps
            <= self.target_video_bitrate_max_bps
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "encoder": self.encoder,
            "encoder_version_min": _format_version(self.encoder_version_min),
            "encoder_version_max_exclusive": _format_version(self.encoder_version_max_exclusive),
            "encoder_runtime_major": self.encoder_runtime_major,
            "quality_tool": self.quality_tool,
            "quality_tool_major": self.quality_tool_major,
            "measurement_basis": self.measurement_basis,
            "assessment_contract": self.assessment_contract,
            "preset": self.preset,
            "pixel_format": self.pixel_format,
            "bit_depth": self.bit_depth,
            "output_width": self.output_width,
            "output_height": self.output_height,
            "frame_rate": self.frame_rate,
            "cadence_transform": self.cadence_transform,
            "video_filter": self.video_filter,
            "encoder_parameters": list(self.encoder_parameters),
            "grain_strength": self.grain_strength,
            "grain_denoise": self.grain_denoise,
            "quality_metric": self.quality_metric,
            "quality_target": self.quality_target,
            "minimum_quality_score": self.minimum_quality_score,
            "output_container": self.output_container,
            "target_video_bitrate_min_bps": self.target_video_bitrate_min_bps,
            "target_video_bitrate_max_bps": self.target_video_bitrate_max_bps,
        }


@dataclass(frozen=True, slots=True)
class AV1ColdStartConfidenceV1:
    level: AV1ColdStartConfidence
    score: float
    evidence_count: int
    independent_source_count: int

    def __post_init__(self) -> None:
        if self.level not in {"none", "limited", "moderate", "high"}:
            raise AV1ColdStartContractError("AV1 prior confidence level is unsupported")
        if not math.isfinite(self.score) or not 0 <= self.score <= 1:
            raise AV1ColdStartContractError("AV1 prior confidence score must be between zero and one")
        if self.evidence_count < 0 or self.independent_source_count < 0:
            raise AV1ColdStartContractError("AV1 prior evidence counts must be non-negative")
        if self.independent_source_count > self.evidence_count:
            raise AV1ColdStartContractError("AV1 prior independent-source count exceeds evidence count")

    @property
    def actionable(self) -> bool:
        return (
            self.level in {"moderate", "high"}
            and self.score >= AV1_COLD_START_MIN_CONFIDENCE_SCORE
            and self.evidence_count >= 3
            and self.independent_source_count >= 3
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "score": self.score,
            "evidence_count": self.evidence_count,
            "independent_source_count": self.independent_source_count,
        }


@dataclass(frozen=True, slots=True)
class AV1ColdStartEvaluationSummaryV1:
    held_out_count: int
    range_hit_count: int
    baseline_candidate_count: int
    guided_candidate_count: int
    quality_floor_regression_count: int
    final_size_regression_count: int
    visual_rejection_regression_count: int
    operator_attention_regression_count: int

    def __post_init__(self) -> None:
        values = (
            self.held_out_count,
            self.range_hit_count,
            self.baseline_candidate_count,
            self.guided_candidate_count,
            self.quality_floor_regression_count,
            self.final_size_regression_count,
            self.visual_rejection_regression_count,
            self.operator_attention_regression_count,
        )
        if any(value < 0 for value in values):
            raise AV1ColdStartContractError("AV1 prior evaluation counts must be non-negative")
        if self.range_hit_count > self.held_out_count:
            raise AV1ColdStartContractError("AV1 prior range hits exceed held-out cases")

    @property
    def safety_regression_count(self) -> int:
        return (
            self.quality_floor_regression_count
            + self.final_size_regression_count
            + self.visual_rejection_regression_count
            + self.operator_attention_regression_count
        )

    @property
    def supports_publication(self) -> bool:
        return (
            self.held_out_count >= AV1_COLD_START_MIN_HELD_OUT_COUNT
            and self.range_hit_count / self.held_out_count >= AV1_COLD_START_MIN_RANGE_HIT_RATE
            and self.guided_candidate_count < self.baseline_candidate_count
            and self.safety_regression_count == 0
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "held_out_count": self.held_out_count,
            "range_hit_count": self.range_hit_count,
            "baseline_candidate_count": self.baseline_candidate_count,
            "guided_candidate_count": self.guided_candidate_count,
            "quality_floor_regression_count": self.quality_floor_regression_count,
            "final_size_regression_count": self.final_size_regression_count,
            "visual_rejection_regression_count": self.visual_rejection_regression_count,
            "operator_attention_regression_count": self.operator_attention_regression_count,
        }


@dataclass(frozen=True, slots=True)
class AV1ColdStartPriorCellV1:
    cell_id: str
    intent_level: CompressionIntentLevel
    optimization_objective: str
    required_traits: tuple[AV1ColdStartTrait, ...]
    compatibility: AV1ColdStartCellCompatibilityV1
    crf_lower: float
    crf_center: float
    crf_upper: float
    confidence: AV1ColdStartConfidenceV1
    evaluation: AV1ColdStartEvaluationSummaryV1
    provenance_version: str
    provenance_method: str
    reviewed_at: str
    review_risks: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.intent_level not in COMPRESSION_INTENT_LEVELS:
            raise AV1ColdStartContractError("AV1 prior cell requires a confirmed compression intent")
        expected_objective = AV1_COLD_START_INTENT_OBJECTIVES[self.intent_level]
        if self.optimization_objective != expected_objective:
            raise AV1ColdStartContractError("AV1 prior cell objective does not match its intent")
        if not self.required_traits or tuple(sorted(set(self.required_traits))) != self.required_traits:
            raise AV1ColdStartContractError("AV1 prior traits must be unique and sorted")
        if any(trait not in AV1_COLD_START_TRAITS for trait in self.required_traits):
            raise AV1ColdStartContractError("AV1 prior cell contains an unsupported content trait")
        if not all(math.isfinite(value) for value in (self.crf_lower, self.crf_center, self.crf_upper)):
            raise AV1ColdStartContractError("AV1 prior CRF range must be finite")
        if not 0 <= self.crf_lower <= self.crf_center <= self.crf_upper <= 63:
            raise AV1ColdStartContractError("AV1 prior CRF range is invalid")
        if not self.provenance_version.strip() or not self.provenance_method.strip():
            raise AV1ColdStartContractError("AV1 prior cells require aggregate provenance")
        _parse_timestamp(self.reviewed_at, "AV1 prior review timestamp")
        if tuple(sorted(set(self.review_risks))) != self.review_risks:
            raise AV1ColdStartContractError("AV1 prior review risks must be unique and sorted")
        if any(risk not in AV1_COLD_START_REVIEW_RISKS for risk in self.review_risks):
            raise AV1ColdStartContractError("AV1 prior cell contains an unsupported review risk")
        if self.cell_id != _prior_cell_id(self.semantic_payload()):
            raise AV1ColdStartContractError("AV1 prior cell identity does not match its payload")

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "intent_level": self.intent_level,
            "optimization_objective": self.optimization_objective,
            "required_traits": list(self.required_traits),
            "compatibility": self.compatibility.to_payload(),
            "crf_range": {
                "lower": self.crf_lower,
                "center": self.crf_center,
                "upper": self.crf_upper,
            },
            "confidence": self.confidence.to_payload(),
            "evaluation": self.evaluation.to_payload(),
            "provenance": {
                "version": self.provenance_version,
                "method": self.provenance_method,
                "reviewed_at": self.reviewed_at,
            },
            "review_risks": list(self.review_risks),
        }

    def to_payload(self) -> dict[str, Any]:
        return {"cell_id": self.cell_id, **self.semantic_payload()}


@dataclass(frozen=True, slots=True)
class AV1ColdStartPriorBundleV1:
    bundle_version: str
    predictor_contract_version: str
    state: AV1ColdStartBundleState
    published_at: str
    valid_until: str
    provenance_source: str
    provenance_method: str
    privacy_contract: str
    governance_issue: str
    cells: tuple[AV1ColdStartPriorCellV1, ...]

    def __post_init__(self) -> None:
        if not self.bundle_version.strip():
            raise AV1ColdStartContractError("AV1 prior bundle version is required")
        if self.predictor_contract_version != AV1_COLD_START_PREDICTOR_VERSION:
            raise AV1ColdStartContractError("AV1 prior predictor contract version is unsupported")
        if self.state not in {"validation_pending", "active", "retired"}:
            raise AV1ColdStartContractError("AV1 prior bundle state is unsupported")
        published_at = _parse_timestamp(self.published_at, "AV1 prior publication timestamp")
        valid_until = _parse_timestamp(self.valid_until, "AV1 prior expiration timestamp")
        if valid_until <= published_at:
            raise AV1ColdStartContractError("AV1 prior expiration must follow publication")
        if self.state == "active" and not self.cells:
            raise AV1ColdStartContractError("Active AV1 prior bundles require at least one cell")
        if self.state == "validation_pending" and self.cells:
            raise AV1ColdStartContractError("Validation-pending AV1 prior bundles cannot ship active cells")
        if tuple(sorted(self.cells, key=lambda cell: cell.cell_id)) != self.cells:
            raise AV1ColdStartContractError("AV1 prior bundle cells must be sorted by identity")
        if len({cell.cell_id for cell in self.cells}) != len(self.cells):
            raise AV1ColdStartContractError("AV1 prior bundle cell identities must be unique")
        coverage_keys = [
            (
                cell.intent_level,
                cell.required_traits,
                canonical_json_text(cell.compatibility.to_payload()),
            )
            for cell in self.cells
        ]
        if len(set(coverage_keys)) != len(coverage_keys):
            raise AV1ColdStartContractError("AV1 prior bundle coverage cells must be unique")
        for value in (
            self.provenance_source,
            self.provenance_method,
            self.privacy_contract,
            self.governance_issue,
        ):
            if not value.strip():
                raise AV1ColdStartContractError("AV1 prior bundle provenance is incomplete")
        _assert_public_payload_safe(self.semantic_payload())

    @property
    def payload_sha256(self) -> str:
        return f"sha256:{stable_json_hash(self.semantic_payload())}"

    @property
    def bundle_id(self) -> str:
        return f"acspb1_{stable_json_hash(self.semantic_payload())[:32]}"

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema": AV1_COLD_START_BUNDLE_SCHEMA,
            "schema_version": AV1_COLD_START_BUNDLE_SCHEMA_VERSION,
            "bundle_version": self.bundle_version,
            "predictor_contract_version": self.predictor_contract_version,
            "state": self.state,
            "published_at": self.published_at,
            "valid_until": self.valid_until,
            "provenance": {
                "source": self.provenance_source,
                "method": self.provenance_method,
                "privacy_contract": self.privacy_contract,
                "governance_issue": self.governance_issue,
            },
            "cells": [cell.to_payload() for cell in self.cells],
        }

    def to_payload(self) -> dict[str, Any]:
        return {**self.semantic_payload(), "payload_sha256": self.payload_sha256}


@dataclass(frozen=True, slots=True)
class AV1ColdStartBundleLoadResult:
    bundle: AV1ColdStartPriorBundleV1 | None
    fallback_reason: str | None


@dataclass(frozen=True, slots=True)
class AV1ColdStartRequestV1:
    intent_level: CompressionIntentLevel
    intent_semantic_id: str
    content_traits: tuple[AV1ColdStartTrait, ...]
    runtime_compatibility: AV1ColdStartRuntimeCompatibilityV1
    configured_min_crf: int
    configured_max_crf: int
    as_of: str

    def __post_init__(self) -> None:
        if self.intent_level not in COMPRESSION_INTENT_LEVELS:
            raise AV1ColdStartContractError("AV1 cold-start requests require confirmed intent")
        if not self.intent_semantic_id.strip():
            raise AV1ColdStartContractError("AV1 cold-start requests require intent identity")
        if not self.content_traits or tuple(sorted(set(self.content_traits))) != self.content_traits:
            raise AV1ColdStartContractError("AV1 cold-start request traits must be unique and sorted")
        if not 0 <= self.configured_min_crf <= self.configured_max_crf <= 63:
            raise AV1ColdStartContractError("AV1 cold-start request CRF bounds are invalid")
        _parse_timestamp(self.as_of, "AV1 cold-start request timestamp")

    @property
    def search_signature_id(self) -> str:
        payload = {
            "predictor_version": AV1_COLD_START_PREDICTOR_VERSION,
            "intent_semantic_id": self.intent_semantic_id,
            "content_traits": list(self.content_traits),
            "runtime_compatibility_id": self.runtime_compatibility.compatibility_id,
            "configured_min_crf": self.configured_min_crf,
            "configured_max_crf": self.configured_max_crf,
        }
        return f"acss1_{stable_json_hash(payload)[:32]}"


@dataclass(frozen=True, slots=True)
class AV1ColdStartPrediction:
    status: AV1ColdStartPredictionStatus
    source: AV1ColdStartPredictionSource
    fallback_reason: str | None
    intent_level: str | None
    optimization_objective: str | None
    content_traits: tuple[str, ...]
    requested_crf: float | None
    candidate_crf: int | None
    crf_lower: float | None
    crf_upper: float | None
    confidence: AV1ColdStartConfidence
    confidence_score: float
    provenance_ids: tuple[str, ...]
    review_risks: tuple[str, ...]
    search_signature_id: str | None
    bundle_id: str | None
    local_scope: str | None
    local_evidence_present: bool = False
    public_evidence_present: bool = False

    @property
    def recommended(self) -> bool:
        return self.status == "recommended" and self.candidate_crf is not None

    def search_hint(self) -> QualitySearchWarmStart | None:
        if not self.recommended or self.requested_crf is None or self.search_signature_id is None:
            return None
        provenance_id = self.provenance_ids[0] if self.provenance_ids else None
        cohort_id = f"acsh1_{stable_json_hash(self.provenance_ids or (self.source,))[:24]}"
        return QualitySearchWarmStart(
            requested_crf=self.requested_crf,
            candidate_crf=cast(int, self.candidate_crf),
            search_signature_id=self.search_signature_id,
            cohort_id=cohort_id,
            source=f"av1_cold_start_{self.source}",
            confidence=self.confidence,
            provenance_id=provenance_id,
            review_risks=self.review_risks,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "predictor_version": AV1_COLD_START_PREDICTOR_VERSION,
            "status": self.status,
            "source": self.source,
            "fallback_reason": self.fallback_reason,
            "intent_level": self.intent_level,
            "optimization_objective": self.optimization_objective,
            "content_traits": list(self.content_traits),
            "requested_crf": self.requested_crf,
            "candidate_crf": self.candidate_crf,
            "crf_range": (
                {"lower": self.crf_lower, "upper": self.crf_upper}
                if self.crf_lower is not None and self.crf_upper is not None
                else None
            ),
            "confidence": {
                "level": self.confidence,
                "score": self.confidence_score,
            },
            "provenance_ids": list(self.provenance_ids),
            "review_risks": list(self.review_risks),
            "bundle_id": self.bundle_id,
            "local_scope": self.local_scope,
            "fallback": {
                "full_measured_calibration": not self.recommended,
                "quality_floor_unchanged": True,
                "target_size_authority_unchanged": True,
            },
        }


def build_av1_cold_start_runtime_compatibility(
        compatibility: ContentIntentBoundaryCompatibilityV1,
        *,
        target_video_bitrate_bps: int,
) -> AV1ColdStartRuntimeCompatibilityV1:
    parameters = tuple(sorted(compatibility.encoder_parameters))
    grain_strength, grain_denoise = _grain_settings(parameters)
    encoder_version, encoder_version_supported = _runtime_version(
        compatibility.encoder_version,
        "encoder version",
    )
    encoder_runtime_version, encoder_runtime_supported = _runtime_version(
        compatibility.encoder_runtime_version,
        "encoder runtime version",
    )
    quality_tool_version, quality_tool_supported = _runtime_version(
        compatibility.quality_tool_version,
        "quality tool version",
    )
    return AV1ColdStartRuntimeCompatibilityV1(
        encoder=_normalized_encoder(compatibility.encoder),
        encoder_version=encoder_version,
        public_version_contract_supported=(
            encoder_version_supported
            and encoder_runtime_supported
            and quality_tool_supported
        ),
        encoder_runtime_major=encoder_runtime_version[0],
        quality_tool=compatibility.quality_tool.casefold(),
        quality_tool_major=quality_tool_version[0],
        measurement_basis=compatibility.measurement_basis,
        assessment_contract=compatibility.assessment_contract,
        preset=compatibility.preset,
        pixel_format=compatibility.pixel_format.casefold(),
        bit_depth=_pixel_format_bit_depth(compatibility.pixel_format),
        output_width=compatibility.output_width,
        output_height=compatibility.output_height,
        frame_rate=compatibility.frame_rate,
        cadence_transform=compatibility.cadence_transform.casefold(),
        video_filter=compatibility.video_filter,
        encoder_parameters=parameters,
        grain_strength=grain_strength,
        grain_denoise=grain_denoise,
        quality_metric=compatibility.quality_metric.upper(),
        quality_target=round(compatibility.quality_target, 3),
        minimum_quality_score=round(compatibility.minimum_quality_score, 3),
        output_container=compatibility.output_container.casefold(),
        target_video_bitrate_bps=_positive_int(
            target_video_bitrate_bps,
            "target video bitrate",
        ),
    )


def build_av1_cold_start_cell(
        *,
        intent_level: CompressionIntentLevel,
        required_traits: Sequence[str],
        compatibility: AV1ColdStartCellCompatibilityV1,
        crf_lower: float,
        crf_center: float,
        crf_upper: float,
        confidence: AV1ColdStartConfidenceV1,
        evaluation: AV1ColdStartEvaluationSummaryV1,
        provenance_version: str,
        provenance_method: str,
        reviewed_at: str,
        review_risks: Sequence[str],
) -> AV1ColdStartPriorCellV1:
    normalized_traits = tuple(sorted({cast(AV1ColdStartTrait, str(value).strip()) for value in required_traits}))
    normalized_risks = tuple(sorted({str(value).strip() for value in review_risks if str(value).strip()}))
    semantic_payload = {
        "intent_level": intent_level,
        "optimization_objective": AV1_COLD_START_INTENT_OBJECTIVES.get(intent_level, ""),
        "required_traits": list(normalized_traits),
        "compatibility": compatibility.to_payload(),
        "crf_range": {
            "lower": round(float(crf_lower), 3),
            "center": round(float(crf_center), 3),
            "upper": round(float(crf_upper), 3),
        },
        "confidence": confidence.to_payload(),
        "evaluation": evaluation.to_payload(),
        "provenance": {
            "version": provenance_version.strip(),
            "method": provenance_method.strip(),
            "reviewed_at": reviewed_at.strip(),
        },
        "review_risks": list(normalized_risks),
    }
    return AV1ColdStartPriorCellV1(
        cell_id=_prior_cell_id(semantic_payload),
        intent_level=intent_level,
        optimization_objective=semantic_payload["optimization_objective"],
        required_traits=normalized_traits,
        compatibility=compatibility,
        crf_lower=semantic_payload["crf_range"]["lower"],
        crf_center=semantic_payload["crf_range"]["center"],
        crf_upper=semantic_payload["crf_range"]["upper"],
        confidence=confidence,
        evaluation=evaluation,
        provenance_version=semantic_payload["provenance"]["version"],
        provenance_method=semantic_payload["provenance"]["method"],
        reviewed_at=semantic_payload["provenance"]["reviewed_at"],
        review_risks=normalized_risks,
    )


def serialize_av1_cold_start_bundle(bundle: AV1ColdStartPriorBundleV1) -> bytes:
    return f"{canonical_json_text(bundle.to_payload())}\n".encode("utf-8")


def av1_cold_start_bundle_from_payload(payload: Mapping[str, Any]) -> AV1ColdStartPriorBundleV1:
    value = object_dict(payload)
    _require_exact_keys(
        value,
        {
            "schema",
            "schema_version",
            "bundle_version",
            "predictor_contract_version",
            "state",
            "published_at",
            "valid_until",
            "provenance",
            "cells",
            "payload_sha256",
        },
        "AV1 prior bundle",
    )
    if value.get("schema") != AV1_COLD_START_BUNDLE_SCHEMA:
        raise AV1ColdStartContractError("AV1 prior bundle schema is unsupported")
    if int_value(value.get("schema_version")) != AV1_COLD_START_BUNDLE_SCHEMA_VERSION:
        raise AV1ColdStartContractError("AV1 prior bundle schema version is unsupported")
    provenance = object_dict(value.get("provenance"))
    _require_exact_keys(
        provenance,
        {"source", "method", "privacy_contract", "governance_issue"},
        "AV1 prior bundle provenance",
    )
    cells = tuple(
        sorted(
            (_prior_cell_from_payload(object_dict(raw_cell)) for raw_cell in object_list(value.get("cells"))),
            key=lambda cell: cell.cell_id,
        )
    )
    bundle = AV1ColdStartPriorBundleV1(
        bundle_version=_required_text(value.get("bundle_version"), "bundle version"),
        predictor_contract_version=_required_text(
            value.get("predictor_contract_version"),
            "predictor contract version",
        ),
        state=cast(AV1ColdStartBundleState, _required_text(value.get("state"), "bundle state")),
        published_at=_required_text(value.get("published_at"), "publication timestamp"),
        valid_until=_required_text(value.get("valid_until"), "expiration timestamp"),
        provenance_source=_required_text(provenance.get("source"), "provenance source"),
        provenance_method=_required_text(provenance.get("method"), "provenance method"),
        privacy_contract=_required_text(provenance.get("privacy_contract"), "privacy contract"),
        governance_issue=_required_text(provenance.get("governance_issue"), "governance issue"),
        cells=cells,
    )
    if value.get("payload_sha256") != bundle.payload_sha256:
        raise AV1ColdStartContractError("AV1 prior bundle digest does not match its payload")
    _assert_public_payload_safe(value)
    return bundle


@lru_cache(maxsize=1)
def load_packaged_av1_cold_start_bundle() -> AV1ColdStartBundleLoadResult:
    try:
        artifact = resources.files(AV1_COLD_START_ARTIFACT_PACKAGE).joinpath(AV1_COLD_START_ARTIFACT_NAME)
        raw_bytes = artifact.read_bytes()
        payload = object_dict(json.loads(raw_bytes.decode("utf-8")))
        bundle = av1_cold_start_bundle_from_payload(payload)
        if raw_bytes != serialize_av1_cold_start_bundle(bundle):
            raise AV1ColdStartContractError("AV1 prior bundle bytes are not canonical")
        return AV1ColdStartBundleLoadResult(bundle=bundle, fallback_reason=None)
    except (FileNotFoundError, ModuleNotFoundError):
        return AV1ColdStartBundleLoadResult(bundle=None, fallback_reason="public_bundle_missing")
    except (UnicodeDecodeError, json.JSONDecodeError, AV1ColdStartContractError, TypeError, ValueError):
        return AV1ColdStartBundleLoadResult(bundle=None, fallback_reason="public_bundle_invalid")


def build_av1_cold_start_request(
        *,
        sample_item: Mapping[str, Any],
        compatibility_payload: Mapping[str, Any],
        configured_min_crf: int,
        configured_max_crf: int,
        as_of: str,
) -> AV1ColdStartRequestV1:
    item = object_dict(sample_item)
    intent = compression_intent_from_item(item)
    if intent.requires_confirmation:
        raise AV1ColdStartContractError("compression_intent_unconfirmed")
    decision = object_dict(item.get("media_fingerprint_decision"))
    if str(decision.get("status") or "") != "measured":
        raise AV1ColdStartContractError("content_evidence_missing")
    replay_context = content_intent_replay_context(item)
    compatibility = content_intent_boundary_compatibility_from_payload(compatibility_payload)
    stream_budget = object_dict(item.get("stream_budget_ledger"))
    target_video_bitrate_bps = int_value(
        object_dict(stream_budget.get("totals")).get("remaining_video_bitrate_bps")
    )
    if target_video_bitrate_bps <= 0:
        raise AV1ColdStartContractError("target_video_bitrate_missing")
    return AV1ColdStartRequestV1(
        intent_level=intent.level,
        intent_semantic_id=intent.semantic_id,
        content_traits=av1_cold_start_traits(replay_context.content_traits),
        runtime_compatibility=build_av1_cold_start_runtime_compatibility(
            compatibility,
            target_video_bitrate_bps=target_video_bitrate_bps,
        ),
        configured_min_crf=configured_min_crf,
        configured_max_crf=configured_max_crf,
        as_of=as_of,
    )


def av1_cold_start_traits(measured_traits: Sequence[str]) -> tuple[AV1ColdStartTrait, ...]:
    normalized = {str(value).strip().casefold() for value in measured_traits if str(value).strip()}
    traits: set[AV1ColdStartTrait] = set()
    if "animation_cues" in normalized:
        traits.add("animation")
    if normalized.intersection({"dark_luma", "dark_gradient_banding_risk"}):
        traits.add("darkness")
    if "high_motion" in normalized:
        traits.add("motion")
    if normalized.intersection({
        "likely_film_grain",
        "likely_analog_noise",
        "compression_noise_advisory",
        "uncertain_noise_mix",
    }):
        traits.add("grain_noise")
    if "high_texture" in normalized:
        traits.add("texture_detail")
    if "low_motion_dialogue" in normalized:
        traits.add("low_motion_dialogue")
    if "typical" in normalized:
        traits.add("typical")
    core_traits = traits.difference({"typical"})
    if len(core_traits) > 1:
        traits.add("mixed")
    if not traits or "unknown" in normalized:
        traits.add("unknown")
    return tuple(sorted(traits))


def predict_public_av1_cold_start(
        bundle: AV1ColdStartPriorBundleV1,
        request: AV1ColdStartRequestV1,
) -> AV1ColdStartPrediction:
    bundle_id = bundle.bundle_id
    if bundle.state != "active":
        return _no_recommendation(
            request,
            "no_public_evidence" if bundle.state == "validation_pending" else "public_bundle_retired",
            bundle_id=bundle_id,
        )
    request_time = _parse_timestamp(request.as_of, "request timestamp")
    if request_time < _parse_timestamp(bundle.published_at, "bundle publication"):
        return _no_recommendation(
            request,
            "public_bundle_not_yet_effective",
            bundle_id=bundle_id,
            public_evidence_present=bool(bundle.cells),
        )
    if request_time >= _parse_timestamp(
            bundle.valid_until,
            "bundle expiration",
    ):
        return _no_recommendation(
            request,
            "public_bundle_stale",
            bundle_id=bundle_id,
            public_evidence_present=bool(bundle.cells),
        )
    if not request.runtime_compatibility.public_version_contract_supported:
        return _no_recommendation(
            request,
            "public_runtime_version_unsupported",
            bundle_id=bundle_id,
        )
    intent_cells = [cell for cell in bundle.cells if cell.intent_level == request.intent_level]
    trait_cells = [cell for cell in intent_cells if cell.required_traits == request.content_traits]
    compatible_cells = [
        cell
        for cell in trait_cells
        if cell.compatibility.matches(request.runtime_compatibility)
    ]
    if not intent_cells:
        return _no_recommendation(request, "no_public_evidence", bundle_id=bundle_id)
    if not trait_cells:
        return _no_recommendation(
            request,
            "content_profile_uncovered",
            bundle_id=bundle_id,
            public_evidence_present=True,
        )
    if not compatible_cells:
        return _no_recommendation(
            request,
            "public_evidence_incompatible",
            bundle_id=bundle_id,
            public_evidence_present=True,
        )
    top_specificity = max(len(cell.required_traits) for cell in compatible_cells)
    top_cells = [cell for cell in compatible_cells if len(cell.required_traits) == top_specificity]
    ranges = {(cell.crf_lower, cell.crf_center, cell.crf_upper) for cell in top_cells}
    if len(ranges) != 1:
        return _no_recommendation(
            request,
            "public_evidence_conflicting",
            bundle_id=bundle_id,
            public_evidence_present=True,
            provenance_ids=tuple(sorted(cell.cell_id for cell in top_cells)),
        )
    selected = sorted(
        top_cells,
        key=lambda cell: (-cell.confidence.score, -cell.confidence.evidence_count, cell.cell_id),
    )[0]
    if not selected.confidence.actionable:
        return _no_recommendation(
            request,
            "public_evidence_low_confidence",
            bundle_id=bundle_id,
            public_evidence_present=True,
            provenance_ids=(selected.cell_id,),
        )
    if not selected.evaluation.supports_publication:
        return _no_recommendation(
            request,
            "public_evidence_not_validated",
            bundle_id=bundle_id,
            public_evidence_present=True,
            provenance_ids=(selected.cell_id,),
        )
    return _recommended_prediction(
        request,
        source="public",
        crf_lower=selected.crf_lower,
        crf_center=selected.crf_center,
        crf_upper=selected.crf_upper,
        confidence=selected.confidence.level,
        confidence_score=selected.confidence.score,
        provenance_ids=(bundle_id, selected.cell_id),
        review_risks=tuple(sorted(set(selected.review_risks).union(_trait_review_risks(request.content_traits)))),
        bundle_id=bundle_id,
        public_evidence_present=True,
    )


def predict_local_av1_cold_start(
        all_state: ContentIntentPersonalizationState | None,
        fresh_state: ContentIntentPersonalizationState | None,
        request: AV1ColdStartRequestV1,
        *,
        freshness_fallback_reason: str | None = None,
) -> AV1ColdStartPrediction:
    if all_state is None:
        return _no_recommendation(request, "no_local_evidence")
    all_cohort = _first_evidence_cohort(all_state)
    if all_cohort is None:
        return _no_recommendation(request, "no_local_evidence")
    local_provenance = _local_provenance_id(all_cohort)
    if freshness_fallback_reason is not None:
        return _no_recommendation(
            request,
            freshness_fallback_reason,
            local_scope=all_cohort.scope,
            local_evidence_present=True,
            provenance_ids=(local_provenance,),
        )
    if all_cohort.boundary_status == "conflicting":
        return _no_recommendation(
            request,
            "local_evidence_conflicting",
            local_scope=all_cohort.scope,
            local_evidence_present=True,
            provenance_ids=(local_provenance,),
        )
    fresh_cohort = _cohort_for_scope(fresh_state, all_cohort.scope)
    if fresh_cohort is None or fresh_cohort.observation_count == 0:
        return _no_recommendation(
            request,
            "local_evidence_stale",
            local_scope=all_cohort.scope,
            local_evidence_present=True,
            provenance_ids=(local_provenance,),
        )
    local_provenance = _local_provenance_id(fresh_cohort)
    if fresh_cohort.boundary_status == "conflicting":
        return _no_recommendation(
            request,
            "local_evidence_conflicting",
            local_scope=fresh_cohort.scope,
            local_evidence_present=True,
            provenance_ids=(local_provenance,),
        )
    effective_confidence: AV1ColdStartConfidence = fresh_cohort.confidence
    if fresh_cohort.scope == "item" and fresh_cohort.actionable:
        effective_confidence = "moderate"
    elif fresh_cohort.confidence not in {"moderate", "high"}:
        return _no_recommendation(
            request,
            "local_evidence_low_confidence",
            local_scope=fresh_cohort.scope,
            local_evidence_present=True,
            provenance_ids=(local_provenance,),
        )
    if (
            fresh_cohort.median_acceptable_crf is None
            or fresh_cohort.minimum_acceptable_crf is None
            or fresh_cohort.maximum_acceptable_crf is None
    ):
        return _no_recommendation(
            request,
            "local_evidence_missing_crf_distribution",
            local_scope=fresh_cohort.scope,
            local_evidence_present=True,
            provenance_ids=(local_provenance,),
        )
    if (
            fresh_cohort.minimum_acceptable_bitrate_bps is None
            or fresh_cohort.maximum_acceptable_bitrate_bps is None
            or not fresh_cohort.minimum_acceptable_bitrate_bps
            <= request.runtime_compatibility.target_video_bitrate_bps
            <= fresh_cohort.maximum_acceptable_bitrate_bps
    ):
        return _no_recommendation(
            request,
            "local_evidence_target_incompatible",
            local_scope=fresh_cohort.scope,
            local_evidence_present=True,
            provenance_ids=(local_provenance,),
        )
    if (
            fresh_cohort.acceptable_crf_mad is None
            or fresh_cohort.acceptable_crf_mad > AV1_COLD_START_MAX_LOCAL_CRF_MAD
            or fresh_cohort.maximum_acceptable_crf - fresh_cohort.minimum_acceptable_crf
            > AV1_COLD_START_MAX_LOCAL_CRF_RANGE
    ):
        return _no_recommendation(
            request,
            "local_evidence_crf_dispersion",
            local_scope=fresh_cohort.scope,
            local_evidence_present=True,
            provenance_ids=(local_provenance,),
        )
    return _recommended_prediction(
        request,
        source="local",
        crf_lower=fresh_cohort.minimum_acceptable_crf,
        crf_center=fresh_cohort.median_acceptable_crf,
        crf_upper=fresh_cohort.maximum_acceptable_crf,
        confidence=effective_confidence,
        confidence_score=_local_confidence_score(fresh_cohort),
        provenance_ids=(local_provenance,),
        review_risks=_trait_review_risks(request.content_traits),
        local_scope=fresh_cohort.scope,
        local_evidence_present=True,
    )


def combine_av1_cold_start_predictions(
        public: AV1ColdStartPrediction,
        local: AV1ColdStartPrediction,
        request: AV1ColdStartRequestV1,
) -> AV1ColdStartPrediction:
    if local.local_evidence_present and not local.recommended:
        return _no_recommendation(
            request,
            cast(str, local.fallback_reason or "local_evidence_unavailable"),
            bundle_id=public.bundle_id,
            local_scope=local.local_scope,
            local_evidence_present=True,
            public_evidence_present=public.public_evidence_present,
            provenance_ids=tuple(sorted(set(public.provenance_ids + local.provenance_ids))),
        )
    if public.public_evidence_present and not public.recommended:
        return _no_recommendation(
            request,
            cast(str, public.fallback_reason or "public_evidence_unavailable"),
            bundle_id=public.bundle_id,
            local_scope=local.local_scope,
            local_evidence_present=local.local_evidence_present,
            public_evidence_present=True,
            provenance_ids=tuple(sorted(set(public.provenance_ids + local.provenance_ids))),
        )
    if public.recommended and local.recommended:
        lower = max(cast(float, public.crf_lower), cast(float, local.crf_lower))
        upper = min(cast(float, public.crf_upper), cast(float, local.crf_upper))
        local_center = cast(float, local.requested_crf)
        if lower > upper or not lower <= local_center <= upper:
            return _no_recommendation(
                request,
                "public_local_evidence_conflicting",
                bundle_id=public.bundle_id,
                local_scope=local.local_scope,
                local_evidence_present=True,
                public_evidence_present=True,
                provenance_ids=tuple(sorted(set(public.provenance_ids + local.provenance_ids))),
            )
        confidence = _lower_confidence(public.confidence, local.confidence)
        return _recommended_prediction(
            request,
            source="combined",
            crf_lower=lower,
            crf_center=local_center,
            crf_upper=upper,
            confidence=confidence,
            confidence_score=min(public.confidence_score, local.confidence_score),
            provenance_ids=tuple(sorted(set(public.provenance_ids + local.provenance_ids))),
            review_risks=tuple(sorted(set(public.review_risks + local.review_risks))),
            bundle_id=public.bundle_id,
            local_scope=local.local_scope,
            local_evidence_present=True,
            public_evidence_present=True,
        )
    if local.recommended:
        return local
    if public.recommended:
        return public
    fallback_reason = public.fallback_reason or local.fallback_reason or "no_compatible_evidence"
    return _no_recommendation(
        request,
        fallback_reason,
        bundle_id=public.bundle_id,
        local_evidence_present=local.local_evidence_present,
        public_evidence_present=public.public_evidence_present,
    )


def plan_av1_cold_start(
        connection: DBClient,
        *,
        sample_item: Mapping[str, Any],
        prefix: str,
        compatibility_payload: Mapping[str, Any],
        configured_min_crf: int,
        configured_max_crf: int,
        as_of: str,
        bundle_result: AV1ColdStartBundleLoadResult | None = None,
) -> AV1ColdStartPrediction:
    try:
        request = build_av1_cold_start_request(
            sample_item=sample_item,
            compatibility_payload=compatibility_payload,
            configured_min_crf=configured_min_crf,
            configured_max_crf=configured_max_crf,
            as_of=as_of,
        )
    except ValueError as exc:
        return _no_recommendation(None, _request_fallback_reason(exc))
    loaded = bundle_result or load_packaged_av1_cold_start_bundle()
    if loaded.bundle is None:
        return _no_recommendation(
            request,
            _bundle_fallback_reason(loaded.fallback_reason),
        )
    public_prediction = predict_public_av1_cold_start(loaded.bundle, request)
    try:
        local_compatibility = content_intent_boundary_compatibility_from_payload(compatibility_payload)
        replay_context = content_intent_replay_context(sample_item)
    except ValueError:
        return _no_recommendation(
            request,
            "cold_start_request_invalid",
            bundle_id=public_prediction.bundle_id,
            public_evidence_present=public_prediction.public_evidence_present,
            provenance_ids=public_prediction.provenance_ids,
        )
    try:
        loaded_rows = load_current_content_intent_boundary_observations(
            connection,
            intent_semantic_id=request.intent_semantic_id,
            compatibility_key=local_compatibility.compatibility_key,
        )
        rows = _rows_for_replay_context(loaded_rows, replay_context)
        all_state = _replay_local_state(
            rows,
            replay_context=replay_context,
            prefix=prefix,
            intent_semantic_id=request.intent_semantic_id,
            compatibility_key=local_compatibility.compatibility_key,
        )
    except ValueError:
        return _invalid_local_planning_prediction(request, public_prediction)
    all_cohort = _first_evidence_cohort(all_state) if all_state is not None else None
    request_time = _parse_timestamp(as_of, "request timestamp")
    cutoff = request_time - timedelta(
        days=AV1_COLD_START_LOCAL_MAX_AGE_DAYS,
    )
    fresh_rows = [row for row in rows if _row_at_or_after(row, cutoff, request_time)]
    try:
        selected_scope_rows = (
            content_intent_replay_scope_rows(
                rows,
                source_id=replay_context.source_id,
                content_id=replay_context.content_id,
                prefix=prefix,
                content_profile_id=replay_context.content_profile_id,
                intent_semantic_id=request.intent_semantic_id,
                compatibility_key=local_compatibility.compatibility_key,
                scope=all_cohort.scope,
            )
            if all_cohort is not None
            else ()
        )
    except ValueError:
        return _invalid_local_planning_prediction(request, public_prediction)
    selected_timestamp_statuses = {
        _row_timestamp_status(row, cutoff, request_time)
        for row in selected_scope_rows
    }
    if "unversioned" in selected_timestamp_statuses:
        freshness_fallback_reason = "local_evidence_unversioned"
    elif "future" in selected_timestamp_statuses:
        freshness_fallback_reason = "local_evidence_future_dated"
    else:
        freshness_fallback_reason = None
    try:
        fresh_state = _replay_local_state(
            fresh_rows,
            replay_context=replay_context,
            prefix=prefix,
            intent_semantic_id=request.intent_semantic_id,
            compatibility_key=local_compatibility.compatibility_key,
        )
    except ValueError:
        return _invalid_local_planning_prediction(request, public_prediction)
    local_prediction = predict_local_av1_cold_start(
        all_state,
        fresh_state,
        request,
        freshness_fallback_reason=freshness_fallback_reason,
    )
    return combine_av1_cold_start_predictions(public_prediction, local_prediction, request)


def unavailable_av1_cold_start_prediction(fallback_reason: str) -> AV1ColdStartPrediction:
    return _no_recommendation(None, fallback_reason)


def _prior_cell_from_payload(payload: Mapping[str, Any]) -> AV1ColdStartPriorCellV1:
    value = object_dict(payload)
    _require_exact_keys(
        value,
        {
            "cell_id",
            "intent_level",
            "optimization_objective",
            "required_traits",
            "compatibility",
            "crf_range",
            "confidence",
            "evaluation",
            "provenance",
            "review_risks",
        },
        "AV1 prior cell",
    )
    compatibility = _cell_compatibility_from_payload(object_dict(value.get("compatibility")))
    crf_range = object_dict(value.get("crf_range"))
    _require_exact_keys(crf_range, {"lower", "center", "upper"}, "AV1 prior CRF range")
    confidence_payload = object_dict(value.get("confidence"))
    _require_exact_keys(
        confidence_payload,
        {"level", "score", "evidence_count", "independent_source_count"},
        "AV1 prior confidence",
    )
    confidence = AV1ColdStartConfidenceV1(
        level=cast(AV1ColdStartConfidence, _required_text(confidence_payload.get("level"), "confidence level")),
        score=_finite_float(confidence_payload.get("score"), "confidence score"),
        evidence_count=_nonnegative_int(confidence_payload.get("evidence_count"), "evidence count"),
        independent_source_count=_nonnegative_int(
            confidence_payload.get("independent_source_count"),
            "independent source count",
        ),
    )
    evaluation_payload = object_dict(value.get("evaluation"))
    _require_exact_keys(
        evaluation_payload,
        {
            "held_out_count",
            "range_hit_count",
            "baseline_candidate_count",
            "guided_candidate_count",
            "quality_floor_regression_count",
            "final_size_regression_count",
            "visual_rejection_regression_count",
            "operator_attention_regression_count",
        },
        "AV1 prior evaluation",
    )
    evaluation = AV1ColdStartEvaluationSummaryV1(
        held_out_count=_nonnegative_int(evaluation_payload.get("held_out_count"), "held-out count"),
        range_hit_count=_nonnegative_int(evaluation_payload.get("range_hit_count"), "range hit count"),
        baseline_candidate_count=_nonnegative_int(
            evaluation_payload.get("baseline_candidate_count"),
            "baseline candidate count",
        ),
        guided_candidate_count=_nonnegative_int(
            evaluation_payload.get("guided_candidate_count"),
            "guided candidate count",
        ),
        quality_floor_regression_count=_nonnegative_int(
            evaluation_payload.get("quality_floor_regression_count"),
            "quality-floor regression count",
        ),
        final_size_regression_count=_nonnegative_int(
            evaluation_payload.get("final_size_regression_count"),
            "final-size regression count",
        ),
        visual_rejection_regression_count=_nonnegative_int(
            evaluation_payload.get("visual_rejection_regression_count"),
            "visual rejection regression count",
        ),
        operator_attention_regression_count=_nonnegative_int(
            evaluation_payload.get("operator_attention_regression_count"),
            "operator-attention regression count",
        ),
    )
    provenance = object_dict(value.get("provenance"))
    _require_exact_keys(provenance, {"version", "method", "reviewed_at"}, "AV1 prior cell provenance")
    return AV1ColdStartPriorCellV1(
        cell_id=_required_text(value.get("cell_id"), "cell identity"),
        intent_level=cast(CompressionIntentLevel, _required_text(value.get("intent_level"), "intent level")),
        optimization_objective=_required_text(
            value.get("optimization_objective"),
            "optimization objective",
        ),
        required_traits=tuple(
            sorted(
                cast(AV1ColdStartTrait, str(raw_trait).strip())
                for raw_trait in object_list(value.get("required_traits"))
                if str(raw_trait).strip()
            )
        ),
        compatibility=compatibility,
        crf_lower=_finite_float(crf_range.get("lower"), "lower CRF"),
        crf_center=_finite_float(crf_range.get("center"), "center CRF"),
        crf_upper=_finite_float(crf_range.get("upper"), "upper CRF"),
        confidence=confidence,
        evaluation=evaluation,
        provenance_version=_required_text(provenance.get("version"), "provenance version"),
        provenance_method=_required_text(provenance.get("method"), "provenance method"),
        reviewed_at=_required_text(provenance.get("reviewed_at"), "review timestamp"),
        review_risks=tuple(
            sorted(str(raw_risk).strip() for raw_risk in object_list(value.get("review_risks")) if str(raw_risk).strip())
        ),
    )


def _cell_compatibility_from_payload(payload: Mapping[str, Any]) -> AV1ColdStartCellCompatibilityV1:
    value = object_dict(payload)
    _require_exact_keys(
        value,
        {
            "encoder",
            "encoder_version_min",
            "encoder_version_max_exclusive",
            "encoder_runtime_major",
            "quality_tool",
            "quality_tool_major",
            "measurement_basis",
            "assessment_contract",
            "preset",
            "pixel_format",
            "bit_depth",
            "output_width",
            "output_height",
            "frame_rate",
            "cadence_transform",
            "video_filter",
            "encoder_parameters",
            "grain_strength",
            "grain_denoise",
            "quality_metric",
            "quality_target",
            "minimum_quality_score",
            "output_container",
            "target_video_bitrate_min_bps",
            "target_video_bitrate_max_bps",
        },
        "AV1 prior cell compatibility",
    )
    return AV1ColdStartCellCompatibilityV1(
        encoder=_normalized_encoder(_required_text(value.get("encoder"), "encoder")),
        encoder_version_min=_parse_version(value.get("encoder_version_min"), "minimum encoder version"),
        encoder_version_max_exclusive=_parse_version(
            value.get("encoder_version_max_exclusive"),
            "maximum encoder version",
        ),
        encoder_runtime_major=_nonnegative_int(value.get("encoder_runtime_major"), "encoder runtime major"),
        quality_tool=_required_text(value.get("quality_tool"), "quality tool").casefold(),
        quality_tool_major=_nonnegative_int(value.get("quality_tool_major"), "quality tool major"),
        measurement_basis=_required_text(value.get("measurement_basis"), "measurement basis").casefold(),
        assessment_contract=_required_text(value.get("assessment_contract"), "assessment contract"),
        preset=_nonnegative_int(value.get("preset"), "preset"),
        pixel_format=_required_text(value.get("pixel_format"), "pixel format").casefold(),
        bit_depth=_nonnegative_int(value.get("bit_depth"), "bit depth"),
        output_width=_positive_int(value.get("output_width"), "output width"),
        output_height=_positive_int(value.get("output_height"), "output height"),
        frame_rate=_required_text(value.get("frame_rate"), "frame rate"),
        cadence_transform=_required_text(value.get("cadence_transform"), "cadence transform").casefold(),
        video_filter=_optional_text(value.get("video_filter")),
        encoder_parameters=tuple(
            sorted(_required_text(raw_parameter, "encoder parameter") for raw_parameter in object_list(value.get("encoder_parameters")))
        ),
        grain_strength=_nonnegative_int(value.get("grain_strength"), "grain strength"),
        grain_denoise=_nonnegative_int(value.get("grain_denoise"), "grain denoise"),
        quality_metric=_required_text(value.get("quality_metric"), "quality metric").upper(),
        quality_target=round(_finite_float(value.get("quality_target"), "quality target"), 3),
        minimum_quality_score=round(
            _finite_float(value.get("minimum_quality_score"), "minimum quality score"),
            3,
        ),
        output_container=_required_text(value.get("output_container"), "output container").casefold(),
        target_video_bitrate_min_bps=_positive_int(
            value.get("target_video_bitrate_min_bps"),
            "minimum target video bitrate",
        ),
        target_video_bitrate_max_bps=_positive_int(
            value.get("target_video_bitrate_max_bps"),
            "maximum target video bitrate",
        ),
    )


def _replay_local_state(
        rows: Sequence[Mapping[str, Any]],
        *,
        replay_context: Any,
        prefix: str,
        intent_semantic_id: str,
        compatibility_key: str,
) -> ContentIntentPersonalizationState | None:
    if not rows:
        return None
    return replay_content_intent_personalization(
        rows,
        source_id=replay_context.source_id,
        content_id=replay_context.content_id,
        prefix=prefix,
        content_profile_id=replay_context.content_profile_id,
        intent_semantic_id=intent_semantic_id,
        compatibility_key=compatibility_key,
    )


def _rows_for_replay_context(
        rows: Sequence[Mapping[str, Any]],
        replay_context: ContentIntentReplayContext,
) -> list[Mapping[str, Any]]:
    return [
        row
        for row in rows
        if (
            str(row.get("source_id") or "") == replay_context.source_id
            and str(row.get("content_id") or "") == replay_context.content_id
        )
        or str(row.get("content_profile_id") or "") == replay_context.content_profile_id
    ]


def _recommended_prediction(
        request: AV1ColdStartRequestV1,
        *,
        source: AV1ColdStartPredictionSource,
        crf_lower: float,
        crf_center: float,
        crf_upper: float,
        confidence: AV1ColdStartConfidence,
        confidence_score: float,
        provenance_ids: tuple[str, ...],
        review_risks: tuple[str, ...],
        bundle_id: str | None = None,
        local_scope: str | None = None,
        local_evidence_present: bool = False,
        public_evidence_present: bool = False,
) -> AV1ColdStartPrediction:
    if request.intent_level not in AV1_COLD_START_PROBE_SUPPORTED_INTENTS:
        return _no_recommendation(
            request,
            "compression_intent_requires_directional_search",
            bundle_id=bundle_id,
            local_scope=local_scope,
            local_evidence_present=local_evidence_present,
            public_evidence_present=public_evidence_present,
            provenance_ids=provenance_ids,
        )
    if (
            crf_lower < request.configured_min_crf
            or crf_upper > request.configured_max_crf
            or crf_lower > crf_center
            or crf_center > crf_upper
    ):
        return _no_recommendation(
            request,
            "recommended_range_outside_configured_bounds",
            bundle_id=bundle_id,
            local_scope=local_scope,
            local_evidence_present=local_evidence_present,
            public_evidence_present=public_evidence_present,
            provenance_ids=provenance_ids,
        )
    minimum_candidate_crf = math.ceil(crf_lower)
    maximum_candidate_crf = math.floor(crf_upper)
    if minimum_candidate_crf > maximum_candidate_crf:
        return _no_recommendation(
            request,
            "recommended_range_has_no_integer_probe",
            bundle_id=bundle_id,
            local_scope=local_scope,
            local_evidence_present=local_evidence_present,
            public_evidence_present=public_evidence_present,
            provenance_ids=provenance_ids,
        )
    candidate_crf = min(
        max(_round_crf(crf_center), minimum_candidate_crf),
        maximum_candidate_crf,
    )
    return AV1ColdStartPrediction(
        status="recommended",
        source=source,
        fallback_reason=None,
        intent_level=request.intent_level,
        optimization_objective=AV1_COLD_START_INTENT_OBJECTIVES[request.intent_level],
        content_traits=tuple(request.content_traits),
        requested_crf=round(crf_center, 3),
        candidate_crf=candidate_crf,
        crf_lower=round(crf_lower, 3),
        crf_upper=round(crf_upper, 3),
        confidence=confidence,
        confidence_score=round(confidence_score, 3),
        provenance_ids=tuple(sorted(set(provenance_ids))),
        review_risks=tuple(sorted(set(review_risks))),
        search_signature_id=request.search_signature_id,
        bundle_id=bundle_id,
        local_scope=local_scope,
        local_evidence_present=local_evidence_present,
        public_evidence_present=public_evidence_present,
    )


def _no_recommendation(
        request: AV1ColdStartRequestV1 | None,
        fallback_reason: str,
        *,
        bundle_id: str | None = None,
        local_scope: str | None = None,
        local_evidence_present: bool = False,
        public_evidence_present: bool = False,
        provenance_ids: tuple[str, ...] = (),
) -> AV1ColdStartPrediction:
    intent_level = request.intent_level if request is not None else None
    return AV1ColdStartPrediction(
        status="no_recommendation",
        source="none",
        fallback_reason=fallback_reason,
        intent_level=intent_level,
        optimization_objective=(
            AV1_COLD_START_INTENT_OBJECTIVES[intent_level]
            if intent_level in AV1_COLD_START_INTENT_OBJECTIVES
            else None
        ),
        content_traits=tuple(request.content_traits) if request is not None else (),
        requested_crf=None,
        candidate_crf=None,
        crf_lower=None,
        crf_upper=None,
        confidence="none",
        confidence_score=0.0,
        provenance_ids=tuple(sorted(set(provenance_ids))),
        review_risks=_trait_review_risks(request.content_traits) if request is not None else (),
        search_signature_id=request.search_signature_id if request is not None else None,
        bundle_id=bundle_id,
        local_scope=local_scope,
        local_evidence_present=local_evidence_present,
        public_evidence_present=public_evidence_present,
    )


def _first_evidence_cohort(state: ContentIntentPersonalizationState) -> ContentIntentBoundaryCohort | None:
    return next((cohort for cohort in state.cohorts if cohort.observation_count > 0), None)


def _cohort_for_scope(
        state: ContentIntentPersonalizationState | None,
        scope: str,
) -> ContentIntentBoundaryCohort | None:
    if state is None:
        return None
    return next((cohort for cohort in state.cohorts if cohort.scope == scope), None)


def _local_confidence_score(cohort: ContentIntentBoundaryCohort) -> float:
    if cohort.scope == "item" and cohort.actionable:
        return 0.8
    score = {"none": 0.0, "limited": 0.45, "moderate": 0.75, "high": 0.9}[cohort.confidence]
    return min(0.99, score + min(cohort.source_count, 10) * 0.005)


def _local_provenance_id(cohort: ContentIntentBoundaryCohort) -> str:
    return f"acsl1_{stable_json_hash((
        cohort.scope,
        cohort.cohort_id,
        cohort.evidence_snapshot_id,
    ))[:24]}"


def _lower_confidence(
        first: AV1ColdStartConfidence,
        second: AV1ColdStartConfidence,
) -> AV1ColdStartConfidence:
    order: tuple[AV1ColdStartConfidence, ...] = ("none", "limited", "moderate", "high")
    return order[min(order.index(first), order.index(second))]


def _trait_review_risks(traits: Sequence[str]) -> tuple[str, ...]:
    mapping = {
        "animation": "flat_regions_and_edges",
        "darkness": "banding_dark_scene_damage",
        "motion": "motion_breakup",
        "grain_noise": "grain_noise_treatment",
        "texture_detail": "softness_detail_loss",
        "low_motion_dialogue": "dialogue_facial_detail",
        "mixed": "mixed_or_unknown_content",
        "unknown": "mixed_or_unknown_content",
    }
    return tuple(sorted({mapping[trait] for trait in traits if trait in mapping}))


def _row_at_or_after(
        row: Mapping[str, Any],
        cutoff: datetime,
        as_of: datetime | None = None,
) -> bool:
    return _row_timestamp_status(row, cutoff, as_of) == "fresh"


def _row_timestamp_status(
        row: Mapping[str, Any],
        cutoff: datetime,
        as_of: datetime | None = None,
) -> str:
    try:
        recorded_at = str(row.get("recorded_at") or "").strip()
        provenance = object_dict(json.loads(str(row.get("provenance_json") or "{}")))
        if str(provenance.get("recorded_at") or "").strip() != recorded_at:
            return "unversioned"
        timestamp = _parse_timestamp(recorded_at, "local observation timestamp")
        if as_of is not None and timestamp > as_of:
            return "future"
        return "fresh" if timestamp >= cutoff else "stale"
    except (AV1ColdStartContractError, TypeError, ValueError, json.JSONDecodeError):
        return "unversioned"


def _grain_settings(parameters: Sequence[str]) -> tuple[int, int]:
    values: dict[str, int] = {}
    for parameter in parameters:
        key, separator, raw_value = str(parameter).partition("=")
        if separator and key in {"film-grain", "film-grain-denoise"}:
            try:
                values[key] = int(raw_value)
            except ValueError as exc:
                raise AV1ColdStartContractError("AV1 prior grain settings are not numeric") from exc
    grain_strength = values.get("film-grain", 0)
    grain_denoise = values.get("film-grain-denoise", 0)
    if not 0 <= grain_strength <= 50 or grain_denoise not in {0, 1}:
        raise AV1ColdStartContractError("AV1 prior grain settings are invalid")
    return grain_strength, grain_denoise


def _pixel_format_bit_depth(pixel_format: str) -> int:
    normalized = pixel_format.strip().casefold()
    match = re.search(r"p(10|12)(?:le|be)?$", normalized)
    if match is not None:
        return int(match.group(1))
    if normalized.endswith("p") or normalized.endswith("p8"):
        return 8
    raise AV1ColdStartContractError("AV1 prior pixel format has unsupported bit depth")


def _normalized_encoder(value: str) -> str:
    normalized = value.strip().casefold()
    if normalized in {"libsvtav1", "svt-av1", "svt_av1"}:
        return "svt-av1"
    raise AV1ColdStartContractError("AV1 cold-start priors support only SVT-AV1")


def _parse_version(value: object, label: str) -> tuple[int, int, int]:
    match = _VERSION_RE.search(str(value or ""))
    if match is None:
        raise AV1ColdStartContractError(f"AV1 prior {label} is not a semantic version")
    return int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)


def _runtime_version(value: object, label: str) -> tuple[tuple[int, int, int], bool]:
    try:
        return _parse_version(value, label), True
    except AV1ColdStartContractError:
        return (0, 0, 0), False


def _format_version(value: tuple[int, int, int]) -> str:
    return ".".join(str(component) for component in value)


def _parse_timestamp(value: object, label: str) -> datetime:
    normalized = str(value or "").strip()
    if not normalized:
        raise AV1ColdStartContractError(f"{label} is required")
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AV1ColdStartContractError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _assert_public_payload_safe(value: object) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized_key = str(key).strip().casefold()
            if normalized_key in AV1_COLD_START_PRIVATE_PUBLIC_KEYS:
                raise AV1ColdStartContractError(f"AV1 prior public payload contains private field {key}")
            _assert_public_payload_safe(child)
        return
    if isinstance(value, list | tuple):
        for child in value:
            _assert_public_payload_safe(child)
        return
    if isinstance(value, str) and AV1_COLD_START_PRIVATE_PUBLIC_TEXT_RE.search(value):
        raise AV1ColdStartContractError("AV1 prior public payload contains a local path or runtime artifact")


def assert_av1_cold_start_public_payload_safe(value: object) -> None:
    _assert_public_payload_safe(value)


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected.difference(actual))
        unexpected = sorted(actual.difference(expected))
        raise AV1ColdStartContractError(
            f"{label} fields do not match the contract; missing={missing}, unexpected={unexpected}"
        )


def _prior_cell_id(payload: Mapping[str, Any]) -> str:
    return f"acspc1_{stable_json_hash(payload)[:32]}"


def _request_fallback_reason(exc: ValueError) -> str:
    message = str(exc).strip()
    if message in AV1_COLD_START_REQUEST_FALLBACK_CODES:
        return message
    return "cold_start_request_invalid"


def _bundle_fallback_reason(value: str | None) -> str:
    if value in AV1_COLD_START_BUNDLE_FALLBACK_CODES:
        return cast(str, value)
    return "public_bundle_invalid"


def _invalid_local_planning_prediction(
        request: AV1ColdStartRequestV1,
        public_prediction: AV1ColdStartPrediction,
) -> AV1ColdStartPrediction:
    return _no_recommendation(
        request,
        "cold_start_request_invalid",
        bundle_id=public_prediction.bundle_id,
        public_evidence_present=public_prediction.public_evidence_present,
        provenance_ids=public_prediction.provenance_ids,
    )


def _required_text(value: object, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise AV1ColdStartContractError(f"AV1 prior {label} is required")
    return normalized


def _optional_text(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _finite_float(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AV1ColdStartContractError(f"AV1 prior {label} must be numeric")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise AV1ColdStartContractError(f"AV1 prior {label} must be finite")
    return normalized


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AV1ColdStartContractError(f"AV1 prior {label} must be a non-negative integer")
    return value


def _positive_int(value: object, label: str) -> int:
    normalized = _nonnegative_int(value, label)
    if normalized <= 0:
        raise AV1ColdStartContractError(f"AV1 prior {label} must be positive")
    return normalized


def _round_crf(value: float) -> int:
    return int(math.floor(value + 0.5))
