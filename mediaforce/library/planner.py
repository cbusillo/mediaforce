import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.evidence import stable_source_id
from mediaforce.core.type_defs import float_value, object_dict
from mediaforce.encoding.cadence import cadence_manifest_payload
from mediaforce.tuning.size_goals import operator_intent_from_policy
from mediaforce.tuning.stream_budget import resolve_stream_budget_ledger


@dataclass(slots=True)
class Recommendation:
    bucket: str
    score: float
    reason: str


def recommend_item(row: dict[str, Any], config: MediaforceConfig) -> Recommendation:
    planning = config.resolve_policy(str(row["rel_path"]))["planning"]
    size_gib = float(row["size_bytes"]) / (1024 ** 3)
    video_codec = (row["video_codec"] or "unknown").lower()

    score = size_gib * float(planning.get("size_weight", 10))
    reasons: list[str] = []

    codec_bonus = planning.get("codec_bonus", {})
    score += float(codec_bonus.get(video_codec, codec_bonus.get("default", 0)))

    if video_codec == "h264":
        reasons.append("H.264 source is usually the highest-value AV1 target.")
    elif video_codec in {"hevc", "h265"}:
        reasons.append("HEVC source may still be worth it, but benefit depends on current bitrate and quality.")
    elif video_codec == "av1":
        reasons.append("Already AV1, so re-encode should only happen after deliberate review.")
    else:
        reasons.append(f"{video_codec.upper()} source needs case-by-case review.")

    if size_gib >= float(planning.get("large_file_gib", 4.0)):
        score += float(planning.get("large_file_bonus", 25))
        reasons.append("Large source file means even moderate savings will recover real space.")
    elif size_gib < float(planning.get("small_file_gib", 1.5)):
        score += float(planning.get("small_file_penalty", -15))
        reasons.append("Smaller file, so the payoff may not justify a lossy rewrite.")

    if int(row["audio_track_count"] or 0) > 1:
        score += float(planning.get("multi_audio_bonus", 6))
        reasons.append("Multiple audio tracks create an extra cleanup opportunity.")

    if int(row["english_audio_count"] or 0) == 0:
        score += float(planning.get("missing_english_audio_penalty", -30))
        reasons.append("No tagged English audio was found, so this item needs manual review before encode.")

    if int(row["english_subtitle_count"] or 0) > 0:
        score += float(planning.get("english_subtitle_bonus", 3))
        reasons.append("English subtitle metadata already exists and should be easier to normalize.")

    extra_score = float(planning.get("extra_score", 0))
    if extra_score:
        score += extra_score
        reasons.append("Folder policy applies an explicit planning override.")

    thresholds = planning.get("bucket_thresholds", {})
    if int(row["english_audio_count"] or 0) == 0:
        bucket = "manual_review"
    elif score >= float(thresholds.get("priority_encode", 70)):
        bucket = "priority_encode"
    elif score >= float(thresholds.get("review_encode", 35)):
        bucket = "review_encode"
    else:
        bucket = "low_value_review"

    return Recommendation(bucket=bucket, score=round(score, 2), reason=" ".join(reasons))


def build_manifest_item(row: dict[str, Any], config: MediaforceConfig) -> dict[str, Any]:
    policy = config.resolve_policy(str(row["rel_path"]))
    staging_root = config.staging_root
    output_rel = Path(row["rel_path"]).with_suffix(f".{config.output_container}")
    recommendation = recommend_item(row, config)

    audio_summary = json.loads(row["audio_summary_json"])
    subtitle_summary = json.loads(row["subtitle_summary_json"])
    attachment_summary_json = row.get("attachment_summary_json")
    attachment_summary = json.loads(attachment_summary_json) if attachment_summary_json else None
    cadence_summary = json.loads(row["cadence_summary_json"]) if row.get("cadence_summary_json") else None
    source_id = stable_source_id(row)
    cadence_evidence, cadence_decision = cadence_manifest_payload(
        cadence_summary,
        source_id=source_id,
        source_fingerprint=str(row.get("fingerprint") or "") or None,
    )
    video_policy = object_dict(policy.get("video"))
    operator_intent = operator_intent_from_policy(
        video_policy,
        default_video_policy=config.video,
        audio_policy=object_dict(policy.get("audio")),
        subtitle_policy=object_dict(policy.get("subtitle")),
    )

    item = {
        "library_item_id": row["id"],
        "source_path": row["source_path"],
        "rel_path": row["rel_path"],
        "media_root": row["media_root"],
        "source_fingerprint": row["fingerprint"],
        "source_size_bytes": row["size_bytes"],
        "video_codec": row["video_codec"],
        "video_bitrate": row.get("video_bitrate"),
        "width": row.get("width"),
        "height": row.get("height"),
        "duration_seconds": row["duration_seconds"],
        "container": row["container"],
        "output_container": config.output_container,
        "status": row["status"],
        "recommendation": recommendation.bucket,
        "priority_score": recommendation.score,
        "recommendation_reason": recommendation.reason,
        "staging_path": str(staging_root / output_rel),
        "resolved_policy": policy,
        "resolved_operator_intent": operator_intent.to_payload(
            item_runtime_seconds=float_value(row.get("duration_seconds")) or None
        ),
        "audio_summary": audio_summary,
        "subtitle_summary": subtitle_summary,
        "attachment_summary": attachment_summary,
        "cadence_summary": cadence_summary,
        "cadence_evidence": cadence_evidence,
        "cadence_decision": cadence_decision,
        "cadence_class": cadence_decision["classification"],
    }
    item["stream_budget_ledger"] = resolve_stream_budget_ledger(
        item,
        default_video_policy=config.video,
        output_container=config.output_container,
        resolved_size_goal=operator_intent.size_goal.resolve(float_value(row.get("duration_seconds")) or None),
        prefer_persisted=False,
    ).to_payload()
    return item
