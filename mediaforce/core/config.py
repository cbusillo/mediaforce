from __future__ import annotations

import copy
from collections.abc import Callable, Iterator
from contextlib import contextmanager
import errno
import hashlib
from importlib import resources
import json
import logging
import os
import secrets
import shutil
import sqlite3
import stat
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from mediaforce.core.evidence import canonical_json_bytes
from mediaforce.core.file_integrity import (
    FileIntegrityError,
    fsync_durable_file,
    open_stable_directory,
    rename_exchange,
    rename_exclusive,
)
from mediaforce.core.utils import filesystem_collision_key
from mediaforce.library.library_settings import configured_library_definitions, library_definition_map, \
    library_production_supported

LOGGER = logging.getLogger(__name__)

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

_LEGACY_SQLITE_INTENT_SCHEMA = "mediaforce.legacy_sqlite_migration_intent"
_LEGACY_SQLITE_INTENT_VERSION = 5
_LEGACY_SQLITE_CLEANUP_RETAIN = "retain_verified_quarantines"
_LEGACY_SQLITE_CLEANUP_PRESERVE = "preserve_recorded_legacy_artifacts"
_LEGACY_SQLITE_PUBLICATION_EXCLUSIVE = "exclusive_staging_rename"
_LEGACY_SQLITE_PUBLICATION_LEGACY_ALIAS = "legacy_hardlink_retained_alias"
_LEGACY_SQLITE_PUBLICATION_LEGACY_NO_ALIAS = "legacy_hardlink_without_alias"
_LEGACY_SQLITE_CLEANUP_SUFFIXES = ("", "-wal", "-shm", "-journal")
_LEGACY_SQLITE_RECEIPT_SCHEMA = "mediaforce.legacy_sqlite_migration_receipt"
_LEGACY_SQLITE_RECEIPT_VERSION = 1


class _LegacySQLiteMigrationSource(Protocol):
    path: Path

    def assert_parent_stable(self) -> None: ...

    def assert_stable(self) -> None: ...

    def sqlite_uri(self) -> str: ...

    def source_snapshot(self) -> tuple[int, int, int, int, int, int]: ...

    def prepare_sqlite_sidecars_for_write_gate(self) -> None: ...

    def bind_sqlite_sidecars(self) -> None: ...

    def assert_connection_bound(self, connection: object) -> None: ...

    def cleanup_snapshot(self) -> dict[str, dict[str, object]]: ...

    def cleanup_sha256(self) -> str: ...

    def discard_after_publish(
            self,
            *,
            before_remove: Callable[[], None],
            expected_main_sha256: str,
            expected_sidecar_snapshots: dict[str, dict[str, object]],
    ) -> None: ...

    def assert_cleanup_complete(self) -> None: ...


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
    _migrate_legacy_sqlite_database(
        config,
        state_root / "library.sqlite3",
        paths.db_path,
    )
    if not state_root.exists():
        return
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
    receipt_path = _legacy_sqlite_migration_receipt_path(
        config,
        source=source,
    )
    _assert_legacy_sqlite_migration_receipt_outside_destination(
        config,
        receipt_path,
        destination=destination,
    )
    if _path_without_resolution(source) == _path_without_resolution(destination):
        return
    intent_path = _legacy_sqlite_migration_intent_path(destination)
    intent_exists = _path_entry_exists(intent_path)
    receipt_exists = _path_entry_exists(receipt_path)
    source_exists = _path_entry_exists(source)
    destination_exists = _path_entry_exists(destination)
    live_source_sidecar_exists = _legacy_sqlite_live_source_sidecar_exists(source)
    retired_source_residue_exists = (
        _legacy_sqlite_retired_source_residue_exists(source)
    )
    if intent_exists and _resume_legacy_sqlite_migration_intent(
            config,
            source,
            destination,
            intent_path,
            receipt_path,
    ):
        return
    if receipt_exists or retired_source_residue_exists:
        from mediaforce.web.runtime_lock import MediaforceRuntimeBusyError

        raise MediaforceRuntimeBusyError(
            "Legacy SQLite migration authority is missing from the configured destination"
        )
    if source_exists and destination_exists:
        from mediaforce.web.runtime_lock import MediaforceRuntimeBusyError

        raise MediaforceRuntimeBusyError(
            "Legacy and configured SQLite databases both exist without a "
            "resumable migration intent"
        )
    if not source_exists:
        if live_source_sidecar_exists:
            from mediaforce.web.runtime_lock import MediaforceRuntimeBusyError

            raise MediaforceRuntimeBusyError(
                "Legacy SQLite migration source sidecars exist without the main database"
            )
        return
    from mediaforce.web.runtime_lock import exclusive_legacy_sqlite_migration_source

    with exclusive_legacy_sqlite_migration_source(config, source) as locked_source:
        if _path_entry_exists(destination):
            from mediaforce.web.runtime_lock import MediaforceRuntimeBusyError

            raise MediaforceRuntimeBusyError(
                "Configured SQLite migration destination appeared after the "
                "legacy source was locked"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging_path = _reserved_legacy_sqlite_staging_path(destination)
        copying_intent = _legacy_sqlite_migration_intent_payload(
            locked_source=locked_source,
            source=source,
            destination=destination,
            staging_path=staging_path,
            staging_snapshot=None,
        )
        _write_legacy_sqlite_migration_intent(intent_path, copying_intent)
        _execute_legacy_sqlite_copy(
            locked_source=locked_source,
            source=source,
            destination=destination,
            intent_path=intent_path,
            copying_intent=copying_intent,
            receipt_path=receipt_path,
        )


def _execute_legacy_sqlite_copy(
        *,
        locked_source: _LegacySQLiteMigrationSource,
        source: Path,
        destination: Path,
        intent_path: Path,
        copying_intent: dict[str, object],
        receipt_path: Path,
) -> None:
    staging_path = destination.parent / str(copying_intent["staging_name"])
    with _create_legacy_sqlite_staging_path(staging_path) as staging_binding:
        with _copied_legacy_sqlite_database(
                locked_source,
                staging_binding,
        ):
            staging_snapshot = staging_binding.snapshot()
            locked_source.assert_stable()
            ready_intent = _legacy_sqlite_migration_intent_payload(
                locked_source=locked_source,
                source=source,
                destination=destination,
                staging_path=staging_path,
                staging_snapshot=staging_snapshot,
            )
            _assert_legacy_sqlite_migration_source_matches(
                locked_source,
                ready_intent,
            )
            _replace_legacy_sqlite_migration_intent(
                intent_path,
                expected=copying_intent,
                payload=ready_intent,
            )
            staging_snapshot = _legacy_sqlite_migration_snapshot_dict(
                ready_intent["staging_snapshot"],
                "staging snapshot",
            )
            with _opened_legacy_sqlite_migration_destination(
                    destination,
                    expected_parent_identity=ready_intent.get(
                        "destination_parent_identity"
                    ),
            ) as destination_binding:
                destination_binding.publish(
                    staging_path.name,
                    staging_snapshot,
                )
                destination_binding.assert_database_valid()
                cleaning_intent = (
                    _legacy_sqlite_migration_cleaning_intent_payload(
                        ready_intent,
                        locked_source=locked_source,
                    )
                )
                _replace_legacy_sqlite_migration_intent(
                    intent_path,
                    expected=ready_intent,
                    payload=cleaning_intent,
                )
                _finish_legacy_sqlite_cleanup_with_locked_source(
                    locked_source=locked_source,
                    destination_binding=destination_binding,
                    intent_path=intent_path,
                    payload=cleaning_intent,
                    staging_snapshot=staging_snapshot,
                    receipt_path=receipt_path,
                )


def _finish_legacy_sqlite_cleanup_with_locked_source(
        *,
        locked_source: _LegacySQLiteMigrationSource,
        destination_binding: _LegacySQLiteMigrationDestination,
        intent_path: Path,
        payload: dict[str, object],
        staging_snapshot: dict[str, object],
        receipt_path: Path,
) -> None:
    publication_policy = payload.get("publication_policy")

    def assert_destination_authoritative() -> None:
        _assert_legacy_sqlite_v5_publication_state(
            destination_binding,
            staging_name=str(payload["staging_name"]),
            staging_snapshot=staging_snapshot,
            publication_policy=publication_policy,
        )

    assert_destination_authoritative()
    locked_source.discard_after_publish(
        before_remove=assert_destination_authoritative,
        expected_main_sha256=str(payload["source_main_sha256"]),
        expected_sidecar_snapshots=(
            _legacy_sqlite_migration_sidecar_snapshots(
                payload["source_sidecar_snapshots"]
            )
        ),
    )
    _finalize_legacy_sqlite_migration(
        destination_binding=destination_binding,
        intent_path=intent_path,
        payload=payload,
        staging_snapshot=staging_snapshot,
        assert_source_cleanup_complete=locked_source.assert_cleanup_complete,
        receipt_path=receipt_path,
    )


def _path_entry_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _path_without_resolution(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def legacy_sqlite_migration_quarantine_name(
        name: str,
        *,
        device: int,
        inode: int,
        size: int,
        mtime_ns: int,
) -> str:
    identity = f"{name}\0{device}\0{inode}\0{size}\0{mtime_ns}".encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()[:24]
    return f".{name}.mediaforce-retired-{digest}"


@dataclass(slots=True)
class _LegacySQLiteMigrationDestination:
    path: Path
    directory_descriptor: int
    directory_identity: tuple[int, int]

    def assert_parent_stable(self) -> None:
        try:
            descriptor_info = os.fstat(self.directory_descriptor)
            path_info = self.path.parent.stat(follow_symlinks=False)
        except OSError as exc:
            raise OSError(
                "legacy SQLite migration destination parent is unavailable"
            ) from exc
        if (
            not stat.S_ISDIR(descriptor_info.st_mode)
            or not stat.S_ISDIR(path_info.st_mode)
            or stat.S_ISLNK(path_info.st_mode)
            or (descriptor_info.st_dev, descriptor_info.st_ino)
            != self.directory_identity
            or (path_info.st_dev, path_info.st_ino)
            != self.directory_identity
        ):
            raise OSError("legacy SQLite migration destination parent changed")

    def entry_exists(self, name: str) -> bool:
        self.assert_parent_stable()
        try:
            os.stat(
                name,
                dir_fd=self.directory_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return False
        return True

    def assert_file_matches(
            self,
            name: str,
            expected: dict[str, object],
            *,
            allowed_link_counts: set[int],
    ) -> None:
        self.assert_parent_stable()
        current = _legacy_sqlite_migration_file_snapshot_at(
            self.directory_descriptor,
            name,
            include_sha256=True,
            require_single_link=False,
        )
        if (
            current.get("link_count") not in allowed_link_counts
            or any(
                current.get(key) != expected.get(key)
                for key in (
                    "device",
                    "inode",
                    "size",
                    "mtime_ns",
                    "sha256",
                )
            )
        ):
            raise OSError("legacy SQLite migration destination identity changed")
        self.assert_parent_stable()

    def assert_file_identity(
            self,
            name: str,
            expected: dict[str, object],
            *,
            allowed_link_counts: set[int],
    ) -> None:
        self.assert_parent_stable()
        current = _legacy_sqlite_migration_file_snapshot_at(
            self.directory_descriptor,
            name,
            include_sha256=False,
            require_single_link=False,
            include_timestamps=False,
        )
        if (
            current.get("link_count") not in allowed_link_counts
            or any(
                current.get(key) != expected.get(key)
                for key in ("device", "inode")
            )
        ):
            raise OSError("legacy SQLite migration destination identity changed")
        self.assert_parent_stable()

    def assert_database_sidecars_absent(self) -> None:
        self.assert_parent_stable()
        for suffix in ("-wal", "-shm", "-journal"):
            if self.entry_exists(f"{self.path.name}{suffix}"):
                raise OSError(
                    "legacy SQLite migration destination sidecar already exists"
                )
        self.assert_parent_stable()

    def assert_database_sidecars_safe(self) -> None:
        self.assert_parent_stable()
        if not hasattr(os, "O_NOFOLLOW"):
            raise OSError(
                errno.ENOTSUP,
                "legacy SQLite migration destination sidecar checks are unavailable",
            )
        for suffix in ("-wal", "-shm", "-journal"):
            name = f"{self.path.name}{suffix}"
            flags = (
                os.O_RDONLY
                | os.O_NONBLOCK
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0)
            )
            try:
                descriptor = os.open(
                    name,
                    flags,
                    dir_fd=self.directory_descriptor,
                )
            except FileNotFoundError:
                try:
                    os.stat(
                        name,
                        dir_fd=self.directory_descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise OSError(
                        "legacy SQLite migration destination sidecar is unavailable"
                    ) from exc
                raise OSError(
                    "legacy SQLite migration destination sidecar identity changed"
                )
            except OSError as exc:
                raise OSError(
                    "legacy SQLite migration destination sidecar is unsafe"
                ) from exc
            try:
                descriptor_info = os.fstat(descriptor)
                path_info = os.stat(
                    name,
                    dir_fd=self.directory_descriptor,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISREG(descriptor_info.st_mode)
                    or not stat.S_ISREG(path_info.st_mode)
                    or descriptor_info.st_uid != os.getuid()
                    or path_info.st_uid != os.getuid()
                    or descriptor_info.st_nlink != 1
                    or path_info.st_nlink != 1
                    or (
                        descriptor_info.st_dev,
                        descriptor_info.st_ino,
                    )
                    != (path_info.st_dev, path_info.st_ino)
                ):
                    raise OSError(
                        "legacy SQLite migration destination sidecar is unsafe"
                    )
            finally:
                os.close(descriptor)
        self.assert_parent_stable()

    def assert_database_valid(self) -> None:
        self.assert_parent_stable()
        pinned_path = _legacy_sqlite_path_for_directory_descriptor(
            self.directory_descriptor,
            directory_identity=self.directory_identity,
            filename=self.path.name,
        )
        _assert_legacy_sqlite_migration_database_valid(pinned_path)
        self.assert_parent_stable()

    def publish(
            self,
            staging_name: str,
            expected: dict[str, object],
    ) -> None:
        self.assert_database_sidecars_absent()
        self.assert_file_matches(
            staging_name,
            expected,
            allowed_link_counts={1},
        )
        try:
            rename_exclusive(
                source_directory_descriptor=self.directory_descriptor,
                source_name=staging_name,
                destination_directory_descriptor=self.directory_descriptor,
                destination_name=self.path.name,
            )
        except FileExistsError as exc:
            from mediaforce.web.runtime_lock import MediaforceRuntimeBusyError

            raise MediaforceRuntimeBusyError(
                "Configured SQLite destination appeared during legacy migration"
            ) from exc
        os.fsync(self.directory_descriptor)
        if self.entry_exists(staging_name):
            raise OSError("legacy SQLite migration staging publication is incomplete")
        self.assert_file_matches(
            self.path.name,
            expected,
            allowed_link_counts={1},
        )
        self.assert_database_sidecars_absent()

    def assert_retained_staging(
            self,
            staging_name: str,
            expected: dict[str, object],
    ) -> None:
        self.assert_file_matches(
            staging_name,
            expected,
            allowed_link_counts={2},
        )
        self.assert_file_matches(
            self.path.name,
            expected,
            allowed_link_counts={2},
        )
        self.assert_parent_stable()


@dataclass(slots=True)
class _LegacySQLiteMigrationStaging:
    path: Path
    directory_descriptor: int
    directory_identity: tuple[int, int]
    descriptor: int
    file_identity: tuple[int, int]

    def assert_stable(self) -> None:
        try:
            directory_info = os.fstat(self.directory_descriptor)
            parent_info = self.path.parent.stat(follow_symlinks=False)
            descriptor_info = os.fstat(self.descriptor)
            path_info = os.stat(
                self.path.name,
                dir_fd=self.directory_descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise OSError(
                "legacy SQLite migration staging identity is unavailable"
            ) from exc
        if (
            not stat.S_ISDIR(directory_info.st_mode)
            or not stat.S_ISDIR(parent_info.st_mode)
            or (directory_info.st_dev, directory_info.st_ino)
            != self.directory_identity
            or (parent_info.st_dev, parent_info.st_ino)
            != self.directory_identity
            or not stat.S_ISREG(descriptor_info.st_mode)
            or not stat.S_ISREG(path_info.st_mode)
            or descriptor_info.st_uid != os.getuid()
            or path_info.st_uid != os.getuid()
            or stat.S_IMODE(descriptor_info.st_mode) != 0o600
            or stat.S_IMODE(path_info.st_mode) != 0o600
            or descriptor_info.st_nlink != 1
            or path_info.st_nlink != 1
            or (descriptor_info.st_dev, descriptor_info.st_ino)
            != self.file_identity
            or (path_info.st_dev, path_info.st_ino) != self.file_identity
        ):
            raise OSError("legacy SQLite migration staging identity changed")

    def write_from(self, source: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        source_descriptor = os.open(source, flags)
        try:
            source_info = os.fstat(source_descriptor)
            source_path_info = source.stat(follow_symlinks=False)
            if (
                not stat.S_ISREG(source_info.st_mode)
                or not stat.S_ISREG(source_path_info.st_mode)
                or source_info.st_uid != os.getuid()
                or source_info.st_nlink != 1
                or (source_info.st_dev, source_info.st_ino)
                != (source_path_info.st_dev, source_path_info.st_ino)
            ):
                raise OSError("legacy SQLite migration private backup is unsafe")
            self.assert_stable()
            os.lseek(source_descriptor, 0, os.SEEK_SET)
            os.lseek(self.descriptor, 0, os.SEEK_SET)
            os.ftruncate(self.descriptor, 0)
            while True:
                chunk = os.read(source_descriptor, 1024 * 1024)
                if not chunk:
                    break
                offset = 0
                while offset < len(chunk):
                    written = os.write(self.descriptor, chunk[offset:])
                    if written <= 0:
                        raise OSError(
                            "legacy SQLite migration staging write did not progress"
                        )
                    offset += written
            fsync_durable_file(self.descriptor)
            os.fsync(self.directory_descriptor)
            self.assert_stable()
        finally:
            os.close(source_descriptor)

    def snapshot(self) -> dict[str, object]:
        self.assert_stable()
        snapshot = _legacy_sqlite_migration_file_snapshot_descriptor(
            self.descriptor,
            include_sha256=True,
            require_single_link=True,
        )
        self.assert_stable()
        return snapshot


@contextmanager
def _opened_legacy_sqlite_migration_destination(
        destination: Path,
        *,
        expected_parent_identity: object,
) -> Iterator[_LegacySQLiteMigrationDestination]:
    if (
        not isinstance(expected_parent_identity, list)
        or len(expected_parent_identity) != 2
        or any(not isinstance(value, int) for value in expected_parent_identity)
    ):
        raise OSError("legacy SQLite migration destination parent identity is invalid")
    directory_descriptor = -1
    try:
        _, directory_descriptor = open_stable_directory(destination.parent)
        binding = _LegacySQLiteMigrationDestination(
            path=destination,
            directory_descriptor=directory_descriptor,
            directory_identity=(
                expected_parent_identity[0],
                expected_parent_identity[1],
            ),
        )
        binding.assert_parent_stable()
        yield binding
        binding.assert_parent_stable()
    except FileIntegrityError as exc:
        raise OSError(
            "legacy SQLite migration destination parent is unsafe"
        ) from exc
    finally:
        if directory_descriptor >= 0:
            os.close(directory_descriptor)


def _legacy_sqlite_migration_intent_path(destination: Path) -> Path:
    return destination.parent / f".{destination.name}.legacy-migration-intent.json"


def _legacy_sqlite_migration_receipt_path(
        config: MediaforceConfig,
        *,
        source: Path,
) -> Path:
    from mediaforce.web.runtime_lock import mediaforce_runtime_reservation_dir

    identity = hashlib.sha256(canonical_json_bytes({
        "source_path": os.fspath(_path_without_resolution(source)),
    })).hexdigest()
    return mediaforce_runtime_reservation_dir(config) / (
        f"legacy-sqlite-migration-{identity[:40]}.json"
    )


def _assert_legacy_sqlite_migration_receipt_outside_destination(
        config: MediaforceConfig,
        receipt_path: Path,
        *,
        destination: Path,
) -> None:
    discovery_directory = _legacy_sqlite_migration_receipt_discovery_directory(
        config,
        fallback=receipt_path.parent,
    )
    resolved_directory = receipt_path.parent.expanduser().resolve()
    destination_parents = (
        _path_without_resolution(destination.parent),
        destination.parent.expanduser().resolve(),
    )
    if not any(
        _path_is_at_or_below(receipt_directory, destination_parent)
        for receipt_directory in (
            *_path_resolution_candidates(discovery_directory),
            resolved_directory,
        )
        for destination_parent in destination_parents
    ):
        return
    from mediaforce.web.runtime_lock import MediaforceRuntimeBusyError

    raise MediaforceRuntimeBusyError(
        "Legacy SQLite migration receipt storage must be outside the "
        "configured destination parent without a path alias through that parent"
    )


def _legacy_sqlite_migration_receipt_discovery_directory(
        config: MediaforceConfig,
        *,
        fallback: Path,
) -> Path:
    state = config.raw.get("state")
    configured_value = (
        state.get("runtime_reservation_dir")
        if isinstance(state, dict)
        else None
    )
    if isinstance(configured_value, str):
        configured_path = Path(configured_value).expanduser()
        if not configured_path.is_absolute():
            configured_path = config.paths.project_root / configured_path
        return _path_without_resolution(configured_path)
    configured_path = getattr(config.paths, "runtime_reservation_dir", None)
    if configured_path is None:
        return _path_without_resolution(fallback)
    return _path_without_resolution(Path(configured_path))


def _path_is_at_or_below(path: Path, parent: Path) -> bool:
    path_key = filesystem_collision_key(_path_without_resolution(path))
    parent_key = filesystem_collision_key(_path_without_resolution(parent))
    return path_key == parent_key or path_key.startswith(
        f"{parent_key.rstrip(os.sep)}{os.sep}"
    )


def _path_resolution_candidates(path: Path) -> tuple[Path, ...]:
    current = _path_without_resolution(path)
    candidates = [current]
    seen = {filesystem_collision_key(current)}
    for _ in range(40):
        expanded = _expand_first_path_symlink(current)
        if expanded is None:
            return tuple(candidates)
        collision_key = filesystem_collision_key(expanded)
        if collision_key in seen:
            break
        seen.add(collision_key)
        candidates.append(expanded)
        current = expanded
    from mediaforce.web.runtime_lock import MediaforceRuntimeBusyError

    raise MediaforceRuntimeBusyError(
        "Legacy SQLite migration receipt discovery path is invalid"
    )


def _expand_first_path_symlink(path: Path) -> Path | None:
    normalized = _path_without_resolution(path)
    parts = normalized.parts
    current = Path(normalized.anchor)
    for index, component in enumerate(parts[1:], start=1):
        candidate = current / component
        try:
            target = Path(os.readlink(candidate))
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, errno.ENOENT, errno.ENOTDIR}:
                from mediaforce.web.runtime_lock import MediaforceRuntimeBusyError

                raise MediaforceRuntimeBusyError(
                    "Legacy SQLite migration receipt discovery path is unavailable"
                ) from exc
            current = candidate
            continue
        if not target.is_absolute():
            target = candidate.parent / target
        remainder = parts[index + 1:]
        return _path_without_resolution(target.joinpath(*remainder))
    return None


def _legacy_sqlite_migration_receipt_payload(
        payload: dict[str, object],
) -> dict[str, object]:
    source_parent_identity = payload.get("source_parent_identity")
    destination_parent_identity = payload.get("destination_parent_identity")
    staging_snapshot = _legacy_sqlite_migration_snapshot_dict(
        payload.get("staging_snapshot"),
        "staging snapshot",
    )
    if (
        payload.get("schema_version") != _LEGACY_SQLITE_INTENT_VERSION
        or payload.get("phase") not in {"cleaning", "sealing", "complete"}
        or not isinstance(payload.get("source_path"), str)
        or not isinstance(payload.get("destination_path"), str)
        or not isinstance(payload.get("staging_name"), str)
        or not isinstance(source_parent_identity, list)
        or len(source_parent_identity) != 2
        or any(type(value) is not int for value in source_parent_identity)
        or not isinstance(destination_parent_identity, list)
        or len(destination_parent_identity) != 2
        or any(type(value) is not int for value in destination_parent_identity)
        or not _legacy_sqlite_migration_sha256_is_valid(
            staging_snapshot.get("sha256")
        )
        or payload.get("publication_policy") not in {
            _LEGACY_SQLITE_PUBLICATION_EXCLUSIVE,
            _LEGACY_SQLITE_PUBLICATION_LEGACY_ALIAS,
            _LEGACY_SQLITE_PUBLICATION_LEGACY_NO_ALIAS,
        }
    ):
        raise OSError("legacy SQLite migration receipt inputs are invalid")
    return {
        "schema": _LEGACY_SQLITE_RECEIPT_SCHEMA,
        "schema_version": _LEGACY_SQLITE_RECEIPT_VERSION,
        "source_path": payload["source_path"],
        "destination_path": payload["destination_path"],
        "source_parent_identity": list(source_parent_identity),
        "destination_parent_identity": list(destination_parent_identity),
        "staging_name": payload["staging_name"],
        "staging_sha256": staging_snapshot["sha256"],
        "publication_policy": payload["publication_policy"],
    }


def _ensure_legacy_sqlite_migration_receipt(
        receipt_path: Path,
        payload: dict[str, object],
) -> None:
    expected = _legacy_sqlite_migration_receipt_payload(payload)
    _write_legacy_sqlite_migration_intent(receipt_path, expected)
    if _load_legacy_sqlite_migration_intent(receipt_path) != expected:
        raise OSError("legacy SQLite migration receipt changed after publication")


def _legacy_sqlite_retired_source_residue_exists(source: Path) -> bool:
    if not _path_entry_exists(source.parent):
        return False
    directory_descriptor = -1
    try:
        _, directory_descriptor = open_stable_directory(source.parent)
        names = os.listdir(directory_descriptor)
        prefixes = tuple(
            f".{source.name}{suffix}.mediaforce-retired-"
            for suffix in _LEGACY_SQLITE_CLEANUP_SUFFIXES
        )
        return any(name.startswith(prefixes) for name in names)
    except FileNotFoundError:
        return False
    except FileIntegrityError as exc:
        if not _path_entry_exists(source.parent):
            return False
        raise OSError(
            "legacy SQLite migration source parent is unsafe"
        ) from exc
    finally:
        if directory_descriptor >= 0:
            os.close(directory_descriptor)


def _legacy_sqlite_live_source_sidecar_exists(source: Path) -> bool:
    return any(
        _path_entry_exists(Path(f"{source}{suffix}"))
        for suffix in _LEGACY_SQLITE_CLEANUP_SUFFIXES[1:]
    )


def _legacy_sqlite_migration_intent_payload(
        *,
        locked_source: _LegacySQLiteMigrationSource,
        source: Path,
        destination: Path,
        staging_path: Path,
        staging_snapshot: dict[str, object] | None,
        publication_policy: str = _LEGACY_SQLITE_PUBLICATION_EXCLUSIVE,
) -> dict[str, object]:
    locked_source.assert_stable()
    source_parent_info = source.parent.stat(follow_symlinks=False)
    destination_parent_info = destination.parent.stat(follow_symlinks=False)
    payload = {
        "schema": _LEGACY_SQLITE_INTENT_SCHEMA,
        "schema_version": _LEGACY_SQLITE_INTENT_VERSION,
        "phase": "copying" if staging_snapshot is None else "ready",
        "source_path": os.fspath(_path_without_resolution(source)),
        "destination_path": os.fspath(_path_without_resolution(destination)),
        "staging_name": staging_path.name,
        "source_parent_identity": [
            source_parent_info.st_dev,
            source_parent_info.st_ino,
        ],
        "source_main_snapshot": list(locked_source.source_snapshot()),
        "source_main_sha256": None,
        "source_sidecar_snapshots": None,
        "staging_snapshot": staging_snapshot,
        "destination_parent_identity": [
            destination_parent_info.st_dev,
            destination_parent_info.st_ino,
        ],
        "cleanup_policy": _LEGACY_SQLITE_CLEANUP_RETAIN,
        "publication_policy": publication_policy,
        "legacy_cleanup_absent_prefix": [],
        "retained_cleanup_artifacts": None,
    }
    locked_source.assert_stable()
    return payload


def _legacy_sqlite_migration_cleaning_intent_payload(
        ready_payload: dict[str, object],
        *,
        locked_source: _LegacySQLiteMigrationSource,
        publication_policy: str | None = None,
) -> dict[str, object]:
    current_snapshots = _legacy_sqlite_migration_sidecar_snapshots(
        locked_source.cleanup_snapshot()
    )
    if ready_payload.get("phase") == "cleaning":
        current_snapshots = _reconciled_legacy_sqlite_migration_sidecar_snapshots(
            _legacy_sqlite_migration_sidecar_snapshots(
                ready_payload.get("source_sidecar_snapshots"),
                require_sha256=(
                    ready_payload.get("schema_version") in {4, 5}
                ),
            ),
            current_snapshots,
        )
    payload = dict(ready_payload)
    payload["schema_version"] = _LEGACY_SQLITE_INTENT_VERSION
    payload["phase"] = "cleaning"
    payload["source_main_snapshot"] = list(locked_source.source_snapshot())
    payload["source_main_sha256"] = locked_source.cleanup_sha256()
    payload["source_sidecar_snapshots"] = current_snapshots
    payload["cleanup_policy"] = _LEGACY_SQLITE_CLEANUP_RETAIN
    payload["publication_policy"] = (
        publication_policy
        if publication_policy is not None
        else payload.get("publication_policy")
    )
    payload["legacy_cleanup_absent_prefix"] = []
    payload["retained_cleanup_artifacts"] = (
        _legacy_sqlite_expected_cleanup_artifact_payloads(
            source_name=locked_source.path.name,
            expected_main_snapshot=payload["source_main_snapshot"],
            expected_main_sha256=payload["source_main_sha256"],
            expected_sidecar_snapshots=current_snapshots,
            absent_prefix=(),
        )
    )
    return payload


def _legacy_sqlite_migration_file_snapshot(
        path: Path,
        *,
        include_sha256: bool,
        require_single_link: bool,
        include_timestamps: bool = True,
) -> dict[str, object]:
    directory_descriptor = -1
    try:
        _, directory_descriptor = open_stable_directory(path.parent)
        return _legacy_sqlite_migration_file_snapshot_at(
            directory_descriptor,
            path.name,
            include_sha256=include_sha256,
            require_single_link=require_single_link,
            include_timestamps=include_timestamps,
        )
    except FileIntegrityError as exc:
        raise OSError("legacy SQLite migration artifact parent is unsafe") from exc
    finally:
        if directory_descriptor >= 0:
            os.close(directory_descriptor)


def _legacy_sqlite_migration_file_snapshot_at(
        directory_descriptor: int,
        name: str,
        *,
        include_sha256: bool,
        require_single_link: bool,
        include_timestamps: bool = True,
) -> dict[str, object]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    try:
        path_info = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        snapshot = _legacy_sqlite_migration_file_snapshot_descriptor(
            descriptor,
            include_sha256=include_sha256,
            require_single_link=require_single_link,
            include_timestamps=include_timestamps,
        )
        descriptor_info = os.fstat(descriptor)
        final_path_info = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(path_info.st_mode)
            or not stat.S_ISREG(final_path_info.st_mode)
            or (descriptor_info.st_dev, descriptor_info.st_ino)
            != (path_info.st_dev, path_info.st_ino)
            or (descriptor_info.st_dev, descriptor_info.st_ino)
            != (final_path_info.st_dev, final_path_info.st_ino)
            or (
                require_single_link
                and (
                    path_info.st_nlink != 1
                    or final_path_info.st_nlink != 1
                )
            )
        ):
            raise OSError("legacy SQLite migration artifact identity is unsafe")
        return snapshot
    finally:
        os.close(descriptor)


def _legacy_sqlite_migration_file_snapshot_descriptor(
        descriptor: int,
        *,
        include_sha256: bool,
        require_single_link: bool,
        include_timestamps: bool = True,
) -> dict[str, object]:
    before = os.fstat(descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or (require_single_link and before.st_nlink != 1)
    ):
        raise OSError("legacy SQLite migration artifact identity is unsafe")
    snapshot: dict[str, object] = {
        "device": before.st_dev,
        "inode": before.st_ino,
        "size": before.st_size,
        "link_count": before.st_nlink,
    }
    if include_timestamps:
        snapshot.update({
            "mtime_ns": before.st_mtime_ns,
            "ctime_ns": before.st_ctime_ns,
        })
    if include_sha256:
        digest = hashlib.sha256()
        os.lseek(descriptor, 0, os.SEEK_SET)
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        snapshot["sha256"] = f"sha256:{digest.hexdigest()}"
    after = os.fstat(descriptor)
    if (
        not stat.S_ISREG(after.st_mode)
        or (after.st_dev, after.st_ino, after.st_size, after.st_nlink)
        != (before.st_dev, before.st_ino, before.st_size, before.st_nlink)
        or (after.st_mtime_ns, after.st_ctime_ns)
        != (before.st_mtime_ns, before.st_ctime_ns)
    ):
        raise OSError("legacy SQLite migration artifact changed during snapshot")
    return snapshot


def _legacy_sqlite_migration_payload_digest(
        payload: dict[str, object],
) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _legacy_sqlite_migration_intent_transition_path(
        intent_path: Path,
        *,
        expected: dict[str, object],
        payload: dict[str, object],
) -> Path:
    expected_digest = _legacy_sqlite_migration_payload_digest(expected)
    payload_digest = _legacy_sqlite_migration_payload_digest(payload)
    return intent_path.parent / (
        f"{intent_path.name}.transition-{expected_digest}-{payload_digest}.json"
    )


def _open_legacy_sqlite_migration_intent_descriptor_at(
        directory_descriptor: int,
        intent_name: str,
) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(
        intent_name,
        flags,
        dir_fd=directory_descriptor,
    )
    try:
        _load_legacy_sqlite_migration_intent_descriptor(descriptor)
        _assert_legacy_sqlite_migration_intent_descriptor_at(
            directory_descriptor,
            intent_name,
            descriptor,
        )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _assert_legacy_sqlite_migration_intent_descriptor_at(
        directory_descriptor: int,
        intent_name: str,
        descriptor: int,
) -> None:
    descriptor_info = os.fstat(descriptor)
    path_info = os.stat(
        intent_name,
        dir_fd=directory_descriptor,
        follow_symlinks=False,
    )
    if (
        not stat.S_ISREG(descriptor_info.st_mode)
        or not stat.S_ISREG(path_info.st_mode)
        or descriptor_info.st_uid != os.getuid()
        or path_info.st_uid != os.getuid()
        or stat.S_IMODE(descriptor_info.st_mode) != 0o400
        or stat.S_IMODE(path_info.st_mode) != 0o400
        or descriptor_info.st_nlink != 1
        or path_info.st_nlink != 1
        or (descriptor_info.st_dev, descriptor_info.st_ino)
        != (path_info.st_dev, path_info.st_ino)
    ):
        raise OSError("legacy SQLite migration intent identity changed")


def _load_legacy_sqlite_migration_intent_descriptor(
        descriptor: int,
) -> dict[str, object]:
    descriptor_info = os.fstat(descriptor)
    if (
        not stat.S_ISREG(descriptor_info.st_mode)
        or descriptor_info.st_uid != os.getuid()
        or stat.S_IMODE(descriptor_info.st_mode) != 0o400
        or descriptor_info.st_nlink != 1
    ):
        raise OSError("legacy SQLite migration intent is unsafe")
    os.lseek(descriptor, 0, os.SEEK_SET)
    raw = bytearray()
    while len(raw) <= 64 * 1024:
        chunk = os.read(descriptor, 16 * 1024)
        if not chunk:
            break
        raw.extend(chunk)
    if len(raw) > 64 * 1024:
        raise OSError("legacy SQLite migration intent is oversized")
    try:
        payload = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OSError("legacy SQLite migration intent is invalid") from exc
    if not isinstance(payload, dict) or canonical_json_bytes(payload) != bytes(raw):
        raise OSError("legacy SQLite migration intent is invalid")
    return payload


def _write_legacy_sqlite_migration_intent(
        intent_path: Path,
        payload: dict[str, object],
) -> None:
    directory_descriptor = -1
    try:
        _, directory_descriptor = open_stable_directory(intent_path.parent)
        _write_legacy_sqlite_migration_intent_at(
            directory_descriptor=directory_descriptor,
            intent_name=intent_path.name,
            payload=payload,
        )
        _fsync_directory(intent_path.parent)
    except FileIntegrityError as exc:
        raise OSError("legacy SQLite migration intent parent is unsafe") from exc
    finally:
        if directory_descriptor >= 0:
            os.close(directory_descriptor)


def _write_legacy_sqlite_migration_intent_at(
        *,
        directory_descriptor: int,
        intent_name: str,
        payload: dict[str, object],
) -> None:
    if Path(intent_name).name != intent_name:
        raise OSError("legacy SQLite migration intent name is invalid")
    encoded = canonical_json_bytes(payload)
    descriptor = -1
    temporary_name = ""
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
    )
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        try:
            existing = _load_legacy_sqlite_migration_intent_at(
                directory_descriptor,
                intent_name,
            )
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if existing == payload:
                return
            from mediaforce.web.runtime_lock import MediaforceRuntimeBusyError

            raise MediaforceRuntimeBusyError(
                "Legacy SQLite migration intent conflicts with existing state"
            )
        for _attempt in range(16):
            temporary_name = (
                ".legacy-sqlite-intent-prepare-"
                f"{_legacy_sqlite_migration_payload_digest(payload)[:32]}-"
                f"{secrets.token_hex(8)}.tmp"
            )
            try:
                descriptor = os.open(
                    temporary_name,
                    flags,
                    0o600,
                    dir_fd=directory_descriptor,
                )
            except FileExistsError:
                continue
            break
        if descriptor < 0:
            raise OSError(
                "legacy SQLite migration intent temporary reservation failed"
            )
        offset = 0
        while offset < len(encoded):
            written = os.write(descriptor, encoded[offset:])
            if written <= 0:
                raise OSError(
                    "legacy SQLite migration intent write did not progress"
                )
            offset += written
        os.fchmod(descriptor, 0o400)
        fsync_durable_file(descriptor)
        try:
            rename_exclusive(
                source_directory_descriptor=directory_descriptor,
                source_name=temporary_name,
                destination_directory_descriptor=directory_descriptor,
                destination_name=intent_name,
            )
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise
            existing = _load_legacy_sqlite_migration_intent_at(
                directory_descriptor,
                intent_name,
            )
            if existing != payload:
                from mediaforce.web.runtime_lock import MediaforceRuntimeBusyError

                raise MediaforceRuntimeBusyError(
                    "Legacy SQLite migration intent conflicts with existing state"
                ) from exc
            return
        os.fsync(directory_descriptor)
        descriptor_info = os.fstat(descriptor)
        path_info = os.stat(
            intent_name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (
            (descriptor_info.st_dev, descriptor_info.st_ino)
            != (path_info.st_dev, path_info.st_ino)
            or _load_legacy_sqlite_migration_intent_at(
                directory_descriptor,
                intent_name,
            ) != payload
        ):
            raise OSError("legacy SQLite migration intent restoration failed")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _replace_legacy_sqlite_migration_intent(
        intent_path: Path,
        *,
        expected: dict[str, object],
        payload: dict[str, object],
        before_exchange: Callable[[], None] | None = None,
) -> None:
    directory_descriptor = -1
    expected_descriptor = -1
    payload_descriptor = -1
    transition_path = _legacy_sqlite_migration_intent_transition_path(
        intent_path,
        expected=expected,
        payload=payload,
    )
    try:
        _, directory_descriptor = open_stable_directory(
            intent_path.parent
        )
        directory_info = os.fstat(directory_descriptor)
        expected_parent_identity = expected.get("destination_parent_identity")
        if (
            not isinstance(expected_parent_identity, list)
            or len(expected_parent_identity) != 2
            or any(type(value) is not int for value in expected_parent_identity)
            or payload.get("destination_parent_identity")
            != expected_parent_identity
            or expected_parent_identity
            != [directory_info.st_dev, directory_info.st_ino]
        ):
            raise OSError("legacy SQLite migration destination parent changed")
        _write_legacy_sqlite_migration_intent_at(
            directory_descriptor=directory_descriptor,
            intent_name=transition_path.name,
            payload=payload,
        )
        expected_descriptor = _open_legacy_sqlite_migration_intent_descriptor_at(
            directory_descriptor,
            intent_path.name,
        )
        payload_descriptor = _open_legacy_sqlite_migration_intent_descriptor_at(
            directory_descriptor,
            transition_path.name,
        )
        current_payload = _load_legacy_sqlite_migration_intent_descriptor(
            expected_descriptor
        )
        transition_payload = _load_legacy_sqlite_migration_intent_descriptor(
            payload_descriptor
        )
        if current_payload == payload and transition_payload == expected:
            _assert_legacy_sqlite_migration_intent_descriptor_at(
                directory_descriptor,
                intent_path.name,
                payload_descriptor,
            )
            _assert_legacy_sqlite_migration_intent_descriptor_at(
                directory_descriptor,
                transition_path.name,
                expected_descriptor,
            )
            return
        if current_payload != expected or transition_payload != payload:
            raise OSError(
                "legacy SQLite migration intent changed before finalization"
            )
        _assert_legacy_sqlite_migration_intent_descriptor_at(
            directory_descriptor,
            intent_path.name,
            expected_descriptor,
        )
        _assert_legacy_sqlite_migration_intent_descriptor_at(
            directory_descriptor,
            transition_path.name,
            payload_descriptor,
        )
        if before_exchange is not None:
            before_exchange()
            _assert_legacy_sqlite_migration_intent_descriptor_at(
                directory_descriptor,
                intent_path.name,
                expected_descriptor,
            )
            _assert_legacy_sqlite_migration_intent_descriptor_at(
                directory_descriptor,
                transition_path.name,
                payload_descriptor,
            )
            if (
                _load_legacy_sqlite_migration_intent_at(
                    directory_descriptor,
                    intent_path.name,
                ) != expected
                or _load_legacy_sqlite_migration_intent_at(
                    directory_descriptor,
                    transition_path.name,
                ) != payload
            ):
                raise OSError(
                    "legacy SQLite migration intent changed before finalization"
                )
        rename_exchange(
            first_directory_descriptor=directory_descriptor,
            first_name=intent_path.name,
            second_directory_descriptor=directory_descriptor,
            second_name=transition_path.name,
        )
        os.fsync(directory_descriptor)
        _assert_legacy_sqlite_migration_intent_descriptor_at(
            directory_descriptor,
            intent_path.name,
            payload_descriptor,
        )
        _assert_legacy_sqlite_migration_intent_descriptor_at(
            directory_descriptor,
            transition_path.name,
            expected_descriptor,
        )
        if (
            _load_legacy_sqlite_migration_intent_at(
                directory_descriptor,
                intent_path.name,
            ) != payload
            or _load_legacy_sqlite_migration_intent_at(
                directory_descriptor,
                transition_path.name,
            ) != expected
        ):
            raise OSError("legacy SQLite migration intent finalization failed")
    except FileIntegrityError as exc:
        raise OSError("legacy SQLite migration intent parent is unsafe") from exc
    finally:
        if expected_descriptor >= 0:
            os.close(expected_descriptor)
        if payload_descriptor >= 0:
            os.close(payload_descriptor)
        if directory_descriptor >= 0:
            os.close(directory_descriptor)


def _load_legacy_sqlite_migration_intent(
        intent_path: Path,
) -> dict[str, object]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(intent_path, flags)
    try:
        descriptor_info = os.fstat(descriptor)
        path_info = intent_path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(descriptor_info.st_mode)
            or not stat.S_ISREG(path_info.st_mode)
            or descriptor_info.st_uid != os.getuid()
            or stat.S_IMODE(descriptor_info.st_mode) != 0o400
            or descriptor_info.st_nlink != 1
            or (
                descriptor_info.st_dev,
                descriptor_info.st_ino,
            )
            != (path_info.st_dev, path_info.st_ino)
        ):
            raise OSError("legacy SQLite migration intent is unsafe")
        raw = bytearray()
        while len(raw) <= 64 * 1024:
            chunk = os.read(descriptor, 16 * 1024)
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > 64 * 1024:
            raise OSError("legacy SQLite migration intent is oversized")
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OSError("legacy SQLite migration intent is invalid") from exc
    if not isinstance(payload, dict) or canonical_json_bytes(payload) != bytes(raw):
        raise OSError("legacy SQLite migration intent is invalid")
    return payload


def _load_legacy_sqlite_migration_intent_at(
        directory_descriptor: int,
        intent_name: str,
) -> dict[str, object]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(
        intent_name,
        flags,
        dir_fd=directory_descriptor,
    )
    try:
        descriptor_info = os.fstat(descriptor)
        path_info = os.stat(
            intent_name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(descriptor_info.st_mode)
            or not stat.S_ISREG(path_info.st_mode)
            or descriptor_info.st_uid != os.getuid()
            or stat.S_IMODE(descriptor_info.st_mode) != 0o400
            or descriptor_info.st_nlink != 1
            or (
                descriptor_info.st_dev,
                descriptor_info.st_ino,
            )
            != (path_info.st_dev, path_info.st_ino)
        ):
            raise OSError("legacy SQLite migration intent is unsafe")
        raw = bytearray()
        while len(raw) <= 64 * 1024:
            chunk = os.read(descriptor, 16 * 1024)
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > 64 * 1024:
            raise OSError("legacy SQLite migration intent is oversized")
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OSError("legacy SQLite migration intent is invalid") from exc
    if not isinstance(payload, dict) or canonical_json_bytes(payload) != bytes(raw):
        raise OSError("legacy SQLite migration intent is invalid")
    return payload


def _legacy_sqlite_migration_transition_digests(
        *,
        intent_name: str,
        transition_name: str,
) -> tuple[str, str] | None:
    prefix = f"{intent_name}.transition-"
    suffix = ".json"
    if not transition_name.startswith(prefix) or not transition_name.endswith(suffix):
        return None
    body = transition_name[len(prefix):-len(suffix)]
    parts = body.split("-")
    if (
        len(parts) != 2
        or any(len(part) != 64 for part in parts)
        or any(any(character not in "0123456789abcdef" for character in part) for part in parts)
    ):
        raise OSError("legacy SQLite migration intent transition name is invalid")
    return parts[0], parts[1]


def _recover_legacy_sqlite_migration_intent_transitions(
        intent_path: Path,
        *,
        source: Path,
        destination: Path,
) -> tuple[dict[str, object], dict[str, object] | None]:
    for _attempt in range(64):
        current = _load_legacy_sqlite_migration_intent(intent_path)
        _validated_legacy_sqlite_migration_intent(
            current,
            source=source,
            destination=destination,
        )
        current_digest = _legacy_sqlite_migration_payload_digest(current)
        directory_descriptor = -1
        try:
            _, directory_descriptor = open_stable_directory(intent_path.parent)
            completed_edges: dict[str, str] = {}
            completed_predecessors: dict[str, str] = {}
            pending: list[dict[str, object]] = []
            for name in os.listdir(directory_descriptor):
                digests = _legacy_sqlite_migration_transition_digests(
                    intent_name=intent_path.name,
                    transition_name=name,
                )
                if digests is None:
                    continue
                predecessor_digest, successor_digest = digests
                artifact_payload = _load_legacy_sqlite_migration_intent_at(
                    directory_descriptor,
                    name,
                )
                _validated_legacy_sqlite_migration_intent(
                    artifact_payload,
                    source=source,
                    destination=destination,
                    validate_current_source_parent=False,
                )
                artifact_digest = _legacy_sqlite_migration_payload_digest(
                    artifact_payload
                )
                if artifact_digest == successor_digest:
                    if predecessor_digest != current_digest:
                        raise OSError(
                            "legacy SQLite migration intent has an orphaned prepared transition"
                        )
                    pending.append(artifact_payload)
                    continue
                if artifact_digest != predecessor_digest:
                    raise OSError(
                        "legacy SQLite migration intent transition changed"
                    )
                if (
                    predecessor_digest in completed_edges
                    or successor_digest in completed_predecessors
                ):
                    raise OSError(
                        "legacy SQLite migration intent transition history branches"
                    )
                completed_edges[predecessor_digest] = successor_digest
                completed_predecessors[successor_digest] = predecessor_digest
            cursor = current_digest
            visited_edges = 0
            seen: set[str] = set()
            while cursor in completed_predecessors:
                if cursor in seen:
                    raise OSError(
                        "legacy SQLite migration intent transition history cycles"
                    )
                seen.add(cursor)
                cursor = completed_predecessors[cursor]
                visited_edges += 1
            if visited_edges != len(completed_edges) or current_digest in completed_edges:
                raise OSError(
                    "legacy SQLite migration intent transition history is disconnected"
                )
            if len(pending) > 1:
                raise OSError(
                    "legacy SQLite migration intent has conflicting prepared transitions"
                )
        except FileIntegrityError as exc:
            raise OSError(
                "legacy SQLite migration intent parent is unsafe"
            ) from exc
        finally:
            if directory_descriptor >= 0:
                os.close(directory_descriptor)
        if not pending:
            return current, None
        if (
            current.get("phase") in {"cleaning", "sealing"}
            and pending[0].get("phase") == "complete"
        ):
            if current.get("phase") == "sealing":
                expected_completion = _legacy_sqlite_migration_sealed_payload(
                    current
                )
            else:
                expected_completion = (
                    _legacy_sqlite_migration_legacy_completion_payload(current)
                )
            if pending[0] != expected_completion:
                raise OSError(
                    "legacy SQLite migration completion transition is invalid"
                )
            return current, pending[0]
        _replace_legacy_sqlite_migration_intent(
            intent_path,
            expected=current,
            payload=pending[0],
        )
    raise OSError("legacy SQLite migration intent transition chain is too deep")


def _resume_legacy_sqlite_migration_intent(
        config: MediaforceConfig,
        source: Path,
        destination: Path,
        intent_path: Path,
        receipt_path: Path,
) -> bool:
    from mediaforce.web.runtime_lock import (
        MediaforceRuntimeBusyError,
        exclusive_legacy_sqlite_migration_source,
    )

    try:
        payload, prepared_completion_payload = (
            _recover_legacy_sqlite_migration_intent_transitions(
                intent_path,
                source=source,
                destination=destination,
            )
        )
        staging_path = _validated_legacy_sqlite_migration_intent(
            payload,
            source=source,
            destination=destination,
        )
        destination_exists = _path_entry_exists(destination)
        if payload.get("phase") == "copying":
            if destination_exists or not _path_entry_exists(source):
                raise OSError(
                    "legacy SQLite copying intent has an invalid publication state"
                )
            if payload.get("schema_version") == 4:
                payload = _upgrade_legacy_sqlite_v4_intent(
                    intent_path=intent_path,
                    payload=payload,
                    source=source,
                    destination_binding=None,
                    staging_path=staging_path,
                    receipt_path=receipt_path,
                )
            elif payload.get("schema_version") in {2, 3}:
                payload = _upgrade_legacy_sqlite_precleaning_intent(
                    intent_path=intent_path,
                    payload=payload,
                    publication_policy=(
                        _LEGACY_SQLITE_PUBLICATION_EXCLUSIVE
                    ),
                )
            elif payload.get("schema_version") != 5:
                raise OSError(
                    "legacy SQLite copying intent schema is unsupported"
                )
            with exclusive_legacy_sqlite_migration_source(
                    config,
                    source,
            ) as locked_source:
                _assert_legacy_sqlite_migration_source_matches(
                    locked_source,
                    payload,
                )
                if _legacy_sqlite_staging_family_exists(staging_path):
                    staging_path = _reserved_legacy_sqlite_staging_path(
                        destination
                    )
                    replacement_copying_intent = (
                        _legacy_sqlite_migration_intent_payload(
                            locked_source=locked_source,
                            source=source,
                            destination=destination,
                            staging_path=staging_path,
                            staging_snapshot=None,
                        )
                    )
                    _replace_legacy_sqlite_migration_intent(
                        intent_path,
                        expected=payload,
                        payload=replacement_copying_intent,
                    )
                    payload = replacement_copying_intent
                _execute_legacy_sqlite_copy(
                    locked_source=locked_source,
                    source=source,
                    destination=destination,
                    intent_path=intent_path,
                    copying_intent=payload,
                    receipt_path=receipt_path,
                )
            return True
        staging_snapshot = _legacy_sqlite_migration_snapshot_dict(
            payload.get("staging_snapshot"),
            "staging snapshot",
        )
        with _opened_legacy_sqlite_migration_destination(
                destination,
                expected_parent_identity=payload.get(
                    "destination_parent_identity"
                ),
        ) as destination_binding:
            if payload.get("schema_version") == 4:
                payload = _upgrade_legacy_sqlite_v4_intent(
                    intent_path=intent_path,
                    payload=payload,
                    source=source,
                    destination_binding=destination_binding,
                    staging_path=staging_path,
                    receipt_path=receipt_path,
                )
                _validated_legacy_sqlite_migration_intent(
                    payload,
                    source=source,
                    destination=destination,
                )
            destination_exists = destination_binding.entry_exists(
                destination.name
            )
            phase = payload.get("phase")
            if (
                prepared_completion_payload is not None
                or phase in {"sealing", "complete"}
            ):
                if not destination_exists:
                    raise OSError(
                        "completed legacy SQLite migration destination is missing"
                    )
                retained_artifacts = _legacy_sqlite_retained_cleanup_artifacts(
                    payload.get("retained_cleanup_artifacts")
                )

                def assert_source_cleanup_complete() -> None:
                    _assert_recorded_legacy_sqlite_source_cleanup_complete(
                        source,
                        payload=payload,
                        retained_artifacts=retained_artifacts,
                    )

                if phase in {"cleaning", "sealing"}:
                    _finalize_legacy_sqlite_migration(
                        destination_binding=destination_binding,
                        intent_path=intent_path,
                        payload=payload,
                        staging_snapshot=staging_snapshot,
                        assert_source_cleanup_complete=(
                            assert_source_cleanup_complete
                        ),
                        prepared_completion_payload=(
                            prepared_completion_payload
                        ),
                        receipt_path=receipt_path,
                    )
                else:
                    _assert_completed_legacy_sqlite_migration(
                        destination_binding=destination_binding,
                        payload=payload,
                        staging_snapshot=staging_snapshot,
                        assert_source_cleanup_complete=(
                            assert_source_cleanup_complete
                        ),
                        receipt_path=receipt_path,
                    )
                return True
            if not destination_exists:
                if (
                    payload.get("phase") != "ready"
                    or not _path_entry_exists(source)
                    or not destination_binding.entry_exists(staging_path.name)
                ):
                    raise OSError(
                        "legacy SQLite migration intent is missing source or staging state"
                    )
                if payload.get("schema_version") in {2, 3}:
                    payload = _upgrade_legacy_sqlite_precleaning_intent(
                        intent_path=intent_path,
                        payload=payload,
                        publication_policy=(
                            _LEGACY_SQLITE_PUBLICATION_EXCLUSIVE
                        ),
                    )
                if (
                    payload.get("schema_version") != 5
                    or payload.get("publication_policy")
                    != _LEGACY_SQLITE_PUBLICATION_EXCLUSIVE
                ):
                    raise OSError(
                        "legacy SQLite unpublished intent policy is invalid"
                    )
                destination_binding.assert_file_matches(
                    staging_path.name,
                    staging_snapshot,
                    allowed_link_counts={1},
                )
                with exclusive_legacy_sqlite_migration_source(
                        config,
                        source,
                ) as locked_source:
                    with _opened_legacy_sqlite_write_gate(locked_source):
                        _assert_legacy_sqlite_migration_source_matches(
                            locked_source,
                            payload,
                        )
                        destination_binding.publish(
                            staging_path.name,
                            staging_snapshot,
                        )
                        destination_binding.assert_database_valid()
                        cleaning_payload = (
                            _legacy_sqlite_migration_cleaning_intent_payload(
                                payload,
                                locked_source=locked_source,
                            )
                        )
                        _replace_legacy_sqlite_migration_intent(
                            intent_path,
                            expected=payload,
                            payload=cleaning_payload,
                        )
                        _finish_legacy_sqlite_cleanup_with_locked_source(
                            locked_source=locked_source,
                            destination_binding=destination_binding,
                            intent_path=intent_path,
                            payload=cleaning_payload,
                            staging_snapshot=staging_snapshot,
                            receipt_path=receipt_path,
                        )
                return True
            if payload.get("schema_version") in {2, 3}:
                publication_policy = (
                    _legacy_sqlite_publication_policy_for_state(
                        destination_binding,
                        staging_path.name,
                        staging_snapshot,
                    )
                )
            else:
                publication_policy = payload.get("publication_policy")
                _assert_legacy_sqlite_v5_publication_state(
                    destination_binding,
                    staging_name=staging_path.name,
                    staging_snapshot=staging_snapshot,
                    publication_policy=publication_policy,
                )
            destination_binding.assert_database_valid()
            if _path_entry_exists(source):
                with exclusive_legacy_sqlite_migration_source(
                        config,
                        source,
                ) as locked_source:
                    with _opened_legacy_sqlite_write_gate(locked_source):
                        _assert_legacy_sqlite_migration_source_matches(
                            locked_source,
                            payload,
                        )
                        cleaning_payload = (
                            _legacy_sqlite_migration_cleaning_intent_payload(
                                payload,
                                locked_source=locked_source,
                                publication_policy=str(publication_policy),
                            )
                        )
                        if cleaning_payload != payload:
                            _replace_legacy_sqlite_migration_intent(
                                intent_path,
                                expected=payload,
                                payload=cleaning_payload,
                            )
                        _finish_legacy_sqlite_cleanup_with_locked_source(
                            locked_source=locked_source,
                            destination_binding=destination_binding,
                            intent_path=intent_path,
                            payload=cleaning_payload,
                            staging_snapshot=staging_snapshot,
                            receipt_path=receipt_path,
                        )
                return True
            if (
                (
                    payload.get("schema_version") == 2
                    and payload.get("phase") == "ready"
                )
                or (
                    payload.get("schema_version") == 3
                    and payload.get("phase") == "cleaning"
                )
            ):
                payload = _upgrade_digestless_legacy_sqlite_cleanup_intent(
                    intent_path=intent_path,
                    payload=payload,
                    source=source,
                    publication_policy=str(publication_policy),
                    receipt_path=receipt_path,
                )
            if (
                payload.get("schema_version") != 5
                or payload.get("phase") != "cleaning"
            ):
                raise OSError(
                    "legacy SQLite migration source disappeared before cleanup was authorized"
                )
            _assert_legacy_sqlite_v5_publication_state(
                destination_binding,
                staging_name=staging_path.name,
                staging_snapshot=staging_snapshot,
                publication_policy=payload.get("publication_policy"),
            )
            retained_artifacts = _legacy_sqlite_retained_cleanup_artifacts(
                payload.get("retained_cleanup_artifacts")
            )

            def assert_destination_authoritative() -> None:
                _assert_legacy_sqlite_v5_publication_state(
                    destination_binding,
                    staging_name=staging_path.name,
                    staging_snapshot=staging_snapshot,
                    publication_policy=payload.get("publication_policy"),
                )

            if payload.get("cleanup_policy") == _LEGACY_SQLITE_CLEANUP_RETAIN:
                if _path_entry_exists(source.parent):
                    retained_artifacts = _complete_legacy_sqlite_source_cleanup(
                        source,
                        expected_parent_identity=payload.get(
                            "source_parent_identity"
                        ),
                        expected_main_snapshot=payload.get(
                            "source_main_snapshot"
                        ),
                        expected_main_sha256=payload.get(
                            "source_main_sha256"
                        ),
                        expected_sidecar_snapshots=payload.get(
                            "source_sidecar_snapshots"
                        ),
                        before_remove=assert_destination_authoritative,
                        allowed_absent_suffixes=(
                            _legacy_sqlite_cleanup_absent_prefix(
                                payload.get("legacy_cleanup_absent_prefix")
                            )
                        ),
                        expected_retained_artifacts=retained_artifacts,
                    )
                elif not _legacy_sqlite_source_cleanup_recorded_all_absent(
                        payload,
                        retained_artifacts=retained_artifacts,
                ):
                    raise OSError(
                        "legacy SQLite migration source parent disappeared"
                    )
            elif payload.get("cleanup_policy") == _LEGACY_SQLITE_CLEANUP_PRESERVE:
                _assert_legacy_sqlite_source_cleanup_complete(
                    source,
                    expected_parent_identity=payload.get(
                        "source_parent_identity"
                    ),
                    allowed_cleanup_artifacts=retained_artifacts,
                )
            else:
                raise OSError(
                    "legacy SQLite migration cleanup policy is invalid"
                )
            if retained_artifacts:
                LOGGER.warning(
                    "Retained legacy SQLite cleanup artifacts after safe migration: %s",
                    ", ".join(
                        artifact.name
                        for artifact in retained_artifacts
                    ),
                )

            def assert_source_cleanup_complete() -> None:
                _assert_recorded_legacy_sqlite_source_cleanup_complete(
                    source,
                    payload=payload,
                    retained_artifacts=retained_artifacts,
                )

            _complete_legacy_sqlite_migration_without_live_source(
                destination_binding=destination_binding,
                intent_path=intent_path,
                payload=payload,
                staging_snapshot=staging_snapshot,
                assert_source_cleanup_complete=assert_source_cleanup_complete,
                receipt_path=receipt_path,
            )
            return True
    except MediaforceRuntimeBusyError:
        raise
    except (OSError, RuntimeError, sqlite3.DatabaseError, TypeError, ValueError) as exc:
        raise MediaforceRuntimeBusyError(
            "Legacy SQLite migration intent could not be resumed safely"
        ) from exc


def _assert_completed_legacy_sqlite_migration(
        *,
        destination_binding: _LegacySQLiteMigrationDestination,
        payload: dict[str, object],
        staging_snapshot: dict[str, object],
        assert_source_cleanup_complete: Callable[[], None],
        receipt_path: Path,
) -> None:
    assert_source_cleanup_complete()
    _assert_legacy_sqlite_v5_publication_state(
        destination_binding,
        staging_name=str(payload["staging_name"]),
        staging_snapshot=staging_snapshot,
        publication_policy=payload.get("publication_policy"),
        require_exact_bytes=False,
        require_sidecars_absent=False,
    )
    destination_binding.assert_database_sidecars_safe()
    destination_binding.assert_database_valid()
    _ensure_legacy_sqlite_migration_receipt(receipt_path, payload)
    assert_source_cleanup_complete()
    _assert_legacy_sqlite_v5_publication_state(
        destination_binding,
        staging_name=str(payload["staging_name"]),
        staging_snapshot=staging_snapshot,
        publication_policy=payload.get("publication_policy"),
        require_exact_bytes=False,
        require_sidecars_absent=False,
    )
    destination_binding.assert_database_sidecars_safe()


def _finalize_legacy_sqlite_migration(
        *,
        destination_binding: _LegacySQLiteMigrationDestination,
        intent_path: Path,
        payload: dict[str, object],
        staging_snapshot: dict[str, object],
        assert_source_cleanup_complete: Callable[[], None],
        receipt_path: Path,
        prepared_completion_payload: dict[str, object] | None = None,
) -> None:
    def assert_sealable() -> None:
        assert_source_cleanup_complete()
        _assert_legacy_sqlite_v5_publication_state(
            destination_binding,
            staging_name=str(payload["staging_name"]),
            staging_snapshot=staging_snapshot,
            publication_policy=payload.get("publication_policy"),
        )
        destination_binding.assert_database_valid()
        _ensure_legacy_sqlite_migration_receipt(receipt_path, payload)
        assert_source_cleanup_complete()
        _assert_legacy_sqlite_v5_publication_state(
            destination_binding,
            staging_name=str(payload["staging_name"]),
            staging_snapshot=staging_snapshot,
            publication_policy=payload.get("publication_policy"),
        )

    phase = payload.get("phase")
    if prepared_completion_payload is not None:
        if phase == "cleaning":
            expected_completion = (
                _legacy_sqlite_migration_legacy_completion_payload(payload)
            )
        elif phase == "sealing":
            expected_completion = _legacy_sqlite_migration_sealed_payload(
                payload
            )
        else:
            raise OSError(
                "legacy SQLite migration prepared completion state is invalid"
            )
        if prepared_completion_payload != expected_completion:
            raise OSError(
                "legacy SQLite migration completion transition is invalid"
            )
        if phase == "cleaning":
            assert_sealable()
            _replace_legacy_sqlite_migration_intent(
                intent_path,
                expected=payload,
                payload=prepared_completion_payload,
                before_exchange=assert_sealable,
            )
            _assert_completed_legacy_sqlite_migration(
                destination_binding=destination_binding,
                payload=prepared_completion_payload,
                staging_snapshot=staging_snapshot,
                assert_source_cleanup_complete=assert_source_cleanup_complete,
                receipt_path=receipt_path,
            )
            return
    if phase == "cleaning":
        assert_sealable()
        sealing_payload = _legacy_sqlite_migration_completion_payload(payload)
        _replace_legacy_sqlite_migration_intent(
            intent_path,
            expected=payload,
            payload=sealing_payload,
            before_exchange=assert_sealable,
        )
    elif phase == "sealing":
        sealing_payload = payload
    else:
        raise OSError("legacy SQLite migration intent cannot be finalized")

    assert_sealable()
    completed_payload = _legacy_sqlite_migration_sealed_payload(sealing_payload)
    _replace_legacy_sqlite_migration_intent(
        intent_path,
        expected=sealing_payload,
        payload=completed_payload,
        before_exchange=assert_sealable,
    )
    _assert_completed_legacy_sqlite_migration(
        destination_binding=destination_binding,
        payload=completed_payload,
        staging_snapshot=staging_snapshot,
        assert_source_cleanup_complete=assert_source_cleanup_complete,
        receipt_path=receipt_path,
    )


def _complete_legacy_sqlite_migration_without_live_source(
        *,
        destination_binding: _LegacySQLiteMigrationDestination,
        intent_path: Path,
        payload: dict[str, object],
        staging_snapshot: dict[str, object],
        assert_source_cleanup_complete: Callable[[], None],
        receipt_path: Path,
) -> None:
    _finalize_legacy_sqlite_migration(
        destination_binding=destination_binding,
        intent_path=intent_path,
        payload=payload,
        staging_snapshot=staging_snapshot,
        assert_source_cleanup_complete=assert_source_cleanup_complete,
        receipt_path=receipt_path,
    )


def _validated_legacy_sqlite_migration_intent(
        payload: dict[str, object],
        *,
        source: Path,
        destination: Path,
        validate_current_source_parent: bool = True,
) -> Path:
    base_keys = {
        "schema",
        "schema_version",
        "phase",
        "source_path",
        "destination_path",
        "staging_name",
        "source_parent_identity",
        "source_main_snapshot",
        "staging_snapshot",
        "destination_parent_identity",
    }
    schema_version = payload.get("schema_version")
    if schema_version == 2:
        expected_keys = base_keys
        allowed_phases = {"copying", "ready"}
    elif schema_version == 3:
        expected_keys = base_keys | {
            "source_sidecar_snapshots",
        }
        allowed_phases = {"copying", "ready", "cleaning"}
    elif schema_version == 4:
        expected_keys = base_keys | {
            "source_main_sha256",
            "source_sidecar_snapshots",
        }
        allowed_phases = {"copying", "ready", "cleaning"}
    elif schema_version == 5:
        expected_keys = base_keys | {
            "source_main_sha256",
            "source_sidecar_snapshots",
            "cleanup_policy",
            "publication_policy",
            "legacy_cleanup_absent_prefix",
            "retained_cleanup_artifacts",
        }
        allowed_phases = {
            "copying",
            "ready",
            "cleaning",
            "sealing",
            "complete",
        }
    else:
        raise OSError("legacy SQLite migration intent schema is unsupported")
    if (
        set(payload) != expected_keys
        or payload.get("schema") != _LEGACY_SQLITE_INTENT_SCHEMA
        or payload.get("phase") not in allowed_phases
        or payload.get("source_path")
        != os.fspath(_path_without_resolution(source))
        or payload.get("destination_path")
        != os.fspath(_path_without_resolution(destination))
    ):
        raise OSError("legacy SQLite migration intent does not match configured paths")
    staging_name = payload.get("staging_name")
    if (
        not isinstance(staging_name, str)
        or Path(staging_name).name != staging_name
        or not staging_name.startswith(f".{destination.name}.migration-")
        or not staging_name.endswith(".sqlite3")
    ):
        raise OSError("legacy SQLite migration intent staging path is invalid")
    source_parent_identity = payload.get("source_parent_identity")
    if (
        not isinstance(source_parent_identity, list)
        or len(source_parent_identity) != 2
        or any(type(value) is not int for value in source_parent_identity)
    ):
        raise OSError("legacy SQLite migration source parent identity is invalid")
    destination_parent_info = destination.parent.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(destination_parent_info.st_mode)
        or payload.get("destination_parent_identity")
        != [destination_parent_info.st_dev, destination_parent_info.st_ino]
    ):
        raise OSError("legacy SQLite migration destination parent changed")
    if validate_current_source_parent:
        try:
            source_parent_info = source.parent.stat(follow_symlinks=False)
        except FileNotFoundError:
            source_parent_info = None
        if source_parent_info is None:
            if (
                schema_version != 5
                or payload.get("phase")
                not in {"cleaning", "sealing", "complete"}
            ):
                raise OSError("legacy SQLite migration source parent disappeared")
        elif (
            not stat.S_ISDIR(source_parent_info.st_mode)
            or source_parent_identity
            != [source_parent_info.st_dev, source_parent_info.st_ino]
        ):
            raise OSError("legacy SQLite migration source parent changed")
    if (
        payload.get("phase") == "copying"
        and payload.get("staging_snapshot") is not None
    ):
        raise OSError("legacy SQLite copying intent is invalid")
    if (
        payload.get("phase") in {"ready", "cleaning", "sealing", "complete"}
        and not isinstance(payload.get("staging_snapshot"), dict)
    ):
        raise OSError("legacy SQLite ready intent is invalid")
    if schema_version == 3:
        sidecar_snapshots = payload.get("source_sidecar_snapshots")
        if payload.get("phase") == "cleaning":
            _legacy_sqlite_migration_sidecar_snapshots(
                sidecar_snapshots,
                require_sha256=False,
            )
        elif sidecar_snapshots is not None:
            raise OSError("legacy SQLite migration sidecar snapshots are invalid")
    elif schema_version == 4:
        source_main_sha256 = payload.get("source_main_sha256")
        sidecar_snapshots = payload.get("source_sidecar_snapshots")
        if payload.get("phase") == "cleaning":
            if not _legacy_sqlite_migration_sha256_is_valid(
                    source_main_sha256
            ):
                raise OSError(
                    "legacy SQLite migration source digest is invalid"
                )
            _legacy_sqlite_migration_sidecar_snapshots(sidecar_snapshots)
        elif source_main_sha256 is not None or sidecar_snapshots is not None:
            raise OSError("legacy SQLite migration sidecar snapshots are invalid")
    elif schema_version == 5:
        phase = payload.get("phase")
        cleanup_policy = payload.get("cleanup_policy")
        publication_policy = payload.get("publication_policy")
        absent_prefix = _legacy_sqlite_cleanup_absent_prefix(
            payload.get("legacy_cleanup_absent_prefix")
        )
        if publication_policy not in {
            _LEGACY_SQLITE_PUBLICATION_EXCLUSIVE,
            _LEGACY_SQLITE_PUBLICATION_LEGACY_ALIAS,
            _LEGACY_SQLITE_PUBLICATION_LEGACY_NO_ALIAS,
        }:
            raise OSError("legacy SQLite migration publication policy is invalid")
        if phase in {"copying", "ready"}:
            if (
                cleanup_policy != _LEGACY_SQLITE_CLEANUP_RETAIN
                or absent_prefix
                or payload.get("source_main_sha256") is not None
                or payload.get("source_sidecar_snapshots") is not None
                or payload.get("retained_cleanup_artifacts") is not None
            ):
                raise OSError("legacy SQLite migration cleanup policy is invalid")
        elif cleanup_policy == _LEGACY_SQLITE_CLEANUP_RETAIN:
            if not _legacy_sqlite_migration_sha256_is_valid(
                    payload.get("source_main_sha256")
            ):
                raise OSError(
                    "legacy SQLite migration source digest is invalid"
                )
            sidecar_snapshots = _legacy_sqlite_migration_sidecar_snapshots(
                payload.get("source_sidecar_snapshots")
            )
            retained_artifacts = _legacy_sqlite_retained_cleanup_artifacts(
                payload.get("retained_cleanup_artifacts")
            )
            expected_artifacts = _legacy_sqlite_expected_cleanup_artifacts(
                source_name=source.name,
                expected_main_snapshot=payload.get("source_main_snapshot"),
                expected_main_sha256=payload.get("source_main_sha256"),
                expected_sidecar_snapshots=sidecar_snapshots,
                absent_prefix=absent_prefix,
            )
            if retained_artifacts != expected_artifacts:
                raise OSError(
                    "legacy SQLite migration retained cleanup manifest is invalid"
                )
        elif cleanup_policy == _LEGACY_SQLITE_CLEANUP_PRESERVE:
            if (
                absent_prefix
                or payload.get("source_main_sha256") is not None
                or payload.get("source_sidecar_snapshots") is not None
            ):
                raise OSError("legacy SQLite migration cleanup policy is invalid")
            _legacy_sqlite_retained_cleanup_artifacts(
                payload.get("retained_cleanup_artifacts")
            )
        else:
            raise OSError("legacy SQLite migration cleanup policy is invalid")
    return destination.parent / staging_name


def _legacy_sqlite_publication_policy_for_state(
        destination_binding: _LegacySQLiteMigrationDestination,
        staging_name: str,
        staging_snapshot: dict[str, object],
) -> str:
    destination_exists = destination_binding.entry_exists(
        destination_binding.path.name
    )
    staging_exists = destination_binding.entry_exists(staging_name)
    if not destination_exists:
        if not staging_exists:
            raise OSError(
                "legacy SQLite migration is missing its unpublished staging file"
            )
        destination_binding.assert_file_matches(
            staging_name,
            staging_snapshot,
            allowed_link_counts={1},
        )
        return _LEGACY_SQLITE_PUBLICATION_EXCLUSIVE
    if staging_exists:
        destination_binding.assert_retained_staging(
            staging_name,
            staging_snapshot,
        )
        return _LEGACY_SQLITE_PUBLICATION_LEGACY_ALIAS
    destination_binding.assert_file_matches(
        destination_binding.path.name,
        staging_snapshot,
        allowed_link_counts={1},
    )
    return _LEGACY_SQLITE_PUBLICATION_LEGACY_NO_ALIAS


def _assert_legacy_sqlite_v5_publication_state(
        destination_binding: _LegacySQLiteMigrationDestination,
        *,
        staging_name: str,
        staging_snapshot: dict[str, object],
        publication_policy: object,
        require_exact_bytes: bool = True,
        require_sidecars_absent: bool = True,
) -> None:
    if require_sidecars_absent:
        destination_binding.assert_database_sidecars_absent()

    def assert_identity(name: str, *, allowed_link_counts: set[int]) -> None:
        if require_exact_bytes:
            destination_binding.assert_file_matches(
                name,
                staging_snapshot,
                allowed_link_counts=allowed_link_counts,
            )
        else:
            destination_binding.assert_file_identity(
                name,
                staging_snapshot,
                allowed_link_counts=allowed_link_counts,
            )

    if publication_policy == _LEGACY_SQLITE_PUBLICATION_EXCLUSIVE:
        if destination_binding.entry_exists(staging_name):
            raise OSError(
                "legacy SQLite exclusive publication retained a staging entry"
            )
        assert_identity(
            destination_binding.path.name,
            allowed_link_counts={1},
        )
    elif publication_policy == _LEGACY_SQLITE_PUBLICATION_LEGACY_ALIAS:
        if require_exact_bytes:
            destination_binding.assert_retained_staging(
                staging_name,
                staging_snapshot,
            )
        else:
            assert_identity(staging_name, allowed_link_counts={2})
            assert_identity(
                destination_binding.path.name,
                allowed_link_counts={2},
            )
    elif publication_policy == _LEGACY_SQLITE_PUBLICATION_LEGACY_NO_ALIAS:
        if destination_binding.entry_exists(staging_name):
            raise OSError(
                "legacy SQLite migration staging alias reappeared"
            )
        assert_identity(
            destination_binding.path.name,
            allowed_link_counts={1},
        )
    else:
        raise OSError("legacy SQLite migration publication policy is invalid")
    if require_sidecars_absent:
        destination_binding.assert_database_sidecars_absent()


def _legacy_sqlite_v4_cleanup_absent_prefix(
        source: Path,
        payload: dict[str, object],
) -> tuple[tuple[str, ...], bool]:
    expected_main_snapshot = payload.get("source_main_snapshot")
    expected_main_sha256 = payload.get("source_main_sha256")
    if (
        not isinstance(expected_main_snapshot, list)
        or len(expected_main_snapshot) != 6
        or any(type(value) is not int for value in expected_main_snapshot)
        or not _legacy_sqlite_migration_sha256_is_valid(expected_main_sha256)
    ):
        raise OSError("legacy SQLite migration source cleanup manifest is invalid")
    sidecar_snapshots = _legacy_sqlite_migration_sidecar_snapshots(
        payload.get("source_sidecar_snapshots")
    )
    expected_entries: list[
        tuple[str, tuple[int, ...], str, str]
    ] = []
    for suffix in _LEGACY_SQLITE_CLEANUP_SUFFIXES:
        name = f"{source.name}{suffix}"
        if suffix:
            snapshot = _legacy_sqlite_cleanup_sidecar_snapshot(
                sidecar_snapshots[suffix]
            )
            sha256 = str(sidecar_snapshots[suffix]["sha256"])
        else:
            snapshot = tuple(expected_main_snapshot)
            sha256 = str(expected_main_sha256)
        expected_entries.append((
            name,
            snapshot,
            sha256,
            legacy_sqlite_migration_quarantine_name(
                name,
                device=snapshot[0],
                inode=snapshot[1],
                size=snapshot[2],
                mtime_ns=snapshot[3],
            ),
        ))
    directory_descriptor = -1
    try:
        _, directory_descriptor = open_stable_directory(source.parent)
        directory_info = os.fstat(directory_descriptor)
        if payload.get("source_parent_identity") != [
            directory_info.st_dev,
            directory_info.st_ino,
        ]:
            raise OSError("legacy SQLite migration source parent changed")
        states: list[str] = []
        expected_namespace: set[str] = set()
        for name, snapshot, sha256, quarantine_name in expected_entries:
            live_info = _legacy_sqlite_directory_entry_info(
                directory_descriptor,
                name,
            )
            quarantine_info = _legacy_sqlite_directory_entry_info(
                directory_descriptor,
                quarantine_name,
            )
            if live_info is not None and quarantine_info is not None:
                raise OSError(
                    "legacy SQLite migration cleanup has conflicting entries"
                )
            if live_info is not None:
                if (
                    not _legacy_sqlite_cleanup_live_snapshot_matches(
                        live_info,
                        snapshot,
                    )
                    or _legacy_sqlite_migration_file_snapshot_at(
                        directory_descriptor,
                        name,
                        include_sha256=True,
                        require_single_link=True,
                    ).get("sha256") != sha256
                ):
                    raise OSError(
                        "legacy SQLite migration source cleanup is unsafe"
                    )
                states.append("live")
                expected_namespace.add(name)
            elif quarantine_info is not None:
                artifact = _LegacySQLiteRetainedCleanupArtifact(
                    name=quarantine_name,
                    expected_snapshot=snapshot,
                    expected_sha256=sha256,
                )
                if not _legacy_sqlite_retained_cleanup_artifact_matches(
                        directory_descriptor,
                        artifact,
                ):
                    raise OSError(
                        "legacy SQLite migration cleanup quarantine is unsafe"
                    )
                states.append("quarantine")
                expected_namespace.add(quarantine_name)
            else:
                states.append("absent")
        names = set(os.listdir(directory_descriptor))
        cleanup_namespace = {
            name
            for name in names
            if name == source.name
            or name.startswith(f".{source.name}.mediaforce-retired-")
            or any(
                name == f"{source.name}{suffix}"
                or name.startswith(
                    f".{source.name}{suffix}.mediaforce-retired-"
                )
                for suffix in ("-wal", "-shm", "-journal")
            )
        }
        if cleanup_namespace != expected_namespace:
            raise OSError(
                "legacy SQLite migration cleanup contains unidentified artifacts"
            )
        absent_count = 0
        while absent_count < len(states) and states[absent_count] == "absent":
            absent_count += 1
        if absent_count:
            remainder = states[absent_count:]
            if remainder[:1] == ["quarantine"]:
                remainder = remainder[1:]
            if any(state != "live" for state in remainder):
                raise OSError(
                    "legacy SQLite migration cleanup order is invalid"
                )
            _assert_legacy_sqlite_migration_source_parent_stable(
                source,
                payload.get("source_parent_identity"),
            )
            return (
                _LEGACY_SQLITE_CLEANUP_SUFFIXES[:absent_count],
                absent_count == len(states),
            )
        quarantine_count = 0
        while (
            quarantine_count < len(states)
            and states[quarantine_count] == "quarantine"
        ):
            quarantine_count += 1
        if any(state != "live" for state in states[quarantine_count:]):
            raise OSError("legacy SQLite migration cleanup order is invalid")
        _assert_legacy_sqlite_migration_source_parent_stable(
            source,
            payload.get("source_parent_identity"),
        )
        return (), quarantine_count == len(states)
    except FileIntegrityError as exc:
        raise OSError("legacy SQLite migration source parent is unsafe") from exc
    finally:
        if directory_descriptor >= 0:
            os.close(directory_descriptor)


def _upgrade_legacy_sqlite_v4_intent(
        *,
        intent_path: Path,
        payload: dict[str, object],
        source: Path,
        destination_binding: _LegacySQLiteMigrationDestination | None,
        staging_path: Path,
        receipt_path: Path,
) -> dict[str, object]:
    phase = payload.get("phase")
    upgraded = dict(payload)
    upgraded["schema_version"] = _LEGACY_SQLITE_INTENT_VERSION
    upgraded["cleanup_policy"] = _LEGACY_SQLITE_CLEANUP_RETAIN
    upgraded["legacy_cleanup_absent_prefix"] = []
    upgraded["retained_cleanup_artifacts"] = None
    if phase == "copying":
        upgraded["publication_policy"] = (
            _LEGACY_SQLITE_PUBLICATION_EXCLUSIVE
        )
    else:
        if destination_binding is None:
            raise OSError(
                "legacy SQLite migration destination binding is unavailable"
            )
        staging_snapshot = _legacy_sqlite_migration_snapshot_dict(
            payload.get("staging_snapshot"),
            "staging snapshot",
        )
        publication_policy = (
            _legacy_sqlite_publication_policy_for_state(
                destination_binding,
                staging_path.name,
                staging_snapshot,
            )
        )
        upgraded["publication_policy"] = publication_policy
        if (
            phase == "ready"
            and destination_binding.entry_exists(destination_binding.path.name)
            and publication_policy != _LEGACY_SQLITE_PUBLICATION_LEGACY_ALIAS
        ):
            raise OSError(
                "legacy SQLite v4 ready publication state is invalid"
            )
        if phase == "cleaning":
            absent_prefix, cleanup_complete = (
                _legacy_sqlite_v4_cleanup_absent_prefix(
                source,
                payload,
            )
            )
            if (
                not cleanup_complete
                and publication_policy
                != _LEGACY_SQLITE_PUBLICATION_LEGACY_ALIAS
            ):
                raise OSError(
                    "legacy SQLite v4 cleanup publication order is invalid"
                )
            upgraded["legacy_cleanup_absent_prefix"] = list(absent_prefix)
            upgraded["retained_cleanup_artifacts"] = (
                _legacy_sqlite_expected_cleanup_artifact_payloads(
                    source_name=source.name,
                    expected_main_snapshot=payload.get("source_main_snapshot"),
                    expected_main_sha256=payload.get("source_main_sha256"),
                    expected_sidecar_snapshots=payload.get(
                        "source_sidecar_snapshots"
                    ),
                    absent_prefix=absent_prefix,
                )
            )
            _ensure_legacy_sqlite_migration_receipt(
                receipt_path,
                upgraded,
            )
    _replace_legacy_sqlite_migration_intent(
        intent_path,
        expected=payload,
        payload=upgraded,
    )
    return upgraded


def _upgrade_legacy_sqlite_precleaning_intent(
        *,
        intent_path: Path,
        payload: dict[str, object],
        publication_policy: str,
) -> dict[str, object]:
    if payload.get("phase") not in {"copying", "ready"}:
        raise OSError("legacy SQLite migration intent phase is not upgradeable")
    upgraded = dict(payload)
    upgraded["schema_version"] = _LEGACY_SQLITE_INTENT_VERSION
    upgraded["source_main_sha256"] = None
    upgraded["source_sidecar_snapshots"] = None
    upgraded["cleanup_policy"] = _LEGACY_SQLITE_CLEANUP_RETAIN
    upgraded["publication_policy"] = publication_policy
    upgraded["legacy_cleanup_absent_prefix"] = []
    upgraded["retained_cleanup_artifacts"] = None
    _replace_legacy_sqlite_migration_intent(
        intent_path,
        expected=payload,
        payload=upgraded,
    )
    return upgraded


def _legacy_sqlite_staging_family_exists(staging_path: Path) -> bool:
    return any(
        _path_entry_exists(Path(f"{staging_path}{suffix}"))
        for suffix in ("", "-wal", "-shm", "-journal")
    )


def _record_legacy_sqlite_cleanup_artifacts(
        source: Path,
        *,
        expected_parent_identity: object,
        expected_main_snapshot: object,
) -> tuple[_LegacySQLiteRetainedCleanupArtifact, ...]:
    if (
        not isinstance(expected_main_snapshot, list)
        or len(expected_main_snapshot) != 6
        or any(type(value) is not int for value in expected_main_snapshot)
    ):
        raise OSError("legacy SQLite migration source snapshot is invalid")
    directory_descriptor = -1
    try:
        _, directory_descriptor = open_stable_directory(source.parent)
        directory_info = os.fstat(directory_descriptor)
        if expected_parent_identity != [
            directory_info.st_dev,
            directory_info.st_ino,
        ]:
            raise OSError("legacy SQLite migration source parent changed")
        names = set(os.listdir(directory_descriptor))
        if source.name in names:
            raise OSError("legacy SQLite migration source cleanup is incomplete")
        main_snapshot = tuple(expected_main_snapshot)
        main_quarantine_name = legacy_sqlite_migration_quarantine_name(
            source.name,
            device=main_snapshot[0],
            inode=main_snapshot[1],
            size=main_snapshot[2],
            mtime_ns=main_snapshot[3],
        )
        cleanup_names = {
            name
            for name in names
            if name.startswith(f".{source.name}.mediaforce-retired-")
            or any(
                name == f"{source.name}{suffix}"
                or name.startswith(
                    f".{source.name}{suffix}.mediaforce-retired-"
                )
                for suffix in ("-wal", "-shm", "-journal")
            )
        }
        unidentified_main = {
            name
            for name in cleanup_names
            if name.startswith(f".{source.name}.mediaforce-retired-")
            and name != main_quarantine_name
        }
        if unidentified_main:
            raise OSError(
                "legacy SQLite migration cleanup contains unidentified artifacts"
            )
        if main_quarantine_name in cleanup_names:
            main_info = os.stat(
                main_quarantine_name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if not _legacy_sqlite_cleanup_retired_snapshot_matches(
                    main_info,
                    main_snapshot,
            ):
                raise OSError(
                    "legacy SQLite migration cleanup quarantine is unsafe"
                )
        artifacts: list[_LegacySQLiteRetainedCleanupArtifact] = []
        for name in sorted(cleanup_names):
            info = os.stat(
                name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
            ):
                raise OSError(
                    "legacy SQLite migration cleanup artifact is unsafe"
                )
            snapshot = _legacy_sqlite_migration_file_snapshot_at(
                directory_descriptor,
                name,
                include_sha256=True,
                require_single_link=True,
            )
            artifacts.append(_LegacySQLiteRetainedCleanupArtifact(
                name=name,
                expected_snapshot=(
                    int(snapshot["device"]),
                    int(snapshot["inode"]),
                    int(snapshot["size"]),
                    int(snapshot["mtime_ns"]),
                    int(snapshot["ctime_ns"]),
                    int(snapshot["link_count"]),
                ),
                expected_sha256=str(snapshot["sha256"]),
            ))
        _assert_legacy_sqlite_migration_source_parent_stable(
            source,
            expected_parent_identity,
        )
        return tuple(artifacts)
    except FileIntegrityError as exc:
        raise OSError("legacy SQLite migration source parent is unsafe") from exc
    finally:
        if directory_descriptor >= 0:
            os.close(directory_descriptor)


def _upgrade_digestless_legacy_sqlite_cleanup_intent(
        *,
        intent_path: Path,
        payload: dict[str, object],
        source: Path,
        publication_policy: str,
        receipt_path: Path,
) -> dict[str, object]:
    retained_artifacts = _record_legacy_sqlite_cleanup_artifacts(
        source,
        expected_parent_identity=payload.get("source_parent_identity"),
        expected_main_snapshot=payload.get("source_main_snapshot"),
    )
    upgraded = dict(payload)
    upgraded["schema_version"] = _LEGACY_SQLITE_INTENT_VERSION
    upgraded["phase"] = "cleaning"
    upgraded["source_main_sha256"] = None
    upgraded["source_sidecar_snapshots"] = None
    upgraded["cleanup_policy"] = _LEGACY_SQLITE_CLEANUP_PRESERVE
    upgraded["publication_policy"] = publication_policy
    upgraded["legacy_cleanup_absent_prefix"] = []
    upgraded["retained_cleanup_artifacts"] = (
        _legacy_sqlite_retained_cleanup_artifact_payloads(
            retained_artifacts
        )
    )
    _ensure_legacy_sqlite_migration_receipt(receipt_path, upgraded)
    _replace_legacy_sqlite_migration_intent(
        intent_path,
        expected=payload,
        payload=upgraded,
    )
    return upgraded


def _legacy_sqlite_migration_completion_payload(
        payload: dict[str, object],
) -> dict[str, object]:
    if (
        payload.get("schema_version") != _LEGACY_SQLITE_INTENT_VERSION
        or payload.get("phase") != "cleaning"
    ):
        raise OSError("legacy SQLite migration intent cannot be completed")
    sealing = dict(payload)
    sealing["phase"] = "sealing"
    return sealing


def _legacy_sqlite_migration_legacy_completion_payload(
        payload: dict[str, object],
) -> dict[str, object]:
    if (
        payload.get("schema_version") != _LEGACY_SQLITE_INTENT_VERSION
        or payload.get("phase") != "cleaning"
    ):
        raise OSError("legacy SQLite migration intent cannot be completed")
    completed = dict(payload)
    completed["phase"] = "complete"
    return completed


def _legacy_sqlite_migration_sealed_payload(
        payload: dict[str, object],
) -> dict[str, object]:
    if (
        payload.get("schema_version") != _LEGACY_SQLITE_INTENT_VERSION
        or payload.get("phase") != "sealing"
    ):
        raise OSError("legacy SQLite migration intent cannot be sealed")
    completed = dict(payload)
    completed["phase"] = "complete"
    return completed


def _assert_legacy_sqlite_migration_source_matches(
        locked_source: _LegacySQLiteMigrationSource,
        payload: dict[str, object],
) -> None:
    source_parent_info = locked_source.path.parent.stat(follow_symlinks=False)
    source_snapshot = payload.get("source_main_snapshot")
    if (
        not stat.S_ISDIR(source_parent_info.st_mode)
        or payload.get("source_parent_identity")
        != [source_parent_info.st_dev, source_parent_info.st_ino]
        or not isinstance(source_snapshot, list)
        or len(source_snapshot) != 6
    ):
        raise OSError("legacy SQLite migration source identity changed")
    current_source_snapshot = locked_source.source_snapshot()
    if (
        source_snapshot[0] != current_source_snapshot[0]
        or source_snapshot[1] != current_source_snapshot[1]
        or source_snapshot[5] != current_source_snapshot[5]
    ):
        raise OSError("legacy SQLite migration source identity changed")
    if payload.get("phase") == "copying":
        locked_source.assert_stable()
        return
    staging_snapshot = _legacy_sqlite_migration_snapshot_dict(
        payload.get("staging_snapshot"),
        "staging snapshot",
    )
    if _legacy_sqlite_backup_sha256(locked_source) != staging_snapshot["sha256"]:
        raise OSError("legacy SQLite migration source content diverged")
    locked_source.assert_stable()


def _legacy_sqlite_migration_snapshot_dict(
        value: object,
        label: str,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise OSError(f"legacy SQLite migration {label} is invalid")
    required = {
        "device",
        "inode",
        "size",
        "link_count",
        "mtime_ns",
        "ctime_ns",
        "sha256",
    }
    if set(value) != required:
        raise OSError(f"legacy SQLite migration {label} is invalid")
    return value


def _legacy_sqlite_migration_sha256_is_valid(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("sha256:")
        and len(value) == len("sha256:") + 64
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _legacy_sqlite_migration_sidecar_snapshots(
        value: object,
        *,
        require_sha256: bool = True,
) -> dict[str, dict[str, object]]:
    if not isinstance(value, dict) or set(value) != {
        "-wal",
        "-shm",
        "-journal",
    }:
        raise OSError("legacy SQLite migration sidecar snapshots are invalid")
    required = {
        "device",
        "inode",
        "size",
        "link_count",
        "mtime_ns",
        "ctime_ns",
        "uid",
        "mode",
        "guard_created",
    }
    if require_sha256:
        required.add("sha256")
    snapshots: dict[str, dict[str, object]] = {}
    for suffix in ("-wal", "-shm", "-journal"):
        snapshot = value.get(suffix)
        if not isinstance(snapshot, dict) or set(snapshot) != required:
            raise OSError(
                "legacy SQLite migration sidecar snapshot is invalid"
            )
        integer_values = tuple(
            snapshot.get(key)
            for key in (
                "device",
                "inode",
                "size",
                "link_count",
                "mtime_ns",
                "ctime_ns",
                "uid",
                "mode",
            )
        )
        if (
            any(type(item) is not int for item in integer_values)
            or integer_values[0] < 0
            or integer_values[1] <= 0
            or integer_values[2] < 0
            or integer_values[3] != 1
            or integer_values[6] != os.getuid()
            or not 0 <= integer_values[7] <= 0o777
            or type(snapshot.get("guard_created")) is not bool
            or (
                require_sha256
                and not _legacy_sqlite_migration_sha256_is_valid(
                    snapshot.get("sha256")
                )
            )
        ):
            raise OSError(
                "legacy SQLite migration sidecar snapshot is invalid"
            )
        snapshots[suffix] = snapshot
    return snapshots


def _reconciled_legacy_sqlite_migration_sidecar_snapshots(
        expected: dict[str, dict[str, object]],
        current: dict[str, dict[str, object]],
) -> dict[str, dict[str, object]]:
    snapshots: dict[str, dict[str, object]] = {}
    for suffix in ("-wal", "-shm", "-journal"):
        expected_snapshot = expected[suffix]
        current_snapshot = current[suffix]
        expected_identity = {
            key: value
            for key, value in expected_snapshot.items()
            if key != "guard_created"
        }
        current_identity = {
            key: current_snapshot[key]
            for key in expected_identity
        }
        if expected_identity == current_identity:
            reconciled = dict(current_snapshot)
            reconciled["guard_created"] = bool(
                expected_snapshot["guard_created"]
                or current_snapshot["guard_created"]
            )
            snapshots[suffix] = reconciled
            continue
        if current_snapshot["guard_created"] is True:
            snapshots[suffix] = current_snapshot
            continue
        raise OSError(
            "legacy SQLite migration sidecar identity changed during cleanup"
        )
    return snapshots


def _assert_legacy_sqlite_migration_database_valid(path: Path) -> None:
    connection = sqlite3.connect(
        f"{_path_without_resolution(path).as_uri()}?mode=ro&immutable=1",
        uri=True,
        timeout=0,
        isolation_level=None,
    )
    try:
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise sqlite3.DatabaseError(
                "legacy SQLite migration destination failed quick_check"
            )
    finally:
        connection.close()


def _legacy_sqlite_path_for_directory_descriptor(
        directory_descriptor: int,
        *,
        directory_identity: tuple[int, int],
        filename: str,
) -> Path:
    if sys.platform == "darwin":
        pinned_directory = (
            Path("/.vol")
            / str(directory_identity[0])
            / str(directory_identity[1])
        )
    elif sys.platform.startswith("linux") and Path("/proc/self/fd").is_dir():
        pinned_directory = Path("/proc/self/fd") / str(directory_descriptor)
    else:
        raise OSError(
            errno.ENOTSUP,
            "legacy SQLite pinned destination paths are unavailable",
        )
    pinned_parent_info = pinned_directory.stat()
    if (
        not stat.S_ISDIR(pinned_parent_info.st_mode)
        or (pinned_parent_info.st_dev, pinned_parent_info.st_ino)
        != directory_identity
    ):
        raise OSError("legacy SQLite migration destination parent changed")
    return pinned_directory / filename


def _assert_legacy_sqlite_migration_source_parent_stable(
        source: Path,
        expected_parent_identity: object,
) -> None:
    if (
        not isinstance(expected_parent_identity, list)
        or len(expected_parent_identity) != 2
        or any(not isinstance(value, int) for value in expected_parent_identity)
    ):
        raise OSError("legacy SQLite migration source parent identity is invalid")
    try:
        parent_info = source.parent.lstat()
    except OSError as exc:
        raise OSError("legacy SQLite migration source parent changed") from exc
    if (
        not stat.S_ISDIR(parent_info.st_mode)
        or stat.S_ISLNK(parent_info.st_mode)
        or [parent_info.st_dev, parent_info.st_ino] != expected_parent_identity
    ):
        raise OSError("legacy SQLite migration source parent changed")


@dataclass(frozen=True, slots=True)
class _LegacySQLiteRetainedCleanupArtifact:
    name: str
    expected_snapshot: tuple[int, ...]
    expected_sha256: str


def _legacy_sqlite_cleanup_absent_prefix(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
            not isinstance(item, str)
            for item in value
    ):
        raise OSError("legacy SQLite migration cleanup prefix is invalid")
    prefix = tuple(value)
    if prefix != _LEGACY_SQLITE_CLEANUP_SUFFIXES[:len(prefix)]:
        raise OSError("legacy SQLite migration cleanup prefix is invalid")
    return prefix


def _legacy_sqlite_retained_cleanup_artifacts(
        value: object,
) -> tuple[_LegacySQLiteRetainedCleanupArtifact, ...]:
    if not isinstance(value, list):
        raise OSError("legacy SQLite migration retained cleanup manifest is invalid")
    artifacts: list[_LegacySQLiteRetainedCleanupArtifact] = []
    names: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "name",
            "expected_snapshot",
            "expected_sha256",
        }:
            raise OSError(
                "legacy SQLite migration retained cleanup manifest is invalid"
            )
        name = item.get("name")
        snapshot = item.get("expected_snapshot")
        sha256 = item.get("expected_sha256")
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or name in names
            or not isinstance(snapshot, list)
            or len(snapshot) not in {6, 8}
            or any(type(entry) is not int for entry in snapshot)
            or snapshot[0] < 0
            or snapshot[1] <= 0
            or snapshot[2] < 0
            or snapshot[5] != 1
            or not _legacy_sqlite_migration_sha256_is_valid(sha256)
        ):
            raise OSError(
                "legacy SQLite migration retained cleanup manifest is invalid"
            )
        names.add(name)
        artifacts.append(_LegacySQLiteRetainedCleanupArtifact(
            name=name,
            expected_snapshot=tuple(snapshot),
            expected_sha256=str(sha256),
        ))
    return tuple(artifacts)


def _legacy_sqlite_retained_cleanup_artifact_payloads(
        artifacts: tuple[_LegacySQLiteRetainedCleanupArtifact, ...],
) -> list[dict[str, object]]:
    return [
        {
            "name": artifact.name,
            "expected_snapshot": list(artifact.expected_snapshot),
            "expected_sha256": artifact.expected_sha256,
        }
        for artifact in artifacts
    ]


def _legacy_sqlite_expected_cleanup_artifact_payloads(
        *,
        source_name: str,
        expected_main_snapshot: object,
        expected_main_sha256: object,
        expected_sidecar_snapshots: object,
        absent_prefix: tuple[str, ...],
) -> list[dict[str, object]]:
    return _legacy_sqlite_retained_cleanup_artifact_payloads(
        _legacy_sqlite_expected_cleanup_artifacts(
            source_name=source_name,
            expected_main_snapshot=expected_main_snapshot,
            expected_main_sha256=expected_main_sha256,
            expected_sidecar_snapshots=expected_sidecar_snapshots,
            absent_prefix=absent_prefix,
        )
    )


def _legacy_sqlite_expected_cleanup_artifacts(
        *,
        source_name: str,
        expected_main_snapshot: object,
        expected_main_sha256: object,
        expected_sidecar_snapshots: object,
        absent_prefix: tuple[str, ...],
) -> tuple[_LegacySQLiteRetainedCleanupArtifact, ...]:
    if (
        not isinstance(expected_main_snapshot, list)
        or len(expected_main_snapshot) != 6
        or any(type(value) is not int for value in expected_main_snapshot)
        or not _legacy_sqlite_migration_sha256_is_valid(expected_main_sha256)
    ):
        raise OSError("legacy SQLite migration source cleanup manifest is invalid")
    sidecar_snapshots = _legacy_sqlite_migration_sidecar_snapshots(
        expected_sidecar_snapshots
    )
    absent = set(absent_prefix)
    artifacts: list[_LegacySQLiteRetainedCleanupArtifact] = []
    for suffix in _LEGACY_SQLITE_CLEANUP_SUFFIXES:
        if suffix in absent:
            continue
        name = f"{source_name}{suffix}"
        if suffix:
            snapshot = _legacy_sqlite_cleanup_sidecar_snapshot(
                sidecar_snapshots[suffix]
            )
            sha256 = str(sidecar_snapshots[suffix]["sha256"])
        else:
            snapshot = tuple(expected_main_snapshot)
            sha256 = str(expected_main_sha256)
        artifacts.append(_LegacySQLiteRetainedCleanupArtifact(
            name=legacy_sqlite_migration_quarantine_name(
                name,
                device=snapshot[0],
                inode=snapshot[1],
                size=snapshot[2],
                mtime_ns=snapshot[3],
            ),
            expected_snapshot=snapshot,
            expected_sha256=sha256,
        ))
    return tuple(artifacts)


def _complete_legacy_sqlite_source_cleanup(
        source: Path,
        *,
        expected_parent_identity: object,
        expected_main_snapshot: object,
        expected_main_sha256: object,
        expected_sidecar_snapshots: object,
        before_remove: Callable[[], None] | None = None,
        allowed_absent_suffixes: tuple[str, ...] = (),
        expected_retained_artifacts: tuple[
            _LegacySQLiteRetainedCleanupArtifact,
            ...,
        ] | None = None,
) -> tuple[_LegacySQLiteRetainedCleanupArtifact, ...]:
    if (
        not isinstance(expected_parent_identity, list)
        or len(expected_parent_identity) != 2
        or any(not isinstance(value, int) for value in expected_parent_identity)
    ):
        raise OSError("legacy SQLite migration source parent identity is invalid")
    if (
        not isinstance(expected_main_snapshot, list)
        or len(expected_main_snapshot) != 6
        or any(type(value) is not int for value in expected_main_snapshot)
        or expected_main_snapshot[0] < 0
        or expected_main_snapshot[1] <= 0
        or expected_main_snapshot[2] < 0
        or expected_main_snapshot[5] != 1
    ):
        raise OSError("legacy SQLite migration source snapshot is invalid")
    main_snapshot = tuple(expected_main_snapshot)
    if not _legacy_sqlite_migration_sha256_is_valid(expected_main_sha256):
        raise OSError("legacy SQLite migration source digest is invalid")
    main_sha256 = str(expected_main_sha256)
    sidecar_snapshots = _legacy_sqlite_migration_sidecar_snapshots(
        expected_sidecar_snapshots
    )
    directory_descriptor = -1
    try:
        _, directory_descriptor = open_stable_directory(source.parent)

        def assert_parent_stable() -> None:
            directory_info = os.fstat(directory_descriptor)
            _assert_legacy_sqlite_migration_source_parent_stable(
                source,
                expected_parent_identity,
            )
            if (
                not stat.S_ISDIR(directory_info.st_mode)
                or [directory_info.st_dev, directory_info.st_ino]
                != expected_parent_identity
            ):
                raise OSError("legacy SQLite migration source parent changed")

        assert_parent_stable()
        main_quarantine_name = legacy_sqlite_migration_quarantine_name(
            source.name,
            device=main_snapshot[0],
            inode=main_snapshot[1],
            size=main_snapshot[2],
            mtime_ns=main_snapshot[3],
        )
        retained_artifacts: dict[str, _LegacySQLiteRetainedCleanupArtifact] = {}
        artifact = _complete_legacy_sqlite_cleanup_entry(
            directory_descriptor=directory_descriptor,
            name=source.name,
            quarantine_name=main_quarantine_name,
            expected_snapshot=main_snapshot,
            expected_sha256=main_sha256,
            before_remove=before_remove,
            allow_absent="" in allowed_absent_suffixes,
        )
        if artifact is not None:
            retained_artifacts[artifact.name] = artifact
        assert_parent_stable()
        for suffix in ("-wal", "-shm", "-journal"):
            assert_parent_stable()
            name = f"{source.name}{suffix}"
            expected = sidecar_snapshots[suffix]
            quarantine_name = legacy_sqlite_migration_quarantine_name(
                name,
                device=int(expected["device"]),
                inode=int(expected["inode"]),
                size=int(expected["size"]),
                mtime_ns=int(expected["mtime_ns"]),
            )
            artifact = _complete_legacy_sqlite_cleanup_entry(
                directory_descriptor=directory_descriptor,
                name=name,
                quarantine_name=quarantine_name,
                expected_snapshot=_legacy_sqlite_cleanup_sidecar_snapshot(
                    expected
                ),
                expected_sha256=str(expected["sha256"]),
                before_remove=before_remove,
                allow_absent=suffix in allowed_absent_suffixes,
            )
            if artifact is not None:
                retained_artifacts[artifact.name] = artifact
            assert_parent_stable()
        remaining_names = set(os.listdir(directory_descriptor))
        main_quarantine_prefix = f".{source.name}.mediaforce-retired-"
        if source.name in remaining_names:
            raise OSError("legacy SQLite migration source cleanup is incomplete")
        remaining_artifacts = {
            candidate
            for candidate in remaining_names
            if candidate.startswith(main_quarantine_prefix)
            or any(
                candidate == f"{source.name}{suffix}"
                or candidate.startswith(
                    f".{source.name}{suffix}.mediaforce-retired-"
                )
                for suffix in ("-wal", "-shm", "-journal")
            )
        }
        if remaining_artifacts != set(retained_artifacts):
            raise OSError("legacy SQLite migration source cleanup is incomplete")
        for artifact in retained_artifacts.values():
            if not _legacy_sqlite_retained_cleanup_artifact_matches(
                    directory_descriptor,
                    artifact,
            ):
                raise OSError("legacy SQLite migration cleanup quarantine is unsafe")
        if expected_retained_artifacts is not None:
            expected_by_name = {
                artifact.name: artifact
                for artifact in expected_retained_artifacts
            }
            if (
                len(expected_by_name) != len(expected_retained_artifacts)
                or retained_artifacts != expected_by_name
            ):
                raise OSError(
                    "legacy SQLite migration retained cleanup set changed"
                )
        assert_parent_stable()
        os.fsync(directory_descriptor)
        assert_parent_stable()
        return tuple(
            retained_artifacts[name]
            for name in sorted(retained_artifacts)
        )
    except FileIntegrityError as exc:
        raise OSError("legacy SQLite migration source parent is unsafe") from exc
    finally:
        if directory_descriptor >= 0:
            os.close(directory_descriptor)


def _legacy_sqlite_source_cleanup_recorded_all_absent(
        payload: dict[str, object],
        *,
        retained_artifacts: tuple[
            _LegacySQLiteRetainedCleanupArtifact,
            ...,
        ],
) -> bool:
    return (
        payload.get("cleanup_policy") == _LEGACY_SQLITE_CLEANUP_RETAIN
        and _legacy_sqlite_cleanup_absent_prefix(
            payload.get("legacy_cleanup_absent_prefix")
        )
        == _LEGACY_SQLITE_CLEANUP_SUFFIXES
        and not retained_artifacts
    )


def _assert_recorded_legacy_sqlite_source_cleanup_complete(
        source: Path,
        *,
        payload: dict[str, object],
        retained_artifacts: tuple[
            _LegacySQLiteRetainedCleanupArtifact,
            ...,
        ],
) -> None:
    if not _path_entry_exists(source.parent):
        if _legacy_sqlite_source_cleanup_recorded_all_absent(
                payload,
                retained_artifacts=retained_artifacts,
        ):
            return
        raise OSError("legacy SQLite migration source parent disappeared")
    _assert_legacy_sqlite_source_cleanup_complete(
        source,
        expected_parent_identity=payload.get("source_parent_identity"),
        allowed_cleanup_artifacts=retained_artifacts,
    )


def _assert_legacy_sqlite_source_cleanup_complete(
        source: Path,
        *,
        expected_parent_identity: object,
        allowed_cleanup_artifacts: tuple[
            _LegacySQLiteRetainedCleanupArtifact,
            ...,
        ],
) -> None:
    if (
        not isinstance(expected_parent_identity, list)
        or len(expected_parent_identity) != 2
        or any(type(value) is not int for value in expected_parent_identity)
    ):
        raise OSError("legacy SQLite migration source parent identity is invalid")
    directory_descriptor = -1
    try:
        _, directory_descriptor = open_stable_directory(source.parent)
        directory_info = os.fstat(directory_descriptor)
        if [directory_info.st_dev, directory_info.st_ino] != expected_parent_identity:
            raise OSError("legacy SQLite migration source parent changed")
        names = set(os.listdir(directory_descriptor))
        if source.name in names:
            raise OSError("legacy SQLite migration source cleanup is incomplete")
        main_quarantine_prefix = f".{source.name}.mediaforce-retired-"
        current_cleanup_artifacts = {
            name
            for name in names
            if name.startswith(main_quarantine_prefix)
            or any(
                name == f"{source.name}{suffix}"
                or name.startswith(
                    f".{source.name}{suffix}.mediaforce-retired-"
                )
                for suffix in ("-wal", "-shm", "-journal")
            )
        }
        allowed_artifact_names = {
            artifact.name
            for artifact in allowed_cleanup_artifacts
        }
        if (
            len(allowed_artifact_names) != len(allowed_cleanup_artifacts)
            or current_cleanup_artifacts != allowed_artifact_names
        ):
            raise OSError("legacy SQLite migration source cleanup is incomplete")
        for artifact in allowed_cleanup_artifacts:
            if not _legacy_sqlite_retained_cleanup_artifact_matches(
                    directory_descriptor,
                    artifact,
            ):
                raise OSError("legacy SQLite migration cleanup quarantine is unsafe")
        _assert_legacy_sqlite_migration_source_parent_stable(
            source,
            expected_parent_identity,
        )
    except FileIntegrityError as exc:
        raise OSError("legacy SQLite migration source parent is unsafe") from exc
    finally:
        if directory_descriptor >= 0:
            os.close(directory_descriptor)


def _complete_legacy_sqlite_cleanup_entry(
        *,
        directory_descriptor: int,
        name: str,
        quarantine_name: str,
        expected_snapshot: tuple[int, ...],
        expected_sha256: str,
        before_remove: Callable[[], None] | None,
        allow_absent: bool = False,
) -> _LegacySQLiteRetainedCleanupArtifact | None:
    def digest_matches(candidate_name: str) -> bool:
        snapshot = _legacy_sqlite_migration_file_snapshot_at(
            directory_descriptor,
            candidate_name,
            include_sha256=True,
            require_single_link=True,
        )
        return snapshot.get("sha256") == expected_sha256

    live_info = _legacy_sqlite_directory_entry_info(
        directory_descriptor,
        name,
    )
    quarantine_info = _legacy_sqlite_directory_entry_info(
        directory_descriptor,
        quarantine_name,
    )
    if live_info is not None and quarantine_info is not None:
        raise OSError("legacy SQLite migration cleanup has conflicting entries")
    if live_info is None and quarantine_info is None:
        if allow_absent:
            return None
        raise OSError("legacy SQLite migration source cleanup is incomplete")
    if quarantine_info is not None:
        if (
            not _legacy_sqlite_cleanup_retired_snapshot_matches(
                quarantine_info,
                expected_snapshot,
            )
            or not digest_matches(quarantine_name)
        ):
            raise OSError("legacy SQLite migration cleanup quarantine is unsafe")
        if before_remove is not None:
            before_remove()
        current = os.stat(
            quarantine_name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (
            not _legacy_sqlite_cleanup_retired_snapshot_matches(
                current,
                expected_snapshot,
            )
            or not digest_matches(quarantine_name)
        ):
            raise OSError("legacy SQLite migration cleanup quarantine changed")
        os.fsync(directory_descriptor)
        current = os.stat(
            quarantine_name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (
            not _legacy_sqlite_cleanup_retired_snapshot_matches(
                current,
                expected_snapshot,
            )
            or not digest_matches(quarantine_name)
            or _legacy_sqlite_directory_entry_exists(
                directory_descriptor,
                name,
            )
        ):
            raise OSError("legacy SQLite migration cleanup quarantine changed")
        return _LegacySQLiteRetainedCleanupArtifact(
            name=quarantine_name,
            expected_snapshot=expected_snapshot,
            expected_sha256=expected_sha256,
        )
    if (
        live_info is None
        or not _legacy_sqlite_cleanup_live_snapshot_matches(
            live_info,
            expected_snapshot,
        )
        or not digest_matches(name)
    ):
        raise OSError("legacy SQLite migration source cleanup is unsafe")
    if before_remove is not None:
        before_remove()
    rename_exclusive(
        source_directory_descriptor=directory_descriptor,
        source_name=name,
        destination_directory_descriptor=directory_descriptor,
        destination_name=quarantine_name,
    )
    current = os.stat(
        quarantine_name,
        dir_fd=directory_descriptor,
        follow_symlinks=False,
    )
    if (
        not _legacy_sqlite_cleanup_retired_snapshot_matches(
            current,
            expected_snapshot,
        )
        or not digest_matches(quarantine_name)
    ):
        raise OSError("legacy SQLite migration cleanup claimed an unsafe entry")
    os.fsync(directory_descriptor)
    current = os.stat(
        quarantine_name,
        dir_fd=directory_descriptor,
        follow_symlinks=False,
    )
    if (
        not _legacy_sqlite_cleanup_retired_snapshot_matches(
            current,
            expected_snapshot,
        )
        or not digest_matches(quarantine_name)
        or _legacy_sqlite_directory_entry_exists(
            directory_descriptor,
            name,
        )
    ):
        raise OSError("legacy SQLite migration cleanup quarantine changed")
    return _LegacySQLiteRetainedCleanupArtifact(
        name=quarantine_name,
        expected_snapshot=expected_snapshot,
        expected_sha256=expected_sha256,
    )


def _legacy_sqlite_cleanup_live_snapshot_matches(
        info: os.stat_result,
        expected_snapshot: tuple[int, ...],
) -> bool:
    if not stat.S_ISREG(info.st_mode) or len(expected_snapshot) not in {6, 8}:
        return False
    if (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_nlink,
    ) != expected_snapshot[:6]:
        return False
    return len(expected_snapshot) == 6 or (
        info.st_uid,
        stat.S_IMODE(info.st_mode),
    ) == expected_snapshot[6:]


def _legacy_sqlite_cleanup_retired_snapshot_matches(
        info: os.stat_result,
        expected_snapshot: tuple[int, ...],
) -> bool:
    if not stat.S_ISREG(info.st_mode) or len(expected_snapshot) not in {6, 8}:
        return False
    if (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_nlink,
    ) != (
        expected_snapshot[0],
        expected_snapshot[1],
        expected_snapshot[2],
        expected_snapshot[3],
        expected_snapshot[5],
    ):
        return False
    return len(expected_snapshot) == 6 or (
        info.st_uid,
        stat.S_IMODE(info.st_mode),
    ) == expected_snapshot[6:]


def _legacy_sqlite_cleanup_sidecar_snapshot(
        snapshot: dict[str, object],
) -> tuple[int, int, int, int, int, int, int, int]:
    return (
        int(snapshot["device"]),
        int(snapshot["inode"]),
        int(snapshot["size"]),
        int(snapshot["mtime_ns"]),
        int(snapshot["ctime_ns"]),
        int(snapshot["link_count"]),
        int(snapshot["uid"]),
        int(snapshot["mode"]),
    )


def _legacy_sqlite_retained_cleanup_artifact_matches(
        directory_descriptor: int,
        artifact: _LegacySQLiteRetainedCleanupArtifact,
) -> bool:
    info = _legacy_sqlite_directory_entry_info(
        directory_descriptor,
        artifact.name,
    )
    if info is None:
        return False
    if not _legacy_sqlite_cleanup_retired_snapshot_matches(
            info,
            artifact.expected_snapshot,
    ):
        return False
    snapshot = _legacy_sqlite_migration_file_snapshot_at(
        directory_descriptor,
        artifact.name,
        include_sha256=True,
        require_single_link=True,
    )
    return snapshot.get("sha256") == artifact.expected_sha256


def _legacy_sqlite_directory_entry_info(
        directory_descriptor: int,
        name: str,
) -> os.stat_result | None:
    try:
        return os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return None


def _legacy_sqlite_directory_entry_exists(
        directory_descriptor: int,
        name: str,
) -> bool:
    return _legacy_sqlite_directory_entry_info(
        directory_descriptor,
        name,
    ) is not None


def _legacy_sqlite_backup_sha256(
        locked_source: _LegacySQLiteMigrationSource,
) -> str:
    source_connection: sqlite3.Connection | None = None
    backup_connection: sqlite3.Connection | None = None
    with tempfile.TemporaryDirectory(prefix="mediaforce-sqlite-verify-") as directory:
        backup_path = Path(directory) / "source.sqlite3"
        try:
            source_connection = sqlite3.connect(
                locked_source.sqlite_uri(),
                uri=True,
                timeout=0,
                isolation_level=None,
            )
            source_connection.execute("BEGIN")
            source_connection.execute("PRAGMA schema_version").fetchone()
            locked_source.assert_connection_bound(source_connection)
            backup_connection = sqlite3.connect(
                backup_path,
                timeout=0,
                isolation_level=None,
            )
            source_connection.backup(backup_connection)
            locked_source.assert_connection_bound(source_connection)
            backup_connection.close()
            backup_connection = None
            _fsync_file(backup_path)
            snapshot = _legacy_sqlite_migration_file_snapshot(
                backup_path,
                include_sha256=True,
                require_single_link=True,
            )
            digest = snapshot.get("sha256")
            if not isinstance(digest, str):
                raise OSError("legacy SQLite verification backup digest is invalid")
            return digest
        finally:
            if backup_connection is not None:
                backup_connection.close()
            if source_connection is not None:
                try:
                    if source_connection.in_transaction:
                        source_connection.rollback()
                finally:
                    source_connection.close()


def _reserved_legacy_sqlite_staging_path(destination: Path) -> Path:
    return destination.parent / (
        f".{destination.name}.migration-{secrets.token_hex(12)}.sqlite3"
    )


@contextmanager
def _create_legacy_sqlite_staging_path(
        staging_path: Path,
) -> Iterator[_LegacySQLiteMigrationStaging]:
    directory_descriptor = -1
    descriptor = -1
    try:
        _, directory_descriptor = open_stable_directory(staging_path.parent)
        directory_info = os.fstat(directory_descriptor)
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
        )
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(
            staging_path.name,
            flags,
            0o600,
            dir_fd=directory_descriptor,
        )
        info = os.fstat(descriptor)
        binding = _LegacySQLiteMigrationStaging(
            path=staging_path,
            directory_descriptor=directory_descriptor,
            directory_identity=(directory_info.st_dev, directory_info.st_ino),
            descriptor=descriptor,
            file_identity=(info.st_dev, info.st_ino),
        )
        binding.assert_stable()
        yield binding
    except FileIntegrityError as exc:
        raise OSError(
            "legacy SQLite migration staging parent is unsafe"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if directory_descriptor >= 0:
            os.close(directory_descriptor)


@contextmanager
def _copied_legacy_sqlite_database(
        locked_source: _LegacySQLiteMigrationSource,
        staging: _LegacySQLiteMigrationStaging,
) -> Iterator[None]:
    source_connection: sqlite3.Connection | None = None
    backup_connection: sqlite3.Connection | None = None
    with tempfile.TemporaryDirectory(prefix="mediaforce-sqlite-migration-") as directory:
        backup_path = Path(directory) / "backup.sqlite3"
        try:
            with _opened_legacy_sqlite_write_gate(locked_source) as source_uri:
                source_connection = sqlite3.connect(
                    source_uri,
                    uri=True,
                    timeout=0,
                    isolation_level=None,
                )
                source_connection.execute("BEGIN")
                source_connection.execute("PRAGMA schema_version").fetchone()
                locked_source.assert_connection_bound(source_connection)
                backup_connection = sqlite3.connect(
                    backup_path,
                    timeout=0,
                    isolation_level=None,
                )
                source_connection.backup(backup_connection)
                locked_source.assert_connection_bound(source_connection)
                backup_connection.close()
                backup_connection = None
                backup_connection = sqlite3.connect(
                    backup_path,
                    timeout=0,
                    isolation_level=None,
                )
                quick_check = backup_connection.execute(
                    "PRAGMA quick_check"
                ).fetchone()
                if quick_check != ("ok",):
                    raise sqlite3.DatabaseError(
                        "Legacy SQLite backup did not pass quick_check"
                    )
                backup_connection.close()
                backup_connection = None
                _fsync_file(backup_path)
                backup_snapshot = _legacy_sqlite_migration_file_snapshot(
                    backup_path,
                    include_sha256=True,
                    require_single_link=True,
                )
                staging.write_from(backup_path)
                staging_snapshot = staging.snapshot()
                if (
                    staging_snapshot.get("size") != backup_snapshot.get("size")
                    or staging_snapshot.get("sha256")
                    != backup_snapshot.get("sha256")
                ):
                    raise OSError(
                        "legacy SQLite migration staging copy diverged"
                    )
                locked_source.assert_connection_bound(source_connection)
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
            if backup_connection is not None:
                backup_connection.close()
            if source_connection is not None:
                try:
                    if source_connection.in_transaction:
                        source_connection.rollback()
                finally:
                    source_connection.close()


@contextmanager
def _opened_legacy_sqlite_write_gate(
        locked_source: _LegacySQLiteMigrationSource,
) -> Iterator[str]:
    connection: sqlite3.Connection | None = None
    source_uri = locked_source.sqlite_uri()
    try:
        locked_source.prepare_sqlite_sidecars_for_write_gate()
        connection = sqlite3.connect(
            source_uri,
            uri=True,
            timeout=0,
            isolation_level=None,
        )
        connection.execute("BEGIN IMMEDIATE")
        locked_source.bind_sqlite_sidecars()
        locked_source.assert_connection_bound(connection)
        yield source_uri
    except sqlite3.OperationalError as exc:
        from mediaforce.web.runtime_lock import MediaforceRuntimeBusyError

        raise MediaforceRuntimeBusyError(
            "Legacy SQLite source is active or unavailable for migration"
        ) from exc
    finally:
        if connection is not None:
            try:
                if connection.in_transaction:
                    connection.rollback()
            finally:
                connection.close()


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
