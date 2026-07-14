from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.type_defs import object_dict
from mediaforce.library.library_settings import LIBRARY_PROFILE_OPTIONS
from mediaforce.library.media_scopes import MediaScope, media_scope_from_prefix

OTHER_FOLDER_SCOPE_MAX_ITEMS = 250
OTHER_LIBRARY_CATALOG_MAX_ITEMS = 5_000
OTHER_LIBRARY_CATALOG_MAX_WORK_UNITS = 500


def other_group_scope_for_rel_path(rel_path: str, config: MediaforceConfig) -> MediaScope | None:
    normalized = str(rel_path or "").strip().strip("/")
    parts = PurePosixPath(normalized).parts
    if len(parts) < 2:
        return None
    root = parts[0]
    library = config.library_definition_map.get(root, {})
    if str(library.get("type") or "") != "other":
        return None
    grouping = str(object_dict(library.get("policy")).get("grouping") or "folder")
    if grouping == "file" or len(parts) == 2:
        return media_scope_from_prefix(
            normalized,
            match="exact_item",
            library_types=config.library_type_map,
        )
    return media_scope_from_prefix(
        "/".join(parts[:2]),
        match="descendants",
        library_types=config.library_type_map,
    )


def other_scope_boundary_blocker(
        scope: MediaScope,
        library: Mapping[str, Any],
) -> str | None:
    parts = PurePosixPath(scope.prefix).parts
    if len(parts) < 2:
        return "Choose one bounded folder or exact file from the Other Library before processing."
    grouping = str(object_dict(library.get("policy")).get("grouping") or "folder")
    if grouping == "file":
        if scope.match != "exact_item":
            return "This Other root uses exact-file grouping. Open one file before processing."
        return None
    if len(parts) != 2:
        return "This Other root uses top-level folder grouping. Open the bounded folder from Other Library."
    if scope.match not in {"descendants", "exact_item"}:
        return "Choose one bounded folder or exact file from the Other Library before processing."
    return None


def other_profile_label(profile: str) -> str:
    return next(
        (
            option["label"]
            for option in LIBRARY_PROFILE_OPTIONS["other"]
            if option["key"] == profile
        ),
        "Unconfigured profile",
    )


def other_item_profile_blocker(
        row: Mapping[str, Any],
        library: Mapping[str, Any],
) -> str | None:
    profile = str(library.get("default_profile") or "").strip()
    valid_profiles = {option["key"] for option in LIBRARY_PROFILE_OPTIONS["other"]}
    if profile not in valid_profiles:
        return "Choose a supported Other processing profile in Settings."
    video_codec = str(row.get("video_codec") or "").strip().lower()
    if not video_codec or video_codec == "unknown":
        return f"{other_profile_label(profile)} requires a detected video stream."
    if _positive_float(row.get("duration_seconds")) <= 0:
        return f"{other_profile_label(profile)} requires a measured media duration."
    if _positive_int(row.get("width")) <= 0 or _positive_int(row.get("height")) <= 0:
        return f"{other_profile_label(profile)} requires measured frame dimensions."
    return None


def other_profile_readiness(
        library: Mapping[str, Any],
        *,
        item_count: int,
        blockers: list[str],
        membership_complete: bool,
) -> dict[str, Any]:
    availability = str(library.get("availability") or "browse_only")
    profile = str(library.get("default_profile") or "")
    profile_label = other_profile_label(profile)
    if availability != "production":
        return {
            "state": "browse_only",
            "label": "Browse only",
            "detail": "Enable Production in Settings before sampling or queueing this scope.",
            "profile": profile,
            "profile_label": profile_label,
            "blockers": [],
        }
    if not membership_complete:
        return {
            "state": "blocked",
            "label": "Scope too large",
            "detail": (
                f"This folder contains more than {OTHER_FOLDER_SCOPE_MAX_ITEMS} files. "
                "Choose exact-file grouping or split the source folder before processing."
            ),
            "profile": profile,
            "profile_label": profile_label,
            "blockers": ["The complete membership cannot be reviewed as one bounded work unit."],
        }
    unique_blockers = list(dict.fromkeys(blockers))
    if unique_blockers:
        blocked_count = len(blockers)
        return {
            "state": "blocked",
            "label": "Profile blocked",
            "detail": (
                f"{blocked_count} of {item_count} files do not meet the {profile_label} requirements. "
                f"{unique_blockers[0]}"
            ),
            "profile": profile,
            "profile_label": profile_label,
            "blockers": unique_blockers,
        }
    return {
        "state": "ready",
        "label": "Profile ready",
        "detail": (
            f"All {item_count} indexed files meet the {profile_label} probe requirements."
            if item_count
            else f"{profile_label} is selected; this root is currently empty."
        ),
        "profile": profile,
        "profile_label": profile_label,
        "blockers": [],
    }


def _positive_float(value: Any) -> float:
    try:
        return max(float(value or 0), 0.0)
    except (TypeError, ValueError):
        return 0.0


def _positive_int(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0

