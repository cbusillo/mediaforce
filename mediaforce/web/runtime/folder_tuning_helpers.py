import json
from typing import Any

from sqlalchemy import select

from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import tuning_sessions
from mediaforce.core.type_defs import JSONValue, object_dict
from mediaforce.tuning.compression_intent import compression_intent_from_policy
from mediaforce.tuning.size_goals import DEFAULT_SAMPLE_PROJECTION_TOLERANCE_PERCENT
from mediaforce.web.runtime.proposal_recovery import has_assistant_failure


def video_quality_improvement_change(current_video: dict[str, Any], preview_video: dict[str, Any]) -> bool:
    current = object_dict(current_video)
    preview = object_dict(preview_video)
    for key in ("target_vmaf", "target_xpsnr"):
        current_value = _number_or_none(current.get(key))
        preview_value = _number_or_none(preview.get(key))
        if current_value is not None and preview_value is not None and preview_value > current_value + 0.01:
            return True
    for key in ("min_crf", "max_crf", "target_search_max_crf", "grain_denoise", "preset"):
        current_value = _number_or_none(current.get(key))
        preview_value = _number_or_none(preview.get(key))
        if current_value is not None and preview_value is not None and preview_value < current_value - 0.01:
            return True
    current_grain = _number_or_none(current.get("default_grain"))
    preview_grain = _number_or_none(preview.get("default_grain"))
    return bool(current_grain is not None and preview_grain is not None and preview_grain > current_grain + 0.01)


def recent_tuning_sessions(
        connection: DBClient,
        prefix: str,
        *,
        load_json_object_fn: Any,
        limit: int = 8,
) -> list[dict[str, Any]]:
    query = (
        select(
            tuning_sessions.c.session_id,
            tuning_sessions.c.note,
            tuning_sessions.c.summary,
            tuning_sessions.c.diagnosis,
            tuning_sessions.c.confidence,
            tuning_sessions.c.suggested_follow_up,
            tuning_sessions.c.raw_response,
            tuning_sessions.c.toolbelt_json,
            tuning_sessions.c.created_at,
        )
        .where(tuning_sessions.c.prefix == prefix)
        .order_by(tuning_sessions.c.created_at.desc())
    )
    sessions: list[dict[str, Any]] = []
    batch_size = max(limit * 2, 16)
    offset = 0
    while len(sessions) < limit:
        rows = connection.execute(query.limit(batch_size).offset(offset)).mappings().fetchall()
        if not rows:
            break
        offset += len(rows)
        for row in rows:
            raw_response = str(row["raw_response"] or "")
            if has_assistant_failure({"trace": {"raw_response": raw_response}}):
                continue
            note = str(row["note"] or "").strip()
            summary = str(row["summary"] or "").strip()
            diagnosis = str(row["diagnosis"] or "").strip()
            suggested_follow_up = str(row["suggested_follow_up"] or "").strip()
            parsed_raw = load_json_object_fn(raw_response)
            toolbelt = load_json_object_fn(str(row["toolbelt_json"] or ""))
            sessions.append(
                {
                    "session_id": row["session_id"],
                    "note": note,
                    "summary": summary,
                    "diagnosis": diagnosis or None,
                    "confidence": str(row["confidence"] or "").strip() or None,
                    "suggested_follow_up": suggested_follow_up or None,
                    "request_disposition": str(parsed_raw.get("request_disposition") or "").strip() or None,
                    "request_response": str(parsed_raw.get("request_response") or "").strip() or None,
                    "feasibility_note": str(parsed_raw.get("feasibility_note") or "").strip() or None,
                    "requested_experiment": object_dict(toolbelt.get("requested_experiment")) or None,
                    "operator_note_parse": (
                        object_dict(toolbelt.get("operator_note_parse"))
                        or object_dict(object_dict(toolbelt.get("requested_experiment")).get("operator_note_parse"))
                        or None
                    ),
                    "created_at": row["created_at"],
                }
            )
            if len(sessions) == limit:
                break
        if len(rows) < batch_size:
            break
    return sessions


def proposal_signal_copy(
        note: str,
        operator_request: dict[str, Any] | None,
        has_calibration: bool,
        request_disposition: str | None = None,
) -> str:
    disposition = str(request_disposition or "").strip().lower()
    if disposition == "honored_with_risk":
        return "The bench kept your requested experiment and called out the measured caveat before anything queues."
    if disposition == "softened":
        return "The bench softened part of your request and explained why in the reply below."
    if disposition == "rejected":
        return "The bench did not adopt the request as written and explained the mismatch below."
    if operator_request:
        return "The bench translated your note into a concrete draft instead of treating it as background context."
    if note.strip():
        return "The bench translated your note into a draft sample for review before anything queues."
    if not has_calibration:
        return "The bench built a first pass from the current folder context and policy."
    return "The bench drafted the next sample from your note and the latest measured result."


def load_json_object(raw: str) -> dict[str, Any]:
    candidate = raw.strip()
    if not candidate:
        return {}
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _number_or_none(value: JSONValue) -> float | None:
    if not isinstance(value, str | int | float) or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed


def measured_size_budget_policy_fragment(
        *,
        operator_request: dict[str, Any] | None,
        size_target_analysis: dict[str, Any] | None,
) -> dict[str, Any]:
    request = object_dict(operator_request)
    analysis = object_dict(size_target_analysis)
    if str(request.get("request_type") or "").strip().lower() not in {"size_budget", "combined_experiment"}:
        return {}
    size_budget_request = object_dict(request.get("size_budget_request"))
    hard_size_cap = bool(
        request.get("operator_confirmed")
        and (request.get("hard_size_cap") or size_budget_request.get("hard_size_cap"))
    )
    measured_size_followup = bool(
        request.get("operator_confirmed")
        and (request.get("measured_size_followup") or size_budget_request.get("measured_size_followup"))
    )
    if not hard_size_cap and not measured_size_followup:
        return {}
    if str(analysis.get("status") or "").strip().lower() != "over_target":
        return {}
    requested_cap = _number_or_none(request.get("requested_max_encoded_percent"))
    if requested_cap is None:
        requested_cap = _number_or_none(request.get("target_encoded_percent"))
    if requested_cap is None:
        requested_cap = _number_or_none(size_budget_request.get("requested_max_encoded_percent"))
    if requested_cap is None:
        requested_cap = _number_or_none(size_budget_request.get("target_encoded_percent"))
    if requested_cap is None or requested_cap <= 0:
        return {}
    cap = max(1, min(100, int(round(requested_cap))))
    return {"video": {"max_encoded_percent": cap}}


def allows_measured_size_quality_tradeoff(
        *,
        operator_request: dict[str, Any] | None,
        size_target_analysis: dict[str, Any] | None,
        direction: str = "smaller",
) -> bool:
    request = object_dict(operator_request)
    analysis = object_dict(size_target_analysis)
    if str(request.get("request_type") or "").strip().lower() not in {"size_budget", "combined_experiment"}:
        return False
    status = str(analysis.get("status") or "").strip().lower()
    tradeoff_direction = str(direction or "smaller").strip().lower()
    if tradeoff_direction == "larger":
        if status != "under_target":
            return False
        compression_intent = compression_intent_from_policy(
            object_dict(object_dict(request.get("applied_policy")).get("video"))
        )
        if compression_intent.requires_confirmation or compression_intent.accepts_under_target_result:
            return False
        return bool(request.get("operator_confirmed"))
    elif status != "over_target":
        return False
    size_budget_request = object_dict(request.get("size_budget_request"))
    return bool(
        (
            request.get("operator_confirmed")
            and (request.get("hard_size_cap") or size_budget_request.get("hard_size_cap"))
        )
        or (
            request.get("operator_confirmed")
            and (request.get("measured_size_followup") or size_budget_request.get("measured_size_followup"))
        )
    )


def proposal_alignment_issue(
        *,
        operator_request: dict[str, Any] | None,
        request_disposition: str | None,
        current_policy: dict[str, Any],
        preview_policy: dict[str, Any],
        allow_measured_size_quality_tradeoff: bool = False,
        allow_measured_size_quality_increase: bool = False,
) -> str | None:
    if not operator_request:
        return None
    disposition = str(request_disposition or "").strip().lower()
    if disposition in {"softened", "rejected", "unclear", "unavailable"}:
        return None
    request_type = str(operator_request.get("request_type") or "").strip().lower()
    current_video = object_dict(current_policy.get("video"))
    preview_video = object_dict(preview_policy.get("video"))
    current_audio = object_dict(current_policy.get("audio"))
    preview_audio = object_dict(preview_policy.get("audio"))
    requested_video = object_dict(object_dict(operator_request.get("applied_policy")).get("video"))
    requested_audio = object_dict(object_dict(operator_request.get("applied_policy")).get("audio"))
    evidence_authority = str(operator_request.get("evidence_authority") or "none").strip().lower()
    allow_evidence_backed_video_tradeoff = evidence_authority in {
        "operator_observed",
        "approved_visual_result",
    }

    def _float_or_none(value: JSONValue) -> float | None:
        if not isinstance(value, str | int | float):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed

    def _policy_height(video_policy: dict[str, Any]) -> float | None:
        height = _float_or_none(video_policy.get("max_height"))
        if height is not None:
            return height
        return _float_or_none(video_policy.get("target_height"))

    def _normalized_black_bar_handling(value: JSONValue) -> str:
        handling = str(value or "").strip().lower()
        if handling in {"auto", "smart", "true", "yes", "1"}:
            return "smart"
        return handling

    def _unrequested_size_tradeoff_issue() -> str | None:
        if _policy_height(requested_video) is None:
            current_height = _policy_height(current_video)
            preview_height = _policy_height(preview_video)
            if preview_height is not None and (
                    current_height is None or abs(preview_height - current_height) > 0.01
            ):
                return (
                    "The draft changes resolution based only on a size budget. Ask for that resolution "
                    "explicitly before changing the height cap."
                )
        for key in ("stereo_opus_bitrate", "surround_5_1_opus_bitrate", "surround_7_1_opus_bitrate"):
            if key in requested_audio:
                continue
            current_value = current_audio.get(key)
            preview_value = preview_audio.get(key)
            if preview_value is not None and preview_value != current_value:
                return (
                    f"The draft changes audio.{key} based only on a size budget. Ask for that audio tradeoff "
                    "or measure it explicitly before changing audio quality."
                )
        return None

    def _size_budget_alignment_issue() -> str | None:
        typed_goal_issue = _typed_size_goal_alignment_issue()
        if typed_goal_issue is not None:
            return typed_goal_issue
        current_vmaf = _float_or_none(current_video.get("target_vmaf"))
        preview_vmaf = _float_or_none(preview_video.get("target_vmaf"))
        current_xpsnr = _float_or_none(current_video.get("target_xpsnr"))
        preview_xpsnr = _float_or_none(preview_video.get("target_xpsnr"))
        if (
                not allow_measured_size_quality_increase
                and current_vmaf is not None
                and preview_vmaf is not None
                and preview_vmaf > current_vmaf + 0.01
        ):
            return "The draft raises the VMAF target even though your note asked for a smaller encode."
        if (
                not allow_measured_size_quality_tradeoff
                and not allow_evidence_backed_video_tradeoff
                and current_vmaf is not None
                and preview_vmaf is not None
                and preview_vmaf < current_vmaf - 0.01
        ):
            return (
                "The draft lowers the VMAF target based only on a size budget. Run a measured sample or ask for "
                "a specific quality tradeoff before changing quality targets."
            )
        if (
                not allow_measured_size_quality_increase
                and current_xpsnr is not None
                and preview_xpsnr is not None
                and preview_xpsnr > current_xpsnr + 0.01
        ):
            return "The draft raises the XPSNR target even though your note asked for a smaller encode."
        if (
                not allow_measured_size_quality_tradeoff
                and not allow_evidence_backed_video_tradeoff
                and current_xpsnr is not None
                and preview_xpsnr is not None
                and preview_xpsnr < current_xpsnr - 0.01
        ):
            return (
                "The draft lowers the XPSNR target based only on a size budget. Run a measured sample or ask for "
                "a specific quality tradeoff before changing quality targets."
            )
        current_cap = _float_or_none(current_video.get("max_encoded_percent"))
        preview_cap = _float_or_none(preview_video.get("max_encoded_percent"))
        if preview_cap is not None and (current_cap is None or abs(preview_cap - current_cap) > 0.01):
            return (
                "The draft turns the size budget into a max_encoded_percent change. Size budgets are planning "
                "targets for measured comparison unless the operator explicitly asks for a hard cap."
            )
        tradeoff_issue = _unrequested_size_tradeoff_issue()
        if tradeoff_issue is not None:
            return tradeoff_issue
        if not allow_measured_size_quality_tradeoff and not allow_evidence_backed_video_tradeoff:
            for key in (
                    "default_grain",
                    "grain_denoise",
                    "min_crf",
                    "max_crf",
                    "target_search_max_crf",
                    "preset",
            ):
                if key in requested_video:
                    continue
                current_value = current_video.get(key)
                preview_value = preview_video.get(key)
                if allow_measured_size_quality_increase and video_quality_improvement_change(
                        {key: current_value}, {key: preview_value}
                ):
                    continue
                if current_value is not None and preview_value is not None and preview_value != current_value:
                    return (
                        f"The draft changes video.{key} based only on a size budget. Run a measured sample or "
                        "ask for that specific tradeoff before changing video tuning knobs."
                    )
        return None

    def _size_budget_cap_alignment_issue() -> str | None:
        typed_goal_issue = _typed_size_goal_alignment_issue()
        if typed_goal_issue is not None:
            return typed_goal_issue
        requested_cap = _float_or_none(requested_video.get("max_encoded_percent"))
        if requested_cap is None:
            requested_cap = _float_or_none(operator_request.get("applied_max_encoded_percent"))
        if requested_cap is None:
            size_budget_request = object_dict(operator_request.get("size_budget_request"))
            requested_cap = _float_or_none(size_budget_request.get("applied_max_encoded_percent"))
        if requested_cap is None:
            current_cap = _float_or_none(current_video.get("max_encoded_percent"))
            preview_cap = _float_or_none(preview_video.get("max_encoded_percent"))
            if preview_cap is not None and (current_cap is None or abs(preview_cap - current_cap) > 0.01):
                return (
                    "The draft turns the size budget into a max_encoded_percent change. Size budgets are planning "
                    "targets for measured comparison unless the operator explicitly asks for a hard cap."
                )
            tradeoff_issue = _unrequested_size_tradeoff_issue()
            if tradeoff_issue is not None:
                return tradeoff_issue
            if not allow_measured_size_quality_tradeoff and not allow_evidence_backed_video_tradeoff:
                for key in (
                        "default_grain",
                        "grain_denoise",
                        "min_crf",
                        "max_crf",
                        "target_search_max_crf",
                        "preset",
                ):
                    if key in requested_video:
                        continue
                    current_value = current_video.get(key)
                    preview_value = preview_video.get(key)
                    if allow_measured_size_quality_increase and video_quality_improvement_change(
                            {key: current_value}, {key: preview_value}
                    ):
                        continue
                    if current_value is not None and preview_value is not None and preview_value != current_value:
                        return (
                            f"The draft changes video.{key} based only on a size budget. Run a measured sample or "
                            "ask for that specific tradeoff before changing video tuning knobs."
                        )
            return None
        preview_cap = _float_or_none(preview_video.get("max_encoded_percent"))
        if preview_cap is None:
            return f"The draft does not apply the requested {requested_cap:.0f}% size ceiling."
        if preview_cap > requested_cap + 0.01:
            return f"The draft uses a {preview_cap:.0f}% size ceiling instead of the requested {requested_cap:.0f}% ceiling."
        tradeoff_issue = _unrequested_size_tradeoff_issue()
        if tradeoff_issue is not None:
            return tradeoff_issue
        return None

    def _typed_size_goal_alignment_issue() -> str | None:
        requested_mode = str(requested_video.get("size_goal_mode") or "").strip().lower()
        if requested_mode not in {"normalized", "absolute"}:
            return None
        preview_mode = str(preview_video.get("size_goal_mode") or "").strip().lower()
        if preview_mode != requested_mode:
            return f"The draft does not preserve the requested {requested_mode} size-goal mode."
        for key, label in (
                ("target_size_bytes", "canonical size bytes"),
                ("target_size_mb", "size value"),
                ("target_runtime_minutes", "reference runtime"),
                ("sample_projection_tolerance_percent", "test target band"),
                ("final_output_tolerance_percent", "final output band"),
        ):
            requested_value = _float_or_none(requested_video.get(key))
            if requested_value is None:
                continue
            preview_value = _float_or_none(preview_video.get(key))
            if preview_value is None or abs(preview_value - requested_value) > 0.001:
                return f"The draft does not preserve the requested {label}."
        return None

    def _metric_alignment_issue() -> str | None:
        requested_metric = str(operator_request.get("metric") or "").strip().lower()
        requested_target = _float_or_none(operator_request.get("target"))
        if requested_metric not in {"vmaf", "xpsnr"} or requested_target is None:
            return None
        target_key = "target_vmaf" if requested_metric == "vmaf" else "target_xpsnr"
        floor_key = "min_target_vmaf" if requested_metric == "vmaf" else "min_target_xpsnr"
        preview_target = _float_or_none(preview_video.get(target_key))
        if preview_target is None:
            return f"The draft does not apply the requested {requested_metric.upper()} target."
        if abs(preview_target - requested_target) > 0.01:
            return (
                f"The draft uses {preview_target:.2f} {requested_metric.upper()} instead of the requested "
                f"{requested_target:.2f} target."
            )
        requested_floor = _float_or_none(requested_video.get(floor_key))
        if requested_floor is None:
            return None
        preview_floor = _float_or_none(preview_video.get(floor_key))
        if preview_floor is None:
            return f"The draft does not preserve the requested {requested_metric.upper()} floor."
        if abs(preview_floor - requested_floor) > 0.01:
            return (
                f"The draft uses {preview_floor:.2f} {requested_metric.upper()} as the floor instead of the requested "
                f"{requested_floor:.2f} floor."
            )
        return None

    def _scale_alignment_issue() -> str | None:
        requested_height = _policy_height(requested_video)
        if requested_height is None:
            requested_height = _float_or_none(operator_request.get("scale_height"))
        if requested_height is None:
            return None
        preview_height = _policy_height(preview_video)
        if requested_height <= 0:
            if preview_height is not None and preview_height > 0:
                return "The draft sets a height cap even though the operator requested source resolution."
            return None
        if preview_height is None:
            return f"The draft does not apply the requested {requested_height:.0f}p height cap."
        if abs(preview_height - requested_height) > 0.01:
            return f"The draft uses {preview_height:.0f}p instead of the requested {requested_height:.0f}p height cap."
        return None

    def _transform_alignment_issue() -> str | None:
        if "crop" in requested_video:
            requested_crop = str(requested_video.get("crop") or "").strip()
            preview_crop = str(preview_video.get("crop") or "").strip()
            if preview_crop != requested_crop:
                if requested_crop:
                    return f"The draft uses crop {preview_crop or 'off'} instead of the requested {requested_crop}."
                return "The draft keeps a manual crop even though smart black-bar detection was requested."
            if requested_crop:
                return None
        requested_handling = str(requested_video.get("black_bar_handling") or "").strip().lower()
        if requested_handling in {"auto", "smart"}:
            preview_handling = _normalized_black_bar_handling(preview_video.get("black_bar_handling"))
            if preview_handling != _normalized_black_bar_handling(requested_handling):
                return f"The draft does not apply the requested {requested_handling} black-bar handling."
        return None

    if request_type == "size_budget":
        if _float_or_none(operator_request.get("applied_max_encoded_percent")) is not None:
            return _size_budget_cap_alignment_issue()
        if object_dict(operator_request.get("applied_policy")):
            return _size_budget_cap_alignment_issue()
        return _size_budget_alignment_issue()
    if request_type == "metric_target":
        return _metric_alignment_issue()
    if request_type == "scale_target":
        scale_issue = _scale_alignment_issue()
        if scale_issue is not None:
            return scale_issue
        return _transform_alignment_issue()
    if request_type == "combined_experiment":
        metric_issue = _metric_alignment_issue()
        if metric_issue is not None:
            return metric_issue
        scale_issue = _scale_alignment_issue()
        if scale_issue is not None:
            return scale_issue
        transform_issue = _transform_alignment_issue()
        if transform_issue is not None:
            return transform_issue
        if object_dict(operator_request.get("size_budget_request")):
            cap_issue = _size_budget_cap_alignment_issue()
            if cap_issue is not None:
                return cap_issue
            if not operator_request.get("metric"):
                return _size_budget_alignment_issue()
    return None


def size_budget_sample_issue(
        *,
        operator_request: dict[str, Any] | None,
        calibration_payload: dict[str, Any],
        tolerance: float | None = None,
) -> str | None:
    analysis = size_budget_sample_analysis(
        operator_request=operator_request,
        calibration_payload=calibration_payload,
        tolerance=tolerance,
    )
    status = str(analysis.get("status") or "").strip().lower()
    if status == "missing_prediction":
        return "The sampled run did not report a predicted total size for the requested size target."
    if status == "under_target":
        return (
            f"The sampled run predicts {_format_bytes(float(analysis['predicted_total_size_bytes']))}, below the "
            f"requested {_format_bytes(float(analysis['budget_bytes']))} target band. Run another sample that spends "
            "the saved bitrate on quality or explicitly approve a smaller-size tradeoff."
        )
    if status == "over_target":
        return (
            f"The sampled run predicts {_format_bytes(float(analysis['predicted_total_size_bytes']))}, above the "
            f"requested {_format_bytes(float(analysis['budget_bytes']))} target band. Run another sample that moves "
            "closer to the requested size or explicitly approve the larger quality tradeoff."
        )
    return None


def size_budget_sample_analysis(
        *,
        operator_request: dict[str, Any] | None,
        calibration_payload: dict[str, Any],
        tolerance: float | None = None,
) -> dict[str, Any]:
    request = object_dict(operator_request)
    if not request or not bool(request.get("operator_confirmed")):
        return {}
    budget_bytes = _operator_budget_bytes(request)
    if budget_bytes is None or budget_bytes <= 0:
        return {}
    tolerance_percent = _sample_projection_tolerance_percent(request)
    tolerance_ratio = tolerance if tolerance is not None else tolerance_percent / 100.0
    sample_result = object_dict(calibration_payload.get("sample_result"))
    predicted_total = _number_or_none(sample_result.get("predicted_total_size_bytes"))
    lower_bound = float(budget_bytes) * (1.0 - tolerance_ratio)
    upper_bound = float(budget_bytes) * (1.0 + tolerance_ratio)
    analysis = {
        "budget_bytes": budget_bytes,
        "budget_label": request.get("budget_label"),
        "tolerance": tolerance_ratio,
        "sample_projection_tolerance_percent": round(tolerance_ratio * 100.0, 3),
        "lower_bound_bytes": int(round(lower_bound)),
        "upper_bound_bytes": int(round(upper_bound)),
        "predicted_total_size_bytes": int(round(predicted_total)) if predicted_total is not None else None,
    }
    if predicted_total is None or predicted_total <= 0:
        return {**analysis, "status": "missing_prediction"}
    ratio = predicted_total / float(budget_bytes)
    analysis["predicted_to_budget_ratio"] = round(ratio, 4)
    if predicted_total < lower_bound:
        return {**analysis, "status": "under_target"}
    if predicted_total > upper_bound:
        return {**analysis, "status": "over_target"}
    return {**analysis, "status": "inside_target_band"}


def _sample_projection_tolerance_percent(operator_request: dict[str, Any]) -> float:
    request = object_dict(operator_request)
    size_request = object_dict(request.get("size_budget_request"))
    for payload in (request, size_request):
        value = _number_or_none(payload.get("sample_projection_tolerance_percent"))
        if value is not None and value > 0:
            return min(value, 100.0)
    for payload in (request, size_request):
        legacy_value = _number_or_none(payload.get("target_tolerance_percent"))
        if legacy_value is None or legacy_value <= 0:
            continue
        return min(legacy_value * 100.0 if legacy_value <= 1.0 else legacy_value, 100.0)
    return DEFAULT_SAMPLE_PROJECTION_TOLERANCE_PERCENT


def _operator_budget_bytes(operator_request: dict[str, Any]) -> int | None:
    budget = _int_or_none(operator_request.get("budget_bytes"))
    if budget is not None:
        return budget
    size_budget_request = object_dict(operator_request.get("size_budget_request"))
    return _int_or_none(size_budget_request.get("budget_bytes"))


def _int_or_none(value: Any) -> int | None:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _format_bytes(value: float) -> str:
    mib = value / 1024 / 1024
    if mib < 1024:
        return f"{mib:.0f} MiB"
    return f"{mib / 1024:.2f} GiB"
