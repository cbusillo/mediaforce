"""The bundled app's update transaction around Sparkle's standard installer.

Sparkle downloads, verifies and replaces the app. This module owns what Sparkle cannot: fencing new work, saving the
complete old app and database pair before the candidate can migrate, starting and checking the candidate, rollback,
and the record of a failed build. One durable intent record beside the configured database spans app quit,
replacement and reboot; every phase takes the existing deployment lock and reconciles that record before acting.
"""
from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
import plistlib
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

from urllib.request import Request, urlopen

from mediaforce.core.config import MediaforceConfig, load_config
from mediaforce.ops.controller_deploy import (
    DEPLOY_LOCK_WAIT_SECONDS, DEFAULT_STARTUP_TIMEOUT, _api, _listener_is_closed, backup_database,
    require_storage_admission, require_stopped_idle_controller,
)
from mediaforce.ops.login_item import (
    LoginItemPaths, _atomic_write, _bootstrap, _bundle_code_hash, _launch_domain, _launch_target, _require_success,
    _run_command, _verify_signed_bundle, disable_login_item, render_login_item_plist,
)
from mediaforce.ops.runtime_install import (
    UPDATE_INTENT_NAME, read_update_intent, retain_deploy_stage, retained_deploy_stage, runtime_activity,
)
from mediaforce.web.runtime_lock import exclusive_mediaforce_runtime_lock, reserve_mediaforce_database_identity


FAILED_BUILDS_NAME = "failed-update-builds.json"
WAITING = 3


class UpdateRefused(RuntimeError):
    """The update cannot take this step now; the message says why in plain words."""


@dataclass(frozen=True, slots=True)
class UpdateContext:
    config_path: Path
    app: Path
    home: Path
    base_url: str
    port: int

    @property
    def config(self) -> MediaforceConfig:
        return load_config(self.config_path)

    @property
    def support_dir(self) -> Path:
        return self.config.paths.db_path.parent

    @property
    def intent_path(self) -> Path:
        return self.support_dir / "runtime" / UPDATE_INTENT_NAME

    @property
    def failed_path(self) -> Path:
        return self.support_dir / "runtime" / FAILED_BUILDS_NAME

    @property
    def plist(self) -> Path:
        return LoginItemPaths(self.app, self.home).plist


def app_build(app: Path) -> str:
    return str(plistlib.loads((app / "Contents/Info.plist").read_bytes())["CFBundleVersion"])


def failed_builds(context: UpdateContext) -> list[str]:
    try:
        values = json.loads(context.failed_path.read_text())
    except FileNotFoundError:
        return []
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise UpdateRefused("the failed update record is unreadable; nothing is offered until it is fixed")
    return values


def status(context: UpdateContext) -> dict[str, Any]:
    return {"intent": read_update_intent(context.support_dir), "failed_builds": failed_builds(context),
            "installed_build": app_build(context.app)}


def prepare(context: UpdateContext, build: str, *, runner: Callable[[list[str]], subprocess.CompletedProcess[str]] = _run_command) -> dict[str, Any]:
    """Fence new work, wait for active files, then save the old app and database and stop the server.
    Returns the intent; phase "prepared" means Sparkle may install exactly `build`."""
    with _locked(context):
        if build in failed_builds(context):
            raise UpdateRefused(f"build {build} failed before; it is not offered again")
        intent = read_update_intent(context.support_dir)
        installed = app_build(context.app)
        if intent is not None:
            if intent.get("to") != build:
                raise UpdateRefused(f"an update to build {intent.get('to')} is unfinished; finish or cancel it first")
            if intent["phase"] == "prepared":
                return intent
            if intent["phase"] != "fenced":
                raise UpdateRefused(f"the update to build {build} is {intent['phase']}; reconcile it first")
        else:
            intent: dict[str, Any] = {"from": installed, "to": build, "phase": "fenced", "app": str(context.app), "started_at": _now()}
            _write_intent(context, intent)
            intent["paused"] = _fence(context)
            _write_intent(context, intent)
        reasons = _active_work(context)
        if reasons:
            intent["waiting"] = reasons
            _write_intent(context, intent)
            return intent
        stage = context.support_dir / "deployments" / f"update-{installed}-to-{build}-{_stamp()}"
        intent.update(stage=str(stage), previous_stage=_previous_stage(context))
        intent.pop("waiting", None)
        _write_intent(context, intent)
        _save_and_stop(context, intent, stage, runner)
        intent["phase"] = "prepared"
        _write_intent(context, intent)
        return intent


def cancel(context: UpdateContext, *, runner: Callable[[list[str]], subprocess.CompletedProcess[str]] = _run_command) -> None:
    """Return to the unchanged old app. Only the app calls this, with Sparkle's proof that no install is pending."""
    with _locked(context):
        intent = _require_intent(context)
        if intent["phase"] not in {"fenced", "prepared"} or app_build(context.app) != intent["from"]:
            raise UpdateRefused("the app was already replaced; finish or roll back the update instead")
        if intent["phase"] == "prepared":
            intent["phase"] = "restoring"
            _write_intent(context, intent)
            _start_saved_service(Path(intent["stage"]) / "launch-item.before.plist", context, runner)
            _wait_healthy(context)
        _restore_pause(context, intent.get("paused"))
        context.intent_path.unlink()


def finish(context: UpdateContext, *, runner: Callable[[list[str]], subprocess.CompletedProcess[str]] = _run_command,
           startup_timeout: int = DEFAULT_STARTUP_TIMEOUT) -> dict[str, Any]:
    """Start the installed candidate through the saved service path and check it before releasing new work.
    A failure leaves the service stopped and records the failed build; it never restarts old code."""
    with _locked(context):
        intent = _require_intent(context)
        if app_build(context.app) != intent["to"]:
            raise UpdateRefused(f"the installed app is build {app_build(context.app)}, not the prepared build {intent['to']}")
        if intent["phase"] not in {"prepared", "starting"}:
            raise UpdateRefused(f"the update is {intent['phase']}; roll back before trying again")
        stage = Path(intent["stage"])
        _verify_signed_bundle(context.app)
        before = plistlib.loads((stage / "launch-item.before.plist").read_bytes())
        arguments = before["ProgramArguments"]
        credentials = Path(arguments[arguments.index("--credentials") + 1]) if "--credentials" in arguments else None
        paths = LoginItemPaths(context.app, context.home, context.config_path, credentials)
        if Path(arguments[0]) != paths.program:
            raise UpdateRefused("the saved service runs another copy of the app; move this app back to its saved place")
        # From here the candidate may migrate the database; only the saved pair can undo it.
        intent["phase"] = "starting"
        _write_intent(context, intent)
        try:
            _atomic_write(paths.plist, render_login_item_plist(paths))
            started_at = datetime.now(timezone.utc)
            _require_success(runner(["/bin/launchctl", "enable", _launch_target()]), "enable the background service")
            _bootstrap(runner, _launch_domain(), paths.plist)
            _wait_healthy(context, timeout=startup_timeout)
            work = _api(context.base_url, "/api/release/work")["counts"]
            if work["pending_encode"] != intent.get("pending_encode", work["pending_encode"]):
                raise UpdateRefused("queued work changed across the update")
            require_storage_admission(context.base_url, started_at)
        except Exception as exc:
            disable_login_item(paths, runner=runner)
            intent.update(phase="failed", failure=str(exc))
            _write_intent(context, intent)
            _record_failed(context, intent["to"])
            raise
        _atomic_write(stage / "candidate.txt", json.dumps(
            {"build": intent["to"], "app": str(context.app), "code_hash": _bundle_code_hash(context.app)}).encode())
        _restore_pause(context, intent.get("paused"))
        context.intent_path.unlink()
        return intent


def rollback(context: UpdateContext, *, runner: Callable[[list[str]], subprocess.CompletedProcess[str]] = _run_command) -> None:
    """Restore the saved old app, its database and its exact launch item, keeping the failed copies for diagnosis.
    Only before new work was released: once the candidate encoded or published files, reconcile those first."""
    with _locked(context):
        intent = _require_intent(context)
        if intent["phase"] not in {"starting", "failed"}:
            raise UpdateRefused("nothing ran on the new build; cancel the update instead")
        stage = Path(intent["stage"])
        saved_plist = (stage / "launch-item.before.plist").read_bytes()
        saved_app = stage / "Mediaforce.app"
        disable_login_item(LoginItemPaths(context.app, context.home), runner=runner)
        _require_closed(context.port)
        config = context.config
        with exclusive_mediaforce_runtime_lock(config, owner_payload={"operation": "update-rollback"}):
            reserve_mediaforce_database_identity(config)
            database = config.paths.db_path
            failed = stage / "failed"
            failed.mkdir(exist_ok=True)
            for suffix in ("", "-wal", "-shm"):
                current = database.with_name(database.name + suffix)
                if current.exists():
                    current.rename(failed / current.name)
            _copy_database(stage / "before.sqlite", database)
        # The saved service names where the app lives; this command may run from either copy.
        installed_app = Path(intent["app"])
        failed_app = failed / installed_app.name
        if installed_app.exists() and not failed_app.exists():
            shutil.move(installed_app, failed_app)
        _copy_app(saved_app, installed_app)
        _verify_signed_bundle(installed_app)
        if plistlib.loads(saved_plist).get("MediaforceAppCodeHash") != _bundle_code_hash(installed_app):
            raise UpdateRefused("the restored app does not match its saved service")
        _record_failed(context, intent["to"])
        intent["phase"] = "restoring"
        _write_intent(context, intent)
        _start_saved_service(stage / "launch-item.before.plist", context, runner)
        _wait_healthy(context)
        _restore_pause(context, intent.get("paused"))
        context.intent_path.unlink()


def require_startup_allowed(support_dir: Path, app: Path) -> None:
    """The server opens the database only when no unfinished update says otherwise: the old build may run while
    work is fenced; once the old pair is saved, only the candidate being started or the old app being restored."""
    intent = read_update_intent(support_dir)
    if intent is None or intent.get("phase") == "fenced":
        return
    if (intent.get("phase"), app_build(app)) in {("starting", intent.get("to")), ("restoring", intent.get("from"))}:
        return
    raise UpdateRefused(
        f"an update to build {intent.get('to')} is {intent.get('phase')}; open Mediaforce to finish or roll it back"
    )


def _locked(context: UpdateContext) -> ExitStack:
    stack = ExitStack()
    acquired = stack.enter_context(runtime_activity(context.support_dir, wait_seconds=DEPLOY_LOCK_WAIT_SECONDS))
    if not acquired:
        stack.close()
        raise UpdateRefused("cleanup or another update stayed active; try again after it finishes")
    return stack


def _require_intent(context: UpdateContext) -> dict[str, Any]:
    intent = read_update_intent(context.support_dir)
    if intent is None:
        raise UpdateRefused("no update is in progress")
    if intent.get("phase") == "unreadable":
        raise UpdateRefused(f"the update record at {context.intent_path} is unreadable; reconcile it by hand")
    return intent


def _write_intent(context: UpdateContext, intent: dict[str, Any]) -> None:
    context.intent_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(context.intent_path, json.dumps(intent, indent=2, sort_keys=True).encode())


def _record_failed(context: UpdateContext, build: str) -> None:
    builds = failed_builds(context)
    if build not in builds:
        _atomic_write(context.failed_path, json.dumps([*builds, build]).encode())


def _fence(context: UpdateContext) -> dict[str, bool]:
    """The running server pauses new work and reports what was paused before; only it may write its database."""
    return _post(context, "/api/update/fence", {})["paused"]


def _restore_pause(context: UpdateContext, paused: dict[str, bool] | None) -> None:
    # Unknown earlier state stays paused: resuming work nobody asked for would be wrong.
    _post(context, "/api/update/release", paused or {"encode": True, "background": True})


def _post(context: UpdateContext, route: str, body: dict[str, Any]) -> dict[str, Any]:
    request = Request(context.base_url + route, data=json.dumps(body).encode(), method="POST",
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def _active_work(context: UpdateContext) -> list[str]:
    """Every reason the old server must keep running, from the running server's own records."""
    dashboard = _api(context.base_url, "/api/dashboard?preview_limit=0")
    counts = _api(context.base_url, "/api/release/work")["counts"]
    queue, calibration = dashboard["encode_queue"], dashboard["calibration_queue"]
    reasons = []
    for count, words in (
        (queue["running_count"], "files are compressing"),
        (calibration["sample"]["running_count"] + calibration["full"]["running_count"], "samples are running"),
        (counts["unfinished_scan_rows"], "library checks are running"),
        (counts["active_evidence"], "files are being measured"),
    ):
        if count:
            reasons.append(f"{count} {words}")
    return reasons


def _previous_stage(context: UpdateContext) -> str | None:
    stage = retained_deploy_stage(context.support_dir)
    return str(stage) if stage else None


def _save_and_stop(context: UpdateContext, intent: dict[str, Any], stage: Path,
                   runner: Callable[[list[str]], subprocess.CompletedProcess[str]]) -> None:
    """Copy and verify the old app and launch item, stop the server, then save a checked database copy. A failure
    before replacement restarts the unchanged old service; the pointer to the last usable pair moves only at the end."""
    saved_plist = context.plist.read_bytes()
    intent["pending_encode"] = _api(context.base_url, "/api/release/work")["counts"]["pending_encode"]
    stage.mkdir(parents=True, exist_ok=False)
    stopped = False
    try:
        _atomic_write(stage / "launch-item.before.plist", saved_plist)
        shutil.copy2(context.config_path, stage / "config.toml")
        _copy_app(context.app, stage / "Mediaforce.app")
        _verify_signed_bundle(stage / "Mediaforce.app")
        if plistlib.loads(saved_plist).get("MediaforceAppCodeHash") != _bundle_code_hash(stage / "Mediaforce.app"):
            raise UpdateRefused("the running app does not match its saved service; reconcile before updating")
        stopped = True
        disable_login_item(LoginItemPaths(context.app, context.home), runner=runner)
        _require_closed(context.port)
        config = context.config
        with exclusive_mediaforce_runtime_lock(config, owner_payload={"operation": "update-prepare"}):
            reserve_mediaforce_database_identity(config)
            require_stopped_idle_controller(config, port=context.port)
            backup_database(config.paths.db_path, stage / "before.sqlite")
    except Exception:
        if stopped:
            _start_saved_service(stage / "launch-item.before.plist", context, runner)
            _wait_healthy(context)
        _restore_pause(context, intent.get("paused"))
        context.intent_path.unlink()
        raise
    retain_deploy_stage(context.support_dir, stage)


def _start_saved_service(saved: Path, context: UpdateContext,
                         runner: Callable[[list[str]], subprocess.CompletedProcess[str]]) -> None:
    """Restart from the exact saved launch item, never one regenerated by new code."""
    _atomic_write(context.plist, saved.read_bytes())
    _require_success(runner(["/bin/launchctl", "enable", _launch_target()]), "enable the saved background service")
    _bootstrap(runner, _launch_domain(), context.plist)


def _require_closed(port: int) -> None:
    for _ in range(15):
        if _listener_is_closed(port):
            return
        time.sleep(1)
    raise UpdateRefused(f"port {port} is still listening")


def _wait_healthy(context: UpdateContext, *, timeout: int = DEFAULT_STARTUP_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            _api(context.base_url, "/api/dashboard/scan-job")
            return
        except (OSError, ValueError) as exc:
            if time.monotonic() >= deadline:
                raise UpdateRefused("the server did not answer after starting") from exc
            time.sleep(1)


def _copy_app(source: Path, destination: Path) -> None:
    subprocess.run(["/usr/bin/ditto", str(source), str(destination)], check=True, capture_output=True)


def _copy_database(source: Path, destination: Path) -> None:
    with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as saved, sqlite3.connect(destination) as restored:
        saved.backup(restored)
        if restored.execute("pragma quick_check").fetchall() != [("ok",)]:
            raise UpdateRefused("the restored database failed its check")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def context_for(config_path: Path, app: Path, *, home: Path | None = None) -> UpdateContext:
    port = int(os.environ.get("MEDIAFORCE_WEB_PORT", "8777"))
    return UpdateContext(config_path, app, home or Path.home(), f"http://127.0.0.1:{port}", port)


def run_update_step(step: str, app: Path, config_path: Path | None, build: str | None) -> int:
    """The app runs each step and reads one JSON line back. Exit 0 done, 3 still waiting, 1 refused or failed."""
    if config_path is None:
        print(json.dumps({"error": "the bundled app requires --config"}))
        return 1
    context = context_for(config_path.expanduser().resolve(), app)
    try:
        if step == "status":
            result: dict[str, Any] | None = status(context)
        elif step == "prepare":
            if not build:
                raise UpdateRefused("prepare needs the exact --build Sparkle offers")
            result = prepare(context, build)
        elif step == "cancel":
            cancel(context)
            result = None
        elif step == "finish":
            result = finish(context)
        else:
            rollback(context)
            result = None
    except Exception as exc:
        print(json.dumps({"error": str(exc) or type(exc).__name__}))
        return 1
    print(json.dumps({"result": result}))
    return WAITING if result and result.get("waiting") else 0
