from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from http.client import RemoteDisconnected
import json
import os
import plistlib
import shutil
import sqlite3
import subprocess
import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.error import URLError

import pytest
from sqlalchemy import Column, MetaData, Table, Text

from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db import open_db
from mediaforce.encoding.encode_queue import load_queue_state, save_queue_state
from mediaforce.ops import controller_deploy, login_item, runtime_install

LABEL = "com.mediaforce.web.proof"


def _app(path: Path, *, build: str, code: bytes) -> Path:
    (path / "Contents/MacOS").mkdir(parents=True)
    (path / "Contents/Info.plist").write_bytes(plistlib.dumps(
        {"CFBundleVersion": build, "MediaforceServiceLabel": LABEL, "MediaforceServerPort": "8787"}))
    (path / "Contents/MacOS/Mediaforce").write_bytes(code)
    (path / "Contents/MacOS/mediaforce-server").write_text("#!/bin/sh\n")
    return path


def _config(path: Path, support: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'[state]\ndb_path = "{support}/library.sqlite3"\nrun_manifest_dir = "{support}/runs"\n'
                    f'web_state_dir = "{support}/web"\n[media]\nsource_roots = {{}}\n')
    return path


class Controller:
    def __init__(self, tmp_path: Path, *, previous: str, stopped: bool, outcome: str) -> None:
        self.home, self.support = tmp_path / "home", tmp_path / "support"
        self.install = tmp_path / "Applications/Mediaforce.app"
        self.release = _app(tmp_path / "download/Mediaforce.app", build="2", code=b"release app")
        self.outcome, self.stopped = outcome, stopped
        self.legacy_root = self.support / "runtime/releases/legacy-clone"
        if previous == "legacy":
            self.config = _config(self.legacy_root / "config/defaults.toml", self.support)
            (self.legacy_root / ".env").write_text("MEDIAFORCE_FIXTURE=1\n")
            _app(self.install, build="legacy", code=b"legacy app")
            item = login_item.LoginItemPaths(self.legacy_root, self.home, saved_label=LABEL)
        else:
            self.config = _config(tmp_path / "persistent/config.toml", self.support)
            item = login_item.LoginItemPaths(_app(self.install, build="1", code=b"old app"), self.home, self.config)
        self.plist = item.plist
        self.plist.parent.mkdir(parents=True)
        self.previous_payload = login_item.render_login_item_plist(item)
        self.plist.write_bytes(self.previous_payload)
        self.running: str | None = None if stopped else "previous"
        self.started: list[str] = []
        self.resumed = 0

    def disable(self, paths: login_item.LoginItemPaths) -> None:
        assert paths.label == LABEL
        self.running = None

    def start(self, plist: Path) -> None:
        self.running = "previous" if plist.read_bytes() == self.previous_payload else "release"
        self.started.append(self.running)

    def api(self, _base: str, route: str, *, post: bool = False) -> dict[str, Any]:
        if self.running is None:
            raise URLError("connection refused")
        if route.startswith("/api/dashboard?"):
            assert not self.stopped, "a stopped install never asks the old server"
            return {"encode_queue": {"state": {"is_paused": True}, "running_count": 0},
                    "calibration_queue": {"sample": {"running_count": 0}, "full": {"running_count": 0}}}
        if route == "/api/dashboard/scan-job" and self.running == "release" and self.outcome == "unhealthy":
            raise URLError("candidate unavailable")
        if post:
            self.resumed += 1
            if self.outcome == "resume-lost":
                raise RemoteDisconnected("response lost")
        database = str((self.support / "library.sqlite3").resolve())
        build = {"previous": None, "release": "2"}[self.running]
        if self.outcome == "other-database" and self.running == "previous":
            database = "/elsewhere/library.sqlite3"
        if self.outcome == "wrong-build" and self.running == "release":
            build = "1"
        return {"counts": {"pending_encode": 0}, "server": {"build": build, "database": database}}


@pytest.fixture
def controller(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Controller]:
    previous, stopped, outcome = request.param
    mac = Controller(tmp_path, previous=previous, stopped=stopped, outcome=outcome)
    with open_db(mac.support / "library.sqlite3") as connection:
        connection.exec_driver_sql("create table retained_work (id integer)")
        connection.exec_driver_sql("insert into retained_work values (42)")
    actual_install = controller_deploy.install_app

    def install(app: Path, destination: Path) -> None:
        if mac.outcome == "app-install-failed" and app == mac.release:
            raise OSError("Applications folder not writable")
        actual_install(app, destination)

    backup = patch.object(controller_deploy, "backup_database", side_effect=OSError("backup volume unavailable")) \
        if outcome == "backup-failed" else patch.object(controller_deploy, "backup_database", controller_deploy.backup_database)
    with patch.object(controller_deploy, "_api", mac.api), patch.object(controller_deploy, "disable_login_item", mac.disable), \
            patch.object(controller_deploy, "start_saved_launch_item", mac.start), \
            patch.object(controller_deploy, "_verify_signed_bundle"), patch.object(controller_deploy, "install_app", install), \
            patch.object(controller_deploy, "_copy_app", lambda source, target: shutil.copytree(source, target, symlinks=True)), \
            patch.object(controller_deploy, "_listener_is_closed", return_value=outcome != "listener-stuck"), \
            patch.object(controller_deploy, "require_storage_admission"), patch.object(controller_deploy.time, "sleep"), backup:
        yield mac


def _install(mac: Controller) -> None:
    controller_deploy.install_release(mac.release, mac.support / "deployments/fixture-stage", mac.home,
                                      evidence={"CFBundleVersion": "2"}, install=mac.install, startup_timeout=0,
                                      stopped=mac.stopped)


OUTCOMES = ["healthy", "backup-failed", "app-install-failed", "listener-stuck", "unhealthy", "wrong-build", "resume-lost"]


@pytest.mark.parametrize("controller", [
    (previous, stopped, outcome) for previous in ("legacy", "bundled") for stopped in (False, True) for outcome in OUTCOMES
], indirect=True, ids=lambda parameters: "-".join(str(part) for part in parameters))
def test_install_keeps_old_code_off_a_database_the_release_may_have_migrated(controller: Controller) -> None:
    mac = controller
    stage = mac.support / "deployments/fixture-stage"
    release_started = mac.outcome in {"healthy", "resume-lost", "unhealthy", "wrong-build"}
    # Only a database no release ever opened, and that a stopped install has not paused, gets the old service back.
    restored = mac.outcome in {"backup-failed", "app-install-failed"} and not mac.stopped
    if mac.outcome == "healthy":
        _install(mac)
    else:
        with pytest.raises((RuntimeError, OSError)):
            _install(mac)
    assert mac.started == (["release"] if release_started else ["previous"] if restored else [])
    assert mac.running == ("release" if mac.outcome in {"healthy", "resume-lost"} else "previous" if restored else None)
    installed = (mac.install / "Contents/MacOS/Mediaforce").read_bytes()
    assert installed == b"release app" if release_started else installed in {b"legacy app", b"old app"}
    assert (mac.plist.read_bytes() == mac.previous_payload) is not release_started
    assert (stage / "launch-item.before.plist").read_bytes() == mac.previous_payload
    assert (stage / "Mediaforce.app/Contents/MacOS/Mediaforce").read_bytes() in {b"legacy app", b"old app"}
    assert runtime_install.retained_deploy_stage(mac.support) == (stage if mac.outcome in {"healthy", "resume-lost"} else None)
    assert mac.resumed == (1 if mac.outcome in {"healthy", "resume-lost"} else 0)
    if mac.outcome in {"listener-stuck", "backup-failed"}:
        assert not (stage / "before.sqlite").exists()
        return
    with sqlite3.connect(stage / "before.sqlite") as saved:
        assert saved.execute("select id from retained_work").fetchall() == [(42,)]


@pytest.mark.parametrize("controller", [("legacy", False, "healthy")], indirect=True)
def test_first_install_carries_the_clone_runtime_config_and_credentials(controller: Controller) -> None:
    mac = controller
    _install(mac)
    arguments = plistlib.loads(mac.plist.read_bytes())["ProgramArguments"]
    assert arguments == [str(mac.install / "Contents/MacOS/mediaforce-server"), "--no-reload", "--config", str(mac.config),
                         "--credentials", str(mac.legacy_root / ".env")]
    protected = runtime_install.protected_runtime_roots(mac.support)
    assert mac.legacy_root.resolve() in protected and mac.config.parent.resolve() in protected
    assert mac.resumed == 1


@pytest.mark.parametrize("controller", [("bundled", False, "other-database")], indirect=True)
def test_install_refuses_a_running_server_on_another_database_before_changing_anything(controller: Controller) -> None:
    mac = controller
    with pytest.raises(RuntimeError, match="not the saved config's database"):
        _install(mac)
    assert mac.running == "previous" and mac.started == [] and not (mac.support / "deployments").exists()


@pytest.mark.parametrize("controller", [("bundled", False, "healthy")], indirect=True)
def test_install_refuses_an_older_release_than_the_installed_build(controller: Controller) -> None:
    mac = controller
    shutil.rmtree(mac.install)
    _app(mac.install, build="3", code=b"old app")
    with pytest.raises(RuntimeError, match="older release"):
        _install(mac)
    assert mac.running == "previous" and mac.started == []


@pytest.mark.parametrize("controller", [("bundled", True, "healthy")], indirect=True)
def test_install_refuses_while_an_app_update_is_unfinished(controller: Controller) -> None:
    mac = controller
    (mac.support / "runtime").mkdir(parents=True)
    (mac.support / "runtime" / runtime_install.UPDATE_INTENT_NAME).write_text(json.dumps({"phase": "prepared"}))
    with pytest.raises(RuntimeError, match="update is unfinished"):
        _install(mac)
    assert mac.started == []


@pytest.mark.parametrize("label,install", [("com.mediaforce.web", "elsewhere"), (LABEL, "applications")])
def test_only_the_live_app_installs_in_applications(tmp_path: Path, label: str, install: str) -> None:
    app = tmp_path / "Mediaforce.app"
    (app / "Contents").mkdir(parents=True)
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleVersion": "2", "MediaforceServiceLabel": label}))
    destination = controller_deploy.APPLICATIONS_APP if install == "applications" else tmp_path / "install/Mediaforce.app"
    with pytest.raises(RuntimeError, match="only the live app"):
        controller_deploy.install_release(app, tmp_path / "stage", tmp_path, evidence={}, install=destination)


def test_runtime_registration_retains_both_sides_of_switch(tmp_path: Path) -> None:
    old, new, later = (tmp_path / name for name in ("old-runtime", "new-config", "later-config"))
    runtime_install.register_runtime_roots(tmp_path, old, new)
    runtime_install.register_runtime_roots(tmp_path, new, later)
    roots = runtime_install.protected_runtime_roots(tmp_path)
    assert all(root.resolve() in roots for root in (old, new, later))


def test_service_commands_from_a_checkout_address_the_installed_app(tmp_path: Path) -> None:
    home = tmp_path / "home"
    app = _app(tmp_path / "Applications/Mediaforce.app", build="2", code=b"app")
    config = _config(tmp_path / "persistent/config.toml", tmp_path / "support")
    paths = login_item.LoginItemPaths(app, home, config, saved_label="com.mediaforce.web")
    paths.plist.parent.mkdir(parents=True)
    paths.plist.write_bytes(login_item.render_login_item_plist(paths))
    with patch.object(login_item, "enable_login_item", return_value="unchanged") as enable:
        assert login_item.run_login_item_command("restart", project_root=tmp_path / "worktrees/task", home=home) == 0
    assert enable.call_args.args[0].project_root == app
    assert enable.call_args.args[0].config_path == config


def test_service_install_refuses_a_checkout(tmp_path: Path) -> None:
    paths = login_item.LoginItemPaths(tmp_path / "worktrees/task", tmp_path / "home")
    with patch.object(login_item, "_require_supported_platform"), pytest.raises(login_item.LoginItemError):
        login_item.install_login_item(paths)
    assert not paths.plist.exists()


@pytest.mark.parametrize("journal_mode", ["delete", "wal"])
def test_database_backup_preserves_rows_and_refuses_overwrite(tmp_path: Path, journal_mode: str) -> None:
    source, backup = tmp_path / "state.sqlite", tmp_path / "before.sqlite"
    connection = sqlite3.connect(source)
    connection.execute(f"pragma journal_mode={journal_mode}")
    connection.execute("create table jobs (id integer, state text)")
    connection.execute("insert into jobs values (1, 'queued')")
    connection.commit()
    connection.close()
    assert not Path(f"{source}-wal").exists() and not Path(f"{source}-shm").exists()
    controller_deploy.backup_database(source, backup)
    with sqlite3.connect(backup) as connection:
        assert connection.execute("select * from jobs").fetchall() == [(1, "queued")]
    with pytest.raises(RuntimeError):
        controller_deploy.backup_database(source, backup)


@pytest.mark.parametrize("paused,encodes,samples", [(False, 0, 0), (True, 1, 0), (True, 0, 1)])
def test_deploy_refuses_unpaused_or_running_work(paused: bool, encodes: int, samples: int) -> None:
    dashboard = {
        "encode_queue": {"state": {"is_paused": paused}, "running_count": encodes},
        "calibration_queue": {"sample": {"running_count": samples}, "full": {"running_count": 0}},
    }
    with pytest.raises(RuntimeError):
        controller_deploy.require_paused_idle_dashboard(dashboard)


@pytest.mark.parametrize("outcome", ["healthy", "storage-stalled", "storage-unchecked"])
def test_storage_admission_needs_a_second_fresh_pass_by_the_new_server(tmp_path: Path, outcome: str) -> None:
    from mediaforce.hosts.controller_mount import ControllerMountProbe
    from mediaforce.web.runtime import controller_storage_recovery as recovery

    clock = [0.0]
    config = MediaforceConfig(
        raw={"media": {"source_roots": {"tv": "/Volumes/media/tv"}, "staging_root": "/Volumes/media/staging"}},
        paths=ConfigPaths(project_root=tmp_path, config_path=tmp_path / "config.toml", db_path=tmp_path / "library.sqlite3",
                          run_manifest_dir=tmp_path / "runs", web_state_dir=tmp_path / "web",
                          review_dir=tmp_path / "review", runtime_settings_path=tmp_path / "storage-settings.json"),
    )
    passes: list[float] = []

    def wall_clock() -> datetime:
        return datetime(2026, 11, 8, tzinfo=timezone.utc) + timedelta(seconds=clock[0])

    def storage_pass(*, mounted: bool = True) -> None:
        probe = ControllerMountProbe(mounted, mounted, Path("/Volumes/media"), failure_kind=None if mounted else "mount_absent")
        with patch.object(recovery.sys, "platform", "darwin"), patch.object(recovery, "probe_controller_volume", return_value=probe):
            recovery.process_controller_storage_recovery_once(config)
        passes.append(clock[0])

    def api(_base: str, route: str, *, post: bool = False) -> dict[str, Any]:
        assert route == "/api/hosts" and not post
        # The new server checks every 30 seconds. A stalled loop finishes only its first pass; an unchecked one never
        # runs, leaving the old server's failure record.
        if outcome != "storage-unchecked" and not (outcome == "storage-stalled" and passes):
            if not passes or clock[0] - passes[-1] >= 30:
                storage_pass()
        return {"controller_storage": recovery.controller_storage_recovery_snapshot(config),
                "controller_storage_admission": recovery.controller_storage_admission(config)}

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    with patch.object(controller_deploy, "_api", side_effect=api), patch.object(controller_deploy.time, "sleep", side_effect=sleep), \
            patch.object(controller_deploy.time, "monotonic", side_effect=lambda: clock[0]), \
            patch.object(recovery, "_utc_now", side_effect=wall_clock):
        storage_pass(mounted=False)
        passes.clear()
        clock[0] += 1
        started_at = wall_clock()
        if outcome == "healthy":
            controller_deploy.require_storage_admission("http://fixture", started_at)
        else:
            with pytest.raises(RuntimeError, match="storage check is not keeping"):
                controller_deploy.require_storage_admission("http://fixture", started_at)


def _app_at(path: Path, marker: str) -> None:
    (path / "Contents").mkdir(parents=True)
    (path / "Contents/Info.plist").write_text(marker)
    (path / "Contents/stale-resource").write_text(marker)


def _copy_tree(source: Path, target: Path) -> None:
    shutil.copytree(source, target, symlinks=True)


@pytest.mark.parametrize("previous", ["none", "installed", "left-aside-by-interrupted-run"])
def test_app_install_replaces_previous_copy(tmp_path: Path, previous: str) -> None:
    release, applications = tmp_path / "release/Mediaforce.app", tmp_path / "Applications"
    _app_at(release, "new")
    (release / "Contents/stale-resource").unlink()
    installed = applications / controller_deploy.MAC_APP_NAME
    applications.mkdir()
    if previous == "installed":
        _app_at(installed, "old")
    if previous == "left-aside-by-interrupted-run":
        _app_at(applications / f".{controller_deploy.MAC_APP_NAME}.replaced", "old")
    with patch.object(controller_deploy, "_copy_app", _copy_tree):
        controller_deploy.install_app(release, installed)
    assert (installed / "Contents/Info.plist").read_text() == "new"
    assert not (installed / "Contents/stale-resource").exists()
    assert sorted(path.name for path in applications.iterdir()) == [controller_deploy.MAC_APP_NAME]
    assert (release / "Contents/Info.plist").read_text() == "new"


@pytest.mark.parametrize("previous", ["installed", "left-aside-by-interrupted-run"])
@pytest.mark.parametrize("failing_step", ["copy", "move-in"])
def test_failed_app_install_keeps_previous_app(tmp_path: Path, previous: str, failing_step: str) -> None:
    release, applications = tmp_path / "release/Mediaforce.app", tmp_path / "Applications"
    _app_at(release, "new")
    installed = applications / controller_deploy.MAC_APP_NAME
    applications.mkdir()
    _app_at(installed if previous == "installed" else applications / f".{controller_deploy.MAC_APP_NAME}.replaced", "old")
    incoming = applications / f".{controller_deploy.MAC_APP_NAME}.incoming"

    def rename(path: Path, target: Path) -> Path:
        if path == incoming:
            raise OSError("Applications folder refused the move")
        os.rename(path, target)
        return target

    copy = patch.object(controller_deploy, "_copy_app", side_effect=OSError("disk full")) if failing_step == "copy" \
        else patch.object(controller_deploy, "_copy_app", _copy_tree)
    with copy, patch.object(Path, "rename", rename) if failing_step == "move-in" else patch.object(Path, "rename", Path.rename), \
            pytest.raises(OSError):
        controller_deploy.install_app(release, installed)
    assert (installed / "Contents/Info.plist").read_text() == "old"


def test_an_install_waits_for_the_cleanup_its_own_pause_started(tmp_path: Path) -> None:
    support = tmp_path / "support"
    app = _app(tmp_path / "download/Mediaforce.app", build="2", code=b"release app")
    config = _config(tmp_path / "persistent/config.toml", support)
    item = login_item.LoginItemPaths(_app(tmp_path / "Applications/Mediaforce.app", build="1", code=b"old"), tmp_path, config)
    item.plist.parent.mkdir(parents=True)
    item.plist.write_bytes(login_item.render_login_item_plist(item))
    started, finish = threading.Event(), threading.Event()

    def cleanup() -> None:
        with runtime_install.runtime_activity(support, cleanup=True) as acquired:
            assert acquired
            started.set()
            finish.wait(5)

    def install() -> None:
        controller_deploy.install_release(app, support / "deployments/stage", tmp_path, evidence={},
                                          install=tmp_path / "Applications/Mediaforce.app")

    holder = threading.Thread(target=cleanup)
    holder.start()
    started.wait(5)
    with patch.object(controller_deploy, "_switch") as switched:
        with patch.object(controller_deploy, "DEPLOY_LOCK_WAIT_SECONDS", 0.2), pytest.raises(RuntimeError, match="stayed active"):
            install()
        switched.assert_not_called()
        threading.Timer(0.3, finish.set).start()
        install()
    holder.join()
    switched.assert_called_once()


def _stopped_config(root: Path, database: Path) -> MediaforceConfig:
    return MediaforceConfig(
        raw={"media": {"source_roots": {}, "staging_root": str(root / "staging"), "archive_root": str(root / "archive")}},
        paths=ConfigPaths(project_root=root, config_path=root / "config.toml", db_path=database,
                          run_manifest_dir=root / "runs", web_state_dir=root / "web", review_dir=root / "review",
                          runtime_settings_path=root / "settings.json", runtime_reservation_dir=root / "reservations"),
    )


@pytest.mark.parametrize("table,column,status", [
    ("encode_jobs", "status", "running"), ("calibration_jobs", "status", "starting"),
    ("scan_runs", "status", "running"), ("library_item_evidence_state", "work_status", "running"),
])
def test_stopped_deploy_refuses_recorded_running_work(tmp_path: Path, table: str, column: str, status: str) -> None:
    database = tmp_path / "library.sqlite3"
    with open_db(database) as connection:
        running = Table(table, MetaData(), Column(column, Text))
        running.drop(connection)
        running.create(connection)
        connection.execute(running.insert().values({column: status}))
    with patch.object(controller_deploy.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, b"", b"")):
        with pytest.raises(RuntimeError):
            controller_deploy.require_stopped_idle_controller(_stopped_config(tmp_path, database))


@pytest.mark.parametrize("result", [
    subprocess.CompletedProcess([], 0, b"123", b""), subprocess.CompletedProcess([], 1, b"", b"permission denied"),
    subprocess.CompletedProcess([], 2, b"", b""),
])
def test_stopped_deploy_refuses_unknown_or_open_listener(result: subprocess.CompletedProcess[bytes]) -> None:
    with patch.object(controller_deploy.subprocess, "run", return_value=result):
        with pytest.raises(RuntimeError):
            controller_deploy.require_closed_listener()


def test_paused_rollback_backup_preserves_work_and_leaves_source_queue_unchanged(tmp_path: Path) -> None:
    source, backup = tmp_path / "source.sqlite", tmp_path / "before.sqlite"
    retained_job = "retained-job"
    work_id = 42
    with open_db(source) as connection:
        state = load_queue_state(connection)
        state.update(is_paused=False, stop_requested=True, active_job_id=retained_job, updated_at="2026-10-29T00:00:00+00:00")
        save_queue_state(connection, state)
        connection.exec_driver_sql("CREATE TABLE retained_work (job INTEGER)")
        connection.exec_driver_sql("INSERT INTO retained_work VALUES (?)", (work_id,))
    controller_deploy.backup_database(source, backup, pause_queue=True)
    with sqlite3.connect(source) as connection:
        assert connection.execute("SELECT is_paused FROM encode_queue_state").fetchone() == (0,)
    with sqlite3.connect(backup) as connection:
        assert connection.execute("SELECT is_paused, stop_requested, active_job_id FROM encode_queue_state").fetchone() == (1, 1, retained_job)
        assert connection.execute("SELECT job FROM retained_work").fetchall() == [(work_id,)]
