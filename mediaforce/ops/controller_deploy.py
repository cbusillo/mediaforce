"""Staged controller install and app update. Invoke only in an authorized deploy window."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from http.client import HTTPException
import plistlib
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from mediaforce.core.config import MediaforceConfig, load_config
from mediaforce.core.db import open_db, open_readonly_db
from mediaforce.core.utils import timestamp
from mediaforce.encoding.delivery_lock import delivery_lock
from mediaforce.encoding.encode_queue import DEFAULT_QUEUE_NAME, load_queue_state, save_queue_state
from mediaforce.ops.login_item import (
    CommandRunner, LoginItemPaths, _atomic_write, _bootstrap, _bundle_code_hash, _launch_domain, _launch_target,
    _require_success, _run_command, _verify_signed_bundle, disable_login_item, render_login_item_plist,
)
from mediaforce.ops.runtime_install import (
    SERVICE_LABEL, UPDATE_INTENT_NAME, bundle_build, bundle_service, read_update_intent, register_runtime_roots,
    require_own_support, retain_deploy_stage, runtime_activity,
)
from mediaforce.web.runtime.controller_storage_recovery import READY_FRESHNESS_SECONDS
from mediaforce.web.runtime.release_work import release_work_payload
from mediaforce.web.runtime_lock import exclusive_mediaforce_runtime_lock, reserve_mediaforce_database_identity


DEFAULT_STARTUP_TIMEOUT = 180
DEPLOY_LOCK_WAIT_SECONDS = 600
MAC_APP_NAME = "Mediaforce.app"
APPLICATIONS_APP = Path("/Applications") / MAC_APP_NAME
STORAGE_CHECK_SECONDS = 60
UPDATE_FAILED_NAME = "failed-update-builds.json"
# An update drains running files only when every one is expected to finish within this.
DRAIN_START_SECONDS = 15 * 60


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


# A verified release replaces whatever the saved service runs: the clone-based runtime on its first install, or a
# bundled app that cannot update itself. One staged transaction: paused and idle, the port closed, a checked backup,
# the app installed, its server started through the same launch item and checked before work resumes. Old code
# starts again only while the database is provably untouched.


@dataclass(frozen=True, slots=True)
class SavedService:
    payload: bytes
    config_path: Path
    credentials_path: Path | None
    legacy_root: Path | None
    build: str | None

    @classmethod
    def read(cls, plist: Path) -> SavedService:
        payload = plist.read_bytes()
        item = plistlib.loads(payload)
        arguments = item["ProgramArguments"]
        program = Path(arguments[0])
        bundled = program.name == "mediaforce-server"
        legacy_root = None if bundled else Path(item["WorkingDirectory"])
        config = _argument(arguments, "--config") or (legacy_root and legacy_root / "config/defaults.toml")
        credentials = _argument(arguments, "--credentials")
        if credentials is None and legacy_root is not None and (legacy_root / ".env").is_file():
            credentials = legacy_root / ".env"
        if config is None or not config.is_absolute():
            raise RuntimeError(f"the saved service at {plist} names no absolute config")
        build = bundle_build(program.parents[2]) if bundled and program.exists() else None
        return cls(payload, config, credentials, legacy_root, build)

    @property
    def protected_roots(self) -> tuple[Path, ...]:
        return tuple(path for path in (self.legacy_root, self.config_path.parent,
                                       self.credentials_path and self.credentials_path.parent) if path is not None)


def _argument(arguments: list[str], option: str) -> Path | None:
    return Path(arguments[arguments.index(option) + 1]) if option in arguments else None


@dataclass(frozen=True, slots=True)
class ReleaseInstall:
    app: Path
    install: Path
    stage: Path
    home: Path
    saved: SavedService
    config: MediaforceConfig
    stopped: bool
    startup_timeout: int

    @property
    def port(self) -> int:
        return bundle_service(self.app)[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def previous(self) -> LoginItemPaths:
        return LoginItemPaths(self.app, self.home)

    @property
    def service(self) -> LoginItemPaths:
        return LoginItemPaths(self.install, self.home, self.saved.config_path, self.saved.credentials_path,
                              saved_label=self.previous.label)


def install_release(
    app: Path, stage: Path, home: Path, *, evidence: dict[str, str], install: Path = APPLICATIONS_APP,
    startup_timeout: int = DEFAULT_STARTUP_TIMEOUT, stopped: bool = False,
) -> None:
    if (bundle_service(app)[0] == SERVICE_LABEL) != (install == APPLICATIONS_APP):
        raise RuntimeError(f"only the live app installs at {APPLICATIONS_APP}, and it installs nowhere else")
    saved = SavedService.read(LoginItemPaths(app, home).plist)
    if saved.build is not None and int(saved.build) > int(bundle_build(app) or 0):
        raise RuntimeError(f"build {saved.build} is installed; an older release never runs on its database")
    config = load_config(saved.config_path)
    support = config.paths.db_path.parent
    require_own_support(app, config.paths.db_path, home)
    if read_update_intent(support) is not None:
        raise RuntimeError("an app update is unfinished; reconcile or roll it back first")
    # The release flow's own pause request can start the hourly cleanup, so the install waits for it.
    with runtime_activity(support, wait_seconds=DEPLOY_LOCK_WAIT_SECONDS) as acquired:
        if not acquired:
            raise RuntimeError(f"cleanup or another deploy stayed active for {DEPLOY_LOCK_WAIT_SECONDS // 60} minutes; retry after it finishes")
        _switch(ReleaseInstall(app, install, stage, home, saved, config, stopped, startup_timeout), evidence)


def _switch(job: ReleaseInstall, evidence: dict[str, str]) -> None:
    support = job.config.paths.db_path.parent
    _require_fresh_stage(job.stage, support)
    _require_readable(job.saved.credentials_path)
    if not job.stopped:
        _require_running_server(job)
    job.stage.mkdir(parents=True)
    _atomic_write(job.stage / "launch-item.before.plist", job.saved.payload)
    _atomic_write(job.stage / "release.json", json.dumps(evidence, indent=2).encode())
    if _is_present(job.install):
        _copy_app(job.install, job.stage / MAC_APP_NAME)
    register_runtime_roots(support, *job.saved.protected_roots)
    if not job.stopped:
        require_paused_idle_dashboard(_api(job.base_url, "/api/dashboard?preview_limit=0"))
    disable_login_item(job.previous)
    candidate_started = port_closed = False
    try:
        wait_for_closed_listener(job.port)
        port_closed = True
        resume_queue, pending_encode = _back_up_stopped_database(job)
        install_app(job.app, job.install)
        _verify_signed_bundle(job.install)
        _atomic_write(job.service.plist, render_login_item_plist(job.service))
        candidate_started = True
        started_at = _utc_now()
        start_saved_launch_item(job.service.plist)
        wait_until_healthy(job.base_url, job.startup_timeout)
        _require_server(job.base_url, bundle_build(job.install), job.config, pending_encode)
        require_storage_admission(job.base_url, started_at)
    except Exception as failure:
        if candidate_started or not port_closed or job.stopped:
            # Migrations, uncertain listener custody or a database the stopped install already touched require
            # reconciliation; old code never starts on them.
            disable_login_item(job.previous)
        else:
            _restore_previous(job, failure)
        raise
    retain_deploy_stage(support, job.stage)
    if resume_queue:
        # A lost resume response is uncertain; leave the healthy release running.
        _api(job.base_url, "/api/encode-queue/resume", post=True)


def _require_fresh_stage(stage: Path, support: Path) -> None:
    if stage.absolute() != stage.resolve() or stage.parent != support / "deployments":
        raise RuntimeError(f"deploy stage must be a real directory directly under {support / 'deployments'}")
    if stage.exists():
        raise RuntimeError("deploy stage already exists; use a fresh stage")


def _require_readable(credentials: Path | None) -> None:
    if credentials is None:
        return
    with credentials.open("rb"):
        pass


def _require_running_server(job: ReleaseInstall) -> None:
    require_paused_idle_dashboard(_api(job.base_url, "/api/dashboard?preview_limit=0"))
    database = str(job.config.paths.db_path.resolve())
    running = (_api(job.base_url, "/api/release/work").get("server") or {}).get("database")
    if running != database:
        raise RuntimeError(f"the running server uses {running}, not the saved config's database {database}")


def _back_up_stopped_database(job: ReleaseInstall) -> tuple[bool, int]:
    config = job.config
    with exclusive_mediaforce_runtime_lock(config, owner_payload={"operation": "release-install"}):
        reserve_mediaforce_database_identity(config)
        was_paused = require_stopped_idle_controller(config, port=job.port)
        backup_database(config.paths.db_path, job.stage / "before.sqlite", pause_queue=job.stopped)
        if job.stopped:
            pause_stopped_queue(config)
        pending_encode = release_work_payload(config.paths.db_path)["counts"]["pending_encode"]
    return not (job.stopped and was_paused), pending_encode


def _require_server(base_url: str, build: str | None, config: MediaforceConfig, pending_encode: int) -> None:
    work = _api(base_url, "/api/release/work")
    expected = {"build": build, "database": str(config.paths.db_path.resolve())}
    if work.get("server") != expected or work["counts"]["pending_encode"] != pending_encode:
        raise UpdateRefused(f"the started server is not build {build} with the same queued work: {work}")


def _restore_previous(job: ReleaseInstall, failure: Exception) -> None:
    try:
        saved_app = job.stage / MAC_APP_NAME
        if saved_app.exists():
            install_app(saved_app, job.install)
        else:
            shutil.rmtree(job.install, ignore_errors=True)
        _atomic_write(job.previous.plist, job.saved.payload)
        start_saved_launch_item(job.previous.plist)
        wait_until_healthy(job.base_url, job.startup_timeout)
    except Exception as cleanup:
        failure.add_note(f"restarting the previous service also failed: {cleanup}")


def install_app(app: Path, destination: Path) -> None:
    incoming = destination.with_name(f".{destination.name}.incoming")
    replaced = destination.with_name(f".{destination.name}.replaced")
    _restore_interrupted_replacement(destination, replaced)
    shutil.rmtree(incoming, ignore_errors=True)
    _copy_app(app, incoming)
    if _is_present(destination):
        shutil.rmtree(replaced, ignore_errors=True)
        destination.rename(replaced)
    try:
        incoming.rename(destination)
    except OSError:
        _restore_interrupted_replacement(destination, replaced)
        raise
    shutil.rmtree(replaced, ignore_errors=True)


def _restore_interrupted_replacement(installed: Path, replaced: Path) -> None:
    if not _is_present(installed) and _is_present(replaced):
        replaced.rename(installed)


def _is_present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


# The bundled app's update: the same staged transaction, run around Sparkle's automatic installer (Director decision,
# https://github.com/cbusillo/mediaforce/issues/1314#issuecomment-6841277256). Sparkle may download only a prepared
# build; Mediaforce prepares at once when nothing runs, or drains files that finish soon. Nothing is held merely because
# an update exists. The intent record beside the database is the hold while it exists, across quit, replacement and
# reboot. Phases: draining -> saving -> prepared -> (Sparkle installs) -> starting -> done; a candidate that fails its
# checks rolls back on its own: failed -> rolling-back -> restoring -> done.


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


def run_update_step(step: str, app: Path, build: str | None) -> int:
    """The app runs one step and reads one JSON line back. The config is the one its saved service runs with."""
    arguments = plistlib.loads(LoginItemPaths(app, Path.home()).plist.read_bytes())["ProgramArguments"]
    update = AppUpdate(Path(arguments[arguments.index("--config") + 1]), app, Path.home())
    error = outcome = None
    try:
        require_own_support(app, update.config.paths.db_path, update.home)
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
                outcome = reconcile_update(update)
    except Exception as exc:
        error = str(exc) or type(exc).__name__
    print(json.dumps({"error": error, "outcome": outcome, "intent": read_update_intent(update.support),
                      "failed_builds": _failed(update)}))
    return 1 if error else 0


def prepare_update(update: AppUpdate, build: str) -> None:
    """Get ready to install `build` when the time is right: at once when nothing runs; otherwise, once every running
    file is expected to finish within DRAIN_START_SECONDS, stop starting new ones and wait for those. Then save the old
    app, launch item and database and stop the server. Phase "prepared" lets Sparkle install exactly `build`."""
    if build in _failed(update):
        raise UpdateRefused(f"build {build} failed before; it is not offered again")
    existing = read_update_intent(update.support)
    if existing is not None and (existing["phase"] not in {"draining", "prepared"} or bundle_build(update.app) != existing["from"]):
        raise UpdateRefused(f"the update to build {existing.get('to')} is {existing['phase']}; finish or roll it back first")
    # A newer build reuses the same transaction: the saved old pair and the stopped server are just as right for it.
    intent: dict[str, Any] = {**(existing or {"from": bundle_build(update.app), "app": str(update.app)}), "to": build}
    if intent.get("phase") == "prepared":
        _save_intent(update, intent)
        return
    running = _running_work(update)
    if running:
        if intent.get("phase") == "draining" or _finishing_soon(update):
            _save_intent(update, {**intent, "phase": "draining", "waiting": running})
        return
    stage = update.support / "deployments" / f"update-{intent['from']}-to-{build}-{_utc_now():%Y%m%dT%H%M%SZ}"
    work = _api(update.base_url, "/api/release/work")["counts"]
    intent.update(phase="saving", stage=str(stage), pending_encode=work["pending_encode"], waiting=[])
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
    except Exception as cause:
        try:
            back_out_update(update)
        except Exception as cleanup:
            cause.add_note(f"restarting the old service also failed: {cleanup}")
        raise
    # Only a verified pair moves the pointer; a partial attempt never displaces the last usable one.
    retain_deploy_stage(update.support, stage)
    intent["phase"] = "prepared"
    _save_intent(update, intent)


def reconcile_update(update: AppUpdate) -> str | None:
    """What each app launch runs: finish the installed candidate, roll back one that fails its checks, or finish
    whatever an interruption left. Returns "updated" or "rolled back" when it did either."""
    intent = read_update_intent(update.support)
    if intent is None or intent["phase"] in {"draining", "unreadable"}:
        return None
    installed = bundle_build(update.app)
    if intent["phase"] in {"failed", "rolling-back"}:
        rollback_update(update)
        return "rolled back"
    if installed == intent["to"] and intent["phase"] in {"prepared", "starting"}:
        try:
            _finish(update, intent)
        except Exception as failure:
            try:
                rollback_update(update)
            except Exception as cleanup:
                failure.add_note(f"going back to build {intent['from']} also failed: {cleanup}")
                raise failure from None
            return "rolled back"
        return "updated"
    if installed == intent["from"] and intent["phase"] in {"saving", "restoring"}:
        back_out_update(update)
    return None


def back_out_update(update: AppUpdate) -> None:
    """Return to the unchanged old app: after a failed preparation, or when the prepared build is no longer offered."""
    intent = _require_intent(update)
    if bundle_build(update.app) != intent["from"] or intent["phase"] not in {"draining", "saving", "prepared", "restoring"}:
        raise UpdateRefused("the app was already replaced; finish or roll back the update instead")
    if intent["phase"] != "draining":
        intent["phase"] = "restoring"
        _save_intent(update, intent)
        saved = Path(intent["stage"]) / "launch-item.before.plist"
        if saved.exists() and _listener_is_closed(update.port):
            _atomic_write(update.paths.plist, saved.read_bytes())
            start_saved_launch_item(update.paths.plist)
        wait_until_healthy(update.base_url, DEFAULT_STARTUP_TIMEOUT)
    _end(update)


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
        _require_server(update.base_url, intent["to"], update.config, intent["pending_encode"])
        require_storage_admission(update.base_url, started_at)
    except Exception as exc:
        disable_login_item(update.paths)
        intent.update(phase="failed", failure=str(exc))
        _save_intent(update, intent)
        _atomic_write(update.file(UPDATE_FAILED_NAME), json.dumps([*_failed(update), intent["to"]]).encode())
        raise
    _end(update)


def rollback_update(update: AppUpdate) -> None:
    """Restore the saved database, app and exact launch item of a failed candidate, keeping each attempt's failed
    copies. The candidate never released work, so nothing it did is lost."""
    intent = _require_intent(update)
    if intent["phase"] not in {"starting", "failed", "rolling-back"}:
        raise UpdateRefused("nothing ran on the new build; cancel the update instead")
    # Every step below is repeatable from the saved pair; an interrupted rollback is run again, from either app copy.
    intent["phase"] = "rolling-back"
    _save_intent(update, intent)
    stage = Path(intent["stage"])
    installed = Path(intent["app"])
    disable_login_item(update.paths)
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
    _end(update)


def require_update_startup_allowed(support_dir: Path, app: Path) -> None:
    """The server opens the database only when no unfinished update says otherwise."""
    intent = read_update_intent(support_dir)
    if intent is None or intent.get("phase") == "draining":
        return
    if (intent.get("phase"), bundle_build(app)) in {("starting", intent.get("to")), ("restoring", intent.get("from"))}:
        return
    raise UpdateRefused(f"an update to build {intent.get('to')} is {intent.get('phase')}; open Mediaforce to finish it")


def _end(update: AppUpdate) -> None:
    """Removing the record lifts the hold; every pause a person set was never touched."""
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
        (_publishing(update.config.paths.db_path), "files are being published"),
    )
    return [f"{count} {words}" for count, words in found if count]


def _finishing_soon(update: AppUpdate) -> bool:
    """Whether every running file is expected to finish within DRAIN_START_SECONDS, from the app's own estimates. Work
    without an estimate is never cut short: draining waits until one says it is nearly done."""
    activity = _api(update.base_url, "/api/app/activity")
    deadline = _utc_now() + timedelta(seconds=DRAIN_START_SECONDS)
    finishes = [running.get("finishes_at") for computer in activity["computers"] for running in computer["running"]]
    return all(finish and datetime.fromisoformat(finish) <= deadline for finish in finishes)


def _publishing(database: Path) -> int:
    held = 0
    for path in (database.resolve().parent / "delivery-locks").glob("*.lock"):
        with delivery_lock(database, int(path.stem), blocking=False) as acquired:
            held += not acquired
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


def _copy_app(source: Path, destination: Path) -> None:
    subprocess.run(["/usr/bin/ditto", str(source), str(destination)], check=True, capture_output=True)
