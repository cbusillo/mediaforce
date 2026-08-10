"""Shared revision-4 invocation identity derivation.

The helper in this module intentionally mirrors the runner's private call into
the v4 qualification search contract.  It performs no media I/O and exposes no
runtime authority; callers supply already-frozen private paths and stream facts
only to derive the exact digest the runner must later observe.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mediaforce.core.type_defs import object_dict
from mediaforce.encoding.quality import QualitySearchWarmStart
from mediaforce.tuning.av1_validation_v4_qualification_search import (
    av1_validation_v4_qualification_search_invocation_sha256,
)
from mediaforce.tuning.av1_validation_v4r4_contract import (
    AV1_V4R4_POLICY_VALUES,
    av1_v4r4_ordinal_layout,
)


class AV1V4R4InvocationError(ValueError):
    """Raised when revision-4 invocation inputs are invalid."""


def av1_v4r4_video_policy_for_ordinal(ordinal: int) -> dict[str, Any]:
    """Return the frozen runner video policy for one ordinal."""

    layout = _layout_for_ordinal(ordinal)
    return {
        "encoder": AV1_V4R4_POLICY_VALUES["encoder"],
        "pixel_format": AV1_V4R4_POLICY_VALUES["pixel_format"],
        "preset": AV1_V4R4_POLICY_VALUES["preset"],
        "quality_metric": AV1_V4R4_POLICY_VALUES["quality_metric"],
        "target_vmaf": AV1_V4R4_POLICY_VALUES["target_vmaf"],
        "target_xpsnr": AV1_V4R4_POLICY_VALUES["target_xpsnr"],
        "min_target_vmaf": AV1_V4R4_POLICY_VALUES["min_target_vmaf"],
        "min_target_xpsnr": AV1_V4R4_POLICY_VALUES["min_target_xpsnr"],
        "target_size_bytes": layout["target_size_bytes"],
        "target_size_mb": AV1_V4R4_POLICY_VALUES["target_size_mb"],
        "target_runtime_minutes": AV1_V4R4_POLICY_VALUES["target_runtime_minutes"],
        "size_goal_schema_version": AV1_V4R4_POLICY_VALUES["size_goal_schema_version"],
        "size_goal_mode": "absolute",
        "size_goal_source": "av1_v4r4_frozen_ordinal_layout",
        "sample_projection_tolerance_percent": AV1_V4R4_POLICY_VALUES[
            "sample_projection_tolerance_percent"
        ],
        "final_output_tolerance_percent": AV1_V4R4_POLICY_VALUES[
            "final_output_tolerance_percent"
        ],
        "compression_intent_schema_version": AV1_V4R4_POLICY_VALUES[
            "compression_intent_schema_version"
        ],
        "compression_intent": AV1_V4R4_POLICY_VALUES["compression_intent"],
        "compression_intent_source": AV1_V4R4_POLICY_VALUES[
            "compression_intent_source"
        ],
        "compression_intent_confirmed": AV1_V4R4_POLICY_VALUES[
            "compression_intent_confirmed"
        ],
        "min_crf": AV1_V4R4_POLICY_VALUES["min_crf"],
        "max_crf": AV1_V4R4_POLICY_VALUES["max_crf"],
        "target_search_max_crf": AV1_V4R4_POLICY_VALUES["target_search_max_crf"],
        "max_encoded_percent": AV1_V4R4_POLICY_VALUES["max_encoded_percent"],
        "default_grain": AV1_V4R4_POLICY_VALUES["default_grain"],
        "grain_denoise": AV1_V4R4_POLICY_VALUES["grain_denoise"],
        "thorough": AV1_V4R4_POLICY_VALUES["thorough"],
        "max_height": AV1_V4R4_POLICY_VALUES["max_height"],
        "resolution_intent_mode": AV1_V4R4_POLICY_VALUES["resolution_intent_mode"],
        "resolution_intent_source": AV1_V4R4_POLICY_VALUES[
            "resolution_intent_source"
        ],
        "downsample_algorithm": AV1_V4R4_POLICY_VALUES["downsample_algorithm"],
        "black_bar_handling": AV1_V4R4_POLICY_VALUES["black_bar_handling"],
        "black_bar_detect_samples": AV1_V4R4_POLICY_VALUES[
            "black_bar_detect_samples"
        ],
        "black_bar_detect_seconds": AV1_V4R4_POLICY_VALUES[
            "black_bar_detect_seconds"
        ],
        "crop": AV1_V4R4_POLICY_VALUES["crop"],
    }


def av1_v4r4_warm_start_for_ordinal(
    ordinal: int,
) -> QualitySearchWarmStart | None:
    """Return the frozen runner warm start for one ordinal."""

    payload = object_dict(_layout_for_ordinal(ordinal)["warm_start"])
    if not payload:
        return None
    return QualitySearchWarmStart(
        requested_crf=float(payload["requested_crf"]),
        candidate_crf=int(payload["candidate_crf"]),
        search_signature_id=str(payload["search_signature_id"]),
        cohort_id=str(payload["cohort_id"]),
        source=str(payload["source"]),
        confidence=None,
        provenance_id=None,
        review_risks=(),
    )


def av1_v4r4_mode_for_ordinal(ordinal: int) -> str:
    """Return the v4 qualification mode for one frozen r4 ordinal."""

    return "guided" if _layout_for_ordinal(ordinal)["warm_start"] is not None else "baseline"


def av1_v4r4_search_kwargs_for_inputs(
    *,
    source_codec: str,
    width: int,
    height: int,
    quality_temp_path: Path,
) -> dict[str, Any]:
    """Return the exact extra search kwargs retained by the runner."""

    if not isinstance(quality_temp_path, Path) or not quality_temp_path.is_absolute():
        raise AV1V4R4InvocationError("AV1 v4 r4 quality temp path must be absolute")
    if not isinstance(source_codec, str) or not source_codec:
        raise AV1V4R4InvocationError("AV1 v4 r4 source codec is invalid")
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        raise AV1V4R4InvocationError("AV1 v4 r4 source dimensions are invalid")
    return {
        "source_codec": source_codec,
        "width": width,
        "height": height,
        "quality_temp_dir": quality_temp_path,
    }


def av1_v4r4_runner_invocation_sha256(
    *,
    ordinal: int,
    source_path: Path,
    quality_temp_path: Path,
    source_codec: str,
    width: int,
    height: int,
) -> str:
    """Return the exact v4 qualification invocation digest for r4 runner use."""

    if not isinstance(source_path, Path) or not source_path.is_absolute():
        raise AV1V4R4InvocationError("AV1 v4 r4 source path must be absolute")
    return av1_validation_v4_qualification_search_invocation_sha256(
        source_path=source_path,
        video_policy=av1_v4r4_video_policy_for_ordinal(ordinal),
        mode=av1_v4r4_mode_for_ordinal(ordinal),
        warm_start=av1_v4r4_warm_start_for_ordinal(ordinal),
        extra_search_kwargs=av1_v4r4_search_kwargs_for_inputs(
            source_codec=source_codec,
            width=width,
            height=height,
            quality_temp_path=quality_temp_path,
        ),
    )


def _layout_for_ordinal(ordinal: int) -> dict[str, Any]:
    layout = av1_v4r4_ordinal_layout()
    if type(ordinal) is not int or not 1 <= ordinal <= len(layout):
        raise AV1V4R4InvocationError("AV1 v4 r4 ordinal is invalid")
    return layout[ordinal - 1]
