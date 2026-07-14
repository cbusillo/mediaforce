from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

LibraryType = Literal["tv", "movie", "spatial", "other"]
LibraryAvailability = Literal["production", "browse_only", "disabled"]

LIBRARY_TYPE_OPTIONS = (
    {"key": "tv", "label": "TV"},
    {"key": "movie", "label": "Movies"},
    {"key": "spatial", "label": "3D / VR"},
    {"key": "other", "label": "Other"},
)
SUPPORTED_LIBRARY_TYPES = frozenset(option["key"] for option in LIBRARY_TYPE_OPTIONS)
SUPPORTED_LIBRARY_AVAILABILITY = frozenset({"production", "browse_only", "disabled"})

DEFAULT_LIBRARY_PROFILES: dict[LibraryType, str] = {
    "tv": "inherit_defaults",
    "movie": "movie_balanced",
    "spatial": "spatial_preserve",
    "other": "other_conservative",
}

LIBRARY_PROFILE_OPTIONS: dict[LibraryType, tuple[dict[str, str], ...]] = {
    "tv": (
        {"key": "inherit_defaults", "label": "Use assistant defaults"},
    ),
    "movie": (
        {"key": "movie_balanced", "label": "Balanced movie"},
        {"key": "movie_quality_first", "label": "Quality first"},
        {"key": "movie_space_recovery", "label": "Space recovery"},
    ),
    "spatial": (
        {"key": "spatial_preserve", "label": "Preserve source geometry"},
    ),
    "other": (
        {"key": "other_conservative", "label": "Conservative"},
        {"key": "other_source_preserving", "label": "Source preserving"},
    ),
}


def infer_library_type(key: str) -> LibraryType:
    normalized = str(key or "").strip().lower()
    if normalized == "tv":
        return "tv"
    if normalized == "movies":
        return "movie"
    return "other"


def default_library_availability(library_type: LibraryType) -> LibraryAvailability:
    return "production" if library_type == "tv" else "browse_only"


def default_library_policy(library_type: LibraryType) -> dict[str, Any]:
    if library_type == "tv":
        return {
            "series_lifecycle_mode": "auto",
            "current_season_inactive_days": 365,
            "season_acquisition_hold_days": 30,
            "series_metadata_stale_days": 7,
        }
    if library_type == "movie":
        return {
            "grouping": "title",
            "editions": "separate",
            "extras": "exclude",
            "ranking": "oldest_added_first",
        }
    if library_type == "spatial":
        return {
            "playback_target": "",
            "stereo_layout": "unknown",
            "projection": "unknown",
            "geometry_policy": "preserve",
            "container_profile": "unqualified",
        }
    return {
        "grouping": "folder",
    }


def normalize_library_type(value: Any, *, key: str = "") -> LibraryType:
    normalized = str(value or "").strip().lower()
    if normalized in SUPPORTED_LIBRARY_TYPES:
        return normalized  # type: ignore[return-value]
    return infer_library_type(key)


def normalize_library_availability(value: Any, *, library_type: LibraryType) -> LibraryAvailability:
    normalized = str(value or "").strip().lower()
    if normalized in SUPPORTED_LIBRARY_AVAILABILITY:
        return normalized  # type: ignore[return-value]
    return default_library_availability(library_type)


def normalize_library_policy(library_type: LibraryType, value: Any) -> dict[str, Any]:
    defaults = default_library_policy(library_type)
    if not isinstance(value, Mapping):
        return defaults
    return {key: value.get(key, default) for key, default in defaults.items()}


def normalize_library_definition(value: Any, *, key: str) -> dict[str, Any]:
    payload = dict(value) if isinstance(value, Mapping) else {}
    library_type = normalize_library_type(payload.get("type"), key=key)
    label = str(payload.get("label") or key.replace("_", " ").replace("-", " ").title()).strip()
    profile_options = {option["key"] for option in LIBRARY_PROFILE_OPTIONS[library_type]}
    profile = str(payload.get("default_profile") or DEFAULT_LIBRARY_PROFILES[library_type]).strip()
    if profile not in profile_options:
        profile = DEFAULT_LIBRARY_PROFILES[library_type]
    availability = payload.get("availability")
    if availability is None and not bool(payload.get("enabled", True)):
        availability = "disabled"
    if availability is None:
        availability = payload.get("processing_mode")
    return {
        "key": key,
        "label": label,
        "path": str(payload.get("path") or "").strip(),
        "color": str(payload.get("color") or "").strip(),
        "plex_path": str(payload.get("plex_path") or "").strip(),
        "type": library_type,
        "availability": normalize_library_availability(availability, library_type=library_type),
        "default_profile": profile,
        "policy": normalize_library_policy(library_type, payload.get("policy")),
    }


def configured_library_definitions(media: Mapping[str, Any]) -> list[dict[str, Any]]:
    source_roots = media.get("source_roots")
    configured = media.get("libraries")
    if isinstance(configured, list):
        definitions: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in configured:
            if not isinstance(item, Mapping):
                continue
            key = str(item.get("key") or "").strip()
            if not key or key in seen:
                continue
            payload = dict(item)
            if isinstance(source_roots, Mapping):
                payload.setdefault("path", source_roots.get(key))
            definitions.append(normalize_library_definition(payload, key=key))
            seen.add(key)
        return [definition for definition in definitions if str(definition.get("path") or "").strip()]
    if not isinstance(source_roots, Mapping):
        return []
    return [
        normalize_library_definition({"path": value}, key=str(key))
        for key, value in source_roots.items()
        if str(source_roots.get(key) or "").strip()
    ]


def library_definition_map(media: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {definition["key"]: definition for definition in configured_library_definitions(media)}


def library_type_label(library_type: str) -> str:
    return next(
        (option["label"] for option in LIBRARY_TYPE_OPTIONS if option["key"] == library_type),
        "Other",
    )


def library_production_supported(library_type: str) -> bool:
    return library_type in {"tv", "movie", "other"}
