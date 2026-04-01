import json
from typing import Any

from mediaforce.core.db import DBClient
from mediaforce.core.type_defs import JSONValue


def recent_tuning_sessions(
        connection: DBClient,
        prefix: str,
        *,
        load_json_object: Any,
        limit: int = 8,
) -> list[dict[str, Any]]:
    rows = connection.exec_driver_sql(
        """
        SELECT session_id,
               note,
               summary,
               diagnosis,
               confidence,
               suggested_follow_up,
               raw_response,
               created_at
        FROM tuning_sessions
        WHERE prefix = ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (prefix, limit),
    ).mappings().fetchall()
    sessions: list[dict[str, Any]] = []
    for row in rows:
        note = str(row["note"] or "").strip()
        summary = str(row["summary"] or "").strip()
        diagnosis = str(row["diagnosis"] or "").strip()
        suggested_follow_up = str(row["suggested_follow_up"] or "").strip()
        parsed_raw = load_json_object(str(row["raw_response"] or ""))
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
    if request_type != "size_budget":
        return None
    current_video = dict(current_policy.get("video") or {})
    preview_video = dict(preview_policy.get("video") or {})

    def _float_or_none(value: JSONValue) -> float | None:
        if not isinstance(value, str | int | float):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed

    current_vmaf = _float_or_none(current_video.get("target_vmaf"))
    preview_vmaf = _float_or_none(preview_video.get("target_vmaf"))
    current_xpsnr = _float_or_none(current_video.get("target_xpsnr"))
    preview_xpsnr = _float_or_none(preview_video.get("target_xpsnr"))
    if current_vmaf is not None and preview_vmaf is not None and preview_vmaf > current_vmaf + 0.01:
        return "The draft raises the VMAF target even though your note asked for a smaller encode."
    if current_xpsnr is not None and preview_xpsnr is not None and preview_xpsnr > current_xpsnr + 0.01:
        return "The draft raises the XPSNR target even though your note asked for a smaller encode."
    return None
