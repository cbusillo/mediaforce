import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.type_defs import float_value, object_dict, object_list
from mediaforce.tuning.quality_risk import quality_risk_public_view


@dataclass(slots=True)
class FolderStateDeps:
    review_file_from_url: Any
    load_advice_state: Any
    calibration_draft_hash: Any
    tuning_policy_focus: Any
    pending_proposal_trace_public_view: Any


def load_calibration_state(deps: FolderStateDeps, config: MediaforceConfig, prefix: str, calibration_file: Path) -> dict[str, Any] | None:
    path = calibration_file
    if not path.exists():
        return None
    payload = object_dict(json.loads(path.read_text()))
    raw_review_pairs = object_list(payload.get("review_pairs"))
    compare_clips: list[dict[str, Any]] = []
    preview_clips: list[dict[str, Any]] = []
    source_clips: list[dict[str, Any]] = []
    compare_clips_purged = False
    preview_clips_purged = False
    source_clips_purged = False
    for raw_clip in object_list(payload.get("compare_clips")):
        clip = object_dict(raw_clip)
        review_file = deps.review_file_from_url(config, str(clip.get("path") or ""))
        if review_file is None or review_file.exists():
            compare_clips.append(clip)
            continue
        compare_clips_purged = True
    for raw_clip in object_list(payload.get("preview_clips")):
        clip = object_dict(raw_clip)
        review_file = deps.review_file_from_url(config, str(clip.get("path") or ""))
        if review_file is None or review_file.exists():
            preview_clips.append(clip)
            continue
        preview_clips_purged = True
    for raw_clip in object_list(payload.get("source_clips")):
        clip = object_dict(raw_clip)
        review_file = deps.review_file_from_url(config, str(clip.get("path") or ""))
        if review_file is None or review_file.exists():
            source_clips.append(clip)
            continue
        source_clips_purged = True
    payload["compare_clips"] = compare_clips
    payload["preview_clips"] = preview_clips
    payload["source_clips"] = source_clips
    payload["compare_clips_purged"] = compare_clips_purged
    payload["preview_clips_purged"] = preview_clips_purged
    payload["source_clips_purged"] = source_clips_purged
    payload["review_pairs"] = _merge_review_pairs(
        review_pairs(source_clips, preview_clips, compare_clips),
        _valid_retained_review_pairs(deps, config, raw_review_pairs),
    )
    payload["browser_review_ready"] = bool(payload["review_pairs"])
    payload["review_media_ready"] = bool(compare_clips or preview_clips or source_clips or payload["review_pairs"])
    payload.setdefault("mode", "full" if payload.get("encode_result") else "sample")
    advice_payload = deps.load_advice_state(config, prefix)
    if advice_payload is not None:
        payload["advice"] = advice_payload
    payload["draft_hash"] = deps.calibration_draft_hash(payload)
    return payload


def _merge_review_pairs(
        primary_pairs: list[dict[str, Any]],
        fallback_pairs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    fallback_by_key = {
        review_pair_key(float_value(pair.get("timestamp_seconds"))): pair for pair in fallback_pairs
    }
    merged = []
    seen_keys: set[int] = set()
    for pair in primary_pairs:
        key = review_pair_key(float_value(pair.get("timestamp_seconds")))
        seen_keys.add(key)
        fallback_pair = fallback_by_key.get(key)
        if fallback_pair is None:
            merged.append(pair)
            continue
        merged_pair = dict(pair)
        if not object_dict(merged_pair.get("compare_clip")):
            fallback_compare_clip = object_dict(fallback_pair.get("compare_clip"))
            if fallback_compare_clip:
                merged_pair["compare_clip"] = fallback_compare_clip
        if float_value(merged_pair.get("duration_seconds")) <= 0:
            merged_pair["duration_seconds"] = float_value(fallback_pair.get("duration_seconds"))
        merged.append(merged_pair)
    for pair in fallback_pairs:
        key = review_pair_key(float_value(pair.get("timestamp_seconds")))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        merged.append(pair)
    return sorted(merged, key=lambda pair: review_pair_key(float_value(pair.get("timestamp_seconds"))))


def _valid_retained_review_pairs(
        deps: FolderStateDeps,
        config: MediaforceConfig,
        raw_review_pairs: list[Any],
) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    for raw_pair in raw_review_pairs:
        pair = object_dict(raw_pair)
        source_clip = object_dict(pair.get("source_clip"))
        preview_clip = object_dict(pair.get("preview_clip"))
        source_path = deps.review_file_from_url(config, str(source_clip.get("path") or ""))
        preview_path = deps.review_file_from_url(config, str(preview_clip.get("path") or ""))
        if source_path is None or preview_path is None:
            continue
        if not source_path.exists() or not preview_path.exists():
            continue
        compare_clip = object_dict(pair.get("compare_clip"))
        compare_path_text = str(compare_clip.get("path") or "")
        compare_path = deps.review_file_from_url(config, compare_path_text) if compare_path_text else None
        if compare_path_text and compare_path is not None and not compare_path.exists():
            compare_clip = {}
        pairs.append(
            {
                "timestamp_seconds": float_value(pair.get("timestamp_seconds")),
                "duration_seconds": float_value(pair.get("duration_seconds")),
                "source_clip": source_clip,
                "preview_clip": preview_clip,
                "compare_clip": compare_clip or None,
            }
        )
    return pairs


def save_calibration_state(calibration_file: Path, payload: dict[str, Any], *, calibration_draft_hash: Any) -> None:
    stored_payload = dict(payload)
    stored_payload["draft_hash"] = calibration_draft_hash(stored_payload)
    calibration_file.write_text(json.dumps(stored_payload, indent=2) + "\n")


def review_pairs(
        source_clips: list[dict[str, Any]],
        preview_clips: list[dict[str, Any]],
        compare_clips: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    compare_by_timestamp = {
        review_pair_key(float_value(clip.get("timestamp_seconds"))): clip for clip in compare_clips
    }
    preview_by_timestamp = {
        review_pair_key(float_value(clip.get("timestamp_seconds"))): clip for clip in preview_clips
    }
    pairs: list[dict[str, Any]] = []
    for source_clip in source_clips:
        timestamp = float_value(source_clip.get("timestamp_seconds"))
        key = review_pair_key(timestamp)
        preview_clip = preview_by_timestamp.get(key)
        if preview_clip is None:
            continue
        pairs.append(
            {
                "timestamp_seconds": timestamp,
                "duration_seconds": float_value(
                    source_clip.get("duration_seconds") or preview_clip.get("duration_seconds")
                ),
                "source_clip": source_clip,
                "preview_clip": preview_clip,
                "compare_clip": compare_by_timestamp.get(key),
            }
        )
    return pairs


def review_media_context(calibration: dict[str, Any] | None) -> dict[str, Any]:
    payload = object_dict(calibration)
    raw_pairs = payload.get("review_pairs")
    if isinstance(raw_pairs, list):
        pairs = [object_dict(pair) for pair in raw_pairs if isinstance(pair, dict)]
    else:
        pairs = review_pairs(
            [object_dict(clip) for clip in object_list(payload.get("source_clips"))],
            [object_dict(clip) for clip in object_list(payload.get("preview_clips"))],
            [object_dict(clip) for clip in object_list(payload.get("compare_clips"))],
        )
    moments: list[dict[str, Any]] = []
    for index, pair in enumerate(pairs[:3], start=1):
        source_clip = object_dict(pair.get("source_clip"))
        preview_clip = object_dict(pair.get("preview_clip"))
        compare_clip = object_dict(pair.get("compare_clip"))
        moments.append(
            {
                "moment": index,
                "timestamp_seconds": pair.get("timestamp_seconds"),
                "duration_seconds": pair.get("duration_seconds"),
                "source_clip_path": source_clip.get("path"),
                "source_clip_size_bytes": source_clip.get("size_bytes"),
                "preview_clip_path": preview_clip.get("path"),
                "preview_clip_size_bytes": preview_clip.get("size_bytes"),
                "compare_clip_path": compare_clip.get("path"),
            }
        )
    return {
        "review_media_ready": bool(payload.get("review_media_ready")) or bool(moments),
        "browser_review_ready": bool(payload.get("browser_review_ready")) or bool(pairs),
        "moment_count": len(pairs),
        "moments": moments,
    }


def review_pair_key(timestamp_seconds: float) -> int:
    return int(round(timestamp_seconds * 1000))


def save_advice_state(advice_file: Path, advice: dict[str, Any]) -> None:
    advice_file.write_text(json.dumps(dict(advice), indent=2) + "\n")


def load_pending_proposal(proposal_file: Path) -> dict[str, Any] | None:
    if not proposal_file.exists():
        return None
    try:
        payload = json.loads(proposal_file.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    normalized_payload = object_dict(payload)
    return normalized_payload or None


def save_pending_proposal(proposal_file: Path, payload: dict[str, Any]) -> None:
    proposal_file.write_text(json.dumps(payload, indent=2) + "\n")


def clear_pending_proposal(proposal_file: Path) -> None:
    try:
        proposal_file.unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


def pending_proposal_public_view(deps: FolderStateDeps, payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    self_check = object_dict(payload.get("self_check"))
    return {
        "proposal_id": payload.get("proposal_id"),
        "status": payload.get("status"),
        "kind": payload.get("kind"),
        "action": payload.get("action"),
        "created_at": payload.get("created_at"),
        "can_queue": bool(payload.get("can_queue")),
        "message": payload.get("message"),
        "operator_note": payload.get("operator_note"),
        "operator_request": payload.get("operator_request"),
        "request_disposition": payload.get("request_disposition"),
        "request_response": payload.get("request_response"),
        "feasibility_note": payload.get("feasibility_note"),
        "summary": payload.get("summary"),
        "diagnosis": payload.get("diagnosis"),
        "confidence": payload.get("confidence"),
        "suggested_follow_up": payload.get("suggested_follow_up"),
        "applied_policy": deps.tuning_policy_focus(object_dict(payload.get("applied_policy"))),
        "preview_policy": deps.tuning_policy_focus(object_dict(payload.get("preview_policy"))),
        "current_policy": deps.tuning_policy_focus(object_dict(payload.get("current_policy"))),
        "host": object_dict(payload.get("host")),
        "latest_failed_sample_job": object_dict(payload.get("latest_failed_sample_job")) or None,
        "self_check": {
            "status": self_check.get("status"),
            "summary": self_check.get("summary"),
            "issues": object_list(self_check.get("issues")),
        },
        "budget_enforcement": object_dict(payload.get("budget_enforcement")) or None,
        "operator_signal": payload.get("operator_signal"),
        "evidence_checked": object_list(payload.get("evidence_checked")),
        "multimodal_review_pack": object_dict(payload.get("multimodal_review_pack")) or None,
        "quality_risk": quality_risk_public_view(object_dict(payload.get("quality_risk_contract"))),
        "trace": deps.pending_proposal_trace_public_view(object_dict(payload.get("trace"))),
    }


def load_scan_job_state(scan_job_file: Path) -> dict[str, Any] | None:
    if not scan_job_file.exists():
        return None
    return json.loads(scan_job_file.read_text())


def save_scan_job_state(scan_job_file: Path, payload: dict[str, Any]) -> None:
    scan_job_file.write_text(json.dumps(payload, indent=2) + "\n")
