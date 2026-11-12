"""Staged controller deployment. Invoke only in an authorized deploy window."""
from __future__ import annotations

import argparse
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import fcntl
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
    CommandRunner, LoginItemPaths, _atomic_write, _bootstrap, _bundle_code_hash, _launch_domain, _launch_target,
    _require_success, _run_command, _verify_signed_bundle, disable_login_item, enable_login_item,
)
from mediaforce.ops.runtime_install import (
    UPDATE_INTENT_NAME, bundle_build, bundle_service, read_update_intent, register_runtime_roots,
    require_release_directory, retain_deploy_stage, retained_deploy_stage, runtime_activity, runtime_directory,
)
from mediaforce.web.runtime.controller_storage_recovery import READY_FRESHNESS_SECONDS
from mediaforce.web.runtime_lock import exclusive_mediaforce_runtime_lock, reserve_mediaforce_database_identity


DEFAULT_STARTUP_TIMEOUT = 180
DEPLOY_LOCK_WAIT_SECONDS = 600
MAC_APP_NAME = "Mediaforce.app"
APPLICATIONS = Path("/Applications")
KEPT_RELEASES = 3
STORAGE_CHECK_SECONDS = 60
UPDATE_FAILED_NAME = "failed-update-builds.json"


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


def wait_for_closed_listener(port: int) -> None:
    for _ in range(15):
        if _listener_is_closed(port):
            return
        time.sleep(1)
    raise RuntimeError(f"port {port} still listening")


def wait_until_healthy(base_url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            _api(base_url, "/api/dashboard/scan-job")
            return
        except (OSError, HTTPException, ValueError) as exc:
            if time.monotonic() >= deadline:
                raise RuntimeError("candidate did not become healthy") from exc
            time.sleep(1)


def start_saved_launch_item(plist: Path, runner: CommandRunner | None = None) -> None:
    """Start exactly the saved launch item, never one regenerated by new code."""
    runner = runner or _run_command
    _require_success(runner(["/bin/launchctl", "enable", _launch_target(plist.stem)]), "restore the previous login item")
    _bootstrap(runner, _launch_domain(), plist)


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
        wait_for_closed_listener(8777)
        port_closed = True
        backup_database(database, stage / "before.sqlite", pause_queue=stopped_config is not None)
        candidate_started = True
        if stopped_config is not None:
            pause_stopped_queue(stopped_config)
        if release_stopped_runtime is not None:
            release_stopped_runtime()
        started_at = _utc_now()
        enable_login_item(paths)
        wait_until_healthy(base_url, startup_timeout)
        require_storage_admission(base_url, started_at)
    except Exception:
        if not candidate_started and port_closed and stopped_config is None:
            if plist.read_bytes() != previous_payload:
                raise RuntimeError("previous plist changed; reconcile before restarting")
            start_saved_launch_item(plist)
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


# The bundled app's update: the same staged transaction, split around Sparkle's standard installer. Sparkle downloads,
# verifies, replaces and relaunches the app; these steps own admission, the saved old pair, the candidate check and
# rollback. One intent record beside the database spans app quit, replacement and reboot. Phases: fenced -> saving ->
# prepared -> (Sparkle installs) -> starting -> releasing, or failed -> restoring -> releasing.


class UpdateRefused(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AppUpdate:
    config_path: Path
    app: Path
    home: Path

    @property
    def config(self) -> MediaforceConfig:
        return load_config(self.config_path)

    @property
    def support(self) -> Path:
        return self.config.paths.db_path.parent

    @property
    def port(self) -> int:
        return bundle_service(self.app)[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def paths(self) -> LoginItemPaths:
        return LoginItemPaths(self.app, self.home)

    def file(self, name: str) -> Path:
        return self.support / "runtime" / name


def run_update_step(step: str, app: Path, config_path: Path | None, build: str | None) -> int:
    """The app runs one step and reads one JSON line back."""
    if config_path is None:
        print(json.dumps({"error": "the bundled app requires --config", "intent": None, "failed_builds": []}))
        return 1
    update = AppUpdate(config_path.expanduser().resolve(), app, Path.home())
    error = None
    try:
        with runtime_activity(update.support, wait_seconds=DEPLOY_LOCK_WAIT_SECONDS) as acquired:
            if not acquired:
                raise UpdateRefused("cleanup or another update stayed active; try again after it finishes")
            if step == "prepare":
                prepare_update(update, build or "")
            elif step == "cancel":
                back_out_update(update)
            elif step == "rollback":
                rollback_update(update)
            else:
                reconcile_update(update)
    except Exception as exc:
        error = str(exc) or type(exc).__name__
    print(json.dumps({"error": error, "intent": read_update_intent(update.support), "failed_builds": _failed(update)}))
    return 1 if error else 0


def prepare_update(update: AppUpdate, build: str) -> None:
    """Fence new work, wait for running files, then save the old app, launch item and database and stop the server.
    Phase "prepared" lets Sparkle install exactly `build`; anything still running is listed under "waiting"."""
    if build in _failed(update):
        raise UpdateRefused(f"build {build} failed before; it is not offered again")
    intent = read_update_intent(update.support)
    if intent is None:
        intent = {"from": bundle_build(update.app), "to": build, "phase": "fenced", "app": str(update.app)}
        _save_intent(update, intent)
        intent["paused"] = _post(update, "/api/update/fence", {})["paused"]
    elif intent.get("to") != build or intent["phase"] not in {"fenced", "prepared"}:
        raise UpdateRefused(f"the update to build {intent.get('to')} is {intent['phase']}; finish or cancel it first")
    if intent["phase"] == "prepared":
        return
    intent["waiting"] = _running_work(update)
    _save_intent(update, intent)
    if intent["waiting"]:
        return
    stage = update.support / "deployments" / f"update-{intent['from']}-to-{build}-{_utc_now():%Y%m%dT%H%M%SZ}"
    work = _api(update.base_url, "/api/release/work")["counts"]
    intent.update(phase="saving", stage=str(stage), pending_encode=work["pending_encode"])
    _save_intent(update, intent)
    try:
        saved = update.paths.plist.read_bytes()
        stage.mkdir(parents=True)
        _atomic_write(stage / "launch-item.before.plist", saved)
        shutil.copy2(update.config_path, stage / "config.toml")
        _copy_app(update.app, stage / MAC_APP_NAME)
        _verify_signed_bundle(stage / MAC_APP_NAME)
        if plistlib.loads(saved).get("MediaforceAppCodeHash") != _bundle_code_hash(stage / MAC_APP_NAME):
            raise UpdateRefused("the running app does not match its saved service; reconcile before updating")
        disable_login_item(update.paths)
        wait_for_closed_listener(update.port)
        config = update.config
        with exclusive_mediaforce_runtime_lock(config, owner_payload={"operation": "update-prepare"}):
            reserve_mediaforce_database_identity(config)
            require_stopped_idle_controller(config, port=update.port)
            backup_database(config.paths.db_path, stage / "before.sqlite")
    except Exception:
        back_out_update(update)
        raise
    # Only a verified pair moves the pointer; a partial attempt never displaces the last usable one.
    retain_deploy_stage(update.support, stage)
    intent["phase"] = "prepared"
    _save_intent(update, intent)


def reconcile_update(update: AppUpdate) -> None:
    """What each app launch runs: finish what the record says was interrupted, or leave it for Sparkle or a person."""
    intent = read_update_intent(update.support)
    if intent is None or intent["phase"] in {"fenced", "failed", "unreadable"}:
        return
    installed = bundle_build(update.app)
    if intent["phase"] == "releasing":
        _release(update, intent)
    elif installed == intent["to"] and intent["phase"] in {"prepared", "starting"}:
        _finish(update, intent)
    elif installed == intent["from"] and intent["phase"] in {"saving", "restoring"}:
        back_out_update(update)


def back_out_update(update: AppUpdate) -> None:
    """Return to the unchanged old app. The app asks only when Sparkle proves no install is pending."""
    intent = _require_intent(update)
    if bundle_build(update.app) != intent["from"] or intent["phase"] not in {"fenced", "saving", "prepared", "restoring"}:
        raise UpdateRefused("the app was already replaced; finish or roll back the update instead")
    if intent["phase"] != "fenced":
        intent["phase"] = "restoring"
        _save_intent(update, intent)
        saved = Path(intent["stage"]) / "launch-item.before.plist"
        if saved.exists() and _listener_is_closed(update.port):
            _atomic_write(update.paths.plist, saved.read_bytes())
            start_saved_launch_item(update.paths.plist)
        wait_until_healthy(update.base_url, DEFAULT_STARTUP_TIMEOUT)
    _release(update, intent)


def _finish(update: AppUpdate, intent: dict) -> None:
    """Start the installed candidate through the saved launch item and check it before work resumes. A failure stops
    it and records its build; old code never starts on a database the candidate may have migrated."""
    stage = Path(intent["stage"])
    _verify_signed_bundle(update.app)
    saved = plistlib.loads((stage / "launch-item.before.plist").read_bytes())
    if Path(saved["ProgramArguments"][0]) != update.paths.program:
        raise UpdateRefused("the saved service runs another copy of the app; move this app back to its saved place")
    intent["phase"] = "starting"
    _save_intent(update, intent)
    try:
        started_at = _utc_now()
        # The saved launch item stays exactly as it was, except for the build it trusts.
        _atomic_write(update.paths.plist, plistlib.dumps({**saved, "MediaforceAppCodeHash": _bundle_code_hash(update.app)}))
        start_saved_launch_item(update.paths.plist)
        wait_until_healthy(update.base_url, DEFAULT_STARTUP_TIMEOUT)
        work = _api(update.base_url, "/api/release/work")
        expected = {"build": intent["to"], "database": str(update.config.paths.db_path.resolve())}
        if work.get("server") != expected or work["counts"]["pending_encode"] != intent["pending_encode"]:
            raise UpdateRefused(f"the started server is not the prepared build with the same queued work: {work}")
        require_storage_admission(update.base_url, started_at)
    except Exception as exc:
        disable_login_item(update.paths)
        intent.update(phase="failed", failure=str(exc))
        _save_intent(update, intent)
        _atomic_write(update.file(UPDATE_FAILED_NAME), json.dumps([*_failed(update), intent["to"]]).encode())
        raise
    _atomic_write(stage / "candidate.txt", json.dumps({"build": intent["to"], "code_hash": _bundle_code_hash(update.app)}).encode())
    _release(update, intent)


def rollback_update(update: AppUpdate) -> None:
    """Restore the saved database, app and exact launch item of a failed candidate, keeping each attempt's failed
    copies. Only before new work was released: once the candidate published files, reconcile those first."""
    intent = _require_intent(update)
    if intent["phase"] not in {"starting", "failed"}:
        raise UpdateRefused("nothing ran on the new build; cancel the update instead")
    stage = Path(intent["stage"])
    installed = Path(intent["app"])
    disable_login_item(LoginItemPaths(installed, update.home))
    wait_for_closed_listener(update.port)
    failed = stage / f"failed-{_utc_now():%Y%m%dT%H%M%S%fZ}"
    failed.mkdir()
    config = update.config
    with exclusive_mediaforce_runtime_lock(config, owner_payload={"operation": "update-rollback"}):
        reserve_mediaforce_database_identity(config)
        database = config.paths.db_path
        incoming = database.with_name(f".{database.name}.restoring")
        incoming.unlink(missing_ok=True)
        backup_database(stage / "before.sqlite", incoming)
        for suffix in ("", "-wal", "-shm"):
            if (current := database.with_name(database.name + suffix)).exists():
                current.rename(failed / current.name)
        incoming.rename(database)
    incoming_app = installed.with_name(f".{installed.name}.restoring")
    shutil.rmtree(incoming_app, ignore_errors=True)
    _copy_app(stage / MAC_APP_NAME, incoming_app)
    _verify_signed_bundle(incoming_app)
    if installed.exists():
        shutil.move(installed, failed / installed.name)
    incoming_app.rename(installed)
    intent["phase"] = "restoring"
    _save_intent(update, intent)
    _atomic_write(update.paths.plist, (stage / "launch-item.before.plist").read_bytes())
    start_saved_launch_item(update.paths.plist)
    wait_until_healthy(update.base_url, DEFAULT_STARTUP_TIMEOUT)
    _release(update, intent)


def require_update_startup_allowed(support_dir: Path, app: Path) -> None:
    """The server opens the database only when no unfinished update says otherwise."""
    intent = read_update_intent(support_dir)
    if intent is None or intent.get("phase") in {"fenced", "releasing"}:
        return
    if (intent.get("phase"), bundle_build(app)) in {("starting", intent.get("to")), ("restoring", intent.get("from"))}:
        return
    raise UpdateRefused(f"an update to build {intent.get('to')} is {intent.get('phase')}; open Mediaforce to finish it")


def _release(update: AppUpdate, intent: dict) -> None:
    """The one way an update ends: "releasing" is saved before any pause lifts, so nothing rolls back under work that
    already resumed, and an interrupted ending is finished from the record. Unknown earlier pauses stay paused."""
    intent["phase"] = "releasing"
    _save_intent(update, intent)
    _post(update, "/api/update/release", intent.get("paused") or {"encode": True, "background": True})
    update.file(UPDATE_INTENT_NAME).unlink()


def _running_work(update: AppUpdate) -> list[str]:
    dashboard = _api(update.base_url, "/api/dashboard?preview_limit=0")
    counts = _api(update.base_url, "/api/release/work")["counts"]
    calibration = dashboard["calibration_queue"]
    found = (
        (dashboard["encode_queue"]["running_count"], "files are compressing"),
        (calibration["sample"]["running_count"] + calibration["full"]["running_count"], "samples are running"),
        (counts["unfinished_scan_rows"], "library checks are running"),
        (counts["active_evidence"], "files are being measured"),
        (_held_delivery_locks(update.support), "files are being published"),
    )
    return [f"{count} {words}" for count, words in found if count]


def _held_delivery_locks(support_dir: Path) -> int:
    held = 0
    for path in (support_dir / "delivery-locks").glob("*.lock"):
        with path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lock, fcntl.LOCK_UN)
            except BlockingIOError:
                held += 1
    return held


def _require_intent(update: AppUpdate) -> dict:
    intent = read_update_intent(update.support)
    if intent is None or intent.get("phase") == "unreadable":
        raise UpdateRefused(f"no readable update record at {update.file(UPDATE_INTENT_NAME)}")
    return intent


def _save_intent(update: AppUpdate, intent: dict) -> None:
    update.file(UPDATE_INTENT_NAME).parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(update.file(UPDATE_INTENT_NAME), json.dumps(intent, indent=2).encode())


def _failed(update: AppUpdate) -> list[str]:
    try:
        return json.loads(update.file(UPDATE_FAILED_NAME).read_text())
    except FileNotFoundError:
        return []


def _post(update: AppUpdate, route: str, body: dict) -> dict:
    request = Request(update.base_url + route, data=json.dumps(body).encode(), method="POST",
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def _copy_app(source: Path, destination: Path) -> None:
    subprocess.run(["/usr/bin/ditto", str(source), str(destination)], check=True, capture_output=True)


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
