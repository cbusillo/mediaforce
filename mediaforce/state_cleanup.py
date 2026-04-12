import shutil
import time
from pathlib import Path

from mediaforce.core.config import MediaforceConfig
from mediaforce.encoding.quality import default_local_quality_temp_root, legacy_local_quality_temp_root, \
    previous_local_quality_temp_root

SECONDS_PER_DAY = 86400
SECONDS_PER_MINUTE = 60


def purge_transient_artifacts(config: MediaforceConfig, *, force: bool = False) -> bool:
    if not force and not _cleanup_is_due(config):
        return False

    retention_days = _retention_days(config)
    if retention_days <= 0:
        _write_cleanup_stamp(config)
        return True

    cutoff = time.time() - (retention_days * SECONDS_PER_DAY)
    _prune_children(config.paths.review_dir, cutoff)
    for staging_root in _staging_roots_for_cleanup(config):
        _prune_children(staging_root / "_calibration", cutoff)
        _prune_matching_directories(staging_root, ".ab-av1-*", cutoff)
        _prune_matching_directories(staging_root, ".mediaforce-ab-av1-*", cutoff)
    _prune_matching_files(config.paths.web_state_dir, "calibration-*.json", cutoff)
    _prune_matching_files(config.paths.web_state_dir, "*.job.json", cutoff)
    _write_cleanup_stamp(config)
    return True


def _retention_days(config: MediaforceConfig) -> int:
    cleanup = config.raw.get("state", {}).get("cleanup", {})
    value = cleanup.get("transient_artifact_retention_days", 14)
    return max(int(value), 0)


def _cleanup_interval_seconds(config: MediaforceConfig) -> int:
    cleanup = config.raw.get("state", {}).get("cleanup", {})
    value = cleanup.get("periodic_sweep_minutes", 60)
    return max(int(value), 0) * SECONDS_PER_MINUTE


def _cleanup_stamp_path(config: MediaforceConfig) -> Path:
    return config.paths.web_state_dir / ".cleanup-stamp"


def _cleanup_is_due(config: MediaforceConfig) -> bool:
    interval_seconds = _cleanup_interval_seconds(config)
    if interval_seconds <= 0:
        return True
    stamp_path = _cleanup_stamp_path(config)
    try:
        last_run = stamp_path.stat().st_mtime
    except FileNotFoundError:
        return True
    except OSError:
        return True
    return time.time() - last_run >= interval_seconds


def _write_cleanup_stamp(config: MediaforceConfig) -> None:
    stamp_path = _cleanup_stamp_path(config)
    stamp_path.parent.mkdir(parents=True, exist_ok=True)
    stamp_path.touch()


def _prune_children(root: Path, cutoff: float) -> None:
    if not root.exists():
        return
    try:
        children = list(root.iterdir())
    except OSError:
        return
    for child in children:
        if _mtime(child) >= cutoff:
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
            continue
        try:
            child.unlink(missing_ok=True)
        except OSError:
            continue


def _prune_matching_files(root: Path, pattern: str, cutoff: float) -> None:
    if not root.exists():
        return
    try:
        paths = list(root.glob(pattern))
    except OSError:
        return
    for path in paths:
        if _mtime(path) >= cutoff:
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue


def _prune_matching_directories(root: Path, pattern: str, cutoff: float) -> None:
    if not root.exists():
        return
    try:
        paths = list(root.glob(pattern))
    except OSError:
        return
    for path in paths:
        if _mtime(path) >= cutoff:
            continue
        if not path.is_dir():
            continue
        shutil.rmtree(path, ignore_errors=True)


def _staging_roots_for_cleanup(config: MediaforceConfig) -> list[Path]:
    roots: list[Path] = []

    def add_root(path: Path) -> None:
        if any(existing == path for existing in roots):
            return
        roots.append(path)

    add_root(config.staging_root)
    add_root(config.paths.web_state_dir / "quality-temp")
    add_root(previous_local_quality_temp_root())
    add_root(legacy_local_quality_temp_root())
    add_root(default_local_quality_temp_root())
    for host in config.remote_hosts:
        add_root(config.staging_root_for_host(host))
    return roots


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return time.time()
