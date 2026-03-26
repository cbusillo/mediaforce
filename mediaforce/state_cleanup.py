from __future__ import annotations

import shutil
import time
from pathlib import Path

from mediaforce.config import HarnessConfig


SECONDS_PER_DAY = 86400
SECONDS_PER_MINUTE = 60


def purge_transient_artifacts(config: HarnessConfig, *, force: bool = False) -> bool:
    if not force and not _cleanup_is_due(config):
        return False

    retention_days = _retention_days(config)
    if retention_days <= 0:
        _write_cleanup_stamp(config)
        return True

    cutoff = time.time() - (retention_days * SECONDS_PER_DAY)
    _prune_children(config.paths.review_dir, cutoff)
    _prune_children(config.staging_root / "_calibration", cutoff)
    _prune_matching_files(config.paths.web_state_dir, "calibration-*.json", cutoff)
    _prune_matching_files(config.paths.web_state_dir, "*.job.json", cutoff)
    _write_cleanup_stamp(config)
    return True


def _retention_days(config: HarnessConfig) -> int:
    cleanup = config.raw.get("state", {}).get("cleanup", {})
    value = cleanup.get("transient_artifact_retention_days", 14)
    return max(int(value), 0)


def _cleanup_interval_seconds(config: HarnessConfig) -> int:
    cleanup = config.raw.get("state", {}).get("cleanup", {})
    value = cleanup.get("periodic_sweep_minutes", 60)
    return max(int(value), 0) * SECONDS_PER_MINUTE


def _cleanup_stamp_path(config: HarnessConfig) -> Path:
    return config.paths.web_state_dir / ".cleanup-stamp"


def _cleanup_is_due(config: HarnessConfig) -> bool:
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


def _write_cleanup_stamp(config: HarnessConfig) -> None:
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


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return time.time()
