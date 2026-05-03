import logging
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import open_db
from mediaforce.core.db_tables import encode_jobs
from mediaforce.core.type_defs import object_list
from mediaforce.encoding.quality import (
    default_local_quality_temp_root,
    legacy_local_quality_temp_root,
    previous_local_quality_temp_root,
)
from mediaforce.hosts import DEFAULT_HOST_CAPABILITIES
from mediaforce.hosts.config import host_targets_current_machine
from mediaforce.remote import run_remote_command

SECONDS_PER_DAY = 86400
SECONDS_PER_MINUTE = 60
DEFAULT_ORPHAN_TEMP_RETENTION_MINUTES = 6 * 60
REMOTE_PROCESS_PROBE_TIMEOUT_SECONDS = 4
ACTIVE_ARTIFACT_STATUSES = {
    "queued",
    "running",
    "retry_backoff",
    "pending_review",
    "needs_attention",
}
TRANSIENT_TEMP_PATTERNS = (".ab-av1-*", ".mediaforce-ab-av1-*")

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class CleanupProcessSnapshot:
    commands: tuple[str, ...]
    unverified_roots: tuple[Path, ...]


def purge_transient_artifacts(config: MediaforceConfig, *, force: bool = False) -> bool:
    if not force and not _cleanup_is_due(config):
        return False

    retention_days = _retention_days(config)
    if retention_days <= 0:
        _write_cleanup_stamp(config)
        return True

    now = time.time()
    cutoff = now - (retention_days * SECONDS_PER_DAY)
    temp_cutoff = max(cutoff, now - _orphan_temp_retention_seconds(config))
    process_snapshot = _cleanup_process_snapshot(config)
    _prune_children(config.paths.review_dir, cutoff)
    for staging_root in _staging_roots_for_cleanup(config):
        root_verified = not _root_requires_unverified_process_check(
            staging_root, process_snapshot
        )
        if root_verified:
            _prune_children(
                staging_root / "_calibration",
                cutoff,
                active_commands=process_snapshot.commands,
                root_verified=root_verified,
            )
        for pattern in TRANSIENT_TEMP_PATTERNS:
            _prune_matching_directories(
                staging_root,
                pattern,
                temp_cutoff,
                active_commands=process_snapshot.commands,
                root_verified=root_verified,
            )
    _prune_matching_files(config.paths.web_state_dir, "calibration-*.json", cutoff)
    _prune_matching_files(config.paths.web_state_dir, "*.job.json", cutoff)
    _prune_run_manifests(config, cutoff)
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


def _orphan_temp_retention_seconds(config: MediaforceConfig) -> int:
    cleanup = config.raw.get("state", {}).get("cleanup", {})
    value = cleanup.get(
        "orphan_temp_retention_minutes", DEFAULT_ORPHAN_TEMP_RETENTION_MINUTES
    )
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


def _prune_children(
    root: Path,
    cutoff: float,
    *,
    active_commands: tuple[str, ...] = (),
    root_verified: bool = True,
) -> None:
    if not root.exists():
        return
    try:
        children = list(root.iterdir())
    except OSError:
        return
    for child in children:
        if _mtime(child) >= cutoff:
            continue
        if _should_skip_process_owned_path(
            child, active_commands=active_commands, root_verified=root_verified
        ):
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


def _prune_matching_directories(
    root: Path,
    pattern: str,
    cutoff: float,
    *,
    active_commands: tuple[str, ...] = (),
    root_verified: bool = True,
) -> None:
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
        if _should_skip_process_owned_path(
            path, active_commands=active_commands, root_verified=root_verified
        ):
            continue
        shutil.rmtree(path, ignore_errors=True)


def _prune_run_manifests(config: MediaforceConfig, cutoff: float) -> None:
    root = config.paths.run_manifest_dir
    if not root.exists():
        return
    protected_paths = _active_manifest_paths(config)
    try:
        paths = list(root.glob("*.json"))
    except OSError:
        return
    for path in paths:
        if _mtime(path) >= cutoff:
            continue
        if path.resolve() in protected_paths:
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue


def _active_manifest_paths(config: MediaforceConfig) -> set[Path]:
    if not config.paths.db_path.exists():
        return set()
    protected: set[Path] = set()
    try:
        with open_db(config.paths.db_path) as connection:
            rows = (
                connection.execute(
                    select(encode_jobs.c.manifest_path, encode_jobs.c.status).where(
                        encode_jobs.c.manifest_path.is_not(None)
                    )
                )
                .mappings()
                .fetchall()
            )
    except Exception as exc:
        LOGGER.warning("Could not inspect encode manifests for cleanup: %s", exc)
        return {path.resolve() for path in config.paths.run_manifest_dir.glob("*.json")}
    for row in rows:
        status = f"{row['status'] or ''}".strip()
        manifest_path = f"{row['manifest_path'] or ''}".strip()
        if not manifest_path or status not in ACTIVE_ARTIFACT_STATUSES:
            continue
        protected.add(Path(manifest_path).expanduser().resolve())
    return protected


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


def _cleanup_process_snapshot(config: MediaforceConfig) -> CleanupProcessSnapshot:
    commands: list[str] = []
    unverified_roots: list[Path] = []
    try:
        commands.extend(_local_process_commands())
    except Exception as exc:
        LOGGER.warning("Could not inspect local processes for cleanup: %s", exc)
        unverified_roots.extend(_staging_roots_for_cleanup(config))

    for host in _remote_hosts_requiring_process_probe(config):
        root = config.staging_root_for_host(host)
        try:
            result = run_remote_command(
                host, ["ps", "-axo", "command="], REMOTE_PROCESS_PROBE_TIMEOUT_SECONDS
            )
        except Exception as exc:
            LOGGER.warning(
                "Could not inspect remote processes for cleanup on %s: %s",
                _host_label(host),
                exc,
            )
            _add_unique_path(unverified_roots, root)
            continue
        if result.returncode != 0:
            detail = (
                result.stderr.strip()
                or result.stdout.strip()
                or f"exit {result.returncode}"
            )
            LOGGER.warning(
                "Could not inspect remote processes for cleanup on %s: %s",
                _host_label(host),
                detail,
            )
            _add_unique_path(unverified_roots, root)
            continue
        commands.extend(
            line.strip() for line in result.stdout.splitlines() if line.strip()
        )

    return CleanupProcessSnapshot(tuple(commands), tuple(unverified_roots))


def _local_process_commands() -> list[str]:
    result = subprocess.run(
        ["ps", "-axo", "command="],
        capture_output=True,
        text=True,
        timeout=3,
    )
    if result.returncode != 0:
        detail = (
            result.stderr.strip()
            or result.stdout.strip()
            or f"exit {result.returncode}"
        )
        raise RuntimeError(detail)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _remote_hosts_requiring_process_probe(
    config: MediaforceConfig,
) -> list[dict[str, object]]:
    hosts: list[dict[str, object]] = []
    for host in config.remote_hosts:
        raw_mode = host.get("mode")
        if raw_mode is not None and not str(raw_mode).strip():
            continue
        mode = str(raw_mode or "ssh").strip().lower() or "ssh"
        if mode != "ssh":
            continue
        if host_targets_current_machine(host):
            continue
        capabilities = {
            str(capability).strip().lower()
            for capability in object_list(
                host.get("capabilities") or list(DEFAULT_HOST_CAPABILITIES)
            )
            if str(capability).strip()
        }
        if capabilities.isdisjoint({"encode_queue", "sample_calibration"}):
            continue
        hosts.append(host)
    return hosts


def _root_requires_unverified_process_check(
    root: Path, snapshot: CleanupProcessSnapshot
) -> bool:
    resolved_root = root.resolve()
    return any(
        resolved_root == unverified.resolve()
        for unverified in snapshot.unverified_roots
    )


def _should_skip_process_owned_path(
    path: Path,
    *,
    active_commands: tuple[str, ...],
    root_verified: bool,
) -> bool:
    if _is_process_scoped_temp_path(path) and not root_verified:
        return True
    path_text = str(path)
    return bool(path_text) and any(path_text in command for command in active_commands)


def _is_process_scoped_temp_path(path: Path) -> bool:
    return path.name.startswith(".mediaforce-ab-av1-") or path.name.startswith(
        ".ab-av1-"
    )


def _add_unique_path(paths: list[Path], path: Path) -> None:
    resolved = path.expanduser()
    if any(existing == resolved for existing in paths):
        return
    paths.append(resolved)


def _host_label(host: dict[str, object]) -> str:
    return str(
        host.get("label") or host.get("host") or host.get("key") or "remote host"
    )


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return time.time()
