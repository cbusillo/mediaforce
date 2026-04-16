import json
from typing import Any

from sqlalchemy import select

from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import tuning_sessions
from mediaforce.core.type_defs import object_dict
from mediaforce.core.type_defs import JSONValue


def recent_tuning_sessions(
        connection: DBClient,
        prefix: str,
        *,
        load_json_object_fn: Any,
        limit: int = 8,
) -> list[dict[str, Any]]:
    rows = connection.execute(
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
        .limit(limit)
    ).mappings().fetchall()
    sessions: list[dict[str, Any]] = []
    for row in rows:
        note = str(row["note"] or "").strip()
        summary = str(row["summary"] or "").strip()
        diagnosis = str(row["diagnosis"] or "").strip()
        suggested_follow_up = str(row["suggested_follow_up"] or "").strip()
        parsed_raw = load_json_object_fn(str(row["raw_response"] or ""))
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
    return sessions


def proposal_signal_copy(
        note: str,
        operator_request: dict[str, Any] | None,
        has_calibration: bool,
        request_disposition: str | None = None,
) -> str:
    disposition = str(request_disposition or "").strip().lower()
    if disposition == "honored_with_risk":
        return "The bench kept your requested experiment, but called out the risk before anything queues."
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


def proposal_alignment_issue(
        *,
        operator_request: dict[str, Any] | None,
        request_disposition: str | None,
        current_policy: dict[str, Any],
        preview_policy: dict[str, Any],
) -> str | None:
    if not operator_request:
        return None
    disposition = str(request_disposition or "").strip().lower()
    if disposition in {"softened", "rejected", "unclear"}:
        return None
    request_type = str(operator_request.get("request_type") or "").strip().lower()
    current_video = object_dict(current_policy.get("video"))
    preview_video = object_dict(preview_policy.get("video"))
    requested_video = object_dict(object_dict(operator_request.get("applied_policy")).get("video"))

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

    def _size_budget_alignment_issue() -> str | None:
        current_vmaf = _float_or_none(current_video.get("target_vmaf"))
        preview_vmaf = _float_or_none(preview_video.get("target_vmaf"))
        current_xpsnr = _float_or_none(current_video.get("target_xpsnr"))
        preview_xpsnr = _float_or_none(preview_video.get("target_xpsnr"))
        if current_vmaf is not None and preview_vmaf is not None and preview_vmaf > current_vmaf + 0.01:
            return "The draft raises the VMAF target even though your note asked for a smaller encode."
        if current_xpsnr is not None and preview_xpsnr is not None and preview_xpsnr > current_xpsnr + 0.01:
            return "The draft raises the XPSNR target even though your note asked for a smaller encode."
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
        if preview_height is None:
            return f"The draft does not apply the requested {requested_height:.0f}p height cap."
        if abs(preview_height - requested_height) > 0.01:
            return f"The draft uses {preview_height:.0f}p instead of the requested {requested_height:.0f}p height cap."
        return None

    if request_type == "size_budget":
        return _size_budget_alignment_issue()
    if request_type == "metric_target":
        return _metric_alignment_issue()
    if request_type == "scale_target":
        return _scale_alignment_issue()
    if request_type == "combined_experiment":
        metric_issue = _metric_alignment_issue()
        if metric_issue is not None:
            return metric_issue
        scale_issue = _scale_alignment_issue()
        if scale_issue is not None:
            return scale_issue
        if not operator_request.get("metric") and object_dict(operator_request.get("size_budget_request")):
            return _size_budget_alignment_issue()
    return None
