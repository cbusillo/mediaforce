"""Staged controller deployment. Invoke only in an authorized deploy window."""
from __future__ import annotations

import argparse
from collections.abc import Callable
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import json
from http.client import HTTPException
import os
import plistlib
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from urllib.request import Request, urlopen
from uuid import uuid4

from mediaforce.core.config import MediaforceConfig, load_config
from mediaforce.core.db import open_db, open_readonly_db
from mediaforce.core.utils import timestamp
from mediaforce.encoding.encode_queue import DEFAULT_QUEUE_NAME, load_queue_state, save_queue_state
from mediaforce.ops.login_item import (
    LoginItemPaths, _atomic_write, _bootstrap, _launch_domain, _launch_target,
    _require_success, _run_command, disable_login_item, enable_login_item,
)
from mediaforce.ops.runtime_install import (
    register_runtime_roots, require_release_directory, retain_deploy_stage, retained_deploy_stage, runtime_activity, runtime_directory,
)
from mediaforce.web.runtime.controller_storage_recovery import READY_FRESHNESS_SECONDS
from mediaforce.web.runtime_lock import exclusive_mediaforce_runtime_lock, reserve_mediaforce_database_identity


DEFAULT_STARTUP_TIMEOUT = 180
DEPLOY_LOCK_WAIT_SECONDS = 600
MAC_APP_NAME = "Mediaforce.app"
APPLICATIONS = Path("/Applications")
KEPT_RELEASES = 3
STORAGE_CHECK_SECONDS = 60


def _run(command: list[str], *, cwd: Path | None = None) -> str:
    environment = os.environ.copy()
    environment.pop("VIRTUAL_ENV", None)
    if command[0] == "uv":
        if cwd is None:
            raise ValueError("dependency setup requires an explicit release directory")
        environment["UV_PROJECT_ENVIRONMENT"] = str(cwd / ".venv")
        environment.pop("UV_PROJECT", None)
        environment.pop("UV_WORKING_DIR", None)
    if command[0] == "git":
        environment = {key: value for key, value in environment.items() if not key.startswith("GIT_")}
    return subprocess.run(command, cwd=cwd, env=environment, check=True, text=True, capture_output=True).stdout.strip()


def _api(base_url: str, route: str, *, post: bool = False) -> dict:
    with urlopen(Request(base_url + route, method="POST" if post else "GET"), timeout=10) as response:
        return json.load(response)


def require_paused_idle_dashboard(dashboard: dict) -> None:
    queue, calibration = dashboard["encode_queue"], dashboard["calibration_queue"]
    if queue["state"]["is_paused"] is not True or any(
        value != 0 for value in (queue["running_count"], calibration["sample"]["running_count"], calibration["full"]["running_count"])
    ):
        raise RuntimeError("work is running or the encode queue is not paused; not deploying")


def require_storage_admission(base_url: str, started_at: datetime) -> None:
    """A record written once at startup reads fresh for a whole freshness window, so the candidate passes only when
    its storage check is still running a full window after its first pass and still lets work start or names a
    real reason (#1340)."""
    first_pass = _storage_pass_since(base_url, started_at, checked=False, wait_seconds=STORAGE_CHECK_SECONDS)
    _storage_pass_since(
        base_url, first_pass + timedelta(seconds=READY_FRESHNESS_SECONDS), checked=True,
        wait_seconds=READY_FRESHNESS_SECONDS + STORAGE_CHECK_SECONDS,
    )


def _storage_pass_since(base_url: str, since: datetime, *, checked: bool, wait_seconds: int) -> datetime:
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            hosts = _api(base_url, "/api/hosts")
            admission = hosts["controller_storage_admission"]
            updated_at = hosts["controller_storage"]["updated_at"]
            passed_at = datetime.fromisoformat(updated_at) if updated_at else None
            if passed_at is not None and passed_at >= since and (admission["checked"] is True or not checked):
                return passed_at
            issue = admission["issue"] or "its storage check has not run again"
        except (OSError, HTTPException, ValueError, KeyError, TypeError) as exc:
            issue = f"storage could not be read ({exc})"
        if time.monotonic() >= deadline:
            raise RuntimeError(f"candidate storage check is not keeping work admissible: {issue}")
        time.sleep(5)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def prepare_release(source: Path, target: str, home: Path) -> Path:
    sha = _run(["git", "rev-parse", "--verify", f"{target}^{{commit}}"], cwd=source)
    if len(sha) != 40 or any(character not in "0123456789abcdef" for character in sha):
        raise RuntimeError("target did not resolve to a full commit SHA")
    root = runtime_directory(home) / "releases" / f"{sha}-{uuid4().hex[:8]}"
    if root.exists() or root.is_symlink():
        raise RuntimeError(f"release already exists; preserve it and reconcile before retrying: {root}")
    if root.absolute() != root.resolve():
        raise RuntimeError("runtime install path contains a symlink; refusing deployment")
    root.parent.mkdir(parents=True, exist_ok=True)
    # --no-local prevents object hardlinks/alternates back into disposable source trees.
    _run(["git", "clone", "--no-local", "--no-checkout", str(source), str(root)])
    require_release_directory(root, home)
    _run(["git", "fetch", "--no-tags", str(source), sha], cwd=root)
    _run(["git", "checkout", "--detach", sha], cwd=root)
    _run(["uv", "sync", "--project", str(root), "--locked", "--no-dev"], cwd=root)
    _run(["bash", str(root / "scripts/build-mac-app.sh"), "build", str(built_mac_app(root).parent)], cwd=root)
    if not (root / ".venv/bin/mediaforce-web").is_file():
        raise RuntimeError("prepared runtime lacks its entrypoint")
    if not built_mac_app(root).is_dir():
        raise RuntimeError("prepared runtime lacks its Mac app")
    return root


def built_mac_app(release: Path) -> Path:
    return release / ".build/release-app" / MAC_APP_NAME


def install_mac_app(release: Path, applications: Path = APPLICATIONS) -> None:
    installed = applications / MAC_APP_NAME
    incoming = applications / f".{MAC_APP_NAME}.incoming"
    replaced = applications / f".{MAC_APP_NAME}.replaced"
    _restore_interrupted_replacement(installed, replaced)
    shutil.rmtree(incoming, ignore_errors=True)
    shutil.copytree(built_mac_app(release), incoming, symlinks=True)
    if _is_present(installed):
        shutil.rmtree(replaced, ignore_errors=True)
        installed.rename(replaced)
    try:
        incoming.rename(installed)
    except OSError:
        _restore_interrupted_replacement(installed, replaced)
        raise
    shutil.rmtree(replaced, ignore_errors=True)


def _restore_interrupted_replacement(installed: Path, replaced: Path) -> None:
    if not _is_present(installed) and _is_present(replaced):
        replaced.rename(installed)


def _is_present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def trim_releases(home: Path, support_dir: Path) -> None:
    stage = retained_deploy_stage(support_dir)
    if stage is None or stage.absolute() != stage.resolve():
        raise RuntimeError("cannot verify the recorded rollback stage; releases retained")
    previous = Path(plistlib.loads((stage / "launch-item.before.plist").read_bytes())["WorkingDirectory"])
    candidate = Path((stage / "candidate.txt").read_text())
    plist = LoginItemPaths(project_root=candidate, home=home).plist
    running = Path(plistlib.loads(plist.read_bytes())["WorkingDirectory"])
    require_release_directory(running, home)
    if (
        candidate != running or previous == running or not previous.is_absolute()
        or previous.absolute() != previous.resolve() or not previous.is_dir()
    ):
        raise RuntimeError(
            f"cannot verify running and rollback releases; releases retained: "
            f"stage={stage}, candidate={candidate}, running={running}, rollback={previous}"
        )
    releases = [path for path in running.parent.iterdir() if not path.is_file() or path.is_symlink()]
    for release in releases:
        require_release_directory(release, home)
    releases.sort(key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True)
    kept = {running, previous} & set(releases)
    for release in releases:
        if len(kept) < KEPT_RELEASES:
            kept.add(release)
    for release in releases:
        if release not in kept:
            shutil.rmtree(release)


def backup_database(database: Path, destination: Path, *, pause_queue: bool = False) -> None:
    if destination.exists():
        raise RuntimeError("backup already exists; use a fresh deploy stage")
    with sqlite3.connect(database.resolve().as_uri() + "?mode=rw", uri=True) as source:
        with sqlite3.connect(destination) as backup:
            source.backup(backup)
            if pause_queue:
                backup.execute(
                    "INSERT INTO encode_queue_state (queue_name, is_paused, stop_requested, active_job_id, updated_at) "
                    "VALUES (?, 1, 0, NULL, ?) ON CONFLICT (queue_name) DO UPDATE SET "
                    "is_paused = 1, updated_at = excluded.updated_at",
                    (DEFAULT_QUEUE_NAME, timestamp()),
                )
                backup.commit()
            if backup.execute("pragma quick_check").fetchall() != [("ok",)]:
                raise RuntimeError("database backup failed quick_check")


def require_stopped_idle_controller(config: MediaforceConfig, *, port: int = 8777) -> bool:
    """Verify stopped custody and return whether the queue was already paused."""
    require_closed_listener(port)
    with open_readonly_db(config.paths.db_path) as connection:
        counts = [
            connection.exec_driver_sql(statement).scalar_one()
            for statement in (
                "SELECT COUNT(*) FROM encode_jobs WHERE status = 'running'",
                "SELECT COUNT(*) FROM calibration_jobs WHERE status IN ('starting', 'running')",
                "SELECT COUNT(*) FROM scan_runs WHERE status = 'running'",
                "SELECT COUNT(*) FROM library_item_evidence_state WHERE work_status = 'running'",
            )
        ]
        was_paused = bool(load_queue_state(connection)["is_paused"])
    if any(counts):
        raise RuntimeError("stopped controller still records running work; reconcile that work before deploying")
    return was_paused


def require_closed_listener(port: int = 8777) -> None:
    if not _listener_is_closed(port):
        raise RuntimeError(f"port {port} is still listening")


def _listener_is_closed(port: int = 8777) -> bool:
    probe = subprocess.run(["/usr/sbin/lsof", f"-tiTCP:{port}", "-sTCP:LISTEN"], capture_output=True, check=False)
    if probe.returncode == 1 and not probe.stdout and not probe.stderr:
        return True
    if probe.returncode != 0:
        raise RuntimeError("cannot verify listener state")
    return False


def pause_stopped_queue(config: MediaforceConfig) -> None:
    with open_db(config.paths.db_path) as connection:
        state = load_queue_state(connection)
        state.update(is_paused=True, updated_at=timestamp())
        save_queue_state(connection, state)


def deploy(
    source: Path, target: str, stage: Path, home: Path, base_url: str,
    *, database: Path, config_path: Path | None = None, startup_timeout: int = DEFAULT_STARTUP_TIMEOUT,
    stopped: bool = False,
) -> None:
    # The release flow's own pause request can start the hourly cleanup, so the deploy waits for it (#1109).
    with runtime_activity(database.parent, wait_seconds=DEPLOY_LOCK_WAIT_SECONDS) as acquired:
        if not acquired:
            raise RuntimeError(f"cleanup or another deploy stayed active for {DEPLOY_LOCK_WAIT_SECONDS // 60} minutes; retry after it finishes")
        with ExitStack() as stopped_runtime:
            config = load_config(config_path) if stopped else None
            if config is not None:
                if config.paths.db_path.resolve() != database.resolve():
                    raise RuntimeError("stopped deployment database does not match the installed configuration")
                stopped_runtime.enter_context(exclusive_mediaforce_runtime_lock(config, owner_payload={"operation": "stopped-deploy"}))
                reserve_mediaforce_database_identity(config)
            _deploy(source, target, stage, home, base_url, database=database, config_path=config_path,
                    startup_timeout=startup_timeout, stopped_config=config, release_stopped_runtime=stopped_runtime.close)


def _deploy(
    source: Path, target: str, stage: Path, home: Path, base_url: str,
    *, database: Path, config_path: Path | None = None, startup_timeout: int = DEFAULT_STARTUP_TIMEOUT,
    stopped_config: MediaforceConfig | None = None, release_stopped_runtime: Callable[[], None] | None = None,
) -> None:
    resume_queue = stopped_config is None
    if stopped_config is None:
        require_paused_idle_dashboard(_api(base_url, "/api/dashboard?preview_limit=0"))
    plist = LoginItemPaths(project_root=source, home=home).plist
    expected_stage_parent = database.parent / "deployments"
    if stage.absolute() != stage.resolve() or stage.parent != expected_stage_parent:
        raise RuntimeError(f"deploy stage must be a real directory directly under {expected_stage_parent}")
    previous_payload = plist.read_bytes()
    previous_item = plistlib.loads(previous_payload)
    previous_root = Path(previous_item["WorkingDirectory"])
    if not previous_root.is_absolute() or _run(["git", "status", "--porcelain"], cwd=previous_root):
        raise RuntimeError("existing runtime checkout is dirty or its identity is invalid")
    if stage.exists():
        raise RuntimeError("deploy stage already exists; use a fresh stage")
    if stopped_config is not None:
        arguments = previous_item["ProgramArguments"]
        previous_config = Path(arguments[arguments.index("--config") + 1]).resolve() if "--config" in arguments else None
        if previous_config != (config_path.resolve() if config_path else None):
            raise RuntimeError("stopped deployment configuration does not match the installed launch item")
        disable_login_item(LoginItemPaths(project_root=previous_root, home=home, config_path=config_path))
        require_stopped_idle_controller(stopped_config)
    candidate = prepare_release(source, target, home)
    stage.mkdir(parents=True, exist_ok=False)
    _atomic_write(stage / "launch-item.before.plist", previous_payload)
    _atomic_write(stage / "previous-commit.txt", _run(["git", "rev-parse", "HEAD"], cwd=previous_root).encode())
    paths = LoginItemPaths(project_root=candidate, home=home, config_path=config_path)
    # Both sides are protected before launchd changes. Old worktrees remain locked and intact.
    register_runtime_roots(home, previous_root, candidate, support_dir=database.parent)
    _atomic_write(stage / "candidate.txt", str(candidate).encode())
    if stopped_config is None:
        require_paused_idle_dashboard(_api(base_url, "/api/dashboard?preview_limit=0"))
    else:
        resume_queue = not require_stopped_idle_controller(stopped_config)
    disable_login_item(paths)
    candidate_started = False
    port_closed = False
    try:
        for _ in range(15):
            if _listener_is_closed():
                port_closed = True
                break
            time.sleep(1)
        else:
            raise RuntimeError("port 8777 still listening")
        backup_database(database, stage / "before.sqlite", pause_queue=stopped_config is not None)
        candidate_started = True
        if stopped_config is not None:
            pause_stopped_queue(stopped_config)
        if release_stopped_runtime is not None:
            release_stopped_runtime()
        started_at = _utc_now()
        enable_login_item(paths)
        deadline = time.monotonic() + startup_timeout
        while True:
            try:
                _api(base_url, "/api/dashboard/scan-job")
                break
            except (OSError, HTTPException, ValueError) as exc:
                if time.monotonic() >= deadline:
                    raise RuntimeError("candidate did not become healthy") from exc
                time.sleep(1)
        require_storage_admission(base_url, started_at)
    except Exception:
        if not candidate_started and port_closed and stopped_config is None:
            if plist.read_bytes() != previous_payload:
                raise RuntimeError("previous plist changed; reconcile before restarting")
            _require_success(_run_command(["/bin/launchctl", "enable", _launch_target()]), "restore the previous login item")
            _bootstrap(_run_command, _launch_domain(), plist)
        else:
            # Migrations or uncertain listener custody require reconciliation.
            disable_login_item(paths)
        raise
    retain_deploy_stage(database.parent, stage)
    try:
        install_mac_app(candidate)
    finally:
        # A lost resume response is uncertain; leave the healthy candidate running.
        if resume_queue:
            _api(base_url, "/api/encode-queue/resume", post=True)
    try:
        trim_releases(home, database.parent)
    except Exception as exc:
        raise RuntimeError(f"controller deployed successfully; release trim failed for stage {stage}: {exc}") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", required=True, help="exact reviewed main commit")
    parser.add_argument("--stage", type=Path, required=True, help="fresh deploy evidence/backup folder")
    parser.add_argument("--config", type=Path, help="same configuration used by the installed controller")
    parser.add_argument("--stopped", action="store_true", help="deploy while the controller is stopped, after verifying idle work records")
    parser.add_argument("--startup-timeout", type=int, default=DEFAULT_STARTUP_TIMEOUT, help="seconds allowed for startup and migrations")
    args = parser.parse_args()
    if args.startup_timeout <= 0:
        parser.error("--startup-timeout must be positive")
    config = load_config(args.config)
    deploy(
        args.source.resolve(), args.target, args.stage.expanduser(), Path.home(), "http://127.0.0.1:8777",
        database=config.paths.db_path, config_path=args.config.expanduser().resolve() if args.config else None,
        startup_timeout=args.startup_timeout,
        stopped=args.stopped,
    )


if __name__ == "__main__":
    main()
