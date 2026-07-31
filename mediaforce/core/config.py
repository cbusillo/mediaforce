import copy
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from importlib import resources
import json
import os
import shutil
import sqlite3
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from mediaforce.library.library_settings import configured_library_definitions, library_definition_map, \
    library_production_supported

_SOURCE_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_DEFAULT_CONFIG_PATH = _SOURCE_PROJECT_ROOT / "config" / "defaults.toml"


def _source_checkout_default_config_path(project_root: Path) -> Path | None:
    candidate = project_root / "config" / "defaults.toml"
    if (
            candidate.is_file()
            and (project_root / "pyproject.toml").is_file()
            and (project_root / "hatch_build.py").is_file()
    ):
        return candidate
    return None


def _default_config_path() -> Path:
    source_config = _source_checkout_default_config_path(_SOURCE_PROJECT_ROOT)
    if source_config is not None:
        return source_config
    try:
        packaged = resources.files("mediaforce.package_defaults").joinpath("defaults.toml")
    except ModuleNotFoundError:
        return _SOURCE_DEFAULT_CONFIG_PATH
    return Path(str(packaged))


DEFAULT_CONFIG_PATH = _default_config_path()
FOLDER_POLICY_OVERRIDES_KEY = "folder_policy_overrides"
BENCH_SAVED_OVERRIDE_NOTE = "Saved from the calibration bench."


class _LegacySQLiteMigrationSource(Protocol):
    path: Path

    def assert_stable(self) -> None: ...

    def discard_after_publish(self) -> None: ...


@dataclass(frozen=True)
class ConfigPaths:
    project_root: Path
    config_path: Path
    db_path: Path
    run_manifest_dir: Path
    web_state_dir: Path
    review_dir: Path
    runtime_settings_path: Path
    runtime_reservation_dir: Path | None = None


@dataclass(frozen=True)
class MediaforceConfig:
    raw: dict[str, Any]
    paths: ConfigPaths

    @property
    def media(self) -> dict[str, Any]:
        return self.raw["media"]

    @property
    def video(self) -> dict[str, Any]:
        return self.raw["video"]

    @property
    def audio(self) -> dict[str, Any]:
        return self.raw["audio"]

    @property
    def subtitle(self) -> dict[str, Any]:
        return self.raw["subtitle"]

    @property
    def planning(self) -> dict[str, Any]:
        return self.raw["planning"]

    @property
    def validation(self) -> dict[str, Any]:
        return self.raw["validation"]

    @property
    def metadata(self) -> dict[str, Any]:
        value = self.raw.get("metadata")
        return value if isinstance(value, dict) else {}

    @property
    def configured_source_root_map(self) -> dict[str, Path]:
        if isinstance(self.media.get("libraries"), list):
            return {
                str(definition["key"]): Path(str(definition["path"])).expanduser()
                for definition in self.library_definitions
            }
        return {
            key: Path(value).expanduser()
            for key, value in self.media["source_roots"].items()
        }

    @property
    def library_definitions(self) -> list[dict[str, Any]]:
        return configured_library_definitions(self.media)

    @property
    def library_definition_map(self) -> dict[str, dict[str, Any]]:
        return library_definition_map(self.media)

    @property
    def library_type_map(self) -> dict[str, str]:
        return {
            key: str(definition.get("type") or "other")
            for key, definition in self.library_definition_map.items()
        }

    @property
    def scan_source_root_map(self) -> dict[str, Path]:
        configured = self.configured_source_root_map
        if not isinstance(self.media.get("libraries"), list):
            return configured
        definitions = self.library_definition_map
        return {
            key: path
            for key, path in configured.items()
            if definitions.get(key, {}).get("availability") != "disabled"
        }

    @property
    def source_root_map(self) -> dict[str, Path]:
        configured = self.configured_source_root_map
        if not isinstance(self.media.get("libraries"), list):
            return configured
        definitions = self.library_definition_map
        return {
            key: path
            for key, path in configured.items()
            if definitions.get(key, {}).get("availability") == "production"
            and library_production_supported(str(definitions.get(key, {}).get("type") or ""))
        }

    def source_root_map_for_host(self, host: dict[str, Any] | None = None) -> dict[str, Path]:
        resolved = dict(self.source_root_map)
        resolved.update(self.explicit_source_root_map_for_host(host))
        return resolved

    def explicit_source_root_map_for_host(self, host: dict[str, Any] | None = None) -> dict[str, Path]:
        if not isinstance(host, dict):
            return {}
        overrides = host.get("source_roots")
        if not isinstance(overrides, dict):
            return {}
        resolved: dict[str, Path] = {}
        for key, value in overrides.items():
            key_text = str(key or "").strip()
            path_text = str(value or "").strip()
            if not key_text or not path_text:
                continue
            resolved[key_text] = Path(path_text).expanduser()
        return resolved

    @property
    def staging_root(self) -> Path:
        return Path(self.media["staging_root"]).expanduser()

    def staging_root_for_host(self, host: dict[str, Any] | None = None) -> Path:
        if not isinstance(host, dict):
            return self.staging_root
        value = str(host.get("staging_root") or "").strip()
        if not value:
            return self.staging_root
        return Path(value).expanduser()

    @property
    def archive_root(self) -> Path:
        return Path(self.media["archive_root"]).expanduser()

    def archive_root_for_host(self, host: dict[str, Any] | None = None) -> Path:
        if not isinstance(host, dict):
            return self.archive_root
        value = str(host.get("archive_root") or "").strip()
        if value:
            return Path(value).expanduser()
        staging_root = self.staging_root_for_host(host)
        if staging_root != self.staging_root:
            return staging_root / "_replaced"
        return self.archive_root

    @property
    def output_container(self) -> str:
        return str(self.media["output_container"])

    @property
    def overrides(self) -> list[dict[str, Any]]:
        return self.raw.get("overrides", [])

    @property
    def remote_hosts(self) -> list[dict[str, Any]]:
        return list(self.raw.get("remote_hosts", []))

    def resolve_policy(self, rel_path: str) -> dict[str, Any]:
        policy = {
            "video": copy.deepcopy(self.video),
            "audio": copy.deepcopy(self.audio),
            "subtitle": copy.deepcopy(self.subtitle),
            "planning": copy.deepcopy(self.planning),
        }

        normalized_rel_path = rel_path.strip("/")
        root_key = normalized_rel_path.split("/", 1)[0]
        library = self.library_definition_map.get(root_key)
        if library and library.get("type") == "tv":
            library_policy = library.get("policy")
            if isinstance(library_policy, dict):
                for key in (
                    "series_lifecycle_mode",
                    "current_season_inactive_days",
                    "season_acquisition_hold_days",
                    "series_metadata_stale_days",
                ):
                    if key in library_policy:
                        policy["planning"][key] = copy.deepcopy(library_policy[key])
        matching_overrides: list[tuple[int, int, dict[str, Any]]] = []
        for index, override in enumerate(self.overrides):
            prefix = str(override.get("path_prefix", "")).strip("/")
            if prefix and not _path_prefix_matches(normalized_rel_path, prefix):
                continue
            override_library_type = str(override.get("library_type") or "").strip()
            if override_library_type and library and override_library_type != str(library.get("type") or ""):
                continue
            matching_overrides.append((len(prefix), index, override))

        for _, _, override in sorted(matching_overrides):
            for section in ("video", "audio", "subtitle", "planning"):
                values = override.get(section)
                if isinstance(values, dict):
                    normalized_values = copy.deepcopy(values)
                    if section == "video":
                        _migrate_legacy_video_override(self.video, normalized_values)
                    policy[section].update(normalized_values)
        return policy


def _migrate_legacy_video_override(base_video: dict[str, Any], override_video: dict[str, Any]) -> None:
    if "size_goal_mode" not in override_video and (
            "target_size_mb" in override_video
            or "target_size_bytes" in override_video
            or "target_runtime_minutes" in override_video
    ):
        base_size = _positive_number(base_video.get("target_size_mb"))
        base_runtime = _positive_number(base_video.get("target_runtime_minutes"))
        override_size = _positive_number(override_video.get("target_size_mb"))
        override_runtime = _positive_number(override_video.get("target_runtime_minutes"))
        if _same_number(base_size, override_size) and _same_number(base_runtime, override_runtime):
            override_video["size_goal_mode"] = "normalized"
            override_video["size_goal_source"] = "legacy_default_override"
        elif override_runtime is not None and base_runtime is not None and not _same_number(
                override_runtime, base_runtime
        ):
            override_video["size_goal_mode"] = "absolute"
            override_video["size_goal_source"] = "legacy_inferred_absolute"
        else:
            override_video["size_goal_mode"] = "ambiguous"
            override_video["size_goal_source"] = "legacy_ambiguous_override"

    target_size_mb = _positive_number(override_video.get("target_size_mb"))
    if target_size_mb is not None and _positive_number(override_video.get("target_size_bytes")) is None:
        override_video["target_size_bytes"] = int(round(target_size_mb * 1_000_000))

    if "resolution_intent_mode" not in override_video and "max_height" in override_video:
        max_height = _positive_number(override_video.get("max_height"))
        override_video["resolution_intent_mode"] = "max_height" if max_height is not None else "source"
        override_video["resolution_intent_source"] = "legacy_inferred_override"


def _positive_number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _same_number(left: float | None, right: float | None) -> bool:
    return left is not None and right is not None and abs(left - right) <= 0.001


def _resolve_path(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return (base / path).resolve()


def load_config(config_path: Path | None = None) -> MediaforceConfig:
    base_config_path = config_path if config_path is not None else DEFAULT_CONFIG_PATH
    resolved_config_path = base_config_path.expanduser().resolve()
    raw = tomllib.loads(resolved_config_path.read_text())
    raw = _merge_optional_configs(raw, resolved_config_path.parent)
    project_root = resolved_config_path.parents[1]
    runtime_settings_path = _resolve_runtime_settings_path(project_root, raw["state"])
    runtime_settings = load_runtime_settings(runtime_settings_path)
    raw = _merge_runtime_settings(raw, runtime_settings)
    _merge_local_folder_policy_overrides(raw, runtime_settings)

    state = raw["state"]
    web_state_dir = _resolve_path(project_root, state.get("web_state_dir", "state/web"))
    configured_runtime_reservation_dir = state.get("runtime_reservation_dir")
    runtime_reservation_dir = (
        _resolve_path(project_root, configured_runtime_reservation_dir)
        if configured_runtime_reservation_dir is not None
        else web_state_dir.parent.parent / "mediaforce-runtime-reservations"
    )
    paths = ConfigPaths(
        project_root=project_root,
        config_path=resolved_config_path,
        db_path=_resolve_path(project_root, state["db_path"]),
        run_manifest_dir=_resolve_path(project_root, state["run_manifest_dir"]),
        web_state_dir=web_state_dir,
        review_dir=_resolve_path(project_root, state.get("review_dir", "state/review")),
        runtime_settings_path=runtime_settings_path,
        runtime_reservation_dir=runtime_reservation_dir,
    )
    return MediaforceConfig(raw=raw, paths=paths)


def migrate_config_state(config: MediaforceConfig) -> None:
    _migrate_project_state(config)


def load_runtime_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise ValueError(f"Runtime settings at {path} must be a JSON object")
    return payload


def save_runtime_settings(path: Path, payload: dict[str, Any]) -> None:
    with _locked_runtime_settings(path):
        _write_runtime_settings(path, payload)


def update_runtime_settings(path: Path, updater: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    with _locked_runtime_settings(path):
        try:
            current = load_runtime_settings(path)
        except (json.JSONDecodeError, OSError, ValueError):
            current = {}
        updated = updater(copy.deepcopy(current))
        if not isinstance(updated, dict):
            raise ValueError("Runtime settings updater must return a JSON object")
        _write_runtime_settings(path, updated)
        return updated


def upsert_runtime_folder_policy_override(path: Path, prefix: str, policy: dict[str, Any]) -> None:
    override_payload = _build_folder_policy_override(prefix, policy)
    normalized_prefix = prefix.strip("/")

    def _apply(runtime_settings: dict[str, Any]) -> dict[str, Any]:
        overrides = _normalize_folder_policy_overrides(runtime_settings.get(FOLDER_POLICY_OVERRIDES_KEY))
        updated_overrides: list[dict[str, Any]] = []
        replaced = False
        for existing in overrides:
            if str(existing.get("path_prefix", "")).strip("/") == normalized_prefix:
                updated_overrides.append(override_payload)
                replaced = True
                continue
            updated_overrides.append(existing)
        if not replaced:
            updated_overrides.append(override_payload)
        runtime_settings[FOLDER_POLICY_OVERRIDES_KEY] = updated_overrides
        return runtime_settings

    update_runtime_settings(path, _apply)


def with_folder_policy_override(
        config: MediaforceConfig,
        prefix: str,
        policy: dict[str, Any],
) -> MediaforceConfig:
    raw = copy.deepcopy(config.raw)
    overrides = raw.get("overrides")
    normalized_overrides = list(overrides) if isinstance(overrides, list) else []
    normalized_overrides.append(_build_folder_policy_override(prefix, policy))
    raw["overrides"] = normalized_overrides
    return MediaforceConfig(raw=raw, paths=config.paths)


def update_runtime_folder_policy_values(
        path: Path,
        prefix: str,
        *,
        section: str,
        values: dict[str, Any],
) -> None:
    normalized_prefix = prefix.strip("/")
    if section not in {"video", "audio", "subtitle", "planning"}:
        raise ValueError(f"Unsupported folder policy section: {section}")

    def _apply(runtime_settings: dict[str, Any]) -> dict[str, Any]:
        overrides = _normalize_folder_policy_overrides(runtime_settings.get(FOLDER_POLICY_OVERRIDES_KEY))
        existing = next(
            (
                copy.deepcopy(item)
                for item in overrides
                if str(item.get("path_prefix", "")).strip("/") == normalized_prefix
            ),
            {"path_prefix": normalized_prefix, "note": BENCH_SAVED_OVERRIDE_NOTE},
        )
        section_values = existing.get(section)
        merged_values = copy.deepcopy(section_values) if isinstance(section_values, dict) else {}
        merged_values.update(copy.deepcopy(values))
        existing[section] = merged_values
        updated = [
            item
            for item in overrides
            if str(item.get("path_prefix", "")).strip("/") != normalized_prefix
        ]
        updated.append(existing)
        runtime_settings[FOLDER_POLICY_OVERRIDES_KEY] = updated
        return runtime_settings

    update_runtime_settings(path, _apply)


def _migrate_project_state(config: MediaforceConfig) -> None:
    project_root = config.paths.project_root
    paths = config.paths
    state_root = project_root / "state"
    if not state_root.exists():
        return

    _migrate_legacy_sqlite_database(
        config,
        state_root / "library.sqlite3",
        paths.db_path,
    )
    migrations = (
        (state_root / "runs", paths.run_manifest_dir),
        (state_root / "web", paths.web_state_dir),
        (state_root / "review", paths.review_dir),
    )
    for source, destination in migrations:
        _move_if_needed(source, destination)

    for leftover in (
            state_root / ".DS_Store",
            state_root / "mediaforce-web.log",
            state_root / "mediaforce-web.pid",
    ):
        if leftover.exists():
            leftover.unlink()

    _remove_empty_dirs(state_root)


def _migrate_legacy_sqlite_database(
        config: MediaforceConfig,
        source: Path,
        destination: Path,
) -> None:
    if (
            not _path_entry_exists(source)
            or _path_without_resolution(source) == _path_without_resolution(destination)
            or _path_entry_exists(destination)
    ):
        return
    from mediaforce.web.runtime_lock import exclusive_legacy_sqlite_migration_source

    with exclusive_legacy_sqlite_migration_source(config, source) as locked_source:
        if _path_entry_exists(destination):
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging_path = _create_legacy_sqlite_staging_path(destination)
        try:
            with _copied_legacy_sqlite_database(locked_source, staging_path):
                _fsync_file(staging_path)
                locked_source.assert_stable()
                _publish_legacy_sqlite_database(staging_path, destination)
                locked_source.discard_after_publish()
        except BaseException as exc:
            try:
                _discard_legacy_sqlite_staging_path(staging_path)
            except OSError as cleanup_error:
                exc.add_note(
                    "Legacy SQLite staging cleanup also failed: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
            raise
        _discard_legacy_sqlite_staging_path(staging_path)


def _path_entry_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _path_without_resolution(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _create_legacy_sqlite_staging_path(destination: Path) -> Path:
    descriptor, staging_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.migration-",
        suffix=".sqlite3",
    )
    os.close(descriptor)
    return Path(staging_name)


@contextmanager
def _copied_legacy_sqlite_database(
        locked_source: _LegacySQLiteMigrationSource,
        staging_path: Path,
) -> Iterator[None]:
    source_connection: sqlite3.Connection | None = None
    write_gate_connection: sqlite3.Connection | None = None
    staging_connection: sqlite3.Connection | None = None
    source_uri = f"{locked_source.path.as_uri()}?mode=rw"
    try:
        write_gate_connection = sqlite3.connect(
            source_uri,
            uri=True,
            timeout=0,
            isolation_level=None,
        )
        write_gate_connection.execute("BEGIN IMMEDIATE")
        locked_source.assert_stable()
        source_connection = sqlite3.connect(
            source_uri,
            uri=True,
            timeout=0,
            isolation_level=None,
        )
        source_connection.execute("BEGIN")
        source_connection.execute("PRAGMA schema_version").fetchone()
        locked_source.assert_stable()
        staging_connection = sqlite3.connect(
            staging_path,
            timeout=0,
            isolation_level=None,
        )
        source_connection.backup(staging_connection)
        staging_connection.close()
        staging_connection = None
        staging_connection = sqlite3.connect(
            staging_path,
            timeout=0,
            isolation_level=None,
        )
        quick_check = staging_connection.execute("PRAGMA quick_check").fetchone()
        if quick_check != ("ok",):
            raise sqlite3.DatabaseError("Legacy SQLite backup did not pass quick_check")
        staging_connection.close()
        staging_connection = None
        locked_source.assert_stable()
        yield
    except sqlite3.OperationalError as exc:
        from mediaforce.web.runtime_lock import MediaforceRuntimeBusyError

        raise MediaforceRuntimeBusyError(
            "Legacy SQLite source is active or unavailable for migration"
        ) from exc
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(
            "Legacy SQLite source could not be migrated safely"
        ) from exc
    finally:
        if staging_connection is not None:
            staging_connection.close()
        if write_gate_connection is not None:
            try:
                if write_gate_connection.in_transaction:
                    write_gate_connection.rollback()
            finally:
                write_gate_connection.close()
        if source_connection is not None:
            try:
                if source_connection.in_transaction:
                    source_connection.rollback()
            finally:
                source_connection.close()


def _fsync_file(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_legacy_sqlite_database(staging_path: Path, destination: Path) -> None:
    try:
        os.link(staging_path, destination, follow_symlinks=False)
    except FileExistsError as exc:
        from mediaforce.web.runtime_lock import MediaforceRuntimeBusyError

        raise MediaforceRuntimeBusyError(
            "Configured SQLite destination appeared during legacy migration"
        ) from exc
    _fsync_directory(destination.parent)


def _discard_legacy_sqlite_staging_path(staging_path: Path) -> None:
    removed = False
    for candidate in (
            staging_path,
            Path(f"{staging_path}-wal"),
            Path(f"{staging_path}-shm"),
    ):
        try:
            candidate.unlink()
            removed = True
        except FileNotFoundError:
            continue
    if removed:
        _fsync_directory(staging_path.parent)


def _move_if_needed(source: Path, destination: Path) -> None:
    if not source.exists() or source.resolve() == destination.resolve():
        return
    if source.is_dir():
        _merge_directory(source, destination)
        return
    if destination.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))


def _merge_directory(source: Path, destination: Path) -> None:
    if not source.exists():
        return
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        return

    for child in source.iterdir():
        if child.name == ".gitkeep":
            continue
        target = destination / child.name
        if child.is_dir():
            _merge_directory(child, target)
            continue
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(child), str(target))
    _remove_empty_dirs(source)


def _remove_empty_dirs(path: Path) -> None:
    if not path.exists() or not path.is_dir():
        return
    for child in path.iterdir():
        if child.is_dir():
            _remove_empty_dirs(child)
    if any(path.iterdir()):
        return
    path.rmdir()


def _merge_optional_configs(base: dict[str, Any], config_dir: Path) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    include_files = list(base.get("config", {}).get("include_files", []))
    for file_name in include_files:
        include_path = (config_dir / file_name).resolve()
        if not include_path.exists():
            continue
        payload = tomllib.loads(include_path.read_text())
        _deep_merge(merged, payload)
    return merged


def _merge_runtime_settings(base: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    if not payload:
        return base
    merged = copy.deepcopy(base)
    normalized_payload = copy.deepcopy(payload)
    runtime_video = normalized_payload.get("video")
    if isinstance(runtime_video, dict):
        _normalize_runtime_video_defaults(runtime_video)
    _deep_merge(merged, normalized_payload, extend_lists=False)
    return merged


def _normalize_runtime_video_defaults(video: dict[str, Any]) -> None:
    target_size_mb = _positive_number(video.get("target_size_mb"))
    target_size_bytes = _positive_number(video.get("target_size_bytes"))
    if target_size_mb is not None:
        video["target_size_bytes"] = int(round(target_size_mb * 1_000_000))
    elif target_size_bytes is not None:
        video["target_size_mb"] = round(target_size_bytes / 1_000_000, 3)

    if {"target_size_mb", "target_size_bytes", "target_runtime_minutes"} & video.keys():
        video.setdefault("size_goal_schema_version", 1)
        video.setdefault("size_goal_mode", "normalized")
        video.setdefault("size_goal_source", "legacy_runtime_defaults")

    if "max_height" in video and "resolution_intent_mode" not in video:
        max_height = _positive_number(video.get("max_height"))
        video["resolution_intent_mode"] = "max_height" if max_height is not None else "source"
        video["resolution_intent_source"] = "legacy_runtime_defaults"


def _merge_local_folder_policy_overrides(base: dict[str, Any], runtime_settings: dict[str, Any]) -> None:
    overrides = _normalize_folder_policy_overrides(runtime_settings.get(FOLDER_POLICY_OVERRIDES_KEY))
    if not overrides:
        return
    existing_overrides = base.get("overrides")
    if not isinstance(existing_overrides, list):
        base["overrides"] = copy.deepcopy(overrides)
        return
    existing_overrides.extend(copy.deepcopy(overrides))


def _normalize_folder_policy_overrides(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        prefix = str(item.get("path_prefix") or "").strip("/")
        if not prefix:
            continue
        normalized.append(_build_folder_policy_override(prefix, item))
    return normalized


def _build_folder_policy_override(prefix: str, policy: dict[str, Any]) -> dict[str, Any]:
    override: dict[str, Any] = {
        "path_prefix": prefix.strip("/"),
        "note": BENCH_SAVED_OVERRIDE_NOTE,
    }
    for section in ("video", "audio", "subtitle", "planning"):
        values = policy.get(section)
        if isinstance(values, dict) and values:
            override[section] = copy.deepcopy(values)
    library_type = str(policy.get("library_type") or "").strip()
    if library_type:
        override["library_type"] = library_type
    return override


def _path_prefix_matches(rel_path: str, prefix: str) -> bool:
    return rel_path == prefix or rel_path.startswith(f"{prefix}/")


def _resolve_runtime_settings_path(project_root: Path, state: dict[str, Any]) -> Path:
    configured_path = state.get("runtime_settings_path")
    if configured_path:
        return _resolve_path(project_root, str(configured_path))
    db_path = _resolve_path(project_root, state["db_path"])
    return db_path.parent / "runtime-settings.json"


@contextmanager
def _locked_runtime_settings(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / f"{path.name}.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _write_runtime_settings(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
        ) as tmp_file:
            tmp_path = Path(tmp_file.name)
            tmp_file.write(json.dumps(payload, indent=2, sort_keys=True))
            tmp_file.write("\n")
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()


def _deep_merge(target: dict[str, Any], source: dict[str, Any], *, extend_lists: bool = True) -> None:
    for key, value in source.items():
        existing = target.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            _deep_merge(existing, value, extend_lists=extend_lists)
            continue
        if isinstance(existing, list) and isinstance(value, list):
            if extend_lists:
                existing.extend(copy.deepcopy(value))
            else:
                target[key] = copy.deepcopy(value)
            continue
        target[key] = copy.deepcopy(value)
