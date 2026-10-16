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
from mediaforce.hosts.config import configured_remote_host_execution_mode
from mediaforce.remote import run_remote_command
from mediaforce.ops.runtime_install import RuntimeProtectionError, paths_overlap, protected_runtime_roots

SECONDS_PER_DAY = 86400
SECONDS_PER_MINUTE = 60
DEFAULT_ORPHAN_TEMP_RETENTION_MINUTES = 6 * 60
DEFAULT_KEPT_DEPLOY_BACKUPS = 2
DEFAULT_OPERATION_EVIDENCE_RETENTION_DAYS = 30
# Finder writes these when someone opens a folder; they say nothing about when the work happened.
IGNORED_FOR_AGE = frozenset({".DS_Store"})
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

    staging_roots = _staging_roots_for_cleanup(config)
    support_dir = config.paths.db_path.parent
    try:
        protected = (*protected_runtime_roots(support_dir), Path(__file__).resolve().parents[1])
    except RuntimeProtectionError as exc:
        LOGGER.warning("%s", exc)
        return False
    cleanup_roots = (
        config.paths.review_dir, config.paths.web_state_dir, config.paths.run_manifest_dir,
        support_dir / "deployments", support_dir / "operations", *staging_roots,
    )
    if any(paths_overlap(cleanup, runtime) for cleanup in cleanup_roots for runtime in protected):
        LOGGER.warning("Cleanup refused: a cleanup path overlaps a protected controller runtime")
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
    for staging_root in staging_roots:
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
    support_dir = config.paths.db_path.parent
    _prune_deploy_backups(support_dir / "deployments", keep=_kept_deploy_backups(config))
    _prune_operation_evidence(
        support_dir / "operations",
        now - _operation_evidence_retention_days(config) * SECONDS_PER_DAY,
    )
    _write_cleanup_stamp(config)
    return True


def _retention_days(config: MediaforceConfig) -> int:
    cleanup = config.raw.get("state", {}).get("cleanup", {})
    value = cleanup.get("transient_artifact_retention_days", 14)
    return max(int(value), 0)


def _kept_deploy_backups(config: MediaforceConfig) -> int:
    cleanup = config.raw.get("state", {}).get("cleanup", {})
    return max(int(cleanup.get("kept_deploy_backups", DEFAULT_KEPT_DEPLOY_BACKUPS)), 1)


def _operation_evidence_retention_days(config: MediaforceConfig) -> int:
    cleanup = config.raw.get("state", {}).get("cleanup", {})
    value = cleanup.get("operation_evidence_retention_days", DEFAULT_OPERATION_EVIDENCE_RETENTION_DAYS)
    return max(int(value), 1)


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


def _prune_deploy_backups(root: Path, *, keep: int) -> None:
    """Keep the newest deploy folders; only the latest database copy is a useful rollback."""
    folders = sorted(_child_directories(root), key=_content_mtime, reverse=True)
    for folder in folders[keep:]:
        shutil.rmtree(folder, ignore_errors=True)


def _prune_operation_evidence(root: Path, cutoff: float) -> None:
    for folder in _child_directories(root):
        if _content_mtime(folder) < cutoff:
            shutil.rmtree(folder, ignore_errors=True)


def _child_directories(root: Path) -> list[Path]:
    try:
        return [child for child in root.iterdir() if child.is_dir() and not child.is_symlink()]
    except OSError:
        return []


def _content_mtime(folder: Path) -> float:
    """When the work in a folder last changed, judged from its files rather than the folder itself."""
    newest = 0.0
    try:
        files = [path for path in folder.rglob("*") if path.is_file() and path.name not in IGNORED_FOR_AGE]
    except OSError:
        return time.time()
    for path in files:
        newest = max(newest, _mtime(path))
    return newest or _mtime(folder)


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
        except Exception:
            LOGGER.warning(
                "Nonfatal cleanup reachability warning; remote staging cleanup was skipped"
            )
            _add_unique_path(unverified_roots, root)
            continue
        if result.returncode != 0:
            LOGGER.warning(
                "Nonfatal cleanup reachability warning; remote staging cleanup was skipped"
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
        if configured_remote_host_execution_mode(host) != "ssh":
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


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return time.time()
