import json
from collections import Counter
from pathlib import Path
from typing import Any

from sqlalchemy import select

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import library_items
from mediaforce.core.type_defs import mapping_dict
from mediaforce.library.media_scopes import resolve_media_scope, scope_rel_path_filter


def inspect_prefix(connection: DBClient, config: MediaforceConfig, prefix: str) -> dict[str, Any]:
    scope = resolve_media_scope(connection, prefix)
    rows = [
        mapping_dict(row)
        for row in connection.execute(
            select(library_items)
            .where(scope_rel_path_filter(library_items.c.rel_path, scope))
            .order_by(library_items.c.rel_path)
        ).mappings().fetchall()
    ]
    if not rows:
        return {"prefix": prefix, "item_count": 0, "rows": []}

    seasons: Counter[str] = Counter()
    video_codecs: Counter[str] = Counter()
    audio_codecs: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    total_size = 0

    for row in rows:
        total_size += int(row["size_bytes"])
        statuses[str(row["status"])] += 1
        video_codecs[str(row["video_codec"] or "unknown")] += 1
        for audio in json.loads(row["audio_summary_json"]):
            audio_codecs[f"{audio.get('codec_name')}:{audio.get('channels')}"] += 1
        rel_parts = Path(str(row["rel_path"])).parts
        if len(rel_parts) >= 3:
            seasons[rel_parts[2]] += 1

    policy = config.resolve_policy(prefix)
    recommendation = recommend_folder_profile(prefix, rows, policy)
    return {
        "prefix": prefix,
        "item_count": len(rows),
        "total_size_bytes": total_size,
        "statuses": dict(statuses),
        "video_codecs": dict(video_codecs),
        "audio_codecs": dict(audio_codecs),
        "seasons": dict(seasons),
        "resolved_policy": policy,
        "suggested_override": recommendation,
    }

def recommend_folder_profile(prefix: str, rows: list[dict[str, Any]], resolved_policy: dict[str, Any]) -> dict[
    str, Any]:
    video_codecs: Counter[str] = Counter(str(row["video_codec"] or "unknown") for row in rows)
    avg_size_gib = (sum(int(row["size_bytes"]) for row in rows) / max(len(rows), 1)) / (1024 ** 3)
    audio_codecs: Counter[str] = Counter()
    for row in rows:
        for audio in json.loads(row["audio_summary_json"]):
            audio_codecs[str(audio.get("codec_name") or "unknown")] += 1

    suggestion: dict[str, Any] = {
        "path_prefix": prefix,
        "video": {},
        "audio": {},
        "planning": {},
        "reason": [],
    }

    if video_codecs.get("h264", 0) >= max(len(rows) * 0.6, 1):
        suggestion["planning"]["extra_score"] = max(12, int(resolved_policy["planning"].get("extra_score", 0)))
        suggestion["reason"].append("Mostly H.264, so this folder is a strong AV1 candidate.")

    if audio_codecs.get("dts", 0) >= max(len(rows) * 0.6, 1):
        suggestion["audio"]["surround_5_1_opus_bitrate"] = "224k"
        suggestion["reason"].append("DTS 5.1 dominates, so Opus conversion should recover meaningful space.")

    if avg_size_gib >= 2.5:
        suggestion["video"]["max_encoded_percent"] = 55
        suggestion["reason"].append("Episodes are large enough that a size-first AV1 target is worthwhile.")

    if _looks_clean_catalog_tv(prefix):
        suggestion["video"]["default_grain"] = 0
        suggestion["video"]["target_xpsnr"] = 35.5
        suggestion["video"]["min_target_xpsnr"] = 34.5
        suggestion["reason"].append(
            "Clean catalog TV usually does not need synthetic grain and can tolerate a lower XPSNR floor.")

    return suggestion


def _looks_clean_catalog_tv(prefix: str) -> bool:
    lower = prefix.lower()
    return lower.startswith("tv/") and not any(keyword in lower for keyword in ("film", "criterion", "remux"))
