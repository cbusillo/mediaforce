import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mediaforce.core.config import FOLDER_POLICY_OVERRIDES_KEY, MediaforceConfig
from mediaforce.encoding.encode_queue import DEFAULT_SCHEDULER_POLICY
from mediaforce.hosts.config import normalize_host_capabilities
from mediaforce.library.library_settings import DEFAULT_LIBRARY_PROFILES, LIBRARY_PROFILE_OPTIONS, \
    LIBRARY_TYPE_OPTIONS, default_library_policy, library_production_supported, library_type_label, \
    normalize_library_availability, normalize_library_definition, normalize_library_policy, normalize_library_type
from mediaforce.remote import DEFAULT_HOST_CAPABILITIES, DEFAULT_HOST_MEDIA_ACCESS, normalize_host_media_access
from mediaforce.core.type_defs import JSONValue
from mediaforce.tuning.size_goals import megabytes_to_bytes

DEFAULT_LIBRARY_COLOR_PALETTE = (
    "#a16207",
    "#4e6fa6",
    "#c2410c",
    "#0f766e",
    "#7c6142",
    "#6b7280",
)
SCHEDULE_DAY_ORDER = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SCHEDULE_DAY_LABELS = {
    "mon": "Mon",
    "tue": "Tue",
    "wed": "Wed",
    "thu": "Thu",
    "fri": "Fri",
    "sat": "Sat",
    "sun": "Sun",
}
ALWAYS_SCHEDULE_PROFILE = "always"
NEVER_SCHEDULE_PROFILE = "never"
LEGACY_QUEUE_WINDOW_SCHEDULE_PROFILE = "queue_window"
LEGACY_DEFAULT_SCHEDULE_PROFILE = "default"
DEFAULT_HOST_SCHEDULE_PROFILE = ALWAYS_SCHEDULE_PROFILE
DEFAULT_HOST_MAX_PARALLEL_ENCODES = 1
DEFAULT_VIDEO_DEFAULTS = {
    "quality_metric": "auto",
    "target_vmaf": "85",
    "min_target_vmaf": "80",
    "target_xpsnr": "41",
    "min_target_xpsnr": "35",
    "target_size_mb": "300",
    "target_runtime_minutes": "45",
    "size_goal_mode": "normalized",
    "sample_projection_tolerance_percent": "10",
    "final_output_tolerance_percent": "5",
    "decision_model": "size_first_review",
    "quality_engine": "ab_av1_fast_sample",
    "max_height": "1080",
    "default_grain": "8",
    "max_encoded_percent": "80",
}
SUPPORTED_DECISION_MODELS = {"size_first_review", "quality_first_sample"}
SUPPORTED_QUALITY_ENGINES = {"ab_av1_fast_sample"}
HOST_CAPABILITY_OPTIONS = (
    {"key": "encode_queue", "label": "Queue encodes", "help": "Allow this host to run queued folder encodes."},
    {
        "key": "sample_calibration",
        "label": "Sample calibration",
        "help": "Allow this host to handle sampled calibration work and AI-guided sample retries.",
    },
)


class SettingsValidationError(ValueError):
    pass


def settings_library_rows(source_root_map: dict[str, Path], *, min_rows: int = 3) -> list[dict[str, Any]]:
    rows = [
        {
            "key": key,
            "label": key.replace("_", " ").replace("-", " ").title(),
            "path": str(path),
            "color": DEFAULT_LIBRARY_COLOR_PALETTE[index % len(DEFAULT_LIBRARY_COLOR_PALETTE)],
            "library_type": normalize_library_type(None, key=key),
        }
        for index, (key, path) in enumerate(source_root_map.items())
    ]
    return index_settings_library_rows(rows, min_rows=min_rows)


def settings_library_rows_for_config(config: MediaforceConfig, *, min_rows: int = 3) -> list[dict[str, Any]]:
    library_colors = library_color_map_for_config(config)
    plex = config.metadata.get("plex")
    plex_roots = plex.get("library_roots") if isinstance(plex, dict) else None
    plex_root_map = plex_roots if isinstance(plex_roots, dict) else {}
    rows: list[dict[str, Any]] = []
    for definition in config.library_definitions:
        key = str(definition["key"])
        rows.append(
            {
                "key": key,
                "label": str(definition["label"]),
                "path": str(definition.get("path") or ""),
                "color": str(definition.get("color") or library_colors.get(key, "")),
                "library_type": str(definition["type"]),
                "availability": str(definition["availability"]),
                "default_profile": str(definition["default_profile"]),
                "plex_path": str(definition.get("plex_path") or plex_root_map.get(key) or ""),
                "policy": dict(definition["policy"]),
            }
        )
    return index_settings_library_rows(rows, min_rows=min_rows)


def index_settings_library_rows(rows: list[dict[str, Any]], *, min_rows: int = 1) -> list[dict[str, Any]]:
    indexed: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        key = str(row.get("key") or "").strip()
        library_type = normalize_library_type(row.get("library_type") or row.get("type"), key=key)
        definition = normalize_library_definition(
            {
                "label": row.get("label"),
                "type": library_type,
                "availability": row.get("availability"),
                "default_profile": row.get("default_profile"),
                "policy": row.get("policy"),
            },
            key=key,
        )
        indexed.append(
            {
                "index": str(index),
                "key": key,
                "label": definition["label"],
                "path": str(row.get("path") or ""),
                "color": str(row.get("color") or ""),
                "library_type": library_type,
                "availability": definition["availability"],
                "default_profile": definition["default_profile"],
                "plex_path": str(row.get("plex_path") or ""),
                "policy": definition["policy"],
                "readiness": library_readiness(
                    key=key,
                    label=str(definition["label"]),
                    path=str(row.get("path") or ""),
                    library_type=library_type,
                    availability=str(definition["availability"]),
                    policy=dict(definition["policy"]),
                ),
                "type_change_confirmation": "",
            }
        )
    while len(indexed) < min_rows:
        indexed.append(empty_settings_library_row(len(indexed)))
    return indexed


def empty_settings_library_row(index: int) -> dict[str, Any]:
    library_type = "other"
    return {
        "index": str(index),
        "key": "",
        "label": "",
        "path": "",
        "color": "#0f766e",
        "library_type": library_type,
        "availability": "browse_only",
        "default_profile": DEFAULT_LIBRARY_PROFILES[library_type],
        "plex_path": "",
        "policy": default_library_policy(library_type),
        "readiness": {"state": "incomplete", "label": "Incomplete", "detail": "Add a label and local path."},
        "type_change_confirmation": "",
    }


def library_readiness(
        *,
        key: str,
        label: str,
        path: str,
        library_type: str,
        availability: str,
        policy: dict[str, Any],
) -> dict[str, str]:
    if not key or not label.strip() or not path.strip():
        return {"state": "incomplete", "label": "Incomplete", "detail": "Add a label, root ID, and local path."}
    if availability == "disabled":
        return {"state": "disabled", "label": "Disabled", "detail": "Mediaforce will not scan or process this library."}
    if library_type == "spatial":
        missing = []
        if not str(policy.get("playback_target") or "").strip():
            missing.append("playback target")
        if str(policy.get("stereo_layout") or "unknown") == "unknown":
            missing.append("stereo layout")
        if str(policy.get("projection") or "unknown") == "unknown":
            missing.append("projection")
        if missing:
            return {
                "state": "incomplete",
                "label": "Incomplete",
                "detail": f"Add {', '.join(missing)} before playback qualification.",
            }
        return {
            "state": "blocked",
            "label": "Safety blocked",
            "detail": "Browse-only until 3D / VR playback qualification is implemented.",
        }
    if availability == "browse_only":
        return {
            "state": "browse_only",
            "label": "Browse only",
            "detail": "Mediaforce scans and shows this library but never processes it.",
        }
    if not library_production_supported(library_type):
        return {
            "state": "blocked",
            "label": "Workflow blocked",
            "detail": f"{library_type_label(library_type)} production is not available yet.",
        }
    return {"state": "ready", "label": "Ready", "detail": "Scanning and production processing are enabled."}


def settings_remote_rows(remote_hosts: list[dict[str, Any]], *, min_rows: int = 3) -> list[dict[str, Any]]:
    rows = [_settings_remote_row(host) for host in remote_hosts]
    return index_settings_remote_rows(rows, min_rows=min_rows)


def settings_remote_rows_for_config(config: MediaforceConfig, *, min_rows: int = 3) -> list[dict[str, Any]]:
    remote_hosts = config.raw.get("remote_hosts")
    rows: list[dict[str, Any]] = []
    if isinstance(remote_hosts, list):
        for host in remote_hosts:
            if not isinstance(host, dict):
                continue
            rows.append(_settings_remote_row(host))
    return index_settings_remote_rows(rows, min_rows=min_rows)


def _settings_remote_row(host: dict[str, Any]) -> dict[str, Any]:
    return {
        "label": str(host.get("label") or ""),
        "host": str(host.get("host") or ""),
        "repo_path": str(host.get("repo_path") or ""),
        "wake_mac": str(host.get("wake_mac") or host.get("wol_mac") or ""),
        "start_command": str(host.get("start_command") or ""),
        "stop_command": str(host.get("stop_command") or ""),
        "start_timeout_seconds": str(host.get("start_timeout_seconds") or "180"),
        "media_access": normalize_host_media_access(host.get("media_access")),
        "priority": str(host.get("priority") or "0"),
        "max_parallel_encodes": str(host_max_parallel_encodes(host)),
        "schedule_profile": host_schedule_profile_key(host),
        "capabilities": normalize_host_capabilities(host.get("capabilities")),
        "allowed_libraries": normalize_allowed_libraries(host.get("allowed_libraries")),
        "source_roots_json": settings_host_source_roots_json(host.get("source_roots")),
        "staging_root": str(host.get("staging_root") or ""),
    }


def index_settings_remote_rows(rows: list[dict[str, Any]], *, min_rows: int = 1) -> list[dict[str, Any]]:
    indexed = [
        {
            "index": str(index),
            "label": row.get("label", ""),
            "host": row.get("host", ""),
            "repo_path": row.get("repo_path", ""),
            "wake_mac": row.get("wake_mac", ""),
            "start_command": row.get("start_command", ""),
            "stop_command": row.get("stop_command", ""),
            "start_timeout_seconds": row.get("start_timeout_seconds", "180"),
            "media_access": normalize_host_media_access(row.get("media_access", DEFAULT_HOST_MEDIA_ACCESS)),
            "priority": row.get("priority", "0"),
            "max_parallel_encodes": row.get("max_parallel_encodes", str(DEFAULT_HOST_MAX_PARALLEL_ENCODES)),
            "schedule_profile": canonical_schedule_profile_key(
                row.get("schedule_profile", DEFAULT_HOST_SCHEDULE_PROFILE)
            ),
            "capabilities": normalize_host_capabilities(row.get("capabilities", list(DEFAULT_HOST_CAPABILITIES))),
            "allowed_libraries": normalize_allowed_libraries(row.get("allowed_libraries")),
            "source_roots_json": row.get("source_roots_json", ""),
            "staging_root": row.get("staging_root", ""),
        }
        for index, row in enumerate(rows)
    ]
    while len(indexed) < min_rows:
        indexed.append(
            {
                "index": str(len(indexed)),
                "label": "",
                "host": "",
                "repo_path": "",
                "wake_mac": "",
                "start_command": "",
                "stop_command": "",
                "start_timeout_seconds": "180",
                "media_access": DEFAULT_HOST_MEDIA_ACCESS,
                "priority": "0",
                "max_parallel_encodes": str(DEFAULT_HOST_MAX_PARALLEL_ENCODES),
                "schedule_profile": DEFAULT_HOST_SCHEDULE_PROFILE,
                "capabilities": list(DEFAULT_HOST_CAPABILITIES),
                "allowed_libraries": [],
                "source_roots_json": "",
                "staging_root": "",
            }
        )
    return indexed


def settings_schedule_profile_rows_for_config(config: MediaforceConfig, *, min_rows: int = 1) -> list[dict[str, Any]]:
    encode_queue = config.raw.get("encode_queue")
    raw_profiles = encode_queue.get("schedule_profiles") if isinstance(encode_queue, dict) else None
    rows: list[dict[str, Any]] = []
    if isinstance(raw_profiles, list):
        for profile in raw_profiles:
            if not isinstance(profile, dict):
                continue
            key = canonical_schedule_profile_key(str(profile.get("key") or profile.get("name") or ""))
            if not key or key in {ALWAYS_SCHEDULE_PROFILE, NEVER_SCHEDULE_PROFILE}:
                continue
            normalized = normalize_encode_queue_scheduler(profile)
            rows.append(
                {
                    "key": key,
                    "label": str(profile.get("label") or key.replace("_", " ").title()),
                    "days_of_week": list(normalized["days_of_week"]),
                    "all_day_days_of_week": list(normalized["all_day_days_of_week"]),
                    "start_hour": str(normalized["start_hour"]),
                    "end_hour": str(normalized["end_hour"]),
                }
            )
    return index_schedule_profile_rows(rows, min_rows=min_rows)


def index_schedule_profile_rows(rows: list[dict[str, Any]], *, min_rows: int = 1) -> list[dict[str, Any]]:
    indexed = [
        {
            "index": str(index),
            "key": row.get("key", ""),
            "label": row.get("label", ""),
            "days_of_week": normalize_schedule_days(row.get("days_of_week"), default_to_all=True),
            "all_day_days_of_week": normalize_schedule_days(
                row.get("all_day_days_of_week"), default_to_all=False
            ),
            "start_hour": row.get("start_hour", str(DEFAULT_SCHEDULER_POLICY["start_hour"])),
            "end_hour": row.get("end_hour", str(DEFAULT_SCHEDULER_POLICY["end_hour"])),
        }
        for index, row in enumerate(rows)
    ]
    while len(indexed) < min_rows:
        indexed.append(
            {
                "index": str(len(indexed)),
                "key": "",
                "label": "",
                "days_of_week": list(SCHEDULE_DAY_ORDER),
                "all_day_days_of_week": [],
                "start_hour": str(DEFAULT_SCHEDULER_POLICY["start_hour"]),
                "end_hour": str(DEFAULT_SCHEDULER_POLICY["end_hour"]),
            }
        )
    return indexed


def host_max_parallel_encodes(host: dict[str, Any]) -> int:
    try:
        return max(1, int(str(host.get("max_parallel_encodes") or DEFAULT_HOST_MAX_PARALLEL_ENCODES)))
    except (TypeError, ValueError):
        return DEFAULT_HOST_MAX_PARALLEL_ENCODES


def normalize_schedule_profile_key(value: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "_", value.strip().lower()).strip("_")


def canonical_schedule_profile_key(value: JSONValue) -> str:
    key = normalize_schedule_profile_key(str(value or ""))
    if key in {"", "always"}:
        return ALWAYS_SCHEDULE_PROFILE
    if key in {NEVER_SCHEDULE_PROFILE, "disabled"}:
        return NEVER_SCHEDULE_PROFILE
    if key in {LEGACY_DEFAULT_SCHEDULE_PROFILE, "queue_default", LEGACY_QUEUE_WINDOW_SCHEDULE_PROFILE}:
        return ALWAYS_SCHEDULE_PROFILE
    return key


def host_schedule_profile_key(host: dict[str, Any]) -> str:
    return canonical_schedule_profile_key(host.get("schedule_profile") or DEFAULT_HOST_SCHEDULE_PROFILE)


def schedule_profile_options(*, schedule_profiles: list[dict[str, Any]]) -> list[dict[str, str]]:
    options = [
        {
            "key": ALWAYS_SCHEDULE_PROFILE,
            "label": "Always",
            "summary": "Runs anytime.",
        },
        {
            "key": NEVER_SCHEDULE_PROFILE,
            "label": "Never",
            "summary": "Never accepts queued encodes.",
        },
    ]
    for row in schedule_profiles:
        key = canonical_schedule_profile_key(row.get("key"))
        if key in {ALWAYS_SCHEDULE_PROFILE, NEVER_SCHEDULE_PROFILE}:
            continue
        policy = normalize_encode_queue_scheduler(
            {
                "mode": "night",
                "timezone": "host_local",
                "days_of_week": row.get("days_of_week"),
                "all_day_days_of_week": row.get("all_day_days_of_week"),
                "start_hour": row.get("start_hour"),
                "end_hour": row.get("end_hour"),
            }
        )
        options.append(
            {
                "key": key,
                "label": str(row.get("label") or key.replace("_", " ").title()),
                "summary": str(policy["summary"]),
            }
        )
    return options


def settings_transcode_root_value(config: MediaforceConfig) -> str:
    media = config.raw.get("media")
    return stringify_pathlike(media.get("staging_root") if isinstance(media, dict) else None)


def settings_video_defaults_for_config(config: MediaforceConfig) -> dict[str, str]:
    video = config.raw.get("video")
    if not isinstance(video, dict):
        return dict(DEFAULT_VIDEO_DEFAULTS)
    return {
        "quality_metric": str(video.get("quality_metric") or DEFAULT_VIDEO_DEFAULTS["quality_metric"]).strip().lower(),
        "target_vmaf": _settings_number_text(video.get("target_vmaf"), DEFAULT_VIDEO_DEFAULTS["target_vmaf"]),
        "min_target_vmaf": _settings_number_text(video.get("min_target_vmaf"), DEFAULT_VIDEO_DEFAULTS["min_target_vmaf"]),
        "target_xpsnr": _settings_number_text(video.get("target_xpsnr"), DEFAULT_VIDEO_DEFAULTS["target_xpsnr"]),
        "min_target_xpsnr": _settings_number_text(
            video.get("min_target_xpsnr"), DEFAULT_VIDEO_DEFAULTS["min_target_xpsnr"]
        ),
        "target_size_mb": _settings_number_text(video.get("target_size_mb"), DEFAULT_VIDEO_DEFAULTS["target_size_mb"]),
        "target_runtime_minutes": _settings_number_text(
            video.get("target_runtime_minutes"), DEFAULT_VIDEO_DEFAULTS["target_runtime_minutes"]
        ),
        "size_goal_mode": "normalized",
        "sample_projection_tolerance_percent": _settings_number_text(
            video.get("sample_projection_tolerance_percent"),
            DEFAULT_VIDEO_DEFAULTS["sample_projection_tolerance_percent"],
        ),
        "final_output_tolerance_percent": _settings_number_text(
            video.get("final_output_tolerance_percent"),
            DEFAULT_VIDEO_DEFAULTS["final_output_tolerance_percent"],
        ),
        "decision_model": _settings_choice_text(
            video.get("decision_model"),
            default=DEFAULT_VIDEO_DEFAULTS["decision_model"],
            supported=SUPPORTED_DECISION_MODELS,
        ),
        "quality_engine": _settings_choice_text(
            video.get("quality_engine"),
            default=DEFAULT_VIDEO_DEFAULTS["quality_engine"],
            supported=SUPPORTED_QUALITY_ENGINES,
        ),
        "max_height": _settings_number_text(video.get("max_height"), DEFAULT_VIDEO_DEFAULTS["max_height"]),
        "default_grain": _settings_number_text(video.get("default_grain"), DEFAULT_VIDEO_DEFAULTS["default_grain"]),
        "max_encoded_percent": _settings_number_text(
            video.get("max_encoded_percent"), DEFAULT_VIDEO_DEFAULTS["max_encoded_percent"]
        ),
    }


def settings_archive_root(transcode_root: str) -> str:
    cleaned_root = transcode_root.strip()
    if not cleaned_root:
        return ""
    return str(Path(cleaned_root).expanduser() / "_replaced")


def stringify_pathlike(value: JSONValue | Path) -> str:
    if isinstance(value, Path):
        return str(value.expanduser())
    if isinstance(value, str):
        return str(Path(value).expanduser()) if value.strip() else ""
    return ""


def _settings_number_text(value: JSONValue, default: str) -> str:
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return default
    if parsed.is_integer():
        return str(int(parsed))
    return f"{parsed:g}"


def _settings_choice_text(value: JSONValue, *, default: str, supported: set[str]) -> str:
    text = str(value or "").strip().lower()
    return text if text in supported else default


def settings_form_indexes(form_data: dict[str, str], prefix: str) -> list[int]:
    indexes: set[int] = set()
    for key in form_data:
        if not key.startswith(prefix):
            continue
        suffix = key[len(prefix):]
        if suffix.isdigit():
            indexes.add(int(suffix))
    return sorted(indexes)


def clamp_hour(value: JSONValue, default: int) -> int:
    try:
        return max(0, min(23, int(str(value))))
    except (TypeError, ValueError):
        return default


def normalize_schedule_days(value: JSONValue, *, default_to_all: bool) -> list[str]:
    if value is None:
        return list(SCHEDULE_DAY_ORDER) if default_to_all else []
    if isinstance(value, str):
        raw_days = [part.strip().lower() for part in value.split(",")]
    elif isinstance(value, list):
        raw_days = [str(item or "").strip().lower() for item in value]
    else:
        raw_days = []
    normalized: list[str] = []
    seen: set[str] = set()
    for day in SCHEDULE_DAY_ORDER:
        if day not in raw_days or day in seen:
            continue
        seen.add(day)
        normalized.append(day)
    if normalized:
        return normalized
    return list(SCHEDULE_DAY_ORDER) if default_to_all else []


def schedule_days_summary(days_of_week: list[str]) -> str:
    if days_of_week == list(SCHEDULE_DAY_ORDER):
        return "every day"
    if days_of_week == list(SCHEDULE_DAY_ORDER[:5]):
        return "weekdays"
    if days_of_week == list(SCHEDULE_DAY_ORDER[5:]):
        return "weekends"
    return ", ".join(SCHEDULE_DAY_LABELS.get(day, day.title()) for day in days_of_week)


def schedule_day_rule_summary(*, window_days: list[str], all_day_days: list[str], start_hour: int, end_hour: int) -> str:
    segments: list[str] = []
    if all_day_days:
        all_day_summary = schedule_days_summary(all_day_days)
        segments.append("all day" if all_day_summary == "every day" else f"{all_day_summary} all day")
    if window_days:
        window_summary = schedule_days_summary(window_days)
        if start_hour == end_hour:
            segments.append("all day" if window_summary == "every day" else f"{window_summary} all day")
        else:
            window_phrase = f"between {start_hour:02d}:00 and {end_hour:02d}:00"
            segments.append(window_phrase if window_summary == "every day" else f"{window_summary} {window_phrase}")
    return " and ".join(segment for segment in segments if segment)


def normalize_encode_queue_scheduler(raw: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(DEFAULT_SCHEDULER_POLICY)
    if isinstance(raw, dict):
        payload.update(raw)
    mode = str(payload.get("mode") or DEFAULT_SCHEDULER_POLICY["mode"]).strip().lower()
    timezone = str(payload.get("timezone") or DEFAULT_SCHEDULER_POLICY["timezone"]).strip().lower()
    if mode not in {"anytime", "night", "never"}:
        mode = str(DEFAULT_SCHEDULER_POLICY["mode"])
    if timezone not in {"host_local", "controller_local", "utc"}:
        timezone = str(DEFAULT_SCHEDULER_POLICY["timezone"])
    start_hour_value = payload.get("start_hour")
    if not isinstance(start_hour_value, (str, int, float, bool)):
        start_hour_value = None
    end_hour_value = payload.get("end_hour")
    if not isinstance(end_hour_value, (str, int, float, bool)):
        end_hour_value = None
    start_hour = clamp_hour(start_hour_value, int(str(DEFAULT_SCHEDULER_POLICY["start_hour"])))
    end_hour = clamp_hour(end_hour_value, int(str(DEFAULT_SCHEDULER_POLICY["end_hour"])))
    all_day_days_of_week = normalize_schedule_days(payload.get("all_day_days_of_week"), default_to_all=False)
    default_window_days_to_all = payload.get("days_of_week") is None
    days_of_week = [
        day
        for day in normalize_schedule_days(
            payload.get("days_of_week"),
            default_to_all=default_window_days_to_all,
        )
        if day not in all_day_days_of_week
    ]
    normalized = {
        "mode": mode,
        "timezone": timezone,
        "start_hour": start_hour,
        "end_hour": end_hour,
        "days_of_week": days_of_week,
        "all_day_days_of_week": all_day_days_of_week,
    }
    if mode == "anytime":
        normalized["summary"] = "runs anytime"
    elif mode == "never":
        normalized["summary"] = "never runs"
    else:
        rule_summary = schedule_day_rule_summary(
            window_days=days_of_week,
            all_day_days=all_day_days_of_week,
            start_hour=start_hour,
            end_hour=end_hour,
        )
        normalized["summary"] = (
            f"runs {rule_summary} ({timezone.replace('_', ' ')})" if rule_summary else "never runs"
        )
    return normalized


def normalize_submitted_library_policy(library_type: str, value: Any) -> dict[str, Any]:
    policy = normalize_library_policy(normalize_library_type(library_type), value)
    if library_type == "tv":
        lifecycle_mode = str(policy.get("series_lifecycle_mode") or "auto").strip().lower()
        if lifecycle_mode not in {"auto", "on", "off"}:
            raise SettingsValidationError("TV current-season protection must be Automatic, Always, or Off.")
        return {
            "series_lifecycle_mode": lifecycle_mode,
            "current_season_inactive_days": _library_policy_integer(
                policy.get("current_season_inactive_days"), label="Current-season inactivity", minimum=1, maximum=3650
            ),
            "season_acquisition_hold_days": _library_policy_integer(
                policy.get("season_acquisition_hold_days"), label="New-season hold", minimum=0, maximum=365
            ),
            "series_metadata_stale_days": _library_policy_integer(
                policy.get("series_metadata_stale_days"), label="Series metadata freshness", minimum=1, maximum=365
            ),
        }
    if library_type == "movie":
        grouping = str(policy.get("grouping") or "title")
        editions = str(policy.get("editions") or "separate")
        extras = str(policy.get("extras") or "exclude")
        ranking = str(policy.get("ranking") or "oldest_added_first")
        if grouping != "title" or editions != "separate" or extras not in {"exclude", "include"}:
            raise SettingsValidationError("Choose supported movie grouping, edition, and extras policies.")
        if ranking not in {"oldest_added_first", "largest_first"}:
            raise SettingsValidationError("Choose a supported movie ranking policy.")
        return {"grouping": grouping, "editions": editions, "extras": extras, "ranking": ranking}
    if library_type == "spatial":
        stereo_layout = str(policy.get("stereo_layout") or "unknown")
        projection = str(policy.get("projection") or "unknown")
        geometry_policy = str(policy.get("geometry_policy") or "preserve")
        container_profile = str(policy.get("container_profile") or "unqualified")
        if stereo_layout not in {"unknown", "side_by_side", "top_bottom", "frame_sequential"}:
            raise SettingsValidationError("Choose a supported 3D stereo layout.")
        if projection not in {"unknown", "flat", "equirectangular_180", "equirectangular_360"}:
            raise SettingsValidationError("Choose a supported 3D / VR projection.")
        if geometry_policy != "preserve" or container_profile != "unqualified":
            raise SettingsValidationError(
                "3D / VR geometry and container handling must remain source-preserving and unqualified."
            )
        return {
            "playback_target": str(policy.get("playback_target") or "").strip(),
            "stereo_layout": stereo_layout,
            "projection": projection,
            "geometry_policy": geometry_policy,
            "container_profile": container_profile,
        }
    grouping = str(policy.get("grouping") or "folder")
    if grouping not in {"folder", "file"}:
        raise SettingsValidationError("Other libraries must group work by folder or file.")
    return {"grouping": grouping}


def _library_policy_integer(value: Any, *, label: str, minimum: int, maximum: int) -> int:
    try:
        normalized = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise SettingsValidationError(f"{label} must be a whole number.") from exc
    if normalized < minimum or normalized > maximum:
        raise SettingsValidationError(f"{label} must be between {minimum} and {maximum} days.")
    return normalized


def build_runtime_settings_payload(
        *,
        libraries: list[dict[str, Any]],
        remote_hosts: list[dict[str, Any]],
        transcode_root: str,
        video_defaults: dict[str, Any] | None = None,
        encode_queue_scheduler: dict[str, Any],
        schedule_profiles: list[dict[str, Any]],
        metadata: dict[str, Any] | None = None,
        existing_library_types: dict[str, str] | None = None,
        existing_libraries: dict[str, dict[str, Any]] | None = None,
        existing_library_paths: dict[str, str] | None = None,
) -> dict[str, Any]:
    def _text(value: JSONValue, default: str = "") -> str:
        if value is None:
            return default
        return str(value).strip()

    source_roots: dict[str, str] = {}
    library_colors: dict[str, str] = {}
    library_definitions: list[dict[str, Any]] = []
    plex_library_roots: dict[str, str] = {}
    seen_paths: set[str] = set()
    seen_plex_paths: set[str] = set()
    current_types = existing_library_types or {}
    current_libraries = existing_libraries or {}
    current_paths = existing_library_paths or {}
    _reject_submitted_secrets(metadata)
    staging_root = Path(transcode_root).expanduser()
    if not staging_root.is_absolute():
        raise SettingsValidationError("The working folder must be an absolute path.")
    for row in libraries:
        key_text = _text(row.get("key", ""))
        label_text = _text(row.get("label", ""))
        path_text = _text(row.get("path", ""))
        if not key_text and not label_text and not path_text:
            continue
        normalized_key = normalize_library_key(key_text)
        if not normalized_key or not path_text:
            raise SettingsValidationError("Each library needs a root ID and local path.")
        if normalized_key != key_text or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", key_text):
            raise SettingsValidationError("Library root IDs use lowercase letters, numbers, underscores, and hyphens.")
        if not label_text:
            label_text = normalized_key.replace("_", " ").replace("-", " ").title()
        if len(label_text) > 80:
            raise SettingsValidationError("Library labels must be 80 characters or fewer.")
        if normalized_key in source_roots:
            raise SettingsValidationError(f"Duplicate library root ID: {normalized_key}")
        path = Path(path_text).expanduser()
        if not path.is_absolute() and current_paths.get(normalized_key) != path_text:
            raise SettingsValidationError("Library local paths must be absolute.")
        normalized_path = str(path)
        if normalized_path in seen_paths:
            raise SettingsValidationError("Each library needs a distinct local path.")
        if any(_paths_overlap(path, Path(existing)) for existing in seen_paths):
            raise SettingsValidationError("Library local paths cannot contain one another.")
        if _paths_overlap(path, staging_root):
            raise SettingsValidationError("Library paths cannot contain the working folder or be inside it.")
        explicit_type = _text(row.get("library_type", ""))
        if explicit_type and explicit_type not in {option["key"] for option in LIBRARY_TYPE_OPTIONS}:
            raise SettingsValidationError("Choose a supported library type.")
        library_type = normalize_library_type(explicit_type or None, key=normalized_key)
        availability_text = _text(row.get("availability", ""))
        if availability_text and availability_text not in {"production", "browse_only", "disabled"}:
            raise SettingsValidationError("Choose Production, Browse only, or Disabled for each library.")
        availability = normalize_library_availability(availability_text or None, library_type=library_type)
        previous_type = current_types.get(normalized_key)
        expected_confirmation = f"{normalized_key}:{previous_type}->{library_type}" if previous_type else ""
        if previous_type and previous_type != library_type and row.get("type_change_confirmation") != expected_confirmation:
            raise SettingsValidationError("Confirm the library type compatibility preview before saving.")
        if previous_type and previous_type != library_type:
            availability = "browse_only"
        if availability == "production" and not library_production_supported(library_type):
            raise SettingsValidationError(
                f"{library_type_label(library_type)} production is not available yet; use Browse only."
            )
        profile = _text(row.get("default_profile", DEFAULT_LIBRARY_PROFILES[library_type]))
        valid_profiles = {option["key"] for option in LIBRARY_PROFILE_OPTIONS[library_type]}
        if profile not in valid_profiles:
            raise SettingsValidationError(f"Choose a valid default profile for {library_type_label(library_type)}.")
        policy = normalize_submitted_library_policy(library_type, row.get("policy"))
        source_roots[normalized_key] = normalized_path
        seen_paths.add(normalized_path)
        color_text = normalize_library_color(row.get("color"))
        if color_text is not None:
            library_colors[normalized_key] = color_text
        plex_path = _text(row.get("plex_path", ""))
        if plex_path:
            normalized_plex_path = Path(plex_path).expanduser()
            if not normalized_plex_path.is_absolute():
                raise SettingsValidationError("Custom Plex paths must be absolute.")
            if str(normalized_plex_path) in seen_plex_paths:
                raise SettingsValidationError("Each custom Plex path must be unique.")
            plex_library_roots[normalized_key] = str(normalized_plex_path)
            seen_plex_paths.add(str(normalized_plex_path))
        library_definitions.append(
            {
                **current_libraries.get(normalized_key, {}),
                "key": normalized_key,
                "label": label_text,
                "path": normalized_path,
                "color": color_text or "",
                "plex_path": plex_library_roots.get(normalized_key, ""),
                "type": library_type,
                "availability": availability,
                "default_profile": profile,
                "policy": policy,
            }
        )
    if not source_roots:
        raise ValueError("Add at least one library before saving settings.")
    library_colors = library_color_map_from_source_roots(source_roots, library_colors)
    known_library_keys = set(source_roots)

    normalized_remotes: list[dict[str, Any]] = []
    for row in remote_hosts:
        label = _text(row.get("label", ""))
        host = _text(row.get("host", ""))
        repo_path = _text(row.get("repo_path", ""))
        wake_mac = _text(row.get("wake_mac", ""))
        start_command = _text(row.get("start_command", ""))
        stop_command = _text(row.get("stop_command", ""))
        start_timeout_text = _text(row.get("start_timeout_seconds", "180"), "180") or "180"
        media_access = normalize_host_media_access(row.get("media_access", DEFAULT_HOST_MEDIA_ACCESS))
        if not label and not host and not repo_path and not wake_mac and not start_command and not stop_command:
            continue
        if not host:
            raise ValueError("Each remote host row needs an SSH host value.")
        priority_text = _text(row.get("priority", "0"), "0") or "0"
        try:
            priority = int(priority_text)
        except ValueError as exc:
            raise ValueError(f"Host priority must be a whole number for {label or host}.") from exc
        max_parallel_text = _text(
            row.get("max_parallel_encodes", str(DEFAULT_HOST_MAX_PARALLEL_ENCODES)),
            str(DEFAULT_HOST_MAX_PARALLEL_ENCODES),
        )
        try:
            max_parallel_encodes = max(1, int(max_parallel_text or str(DEFAULT_HOST_MAX_PARALLEL_ENCODES)))
        except ValueError as exc:
            raise ValueError(f"Parallel encodes must be a whole number for {label or host}.") from exc
        try:
            start_timeout_seconds = max(1, int(start_timeout_text))
        except ValueError as exc:
            raise ValueError(f"Start timeout must be a whole number for {label or host}.") from exc
        schedule_profile = canonical_schedule_profile_key(row.get("schedule_profile", DEFAULT_HOST_SCHEDULE_PROFILE))
        payload: dict[str, Any] = {"host": host}
        if label:
            payload["label"] = label
        if repo_path:
            payload["repo_path"] = repo_path
        if wake_mac:
            payload["wake_mac"] = wake_mac
        if start_command:
            payload["start_command"] = start_command
        if stop_command:
            payload["stop_command"] = stop_command
        payload["start_timeout_seconds"] = start_timeout_seconds
        payload["media_access"] = media_access
        payload["priority"] = str(priority)
        payload["max_parallel_encodes"] = max_parallel_encodes
        payload["schedule_profile"] = schedule_profile
        payload["capabilities"] = normalize_host_capabilities(row.get("capabilities"))
        allowed_libraries = normalize_allowed_libraries(row.get("allowed_libraries"))
        if allowed_libraries:
            payload["allowed_libraries"] = allowed_libraries
        source_root_overrides = normalize_host_source_root_overrides(
            row.get("source_roots") if "source_roots" in row else row.get("source_roots_json"),
            known_library_keys=known_library_keys,
            host_label=label or host,
        )
        if source_root_overrides:
            payload["source_roots"] = source_root_overrides
        staging_root_override = _text(row.get("staging_root", ""))
        if staging_root_override:
            payload["staging_root"] = str(Path(staging_root_override).expanduser())
        normalized_remotes.append(payload)

    normalized_profiles: list[dict[str, Any]] = []
    seen_profile_keys: set[str] = {ALWAYS_SCHEDULE_PROFILE, NEVER_SCHEDULE_PROFILE}
    for row in schedule_profiles:
        key_text = canonical_schedule_profile_key(row.get("key", ""))
        label_text = _text(row.get("label", ""))
        start_hour_text = _text(
            row.get("start_hour", str(DEFAULT_SCHEDULER_POLICY["start_hour"])),
            str(DEFAULT_SCHEDULER_POLICY["start_hour"]),
        )
        end_hour_text = _text(
            row.get("end_hour", str(DEFAULT_SCHEDULER_POLICY["end_hour"])),
            str(DEFAULT_SCHEDULER_POLICY["end_hour"]),
        )
        all_day_days_of_week = normalize_schedule_days(
            row.get("all_day_days_of_week"), default_to_all=False
        )
        default_window_days_to_all = row.get("days_of_week") is None
        days_of_week = [
            day
            for day in normalize_schedule_days(
                row.get("days_of_week"), default_to_all=default_window_days_to_all
            )
            if day not in all_day_days_of_week
        ]
        if not any((key_text, label_text, start_hour_text, end_hour_text)):
            continue
        if not key_text:
            raise ValueError("Each schedule profile needs a key.")
        if key_text in {ALWAYS_SCHEDULE_PROFILE, NEVER_SCHEDULE_PROFILE}:
            raise ValueError(f"Schedule profile key '{key_text}' is reserved.")
        if key_text in seen_profile_keys:
            raise ValueError(f"Duplicate schedule profile key: {key_text}")
        if not days_of_week and not all_day_days_of_week:
            raise ValueError(f"Select at least one day for schedule profile '{key_text}'.")
        normalized = normalize_encode_queue_scheduler(
            {
                "mode": "night",
                "timezone": "host_local",
                "days_of_week": days_of_week,
                "all_day_days_of_week": all_day_days_of_week,
                "start_hour": start_hour_text,
                "end_hour": end_hour_text,
            }
        )
        normalized_profiles.append(
            {
                "key": key_text,
                "label": label_text or key_text.replace("_", " ").title(),
                "mode": normalized["mode"],
                "timezone": normalized["timezone"],
                "days_of_week": list(normalized["days_of_week"]),
                "all_day_days_of_week": list(normalized["all_day_days_of_week"]),
                "start_hour": normalized["start_hour"],
                "end_hour": normalized["end_hour"],
            }
        )
        seen_profile_keys.add(key_text)

    invalid_host_profiles = sorted(
        {
            str(host.get("schedule_profile") or DEFAULT_HOST_SCHEDULE_PROFILE)
            for host in normalized_remotes
            if str(host.get("schedule_profile") or DEFAULT_HOST_SCHEDULE_PROFILE) not in seen_profile_keys
        }
    )
    if invalid_host_profiles:
        raise ValueError("Unknown schedule profile for host assignment: " + ", ".join(invalid_host_profiles))

    normalized_video_defaults = normalize_video_defaults(video_defaults)
    metadata_payload = dict(metadata) if isinstance(metadata, dict) else {}
    plex_payload = dict(metadata_payload.get("plex")) if isinstance(metadata_payload.get("plex"), dict) else {}
    plex_payload["library_roots"] = plex_library_roots
    metadata_payload["plex"] = plex_payload
    normalized_metadata = normalize_metadata_settings(metadata_payload, known_library_keys=known_library_keys)
    return {
        "media": {
            "source_roots": source_roots,
            "library_colors": library_colors,
            "libraries": library_definitions,
            "staging_root": str(staging_root),
            "archive_root": str(staging_root / "_replaced"),
        },
        "video": normalized_video_defaults,
        "remote_hosts": normalized_remotes,
        "encode_queue": {
            "scheduler": normalize_encode_queue_scheduler(encode_queue_scheduler),
            "schedule_profiles": normalized_profiles,
        },
        "metadata": normalized_metadata,
    }


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _reject_submitted_secrets(value: Any, *, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            key_text = str(key).strip().lower()
            next_path = (*path, key_text)
            secret_key = key_text in {"token", "password", "secret", "api_key", "apikey"}
            if secret_key and nested is not None and nested != "" and nested is not False:
                raise SettingsValidationError("Credential values are not accepted in Settings.")
            _reject_submitted_secrets(nested, path=next_path)
        return
    if isinstance(value, list):
        for nested in value:
            _reject_submitted_secrets(nested, path=path)


def normalize_metadata_settings(
        raw: dict[str, Any] | None,
        *,
        known_library_keys: set[str],
) -> dict[str, Any]:
    payload = dict(raw) if isinstance(raw, dict) else {}
    plex = dict(payload.get("plex")) if isinstance(payload.get("plex"), dict) else {}
    tmdb = dict(payload.get("tmdb")) if isinstance(payload.get("tmdb"), dict) else {}
    plex_base_url = _normalized_provider_url(plex.get("base_url"), allow_http=True)
    tmdb_base_url = _normalized_provider_url(
        tmdb.get("base_url") or "https://api.themoviedb.org/3",
        allow_http=False,
    )
    library_roots = {
        str(key): str(Path(str(value)).expanduser())
        for key, value in (plex.get("library_roots") or {}).items()
        if str(key) in known_library_keys and str(value or "").strip()
    } if isinstance(plex.get("library_roots"), dict) else {}
    return {
        "plex": {
            "enabled": bool(plex.get("enabled", True)),
            "base_url": plex_base_url,
            "token_env": _metadata_token_env(plex.get("token_env"), "MEDIAFORCE_PLEX_TOKEN"),
            "refresh_interval_hours": _metadata_refresh_hours(plex.get("refresh_interval_hours"), 1.0),
            "library_roots": library_roots,
        },
        "tmdb": {
            "enabled": bool(tmdb.get("enabled", True)),
            "base_url": tmdb_base_url,
            "token_env": _metadata_token_env(tmdb.get("token_env"), "MEDIAFORCE_TMDB_TOKEN"),
            "refresh_interval_hours": _metadata_refresh_hours(tmdb.get("refresh_interval_hours"), 24.0),
        },
    }


def _normalized_provider_url(value: object, *, allow_http: bool) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    parsed = urlsplit(text)
    allowed_schemes = {"https"} | ({"http"} if allow_http else set())
    if parsed.scheme not in allowed_schemes or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("Provider URLs must use an allowed HTTP scheme and cannot contain credentials.")
    if parsed.query or parsed.fragment:
        raise ValueError("Provider URLs cannot contain query parameters or fragments.")
    return text


def _metadata_token_env(value: object, default: str) -> str:
    name = str(value or default).strip()
    return name if re.fullmatch(r"MEDIAFORCE_[A-Z0-9_]+", name) else default


def _metadata_refresh_hours(value: object, default: float) -> float:
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def normalize_video_defaults(raw: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(DEFAULT_VIDEO_DEFAULTS)
    if isinstance(raw, dict):
        payload.update(raw)
    metric = str(payload.get("quality_metric") or DEFAULT_VIDEO_DEFAULTS["quality_metric"]).strip().lower()
    if metric not in {"auto", "vmaf", "xpsnr"}:
        metric = DEFAULT_VIDEO_DEFAULTS["quality_metric"]
    decision_model = str(payload.get("decision_model") or DEFAULT_VIDEO_DEFAULTS["decision_model"]).strip().lower()
    if decision_model not in SUPPORTED_DECISION_MODELS:
        decision_model = DEFAULT_VIDEO_DEFAULTS["decision_model"]
    quality_engine = str(payload.get("quality_engine") or DEFAULT_VIDEO_DEFAULTS["quality_engine"]).strip().lower()
    if quality_engine not in SUPPORTED_QUALITY_ENGINES:
        quality_engine = DEFAULT_VIDEO_DEFAULTS["quality_engine"]

    def _float_field(key: str, *, minimum: float, maximum: float) -> float:
        try:
            value = float(str(payload.get(key, DEFAULT_VIDEO_DEFAULTS[key])))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Video default {key} must be a number.") from exc
        if value < minimum or value > maximum:
            raise ValueError(f"Video default {key} must be between {minimum:g} and {maximum:g}.")
        return value

    def _int_field(key: str, *, minimum: int, maximum: int) -> int:
        value = _float_field(key, minimum=minimum, maximum=maximum)
        if not value.is_integer():
            raise ValueError(f"Video default {key} must be a whole number.")
        return int(value)

    target_vmaf = _float_field("target_vmaf", minimum=1, maximum=100)
    min_target_vmaf = _float_field("min_target_vmaf", minimum=1, maximum=100)
    if min_target_vmaf > target_vmaf:
        raise ValueError("Video default min_target_vmaf must be less than or equal to target_vmaf.")
    target_xpsnr = _float_field("target_xpsnr", minimum=1, maximum=100)
    min_target_xpsnr = _float_field("min_target_xpsnr", minimum=1, maximum=100)
    if min_target_xpsnr > target_xpsnr:
        raise ValueError("Video default min_target_xpsnr must be less than or equal to target_xpsnr.")
    target_size_mb = _float_field("target_size_mb", minimum=1, maximum=100_000)
    max_height = _int_field("max_height", minimum=0, maximum=4320)

    return {
        "quality_metric": metric,
        "target_vmaf": target_vmaf,
        "min_target_vmaf": min_target_vmaf,
        "target_xpsnr": target_xpsnr,
        "min_target_xpsnr": min_target_xpsnr,
        "target_size_mb": target_size_mb,
        "target_size_bytes": megabytes_to_bytes(target_size_mb),
        "target_runtime_minutes": _float_field("target_runtime_minutes", minimum=1, maximum=1440),
        "size_goal_schema_version": 1,
        "size_goal_mode": "normalized",
        "size_goal_source": "config_default",
        "sample_projection_tolerance_percent": _float_field(
            "sample_projection_tolerance_percent", minimum=0.1, maximum=100
        ),
        "final_output_tolerance_percent": _float_field(
            "final_output_tolerance_percent", minimum=0.1, maximum=100
        ),
        "decision_model": decision_model,
        "quality_engine": quality_engine,
        "max_height": max_height,
        "resolution_intent_mode": "max_height" if max_height > 0 else "source",
        "resolution_intent_source": "config_default",
        "default_grain": _int_field("default_grain", minimum=0, maximum=50),
        "max_encoded_percent": _float_field("max_encoded_percent", minimum=1, maximum=100),
    }


def merge_runtime_settings_payload(existing: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    if "encode_queue" in updates:
        merged.pop("heavy_queue", None)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            nested = dict(merged[key])
            nested.update(value)
            merged[key] = nested
            continue
        merged[key] = value
    return merged


def runtime_library_signature(payload: dict[str, Any]) -> list[dict[str, Any]]:
    media = payload.get("media")
    if not isinstance(media, dict):
        return []
    libraries = media.get("libraries")
    if not isinstance(libraries, list):
        return []
    signature: list[dict[str, Any]] = []
    for item in libraries:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        if not key:
            continue
        definition = normalize_library_definition(item, key=key)
        signature.append(
            {
                "key": key,
                "type": definition["type"],
                "scannable": definition["availability"] != "disabled",
            }
        )
    return sorted(signature, key=lambda item: str(item["key"]))


def bind_runtime_library_overrides(payload: dict[str, Any], previous_types: dict[str, str]) -> dict[str, Any]:
    if not previous_types:
        return payload
    overrides = payload.get(FOLDER_POLICY_OVERRIDES_KEY)
    if not isinstance(overrides, list):
        return payload
    merged = dict(payload)
    rebound: list[Any] = []
    for override in overrides:
        if not isinstance(override, dict):
            rebound.append(override)
            continue
        root = str(override.get("path_prefix") or "").strip("/").split("/", 1)[0]
        previous_type = previous_types.get(root)
        if previous_type and not str(override.get("library_type") or "").strip():
            rebound.append({**override, "library_type": previous_type})
            continue
        rebound.append(override)
    merged[FOLDER_POLICY_OVERRIDES_KEY] = rebound
    return merged


def runtime_source_roots(payload: dict[str, Any]) -> dict[str, str]:
    media = payload.get("media")
    if not isinstance(media, dict):
        return {}
    source_roots = media.get("source_roots")
    if not isinstance(source_roots, dict):
        return {}
    normalized: dict[str, str] = {}
    for key, value in source_roots.items():
        key_text = str(key).strip()
        value_text = str(value).strip()
        if key_text and value_text:
            normalized[key_text] = str(Path(value_text).expanduser())
    return normalized


def settings_host_source_roots_json(value: JSONValue) -> str:
    if not isinstance(value, dict):
        return ""
    normalized: dict[str, str] = {}
    for key, path in value.items():
        key_text = normalize_library_key(str(key))
        path_text = str(path or "").strip()
        if key_text and path_text:
            normalized[key_text] = str(Path(path_text).expanduser())
    if not normalized:
        return ""
    return json.dumps(normalized, indent=2, sort_keys=True)


def normalize_allowed_libraries(value: JSONValue) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        key_text = normalize_library_key(str(item or ""))
        if not key_text or key_text in seen:
            continue
        seen.add(key_text)
        normalized.append(key_text)
    return normalized


def normalize_host_source_root_overrides(
        value: JSONValue,
        *,
        known_library_keys: set[str],
        host_label: str,
) -> dict[str, str]:
    if value is None:
        return {}
    raw_value = value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            raw_value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Library path overrides must be valid JSON for {host_label}.") from exc
    if not isinstance(raw_value, dict):
        raise ValueError(f"Library path overrides must be a JSON object for {host_label}.")

    normalized: dict[str, str] = {}
    for key, path in raw_value.items():
        key_text = normalize_library_key(str(key))
        if not key_text:
            continue
        if key_text not in known_library_keys:
            raise ValueError(f"Unknown library override '{key_text}' for {host_label}.")
        path_text = str(path or "").strip()
        if not path_text:
            continue
        normalized[key_text] = str(Path(path_text).expanduser())
    return normalized


def normalize_library_color(value: JSONValue) -> str | None:
    text = str(value or "").strip()
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", text):
        return None
    return text.lower()


def runtime_library_colors(payload: dict[str, Any]) -> dict[str, str]:
    media = payload.get("media")
    if not isinstance(media, dict):
        return {}
    library_colors = media.get("library_colors")
    if not isinstance(library_colors, dict):
        return {}
    normalized: dict[str, str] = {}
    for key, value in library_colors.items():
        key_text = str(key).strip()
        color_text = normalize_library_color(value)
        if key_text and color_text:
            normalized[key_text] = color_text
    return normalized


def library_color_map_from_source_roots(
        source_roots: dict[str, str], configured_colors: dict[str, str] | None = None
) -> dict[str, str]:
    resolved: dict[str, str] = {}
    colors = configured_colors if configured_colors is not None else {}
    for index, key in enumerate(source_roots):
        configured_color = colors.get(key)
        resolved[key] = configured_color if configured_color is not None else DEFAULT_LIBRARY_COLOR_PALETTE[
            index % len(DEFAULT_LIBRARY_COLOR_PALETTE)
        ]
    return resolved


def library_color_map_for_config(config: MediaforceConfig) -> dict[str, str]:
    return library_color_map_from_source_roots(runtime_source_roots(config.raw), runtime_library_colors(config.raw))


def normalize_library_key(value: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "_", value.strip().lower()).strip("_")
