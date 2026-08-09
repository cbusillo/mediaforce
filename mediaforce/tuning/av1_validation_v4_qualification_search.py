from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
import math
from pathlib import Path
import re
from typing import Any, Literal, get_args

from mediaforce.core.evidence import stable_json_hash
from mediaforce.core.type_defs import object_dict
from mediaforce.encoding.quality import QualitySearchResult, QualitySearchWarmStart
from mediaforce.tuning.av1_cold_start import (
    AV1ColdStartConfidence,
    assert_av1_cold_start_public_payload_safe,
    unavailable_av1_cold_start_prediction,
)
from mediaforce.tuning.compression_intent import compression_intent_from_policy
from mediaforce.tuning.stream_budget import StreamBudgetLedger


AV1_VALIDATION_V4_QUALIFICATION_SOURCE = "av1_cold_start_v4_qualification"
AV1_VALIDATION_V4_CONTRACT_VERSION = "av1vq4s1"
AV1_VALIDATION_V4_QUALIFICATION_SEARCH_SCHEMA = (
    "mediaforce.av1_cold_start_v4_qualification_search_invocation"
)
_V4_BYPASS_REASON = "cold_start_planner_bypassed_for_v4_qualification"
_V3_HARNESS_SOURCE = "av1_validation_harness"
_ACCEPTED_WARM_STATUSES = frozenset({"accepted", "rejected_fallback"})
_PUBLIC_FALLBACK_REASONS = frozenset(
    {
        "candidate_rejected",
        "quality_floor_miss",
        "size_band_miss",
        "source_cap_miss",
    }
)
_PUBLIC_FALLBACK_REASON_ALIASES = {"target_band_miss": "size_band_miss"}
_PUBLIC_SELECTION_REASONS = frozenset(
    {
        "candidate_inside_sample_projection_band",
        "quality_memory_candidate_inside_sample_projection_band",
    }
)
_ALLOWED_SEARCH_KWARGS = frozenset({
    "cadence_decision",
    "cadence_evidence",
    "cadence_source_fingerprint",
    "detected_crop",
    "height",
    "host",
    "quality_temp_dir",
    "source_codec",
    "width",
})
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,191}\Z")
_CONFIDENCE_LEVELS = frozenset(get_args(AV1ColdStartConfidence))

V4QualificationMode = Literal["baseline", "guided"]


class V4QualificationContractError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class V4QualificationOperationResult:
    mode: V4QualificationMode
    invocation_sha256: str
    quality_result: QualitySearchResult
    cold_start_prior_mirror: dict[str, Any]
    public_summary: dict[str, Any]


def run_v4_qualification_search(
    search_quality_for_source: Callable[..., QualitySearchResult],
    source_path: Path,
    video_policy: Mapping[str, Any],
    *,
    mode: V4QualificationMode,
    stream_budget_ledger: StreamBudgetLedger,
    warm_start: QualitySearchWarmStart | None,
    extra_search_kwargs: Mapping[str, Any] | None = None,
) -> V4QualificationOperationResult:
    if not isinstance(source_path, Path):
        raise V4QualificationContractError(
            "V4 qualification source path must be a pathlib.Path"
        )
    if not source_path.is_absolute():
        raise V4QualificationContractError(
            "V4 qualification source path must be absolute"
        )
    if not isinstance(stream_budget_ledger, StreamBudgetLedger):
        raise V4QualificationContractError(
            "V4 qualification requires a non-null StreamBudgetLedger"
        )
    _require_balanced_intent(video_policy)
    _validate_mode_contract(mode, warm_start)
    _validate_guided_policy_bounds(mode, warm_start, video_policy)
    normalized_search_kwargs = _normalize_extra_search_kwargs(extra_search_kwargs or {})
    invocation_sha256 = av1_validation_v4_qualification_search_invocation_sha256(
        source_path=source_path,
        video_policy=video_policy,
        mode=mode,
        warm_start=warm_start,
        extra_search_kwargs=normalized_search_kwargs,
    )
    search_kwargs = _build_search_kwargs(
        mode,
        stream_budget_ledger,
        warm_start,
        normalized_search_kwargs,
    )
    quality_result = search_quality_for_source(
        source_path,
        dict(video_policy),
        **search_kwargs,
    )
    if not isinstance(quality_result, QualitySearchResult):
        raise V4QualificationContractError(
            "V4 qualification search returned an invalid result"
        )
    target_size_trace = _require_target_size_trace(quality_result)
    _validate_target_size_trace_mode(mode, target_size_trace)
    mirror = _build_cold_start_prior_mirror(target_size_trace)
    _validate_mirror(mode, mirror, warm_start)
    return V4QualificationOperationResult(
        mode=mode,
        invocation_sha256=invocation_sha256,
        quality_result=quality_result,
        cold_start_prior_mirror=mirror,
        public_summary=_public_summary(mode, mirror, target_size_trace),
    )


def av1_validation_v4_qualification_search_invocation_payload(
    *,
    source_path: Path,
    video_policy: Mapping[str, Any],
    mode: V4QualificationMode,
    warm_start: QualitySearchWarmStart | None,
    extra_search_kwargs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(source_path, Path) or not source_path.is_absolute():
        raise V4QualificationContractError(
            "V4 qualification invocation source path must be an absolute pathlib.Path"
        )
    _validate_mode_contract(mode, warm_start)
    normalized_search_kwargs = _normalize_extra_search_kwargs(extra_search_kwargs or {})
    return {
        "schema": AV1_VALIDATION_V4_QUALIFICATION_SEARCH_SCHEMA,
        "schema_version": 1,
        "contract_version": AV1_VALIDATION_V4_CONTRACT_VERSION,
        "protocol_version": 4,
        "experiment_id": "av1_cold_start_v4",
        "mode": mode,
        "planner_bypassed": True,
        "stream_budget_target_size_path_required": True,
        "source_path": str(source_path),
        "video_policy": deepcopy(dict(video_policy)),
        "search_kwargs": _search_kwargs_invocation_payload(normalized_search_kwargs),
        "warm_start": (
            {
                "requested_crf": warm_start.requested_crf,
                "candidate_crf": warm_start.candidate_crf,
                "search_signature_id": warm_start.search_signature_id,
                "expected_search_signature_id": warm_start.search_signature_id,
                "cohort_id": warm_start.cohort_id,
                "source": warm_start.source,
                "confidence": warm_start.confidence,
                "provenance_id": warm_start.provenance_id,
                "review_risks": list(warm_start.review_risks),
            }
            if warm_start is not None
            else None
        ),
    }


def av1_validation_v4_qualification_search_invocation_sha256(
    *,
    source_path: Path,
    video_policy: Mapping[str, Any],
    mode: V4QualificationMode,
    warm_start: QualitySearchWarmStart | None,
    extra_search_kwargs: Mapping[str, Any] | None = None,
) -> str:
    return (
        "sha256:"
        f"{stable_json_hash(av1_validation_v4_qualification_search_invocation_payload(source_path=source_path, video_policy=video_policy, mode=mode, warm_start=warm_start, extra_search_kwargs=extra_search_kwargs))}"
    )


def _require_balanced_intent(video_policy: Mapping[str, Any]) -> None:
    intent = compression_intent_from_policy(video_policy)
    if intent.requires_confirmation or intent.level != "balanced":
        raise V4QualificationContractError(
            "V4 qualification search requires confirmed balanced compression intent"
        )


def _validate_mode_contract(
    mode: V4QualificationMode,
    warm_start: QualitySearchWarmStart | None,
) -> None:
    if mode == "baseline":
        if warm_start is not None:
            raise V4QualificationContractError("Baseline mode requires warm_start=None")
    elif mode == "guided":
        if warm_start is None:
            raise V4QualificationContractError("Guided mode requires a non-null QualitySearchWarmStart")
        _validate_guided_warm_start(warm_start)
    else:
        raise V4QualificationContractError(f"Unknown qualification mode: {mode!r}")


def _validate_guided_warm_start(warm_start: QualitySearchWarmStart) -> None:
    if warm_start.source == _V3_HARNESS_SOURCE:
        raise V4QualificationContractError(
            "V3 validation-harness source is rejected in v4 qualification search"
        )
    if warm_start.source != AV1_VALIDATION_V4_QUALIFICATION_SOURCE:
        raise V4QualificationContractError(
            f"Guided warm_start source must be {AV1_VALIDATION_V4_QUALIFICATION_SOURCE!r},"
            f" got {warm_start.source!r}"
        )
    if not warm_start.search_signature_id:
        raise V4QualificationContractError(
            "Guided warm_start requires a nonempty search_signature_id"
        )
    if not warm_start.cohort_id:
        raise V4QualificationContractError(
            "Guided warm_start requires a nonempty cohort_id"
        )
    if (
        not warm_start.search_signature_id.startswith("acss1_")
        or not _SAFE_ID_RE.fullmatch(warm_start.search_signature_id)
    ):
        raise V4QualificationContractError(
            "Guided warm_start search_signature_id is invalid"
        )
    if (
        not warm_start.cohort_id.startswith("acsh1_")
        or not _SAFE_ID_RE.fullmatch(warm_start.cohort_id)
    ):
        raise V4QualificationContractError(
            "Guided warm_start cohort_id is invalid"
        )
    if (
        isinstance(warm_start.requested_crf, bool)
        or not isinstance(warm_start.requested_crf, (int, float))
        or not math.isfinite(float(warm_start.requested_crf))
        or not 0.0 <= float(warm_start.requested_crf) <= 63.0
    ):
        raise V4QualificationContractError(
            "Guided warm_start requested_crf is invalid"
        )
    if (
        isinstance(warm_start.candidate_crf, bool)
        or not isinstance(warm_start.candidate_crf, int)
        or not 0 <= warm_start.candidate_crf <= 63
    ):
        raise V4QualificationContractError(
            "Guided warm_start candidate_crf is invalid"
        )
    if (
        warm_start.confidence is not None
        and warm_start.confidence not in _CONFIDENCE_LEVELS
    ):
        raise V4QualificationContractError(
            "Guided warm_start confidence is invalid"
        )
    if (
        warm_start.provenance_id is not None
        and not _SAFE_ID_RE.fullmatch(warm_start.provenance_id)
    ):
        raise V4QualificationContractError(
            "Guided warm_start provenance_id is invalid"
        )
    if any(
        not isinstance(risk, str) or not risk or not _SAFE_ID_RE.fullmatch(risk)
        for risk in warm_start.review_risks
    ):
        raise V4QualificationContractError(
            "Guided warm_start review_risks are invalid"
        )


def _validate_guided_policy_bounds(
    mode: V4QualificationMode,
    warm_start: QualitySearchWarmStart | None,
    video_policy: Mapping[str, Any],
) -> None:
    if mode != "guided" or warm_start is None:
        return
    configured_min_crf = video_policy.get("min_crf")
    configured_max_crf = video_policy.get("max_crf")
    if (
        isinstance(configured_min_crf, bool)
        or not isinstance(configured_min_crf, int)
        or isinstance(configured_max_crf, bool)
        or not isinstance(configured_max_crf, int)
        or not 0 <= configured_min_crf <= configured_max_crf <= 63
    ):
        raise V4QualificationContractError(
            "Guided qualification requires valid frozen CRF bounds"
        )
    if not configured_min_crf <= warm_start.candidate_crf <= configured_max_crf:
        raise V4QualificationContractError(
            "Guided warm_start candidate_crf is outside frozen policy bounds"
        )


def _build_search_kwargs(
    mode: V4QualificationMode,
    stream_budget_ledger: StreamBudgetLedger,
    warm_start: QualitySearchWarmStart | None,
    extra_search_kwargs: Mapping[str, Any],
) -> dict[str, Any]:
    search_kwargs = dict(extra_search_kwargs)
    search_kwargs["stream_budget_ledger"] = stream_budget_ledger
    search_kwargs["warm_start"] = warm_start
    search_kwargs["expected_search_signature_id"] = (
        warm_start.search_signature_id
        if mode == "guided" and warm_start is not None
        else None
    )
    return search_kwargs


def _normalize_extra_search_kwargs(
    extra_search_kwargs: Mapping[str, Any],
) -> dict[str, Any]:
    invalid_keys = [
        key
        for key in extra_search_kwargs
        if not isinstance(key, str) or key not in _ALLOWED_SEARCH_KWARGS
    ]
    if invalid_keys:
        raise V4QualificationContractError(
            "Unsupported search kwargs must not be supplied: "
            f"{sorted(str(key) for key in invalid_keys)}"
        )
    normalized = dict(extra_search_kwargs)
    for dimension in ("width", "height"):
        value = normalized.get(dimension)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
        ):
            raise V4QualificationContractError(
                f"Qualification search kwarg {dimension} must be a positive integer"
            )
    for text_key in (
        "source_codec",
        "detected_crop",
        "cadence_source_fingerprint",
    ):
        value = normalized.get(text_key)
        if value is not None and (not isinstance(value, str) or not value):
            raise V4QualificationContractError(
                f"Qualification search kwarg {text_key} must be a nonempty string"
            )
    for mapping_key in ("cadence_decision", "cadence_evidence", "host"):
        value = normalized.get(mapping_key)
        if value is not None and not isinstance(value, Mapping):
            raise V4QualificationContractError(
                f"Qualification search kwarg {mapping_key} must be a mapping"
            )
        if value is not None:
            normalized[mapping_key] = deepcopy(dict(value))
    quality_temp_dir = normalized.get("quality_temp_dir")
    if quality_temp_dir is not None and (
        not isinstance(quality_temp_dir, Path) or not quality_temp_dir.is_absolute()
    ):
        raise V4QualificationContractError(
            "Qualification search kwarg quality_temp_dir must be an absolute pathlib.Path"
        )
    return normalized


def _search_kwargs_invocation_payload(
    search_kwargs: Mapping[str, Any],
) -> dict[str, Any]:
    payload = deepcopy(dict(search_kwargs))
    quality_temp_dir = payload.get("quality_temp_dir")
    if isinstance(quality_temp_dir, Path):
        payload["quality_temp_dir"] = str(quality_temp_dir)
    return payload


def _require_target_size_trace(quality_result: QualitySearchResult) -> dict[str, Any]:
    trace = object_dict(quality_result.target_size_trace) if quality_result.target_size_trace else None
    if not trace:
        raise V4QualificationContractError(
            "Target-size trace is absent from the quality result; cannot construct mirror"
        )
    return dict(trace)


def _validate_target_size_trace_mode(
    mode: V4QualificationMode,
    target_size_trace: Mapping[str, Any],
) -> None:
    if mode == "baseline" and "warm_start" in target_size_trace:
        raise V4QualificationContractError(
            "Baseline result must not contain a warm-start execution trace key"
        )


def _build_cold_start_prior_mirror(target_size_trace: dict[str, Any]) -> dict[str, Any]:
    prediction = unavailable_av1_cold_start_prediction(_V4_BYPASS_REASON)
    mirror = prediction.to_payload()
    execution_payload = object_dict(target_size_trace.get("warm_start")) or None
    execution = deepcopy(execution_payload) if execution_payload is not None else None
    mirror["execution"] = execution
    return mirror


def _validate_mirror(
    mode: V4QualificationMode,
    mirror: dict[str, Any],
    warm_start: QualitySearchWarmStart | None,
) -> None:
    if mirror.get("status") != "no_recommendation":
        raise V4QualificationContractError(
            "Cold-start prior mirror must carry status no_recommendation"
        )
    execution = object_dict(mirror.get("execution")) or None
    if mode == "baseline":
        if execution is not None:
            raise V4QualificationContractError(
                "Baseline result must not carry a warm-start execution trace"
            )
        return
    if execution is None:
        raise V4QualificationContractError(
            "Guided result must carry a warm-start execution trace"
        )
    if not execution.get("attempted"):
        raise V4QualificationContractError(
            "Guided execution trace must record attempted=True"
        )
    status = str(execution.get("status") or "")
    if status not in _ACCEPTED_WARM_STATUSES:
        raise V4QualificationContractError(
            f"Guided execution status must be accepted or rejected_fallback, got {status!r}"
        )
    fallback_used = execution.get("fallback_used")
    fallback_reason = str(execution.get("fallback_reason") or "") or None
    if status == "accepted" and (fallback_used is not False or fallback_reason is not None):
        raise V4QualificationContractError(
            "Accepted guided execution must not carry fallback state"
        )
    if status == "rejected_fallback" and (
        fallback_used is not True or fallback_reason is None
    ):
        raise V4QualificationContractError(
            "Rejected guided execution must carry fallback state and reason"
        )
    if warm_start is None:
        raise V4QualificationContractError("Guided mirror validation requires the frozen warm_start")
    if (
        execution.get("search_signature_id") != warm_start.search_signature_id
        or execution.get("cohort_id") != warm_start.cohort_id
        or execution.get("candidate_crf") != warm_start.candidate_crf
        or execution.get("requested_crf") != warm_start.requested_crf
        or execution.get("source") != warm_start.source
        or execution.get("confidence") != warm_start.confidence
        or execution.get("provenance_id") != warm_start.provenance_id
        or tuple(execution.get("review_risks") or ()) != warm_start.review_risks
    ):
        raise V4QualificationContractError(
            "Guided execution identity does not match frozen warm-start input"
        )


def _public_summary(
    mode: V4QualificationMode,
    mirror: dict[str, Any],
    target_size_trace: Mapping[str, Any],
) -> dict[str, Any]:
    execution = object_dict(mirror.get("execution")) or None
    curve = object_dict(target_size_trace.get("curve"))
    search_candidate_count = _required_nonnegative_int(
        target_size_trace.get("candidate_count", curve.get("candidate_count")),
        "search candidate count",
    )
    execution_status = (
        (str(execution.get("status") or "") or None) if execution else None
    )
    summary = {
        "schema_version": 2,
        "contract_version": AV1_VALIDATION_V4_CONTRACT_VERSION,
        "mode": mode,
        "planner_bypassed": True,
        "execution_attempted": execution is not None and bool(execution.get("attempted")),
        "execution_status": execution_status,
        "execution_fallback_used": (
            bool(execution.get("fallback_used")) if execution else None
        ),
        "execution_fallback_reason": _public_code(
            execution.get("fallback_reason") if execution else None,
            allowed=_PUBLIC_FALLBACK_REASONS,
            aliases=_PUBLIC_FALLBACK_REASON_ALIASES,
            unknown="other_reason",
        ),
        "execution_probe_seconds": (
            _required_nonnegative_float(
                execution.get("duration_seconds"), "execution probe duration"
            )
            if execution
            else None
        ),
        "execution_candidate_count": (
            _required_nonnegative_int(
                execution.get("candidate_count"), "execution candidate count"
            )
            if execution
            else None
        ),
        "fallback_search_candidate_count": (
            _required_nonnegative_int(
                execution.get("baseline_candidate_count"),
                "fallback search candidate count",
            )
            if execution
            else None
        ),
        "search_disposition": (
            "baseline_search_selected"
            if mode == "baseline"
            else "warm_start_selected"
            if execution_status == "accepted"
            else "fallback_search_selected"
        ),
        "search_selection_reason": _public_code(
            target_size_trace.get("selection_reason"),
            allowed=_PUBLIC_SELECTION_REASONS,
            unknown="other_reason",
        ),
        "search_candidate_count": search_candidate_count,
        "evidence_creation_authorized": False,
        "evidence_eligible": False,
        "empirical_authority_conferred": False,
        "private_inventory_read_authorized": False,
        "media_read_authorized": False,
        "runtime_execution_authorized": False,
        "qualification_execution_authorized": False,
        "derivation_authorized": False,
        "holdout_authorized": False,
        "publication_authorized": False,
        "activation_authorized": False,
        "retry_authorized": False,
    }
    assert_av1_cold_start_public_payload_safe(summary)
    return summary


def _public_code(
    value: Any,
    *,
    allowed: frozenset[str],
    aliases: Mapping[str, str] | None = None,
    unknown: str,
) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    public = str((aliases or {}).get(normalized, normalized))
    return public if public in allowed else unknown


def _required_nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise V4QualificationContractError(f"{label} must be a nonnegative integer")
    return value


def _required_nonnegative_float(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise V4QualificationContractError(f"{label} must be a nonnegative number")
    return round(float(value), 3)
