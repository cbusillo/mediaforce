import copy
from collections.abc import Callable
from contextlib import contextmanager
import json
import os
import shutil
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "defaults.toml"
FOLDER_POLICY_OVERRIDES_KEY = "folder_policy_overrides"
BENCH_SAVED_OVERRIDE_NOTE = "Saved from the calibration bench."


@dataclass(frozen=True)
class ConfigPaths:
    project_root: Path
    config_path: Path
    db_path: Path
    run_manifest_dir: Path
    web_state_dir: Path
    review_dir: Path
    runtime_settings_path: Path


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
    def source_root_map(self) -> dict[str, Path]:
        return {
            key: Path(value).expanduser()
            for key, value in self.media["source_roots"].items()
        }

    def source_root_map_for_host(self, host: dict[str, Any] | None = None) -> dict[str, Path]:
        resolved = dict(self.source_root_map)
        if not isinstance(host, dict):
            return resolved
        overrides = host.get("source_roots")
        if not isinstance(overrides, dict):
            return resolved
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
        matching_overrides: list[tuple[int, int, dict[str, Any]]] = []
        for index, override in enumerate(self.overrides):
            prefix = str(override.get("path_prefix", "")).strip("/")
            if prefix and not _path_prefix_matches(normalized_rel_path, prefix):
                continue
            matching_overrides.append((len(prefix), index, override))

        for _, _, override in sorted(matching_overrides):
            for section in ("video", "audio", "subtitle", "planning"):
                values = override.get(section)
                if isinstance(values, dict):
                    policy[section].update(copy.deepcopy(values))
        return policy


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
    paths = ConfigPaths(
        project_root=project_root,
        config_path=resolved_config_path,
        db_path=_resolve_path(project_root, state["db_path"]),
        run_manifest_dir=_resolve_path(project_root, state["run_manifest_dir"]),
        web_state_dir=_resolve_path(project_root, state.get("web_state_dir", "state/web")),
        review_dir=_resolve_path(project_root, state.get("review_dir", "state/review")),
        runtime_settings_path=runtime_settings_path,
    )
    _migrate_project_state(project_root, paths)
    return MediaforceConfig(raw=raw, paths=paths)


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


def _migrate_project_state(project_root: Path, paths: ConfigPaths) -> None:
    state_root = project_root / "state"
    if not state_root.exists():
        return

    migrations = (
        (state_root / "library.sqlite3", paths.db_path),
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
    _deep_merge(merged, payload, extend_lists=False)
    return merged


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
def _locked_runtime_settings(path: Path):
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
