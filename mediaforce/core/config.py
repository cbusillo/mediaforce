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
    rename_exclusive,
)
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
    if _path_without_resolution(source) == _path_without_resolution(destination):
        return
    intent_path = _legacy_sqlite_migration_intent_path(destination)
    if _path_entry_exists(intent_path) and _resume_legacy_sqlite_migration_intent(
            config,
            source,
            destination,
            intent_path,
    ):
        return
    source_exists = _path_entry_exists(source)
    destination_exists = _path_entry_exists(destination)
    if source_exists and destination_exists:
        from mediaforce.web.runtime_lock import MediaforceRuntimeBusyError

        raise MediaforceRuntimeBusyError(
            "Legacy and configured SQLite databases both exist without a "
            "resumable migration intent"
        )
    if not source_exists or destination_exists:
        return
    from mediaforce.web.runtime_lock import exclusive_legacy_sqlite_migration_source

    with exclusive_legacy_sqlite_migration_source(config, source) as locked_source:
        if _path_entry_exists(destination):
            return
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
        ready_intent = False
        try:
            _create_legacy_sqlite_staging_path(staging_path)
            with _copied_legacy_sqlite_database(locked_source, staging_path):
                _fsync_file(staging_path)
                locked_source.assert_stable()
                intent_payload = _legacy_sqlite_migration_intent_payload(
                    locked_source=locked_source,
                    source=source,
                    destination=destination,
                    staging_path=staging_path,
                    staging_snapshot=_legacy_sqlite_migration_file_snapshot(
                        staging_path,
                        include_sha256=True,
                        require_single_link=True,
                    ),
                )
                _replace_legacy_sqlite_migration_intent(
                    intent_path,
                    expected=copying_intent,
                    payload=intent_payload,
                )
                ready_intent = True
                staging_snapshot = _legacy_sqlite_migration_snapshot_dict(
                    intent_payload["staging_snapshot"],
                    "staging snapshot",
                )
                with _opened_legacy_sqlite_migration_destination(
                        destination,
                        expected_parent_identity=intent_payload.get(
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
                            intent_payload,
                            locked_source=locked_source,
                        )
                    )
                    _replace_legacy_sqlite_migration_intent(
                        intent_path,
                        expected=intent_payload,
                        payload=cleaning_intent,
                    )
                    destination_binding.assert_parent_stable()
                    intent_payload = cleaning_intent

                    def assert_destination_authoritative() -> None:
                        destination_binding.assert_file_identity(
                            destination.name,
                            staging_snapshot,
                            allowed_link_counts={2},
                        )

                    locked_source.discard_after_publish(
                        before_remove=assert_destination_authoritative,
                        expected_main_sha256=str(
                            cleaning_intent["source_main_sha256"]
                        ),
                        expected_sidecar_snapshots=(
                            _legacy_sqlite_migration_sidecar_snapshots(
                                cleaning_intent["source_sidecar_snapshots"]
                            )
                        ),
                    )
                    locked_source.assert_cleanup_complete()
                    destination_binding.assert_file_matches(
                        destination.name,
                        staging_snapshot,
                        allowed_link_counts={2},
                    )
                    destination_binding.assert_database_valid()
                    locked_source.assert_parent_stable()
                    destination_binding.discard_staging(
                        staging_path.name,
                        staging_snapshot,
                    )
                    locked_source.assert_cleanup_complete()
                    _discard_legacy_sqlite_migration_intent_after_source_cleanup(
                        destination_binding=destination_binding,
                        intent_path=intent_path,
                        payload=cleaning_intent,
                        assert_source_cleanup_complete=(
                            locked_source.assert_cleanup_complete
                        ),
                    )
        except BaseException as exc:
            if not ready_intent:
                try:
                    _discard_legacy_sqlite_staging_path(staging_path)
                    _discard_legacy_sqlite_migration_intent(intent_path)
                except OSError as cleanup_error:
                    exc.add_note(
                        "Legacy SQLite pre-copy cleanup also failed: "
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )
            raise


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
                )
            )
        ):
            raise OSError("legacy SQLite migration destination identity changed")
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
        self.assert_file_matches(
            staging_name,
            expected,
            allowed_link_counts={1},
        )
        linked = False
        try:
            os.link(
                staging_name,
                self.path.name,
                src_dir_fd=self.directory_descriptor,
                dst_dir_fd=self.directory_descriptor,
                follow_symlinks=False,
            )
            linked = True
            os.fsync(self.directory_descriptor)
            self.assert_file_matches(
                self.path.name,
                expected,
                allowed_link_counts={2},
            )
        except FileExistsError as exc:
            from mediaforce.web.runtime_lock import MediaforceRuntimeBusyError

            raise MediaforceRuntimeBusyError(
                "Configured SQLite destination appeared during legacy migration"
            ) from exc
        except BaseException:
            if linked:
                try:
                    published_info = os.stat(
                        self.path.name,
                        dir_fd=self.directory_descriptor,
                        follow_symlinks=False,
                    )
                    if (
                        published_info.st_dev == expected.get("device")
                        and published_info.st_ino == expected.get("inode")
                    ):
                        os.unlink(
                            self.path.name,
                            dir_fd=self.directory_descriptor,
                        )
                        os.fsync(self.directory_descriptor)
                except OSError:
                    pass
            raise

    def discard_staging(
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
        os.unlink(staging_name, dir_fd=self.directory_descriptor)
        os.fsync(self.directory_descriptor)
        self.assert_file_matches(
            self.path.name,
            expected,
            allowed_link_counts={1},
        )

    def discard_intent(self, intent_name: str) -> None:
        self.assert_parent_stable()
        try:
            os.unlink(intent_name, dir_fd=self.directory_descriptor)
        except FileNotFoundError:
            return
        os.fsync(self.directory_descriptor)
        self.assert_parent_stable()

    def restore_intent(
            self,
            intent_name: str,
            payload: dict[str, object],
    ) -> None:
        self.assert_parent_stable()
        _write_legacy_sqlite_migration_intent_at(
            directory_descriptor=self.directory_descriptor,
            intent_name=intent_name,
            payload=payload,
        )
        self.assert_parent_stable()


def _discard_legacy_sqlite_migration_intent_after_source_cleanup(
        *,
        destination_binding: _LegacySQLiteMigrationDestination,
        intent_path: Path,
        payload: dict[str, object],
        assert_source_cleanup_complete: Callable[[], None],
) -> None:
    assert_source_cleanup_complete()
    try:
        destination_binding.discard_intent(intent_path.name)
        assert_source_cleanup_complete()
    except BaseException as exc:
        try:
            if not destination_binding.entry_exists(intent_path.name):
                destination_binding.restore_intent(intent_path.name, payload)
        except BaseException as restore_error:
            exc.add_note(
                "Legacy SQLite migration intent restoration also failed: "
                f"{type(restore_error).__name__}: {restore_error}"
            )
        raise


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


def _legacy_sqlite_migration_intent_payload(
        *,
        locked_source: _LegacySQLiteMigrationSource,
        source: Path,
        destination: Path,
        staging_path: Path,
        staging_snapshot: dict[str, object] | None,
) -> dict[str, object]:
    locked_source.assert_stable()
    source_parent_info = source.parent.stat(follow_symlinks=False)
    destination_parent_info = destination.parent.stat(follow_symlinks=False)
    payload = {
        "schema": "mediaforce.legacy_sqlite_migration_intent",
        "schema_version": 4,
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
    }
    locked_source.assert_stable()
    return payload


def _legacy_sqlite_migration_cleaning_intent_payload(
        ready_payload: dict[str, object],
        *,
        locked_source: _LegacySQLiteMigrationSource,
) -> dict[str, object]:
    current_snapshots = _legacy_sqlite_migration_sidecar_snapshots(
        locked_source.cleanup_snapshot()
    )
    if ready_payload.get("phase") == "cleaning":
        current_snapshots = _reconciled_legacy_sqlite_migration_sidecar_snapshots(
            _legacy_sqlite_migration_sidecar_snapshots(
                ready_payload.get("source_sidecar_snapshots"),
                require_sha256=(
                    ready_payload.get("schema_version") == 4
                ),
            ),
            current_snapshots,
        )
    payload = dict(ready_payload)
    payload["schema_version"] = 4
    payload["phase"] = "cleaning"
    payload["source_main_snapshot"] = list(locked_source.source_snapshot())
    payload["source_main_sha256"] = locked_source.cleanup_sha256()
    payload["source_sidecar_snapshots"] = current_snapshots
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
        descriptor_info = os.fstat(descriptor)
        path_info = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(descriptor_info.st_mode)
            or not stat.S_ISREG(path_info.st_mode)
            or (
                descriptor_info.st_dev,
                descriptor_info.st_ino,
            )
            != (path_info.st_dev, path_info.st_ino)
            or (
                require_single_link
                and (descriptor_info.st_nlink != 1 or path_info.st_nlink != 1)
            )
        ):
            raise OSError("legacy SQLite migration artifact identity is unsafe")
        snapshot: dict[str, object] = {
            "device": descriptor_info.st_dev,
            "inode": descriptor_info.st_ino,
            "size": descriptor_info.st_size,
            "link_count": descriptor_info.st_nlink,
        }
        if include_timestamps:
            snapshot.update({
                "mtime_ns": descriptor_info.st_mtime_ns,
                "ctime_ns": descriptor_info.st_ctime_ns,
            })
        if include_sha256:
            digest = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            snapshot["sha256"] = f"sha256:{digest.hexdigest()}"
        return snapshot
    finally:
        os.close(descriptor)


def _write_legacy_sqlite_migration_intent(
        intent_path: Path,
        payload: dict[str, object],
) -> None:
    encoded = canonical_json_bytes(payload)
    directory_descriptor = os.open(
        intent_path.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    descriptor, temporary_path_text = tempfile.mkstemp(
        dir=intent_path.parent,
        prefix=f".{intent_path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_path_text)
    published = False
    try:
        offset = 0
        while offset < len(encoded):
            written = os.write(descriptor, encoded[offset:])
            if written <= 0:
                raise OSError("legacy SQLite migration intent write did not progress")
            offset += written
        os.fchmod(descriptor, 0o400)
        fsync_durable_file(descriptor)
        try:
            rename_exclusive(
                source_directory_descriptor=directory_descriptor,
                source_name=temporary_path.name,
                destination_directory_descriptor=directory_descriptor,
                destination_name=intent_path.name,
            )
            published = True
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise
            existing = _load_legacy_sqlite_migration_intent(intent_path)
            if existing != payload:
                from mediaforce.web.runtime_lock import MediaforceRuntimeBusyError

                raise MediaforceRuntimeBusyError(
                    "Legacy SQLite migration intent conflicts with existing state"
                ) from exc
        _fsync_directory(intent_path.parent)
    finally:
        os.close(descriptor)
        os.close(directory_descriptor)
        if not published:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


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
    published = False
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
    )
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        for _attempt in range(16):
            temporary_name = (
                f".{intent_name}.{secrets.token_hex(16)}.tmp"
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
        rename_exclusive(
            source_directory_descriptor=directory_descriptor,
            source_name=temporary_name,
            destination_directory_descriptor=directory_descriptor,
            destination_name=intent_name,
        )
        published = True
        os.fsync(directory_descriptor)
        if _load_legacy_sqlite_migration_intent_at(
                directory_descriptor,
                intent_name,
        ) != payload:
            raise OSError("legacy SQLite migration intent restoration failed")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_name and not published:
            try:
                os.unlink(temporary_name, dir_fd=directory_descriptor)
            except FileNotFoundError:
                pass


def _replace_legacy_sqlite_migration_intent(
        intent_path: Path,
        *,
        expected: dict[str, object],
        payload: dict[str, object],
) -> None:
    if _load_legacy_sqlite_migration_intent(intent_path) != expected:
        raise OSError("legacy SQLite migration intent changed before finalization")
    encoded = canonical_json_bytes(payload)
    directory_descriptor = -1
    descriptor = -1
    temporary_path: Path | None = None
    published = False
    try:
        canonical_parent, directory_descriptor = open_stable_directory(
            intent_path.parent
        )
        descriptor, temporary_path_text = tempfile.mkstemp(
            dir=canonical_parent,
            prefix=f".{intent_path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_path_text)
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
        if _load_legacy_sqlite_migration_intent(intent_path) != expected:
            raise OSError(
                "legacy SQLite migration intent changed before finalization"
            )
        os.replace(
            temporary_path.name,
            intent_path.name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        published = True
        os.fsync(directory_descriptor)
        if _load_legacy_sqlite_migration_intent(intent_path) != payload:
            raise OSError("legacy SQLite migration intent finalization failed")
    except FileIntegrityError as exc:
        raise OSError("legacy SQLite migration intent parent is unsafe") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if directory_descriptor >= 0:
            os.close(directory_descriptor)
        if temporary_path is not None and not published:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


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


def _discard_legacy_sqlite_migration_intent(intent_path: Path) -> None:
    try:
        intent_path.unlink()
    except FileNotFoundError:
        return
    _fsync_directory(intent_path.parent)


def _resume_legacy_sqlite_migration_intent(
        config: MediaforceConfig,
        source: Path,
        destination: Path,
        intent_path: Path,
) -> bool:
    from mediaforce.web.runtime_lock import (
        MediaforceRuntimeBusyError,
        exclusive_legacy_sqlite_migration_source,
    )

    try:
        payload = _load_legacy_sqlite_migration_intent(intent_path)
        staging_path = _validated_legacy_sqlite_migration_intent(
            payload,
            source=source,
            destination=destination,
        )
        destination_exists = _path_entry_exists(destination)
        staging_exists = _path_entry_exists(staging_path)
        if payload.get("phase") == "copying":
            if destination_exists or not _path_entry_exists(source):
                raise OSError(
                    "legacy SQLite copying intent has an invalid publication state"
                )
            _discard_legacy_sqlite_staging_path(staging_path)
            _discard_legacy_sqlite_migration_intent(intent_path)
            return False
        staging_snapshot = _legacy_sqlite_migration_snapshot_dict(
            payload.get("staging_snapshot"),
            "staging snapshot",
        )
        if destination_exists:
            with _opened_legacy_sqlite_migration_destination(
                    destination,
                    expected_parent_identity=payload.get(
                        "destination_parent_identity"
                    ),
            ) as destination_binding:
                staging_exists = destination_binding.entry_exists(
                    staging_path.name
                )
                published_link_counts = {2} if staging_exists else {1}
                destination_binding.assert_file_matches(
                    destination.name,
                    staging_snapshot,
                    allowed_link_counts=published_link_counts,
                )
                if staging_exists:
                    destination_binding.assert_file_matches(
                        staging_path.name,
                        staging_snapshot,
                        allowed_link_counts={2},
                    )
                destination_binding.assert_database_valid()

                def assert_destination_authoritative() -> None:
                    destination_binding.assert_file_identity(
                        destination.name,
                        staging_snapshot,
                        allowed_link_counts=published_link_counts,
                    )

                def finish_migration(
                        assert_source_cleanup_complete: Callable[[], None],
                ) -> None:
                    destination_binding.assert_file_matches(
                        destination.name,
                        staging_snapshot,
                        allowed_link_counts=published_link_counts,
                    )
                    destination_binding.assert_database_valid()
                    assert_source_cleanup_complete()
                    if staging_exists:
                        destination_binding.discard_staging(
                            staging_path.name,
                            staging_snapshot,
                        )
                        assert_source_cleanup_complete()
                    _discard_legacy_sqlite_migration_intent_after_source_cleanup(
                        destination_binding=destination_binding,
                        intent_path=intent_path,
                        payload=payload,
                        assert_source_cleanup_complete=(
                            assert_source_cleanup_complete
                        ),
                    )

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
                                )
                            )
                            _replace_legacy_sqlite_migration_intent(
                                intent_path,
                                expected=payload,
                                payload=cleaning_payload,
                            )
                            destination_binding.assert_parent_stable()
                            payload = cleaning_payload
                            locked_source.discard_after_publish(
                                before_remove=assert_destination_authoritative,
                                expected_main_sha256=str(
                                    cleaning_payload["source_main_sha256"]
                                ),
                                expected_sidecar_snapshots=(
                                    _legacy_sqlite_migration_sidecar_snapshots(
                                        cleaning_payload[
                                            "source_sidecar_snapshots"
                                        ]
                                    )
                                ),
                            )
                        finish_migration(locked_source.assert_cleanup_complete)
                else:
                    preserve_unmanifested_artifacts = False
                    if (
                        payload.get("schema_version") == 4
                        and payload.get("phase") == "cleaning"
                    ):
                        sidecar_snapshots = payload.get(
                            "source_sidecar_snapshots"
                        )
                    elif (
                        (
                            payload.get("schema_version") == 2
                            and payload.get("phase") == "ready"
                        )
                        or (
                            payload.get("schema_version") == 3
                            and payload.get("phase") == "cleaning"
                        )
                    ):
                        sidecar_snapshots = None
                        preserve_unmanifested_artifacts = True
                    else:
                        raise OSError(
                            "legacy SQLite migration source disappeared before cleanup was authorized"
                        )
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
                        expected_sidecar_snapshots=sidecar_snapshots,
                        before_remove=assert_destination_authoritative,
                        preserve_unmanifested_artifacts=(
                            preserve_unmanifested_artifacts
                        ),
                    )
                    if retained_artifacts:
                        LOGGER.warning(
                            "Retained legacy SQLite digestless cleanup artifacts after "
                            "safe migration: %s",
                            ", ".join(retained_artifacts),
                        )

                    def assert_source_cleanup_complete() -> None:
                        _assert_legacy_sqlite_source_cleanup_complete(
                            source,
                            expected_parent_identity=payload.get(
                                "source_parent_identity"
                            ),
                            allowed_sidecar_artifacts=retained_artifacts,
                        )

                    finish_migration(assert_source_cleanup_complete)
            return True
        if not _path_entry_exists(source) or not staging_exists:
            raise OSError(
                "legacy SQLite migration intent is missing source or staging state"
            )
        _assert_legacy_sqlite_migration_file_matches(
            staging_path,
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
        _discard_legacy_sqlite_staging_path(staging_path)
        _discard_legacy_sqlite_migration_intent(intent_path)
        return False
    except MediaforceRuntimeBusyError:
        raise
    except (OSError, RuntimeError, sqlite3.DatabaseError, TypeError, ValueError) as exc:
        raise MediaforceRuntimeBusyError(
            "Legacy SQLite migration intent could not be resumed safely"
        ) from exc


def _validated_legacy_sqlite_migration_intent(
        payload: dict[str, object],
        *,
        source: Path,
        destination: Path,
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
    else:
        raise OSError("legacy SQLite migration intent schema is unsupported")
    if (
        set(payload) != expected_keys
        or payload.get("schema") != "mediaforce.legacy_sqlite_migration_intent"
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
    destination_parent_info = destination.parent.stat(follow_symlinks=False)
    source_parent_info = source.parent.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(destination_parent_info.st_mode)
        or payload.get("destination_parent_identity")
        != [destination_parent_info.st_dev, destination_parent_info.st_ino]
    ):
        raise OSError("legacy SQLite migration destination parent changed")
    if (
        not stat.S_ISDIR(source_parent_info.st_mode)
        or payload.get("source_parent_identity")
        != [source_parent_info.st_dev, source_parent_info.st_ino]
    ):
        raise OSError("legacy SQLite migration source parent changed")
    if (
        payload.get("phase") == "copying"
        and payload.get("staging_snapshot") is not None
    ):
        raise OSError("legacy SQLite copying intent is invalid")
    if (
        payload.get("phase") in {"ready", "cleaning"}
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
    return destination.parent / staging_name


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


def _assert_legacy_sqlite_migration_file_matches(
        path: Path,
        expected: dict[str, object],
        *,
        allowed_link_counts: set[int],
) -> None:
    current = _legacy_sqlite_migration_file_snapshot(
        path,
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


def _complete_legacy_sqlite_source_cleanup(
        source: Path,
        *,
        expected_parent_identity: object,
        expected_main_snapshot: object,
        expected_main_sha256: object,
        expected_sidecar_snapshots: object,
        before_remove: Callable[[], None] | None = None,
        preserve_unmanifested_artifacts: bool = False,
) -> tuple[str, ...]:
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
    main_sha256 = (
        str(expected_main_sha256)
        if _legacy_sqlite_migration_sha256_is_valid(expected_main_sha256)
        else None
    )
    sidecar_snapshots = (
        None
        if expected_sidecar_snapshots is None
        else _legacy_sqlite_migration_sidecar_snapshots(
            expected_sidecar_snapshots
        )
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
        def main_exact_match(info: os.stat_result) -> bool:
            return (
                stat.S_ISREG(info.st_mode)
                and (
                    info.st_dev,
                    info.st_ino,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                    info.st_nlink,
                )
                == main_snapshot
            )

        def main_retired_match(info: os.stat_result) -> bool:
            return (
                stat.S_ISREG(info.st_mode)
                and (
                    info.st_dev,
                    info.st_ino,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_nlink,
                )
                == (
                    main_snapshot[0],
                    main_snapshot[1],
                    main_snapshot[2],
                    main_snapshot[3],
                    main_snapshot[5],
                )
            )

        retained_artifacts: list[str] = []
        if main_sha256 is None and preserve_unmanifested_artifacts:
            live_info = _legacy_sqlite_directory_entry_info(
                directory_descriptor,
                source.name,
            )
            quarantine_info = _legacy_sqlite_directory_entry_info(
                directory_descriptor,
                main_quarantine_name,
            )
            if live_info is not None:
                raise OSError(
                    "legacy SQLite migration source cleanup is incomplete"
                )
            if quarantine_info is not None:
                if not main_retired_match(quarantine_info):
                    raise OSError(
                        "legacy SQLite migration cleanup quarantine is unsafe"
                    )
                retained_artifacts.append(main_quarantine_name)
        else:
            _complete_legacy_sqlite_cleanup_entry(
                directory_descriptor=directory_descriptor,
                name=source.name,
                quarantine_name=main_quarantine_name,
                exact_match=main_exact_match,
                retired_match=main_retired_match,
                expected_sha256=main_sha256,
                before_remove=before_remove,
            )
        assert_parent_stable()
        if sidecar_snapshots is None:
            retained_sidecars = tuple(sorted(
                candidate
                for candidate in os.listdir(directory_descriptor)
                if any(
                    candidate == f"{source.name}{suffix}"
                    or candidate.startswith(
                        f".{source.name}{suffix}.mediaforce-retired-"
                    )
                    for suffix in ("-wal", "-shm", "-journal")
                )
            ))
            if retained_sidecars and not preserve_unmanifested_artifacts:
                raise OSError(
                    "legacy SQLite migration source cleanup is incomplete"
                )
            retained_artifacts.extend(retained_sidecars)
        else:
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
                _complete_legacy_sqlite_cleanup_entry(
                    directory_descriptor=directory_descriptor,
                    name=name,
                    quarantine_name=quarantine_name,
                    exact_match=lambda info, expected=expected: (
                        stat.S_ISREG(info.st_mode)
                        and all(
                            current == expected[key]
                            for key, current in (
                                ("device", info.st_dev),
                                ("inode", info.st_ino),
                                ("size", info.st_size),
                                ("link_count", info.st_nlink),
                                ("mtime_ns", info.st_mtime_ns),
                                ("ctime_ns", info.st_ctime_ns),
                                ("uid", info.st_uid),
                                ("mode", stat.S_IMODE(info.st_mode)),
                            )
                        )
                    ),
                    retired_match=lambda info, expected=expected: (
                        stat.S_ISREG(info.st_mode)
                        and all(
                            current == expected[key]
                            for key, current in (
                                ("device", info.st_dev),
                                ("inode", info.st_ino),
                                ("size", info.st_size),
                                ("link_count", info.st_nlink),
                                ("mtime_ns", info.st_mtime_ns),
                                ("uid", info.st_uid),
                                ("mode", stat.S_IMODE(info.st_mode)),
                            )
                        )
                    ),
                    expected_sha256=str(expected["sha256"]),
                    before_remove=before_remove,
                )
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
        assert_parent_stable()
        os.fsync(directory_descriptor)
        assert_parent_stable()
        return tuple(sorted(retained_artifacts))
    except FileIntegrityError as exc:
        raise OSError("legacy SQLite migration source parent is unsafe") from exc
    finally:
        if directory_descriptor >= 0:
            os.close(directory_descriptor)


def _assert_legacy_sqlite_source_cleanup_complete(
        source: Path,
        *,
        expected_parent_identity: object,
        allowed_sidecar_artifacts: tuple[str, ...],
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
        if current_cleanup_artifacts != set(allowed_sidecar_artifacts):
            raise OSError("legacy SQLite migration source cleanup is incomplete")
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
        exact_match: Callable[[os.stat_result], bool],
        retired_match: Callable[[os.stat_result], bool],
        expected_sha256: str | None,
        before_remove: Callable[[], None] | None,
) -> None:
    def digest_matches(candidate_name: str) -> bool:
        if expected_sha256 is None:
            return False
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
        return
    if quarantine_info is not None:
        if (
            not retired_match(quarantine_info)
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
        if not retired_match(current) or not digest_matches(quarantine_name):
            raise OSError("legacy SQLite migration cleanup quarantine changed")
        os.unlink(quarantine_name, dir_fd=directory_descriptor)
        os.fsync(directory_descriptor)
        if _legacy_sqlite_directory_entry_exists(
                directory_descriptor,
                name,
        ) or _legacy_sqlite_directory_entry_exists(
                directory_descriptor,
                quarantine_name,
        ):
            raise OSError("legacy SQLite migration source cleanup is incomplete")
        return
    if (
        live_info is None
        or not exact_match(live_info)
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
    if not retired_match(current) or not digest_matches(quarantine_name):
        raise OSError("legacy SQLite migration cleanup claimed an unsafe entry")
    os.fsync(directory_descriptor)
    current = os.stat(
        quarantine_name,
        dir_fd=directory_descriptor,
        follow_symlinks=False,
    )
    if not retired_match(current) or not digest_matches(quarantine_name):
        raise OSError("legacy SQLite migration cleanup quarantine changed")
    os.unlink(quarantine_name, dir_fd=directory_descriptor)
    os.fsync(directory_descriptor)
    if _legacy_sqlite_directory_entry_exists(
            directory_descriptor,
            name,
    ) or _legacy_sqlite_directory_entry_exists(
            directory_descriptor,
            quarantine_name,
    ):
        raise OSError("legacy SQLite migration source cleanup is incomplete")


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


def _create_legacy_sqlite_staging_path(staging_path: Path) -> None:
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(staging_path, flags, 0o600)
    try:
        info = os.fstat(descriptor)
        path_info = staging_path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or (info.st_dev, info.st_ino)
            != (path_info.st_dev, path_info.st_ino)
        ):
            raise OSError("legacy SQLite migration staging file is unsafe")
    finally:
        os.close(descriptor)


@contextmanager
def _copied_legacy_sqlite_database(
        locked_source: _LegacySQLiteMigrationSource,
        staging_path: Path,
) -> Iterator[None]:
    source_connection: sqlite3.Connection | None = None
    staging_connection: sqlite3.Connection | None = None
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
            staging_connection = sqlite3.connect(
                staging_path,
                timeout=0,
                isolation_level=None,
            )
            source_connection.backup(staging_connection)
            locked_source.assert_connection_bound(source_connection)
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
        if staging_connection is not None:
            staging_connection.close()
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


def _discard_legacy_sqlite_staging_path(staging_path: Path) -> None:
    directory_descriptor = -1
    removed = False
    try:
        _, directory_descriptor = open_stable_directory(staging_path.parent)
        for name in (
                f"{staging_path.name}-wal",
                f"{staging_path.name}-shm",
                f"{staging_path.name}-journal",
                staging_path.name,
        ):
            try:
                info = os.stat(
                    name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                continue
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink not in {1, 2}
            ):
                raise OSError("legacy SQLite migration staging cleanup is unsafe")
            os.unlink(name, dir_fd=directory_descriptor)
            removed = True
        if removed:
            os.fsync(directory_descriptor)
    except FileIntegrityError as exc:
        raise OSError("legacy SQLite migration staging parent is unsafe") from exc
    finally:
        if directory_descriptor >= 0:
            os.close(directory_descriptor)


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
