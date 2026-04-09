import json
import re
from pathlib import Path
from typing import Any

from mediaforce.core.config import MediaforceConfig
from mediaforce.encoding.encode_queue import DEFAULT_SCHEDULER_POLICY
from mediaforce.hosts.config import normalize_host_capabilities
from mediaforce.remote import DEFAULT_HOST_CAPABILITIES, DEFAULT_HOST_MEDIA_ACCESS, normalize_host_media_access
from mediaforce.core.type_defs import JSONValue

DEFAULT_LIBRARY_COLOR_PALETTE = (
    "#a16207",
    "#4e6fa6",
    "#c2410c",
    "#0f766e",
    "#7c6142",
    "#6b7280",
)
ALWAYS_SCHEDULE_PROFILE = "always"
NEVER_SCHEDULE_PROFILE = "never"
LEGACY_QUEUE_WINDOW_SCHEDULE_PROFILE = "queue_window"
LEGACY_DEFAULT_SCHEDULE_PROFILE = "default"
DEFAULT_HOST_SCHEDULE_PROFILE = ALWAYS_SCHEDULE_PROFILE
DEFAULT_HOST_MAX_PARALLEL_ENCODES = 1
HOST_CAPABILITY_OPTIONS = (
    {"key": "encode_queue", "label": "Queue encodes", "help": "Allow this host to run queued folder encodes."},
    {
        "key": "sample_calibration",
        "label": "Sample calibration",
        "help": "Allow this host to handle sampled calibration work and AI-guided sample retries.",
    },
)


def settings_library_rows(source_root_map: dict[str, Path], *, min_rows: int = 3) -> list[dict[str, str]]:
    rows = [
        {
            "key": key,
            "path": str(path),
            "color": DEFAULT_LIBRARY_COLOR_PALETTE[index % len(DEFAULT_LIBRARY_COLOR_PALETTE)],
        }
        for index, (key, path) in enumerate(source_root_map.items())
    ]
    return index_settings_library_rows(rows, min_rows=min_rows)


def settings_library_rows_for_config(config: MediaforceConfig, *, min_rows: int = 3) -> list[dict[str, str]]:
    media = config.raw.get("media")
    source_roots = media.get("source_roots") if isinstance(media, dict) else None
    library_colors = library_color_map_for_config(config)
    rows: list[dict[str, str]] = []
    if isinstance(source_roots, dict):
        for key, value in source_roots.items():
            key_text = str(key).strip()
            path_text = stringify_pathlike(value)
            if not key_text and not path_text:
                continue
            rows.append({"key": key_text, "path": path_text, "color": library_colors.get(key_text, "")})
    return index_settings_library_rows(rows, min_rows=min_rows)


def index_settings_library_rows(rows: list[dict[str, str]], *, min_rows: int = 1) -> list[dict[str, str]]:
    indexed = [
        {
            "index": str(index),
            "key": row.get("key", ""),
            "path": row.get("path", ""),
            "color": row.get("color", ""),
        }
        for index, row in enumerate(rows)
    ]
    while len(indexed) < min_rows:
        indexed.append({"index": str(len(indexed)), "key": "", "path": "", "color": "#0f766e"})
    return indexed


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


def settings_schedule_profile_rows_for_config(config: MediaforceConfig, *, min_rows: int = 1) -> list[dict[str, str]]:
    encode_queue = config.raw.get("encode_queue")
    raw_profiles = encode_queue.get("schedule_profiles") if isinstance(encode_queue, dict) else None
    rows: list[dict[str, str]] = []
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
                    "start_hour": str(normalized["start_hour"]),
                    "end_hour": str(normalized["end_hour"]),
                }
            )
    return index_schedule_profile_rows(rows, min_rows=min_rows)


def index_schedule_profile_rows(rows: list[dict[str, str]], *, min_rows: int = 1) -> list[dict[str, str]]:
    indexed = [
        {
            "index": str(index),
            "key": row.get("key", ""),
            "label": row.get("label", ""),
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
    normalized = {
        "mode": mode,
        "timezone": timezone,
        "start_hour": start_hour,
        "end_hour": end_hour,
    }
    if mode == "anytime":
        normalized["summary"] = "runs anytime"
    elif mode == "never":
        normalized["summary"] = "never runs"
    else:
        normalized["summary"] = f"runs between {start_hour:02d}:00 and {end_hour:02d}:00 ({timezone.replace('_', ' ')})"
    return normalized


def build_runtime_settings_payload(
        *,
        libraries: list[dict[str, Any]],
        remote_hosts: list[dict[str, Any]],
        transcode_root: str,
        encode_queue_scheduler: dict[str, Any],
        schedule_profiles: list[dict[str, str]],
) -> dict[str, Any]:
    def _text(value: JSONValue, default: str = "") -> str:
        if value is None:
            return default
        return str(value).strip()

    source_roots: dict[str, str] = {}
    library_colors: dict[str, str] = {}
    for row in libraries:
        key_text = _text(row.get("key", ""))
        path_text = _text(row.get("path", ""))
        if not key_text and not path_text:
            continue
        normalized_key = normalize_library_key(key_text)
        if not normalized_key or not path_text:
            raise ValueError("Each library row needs both a library name and a mounted path.")
        if normalized_key in source_roots:
            raise ValueError(f"Duplicate library name: {normalized_key}")
        source_roots[normalized_key] = str(Path(path_text).expanduser())
        color_text = normalize_library_color(row.get("color"))
        if color_text is not None:
            library_colors[normalized_key] = color_text
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
        if not any((key_text, label_text, start_hour_text, end_hour_text)):
            continue
        if not key_text:
            raise ValueError("Each schedule profile needs a key.")
        if key_text in {ALWAYS_SCHEDULE_PROFILE, NEVER_SCHEDULE_PROFILE}:
            raise ValueError(f"Schedule profile key '{key_text}' is reserved.")
        if key_text in seen_profile_keys:
            raise ValueError(f"Duplicate schedule profile key: {key_text}")
        normalized = normalize_encode_queue_scheduler(
            {
                "mode": "night",
                "timezone": "host_local",
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

    staging_root = Path(transcode_root).expanduser()
    return {
        "media": {
            "source_roots": source_roots,
            "library_colors": library_colors,
            "staging_root": str(staging_root),
            "archive_root": str(staging_root / "_replaced"),
        },
        "remote_hosts": normalized_remotes,
        "encode_queue": {
            "scheduler": normalize_encode_queue_scheduler(encode_queue_scheduler),
            "schedule_profiles": normalized_profiles,
        },
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
