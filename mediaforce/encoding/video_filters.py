from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from mediaforce.core.type_defs import int_value
from mediaforce.encoding.cadence import cadence_filter


CROP_RE = re.compile(r"crop=(?P<width>\d+):(?P<height>\d+):(?P<x>\d+):(?P<y>\d+)")
FILTER_VALUE_RE = re.compile(r"^[A-Za-z0-9_./:+*\\\-]+$")


@dataclass(frozen=True, slots=True)
class CropSpec:
    width: int
    height: int
    x: int
    y: int

    @classmethod
    def parse(cls, value: str) -> "CropSpec | None":
        match = CROP_RE.search(value.strip())
        if match is None:
            return None
        return cls(
            width=int(match.group("width")),
            height=int(match.group("height")),
            x=int(match.group("x")),
            y=int(match.group("y")),
        )

    def filter_value(self) -> str:
        return f"{self.width}:{self.height}:{self.x}:{self.y}"


def build_video_filter(
        video_policy: dict[str, Any],
        *,
        width: int | None = None,
        height: int | None = None,
        detected_crop: str | None = None,
        cadence_decision: dict[str, Any] | None = None,
        cadence_evidence: dict[str, Any] | None = None,
        cadence_source_fingerprint: str | None = None,
) -> str | None:
    filters: list[str] = []
    if cadence_decision is not None:
        transform_filter = cadence_filter(
            cadence_decision,
            cadence_evidence,
            source_fingerprint=cadence_source_fingerprint,
        )
        if transform_filter:
            filters.append(transform_filter)

    crop_filter = _crop_filter(video_policy, width=width, height=height, detected_crop=detected_crop)
    if crop_filter:
        filters.append(crop_filter)

    scale_filter = _scale_filter(video_policy, source_height=_post_crop_height(crop_filter, height))
    if scale_filter:
        filters.append(scale_filter)

    if not filters:
        return None
    return ",".join(filters)


def most_common_crop(stderr: str, *, source_width: int | None = None, source_height: int | None = None) -> str | None:
    candidates = [crop for crop in (CropSpec.parse(match.group(0)) for match in CROP_RE.finditer(stderr)) if crop]
    if not candidates:
        return None
    meaningful = [crop for crop in candidates if _meaningful_crop(crop, source_width=source_width, source_height=source_height)]
    if not meaningful:
        return None
    counts = Counter(crop.filter_value() for crop in meaningful)
    value, _ = counts.most_common(1)[0]
    return value


def _crop_filter(
        video_policy: dict[str, Any],
        *,
        width: int | None,
        height: int | None,
        detected_crop: str | None,
) -> str | None:
    manual_crop = str(video_policy.get("crop") or video_policy.get("crop_filter") or "").strip()
    crop_value = manual_crop or _detected_crop_for_policy(video_policy, detected_crop)
    if not crop_value:
        return None
    spec = CropSpec.parse(f"crop={crop_value}")
    if spec is None or not _meaningful_crop(spec, source_width=width, source_height=height):
        return None
    return f"crop={spec.filter_value()}"


def _detected_crop_for_policy(video_policy: dict[str, Any], detected_crop: str | None) -> str:
    handling = str(video_policy.get("black_bar_handling") or video_policy.get("crop_black_bars") or "off").lower()
    if handling in {"1", "true", "yes", "auto", "smart"}:
        return str(detected_crop or "").strip()
    return ""


def _scale_filter(video_policy: dict[str, Any], *, source_height: int | None) -> str | None:
    max_height = int_value(video_policy.get("max_height"))
    if max_height <= 0:
        max_height = int_value(video_policy.get("target_height"))
    if max_height <= 0:
        return None
    if source_height is not None and source_height > 0 and source_height <= max_height:
        return None
    algorithm = str(video_policy.get("downsample_algorithm") or video_policy.get("scale_algorithm") or "lanczos")
    if not FILTER_VALUE_RE.match(algorithm):
        algorithm = "lanczos"
    return f"scale=-2:{max_height}:flags={algorithm}"


def _post_crop_height(crop_filter: str | None, source_height: int | None) -> int | None:
    if not crop_filter:
        return source_height
    spec = CropSpec.parse(crop_filter)
    if spec is None:
        return source_height
    return spec.height


def _meaningful_crop(crop: CropSpec, *, source_width: int | None, source_height: int | None) -> bool:
    if crop.width <= 0 or crop.height <= 0:
        return False
    if source_width and crop.width > source_width:
        return False
    if source_height and crop.height > source_height:
        return False
    if source_width and source_height and crop.width == source_width and crop.height == source_height and crop.x == 0 and crop.y == 0:
        return False
    return crop.width % 2 == 0 and crop.height % 2 == 0
