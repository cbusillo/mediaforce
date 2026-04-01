import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mediaforce.core.config import MediaforceConfig


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

    return {
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
        "status": row["status"],
        "recommendation": recommendation.bucket,
        "priority_score": recommendation.score,
        "recommendation_reason": recommendation.reason,
        "staging_path": str(staging_root / output_rel),
        "resolved_policy": policy,
        "audio_summary": audio_summary,
        "subtitle_summary": subtitle_summary,
    }
