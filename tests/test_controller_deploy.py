from __future__ import annotations

from collections.abc import Iterator
import argparse
import contextlib
from datetime import datetime, timedelta, timezone
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
from alembic import command
from sqlalchemy import Column, MetaData, Table, Text

from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db import open_db
from mediaforce.core.db_migrations import _alembic_config, _alembic_script_location
from mediaforce.encoding.delivery_lock import archive_activity, delivery_lock
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
                    f'web_state_dir = "{support}/web"\n[media]\nsource_roots = {{}}\n', encoding="utf-8")
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
            (self.legacy_root / ".env").chmod(0o600)
            _app(self.install, build="legacy", code=b"legacy app")
            item = login_item.LoginItemPaths(self.legacy_root, self.home, saved_label=LABEL)
        else:
            self.config = _config(tmp_path / "persistent/config.toml", self.support)
            item = login_item.LoginItemPaths(_app(self.install, build="1", code=b"old app"), self.home, self.config)
        self.plist = item.plist
        self.previous_paths = item
        self.plist.parent.mkdir(parents=True)
        self.previous_payload = login_item.render_login_item_plist(item)
        self.plist.write_bytes(self.previous_payload)
        self.running: str | None = None if stopped else "previous"
        self.started: list[str] = []
        self.release_held: list[bool] = []

    def disable(self, paths: login_item.LoginItemPaths) -> None:
        assert paths.label == LABEL
        self.running = None

    def start(self, plist: Path) -> None:
        # Bundled startup refuses an app whose code is not the build its launch item trusts.
        item = plistlib.loads(plist.read_bytes())
        if Path(item["ProgramArguments"][0]).is_relative_to(self.legacy_root):
            started = "previous"
        elif item.get("MediaforceAppCodeHash") != plistlib.loads(login_item.render_login_item_plist(
                login_item.LoginItemPaths(self.install, self.home, self.config)))["MediaforceAppCodeHash"] \
                or not self._update_admits(self.install):
            started = "refused"
        else:
            started = "previous" if plist.read_bytes() == self.previous_payload else "release"
        self.running = None if started == "refused" else started
        self.started.append(started)

    def _update_admits(self, app: Path) -> bool:
        try:
            controller_deploy.require_update_startup_allowed(self.support / "library.sqlite3", app)
        except controller_deploy.UpdateRefused:
            return False
        return True

    def api(self, _base: str, route: str, *, post: bool = False) -> dict[str, Any]:
        if self.running is None:
            raise URLError("connection refused")
        if route.startswith("/api/dashboard?"):
            assert not self.stopped, "a stopped install never asks the old server"
            return {"encode_queue": {"state": {"is_paused": True}, "running_count": 0},
                    "calibration_queue": {"sample": {"running_count": 0}, "full": {"running_count": 0}}}
        if route == "/api/dashboard/scan-job" and self.running == "release":
            self.release_held.append(runtime_install.update_hold_reason(self.support) is not None)
            if self.outcome == "unhealthy":
                raise URLError("candidate unavailable")
        assert not post, "a repair must preserve independent background and evidence pauses"
        database = str((self.support / "library.sqlite3").resolve())
        build = {"previous": "3" if self.outcome == "running-build-3" else None, "release": "2"}[self.running]
        if self.outcome == "other-database" and self.running == "previous":
            database = "/elsewhere/library.sqlite3"
        if self.outcome == "wrong-build" and self.running == "release":
            build = "1"
        scans = int(self.outcome == "scan-running" and self.running == "previous")
        return {"counts": {"pending_encode": 0, "unfinished_scan_rows": scans, "active_evidence": 0},
                "server": {"build": build, "database": database}}


@pytest.fixture
def controller(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Controller]:
    previous, stopped, outcome = request.param
    mac = Controller(tmp_path, previous=previous, stopped=stopped, outcome=outcome)
    with open_db(mac.support / "library.sqlite3") as connection:
        connection.exec_driver_sql("create table retained_work (id integer)")
        connection.exec_driver_sql("insert into retained_work values (42)")
    actual_install = controller_deploy.install_app

    def install(app: Path, destination: Path) -> None:
        if mac.outcome == "app-install-failed" and app == mac.release or mac.outcome == "app-folder-locked" \
                or mac.outcome == "restore-app-failed" and app != mac.release:
            raise OSError("Applications folder not writable")
        actual_install(app, destination)

    backup = patch.object(controller_deploy, "backup_database", side_effect=OSError("backup volume unavailable")) \
        if outcome == "backup-failed" else patch.object(controller_deploy, "backup_database", controller_deploy.backup_database)
    refuse_installed = OSError("the installed copy fails its signature check") \
        if outcome in {"installed-signature-refused", "restore-app-failed"} else None
    with patch.object(controller_deploy, "_api", mac.api), patch.object(controller_deploy, "disable_login_item", mac.disable), \
            patch.object(controller_deploy, "start_saved_launch_item", mac.start), \
            patch.object(controller_deploy, "_verify_signed_bundle", side_effect=refuse_installed), patch.object(controller_deploy, "install_app", install), \
            patch.object(controller_deploy, "_copy_app", lambda source, target: shutil.copytree(source, target, symlinks=True)), \
            patch.object(controller_deploy, "_listener_is_closed", return_value=outcome != "listener-stuck"), \
            patch.object(controller_deploy, "require_storage_admission"), patch.object(controller_deploy.time, "sleep"), backup:
        yield mac


def _install(mac: Controller, **options: bool) -> None:
    controller_deploy.install_release(mac.release, mac.support / "deployments/fixture-stage", mac.home,
                                      evidence={"CFBundleVersion": "2"}, install=mac.install, startup_timeout=0,
                                      stopped=mac.stopped, **options)


OUTCOMES = ["healthy", "backup-failed", "app-install-failed", "app-folder-locked", "installed-signature-refused", "restore-app-failed",
            "listener-stuck", "unhealthy", "wrong-build"]


@pytest.mark.parametrize("controller", [
    (previous, stopped, outcome) for previous in ("legacy", "bundled") for stopped in (False, True) for outcome in OUTCOMES
], indirect=True, ids=lambda parameters: "-".join(str(part) for part in parameters))
def test_install_keeps_old_code_off_a_database_the_release_may_have_migrated(controller: Controller) -> None:
    mac = controller
    stage = mac.support / "deployments/fixture-stage"
    release_started = mac.outcome in {"healthy", "unhealthy", "wrong-build"}
    # Only a database no release ever opened, and that a stopped installation has not paused, gets the old service back.
    restored = mac.outcome in {"backup-failed", "app-install-failed", "app-folder-locked", "installed-signature-refused"} \
        and not mac.stopped
    if mac.outcome == "healthy":
        _install(mac)
    else:
        with pytest.raises((RuntimeError, OSError)):
            _install(mac)
    assert mac.started == (["release"] if release_started else ["previous"] if restored else [])
    assert mac.running == ("release" if mac.outcome == "healthy" else "previous" if restored else None)
    installed = (mac.install / "Contents/MacOS/Mediaforce").read_bytes()
    replaced = release_started or mac.outcome == "restore-app-failed" or (mac.stopped and mac.outcome == "installed-signature-refused")
    assert installed == b"release app" if replaced else installed in {b"legacy app", b"old app"}
    assert (mac.plist.read_bytes() == mac.previous_payload) is not release_started
    assert (stage / "launch-item.before.plist").read_bytes() == mac.previous_payload
    assert (stage / "Mediaforce.app/Contents/MacOS/Mediaforce").read_bytes() in {b"legacy app", b"old app"}
    assert runtime_install.retained_deploy_stage(mac.support) == (stage if mac.outcome == "healthy" else None)
    # New work and publication stay held while the release is checked, and until a failed one is reconciled.
    assert mac.release_held and all(mac.release_held) if release_started else not mac.release_held
    held = runtime_install.read_update_intent(mac.support)
    # The record exists from the backup on, and stays until the old service is back or the release passed.
    assert (held is not None) is (mac.outcome not in {"healthy", "backup-failed", "listener-stuck"} and not restored)
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
    assert {mac.legacy_root.resolve(), mac.config.resolve(), (mac.legacy_root / ".env").resolve()} <= set(protected)


@pytest.mark.parametrize("controller", [("bundled", False, "other-database")], indirect=True)
def test_install_refuses_a_running_server_on_another_database_before_changing_anything(controller: Controller) -> None:
    mac = controller
    with pytest.raises(RuntimeError, match="not the saved config's database"):
        _install(mac)
    assert mac.running == "previous" and mac.started == [] and not (mac.support / "deployments").exists()


@pytest.mark.parametrize("controller", [("bundled", False, "healthy")], indirect=True)
@pytest.mark.parametrize("bundle", ["intact", "info-damaged", "missing"])
def test_install_refuses_an_older_release_than_the_build_the_launch_item_records(controller: Controller, bundle: str) -> None:
    mac = controller
    mac.plist.write_bytes(plistlib.dumps({**plistlib.loads(mac.previous_payload), "MediaforceAppBuild": "3"}))
    if bundle == "info-damaged":
        (mac.install / "Contents/Info.plist").write_text("damaged")
    if bundle == "missing":
        shutil.rmtree(mac.install)
    with pytest.raises(RuntimeError, match="older release"):
        _install(mac)
    assert mac.running == "previous" and mac.started == []


@pytest.mark.parametrize("controller", [("bundled", True, "healthy")], indirect=True)
def test_stopped_repair_of_a_damaged_app_needs_no_readable_bundle(controller: Controller) -> None:
    mac = controller
    (mac.install / "Contents/Info.plist").write_text("damaged")
    _install(mac)
    assert mac.running == "release"


@pytest.mark.parametrize("controller", [("bundled", True, "healthy")], indirect=True)
def test_unknown_installed_build_needs_the_explicit_override(controller: Controller) -> None:
    mac = controller
    unrecorded = plistlib.loads(mac.previous_payload)
    del unrecorded["MediaforceAppBuild"]
    mac.previous_payload = plistlib.dumps(unrecorded)
    mac.plist.write_bytes(mac.previous_payload)
    with pytest.raises(RuntimeError, match="--allow-unknown-build"):
        _install(mac)
    assert mac.started == []
    _install(mac, allow_unknown_build=True)
    assert mac.running == "release"


@pytest.mark.parametrize("controller", [("bundled", False, "healthy")], indirect=True)
def test_install_rereads_the_saved_service_once_it_holds_the_lock(controller: Controller) -> None:
    mac = controller
    newer = {**plistlib.loads(mac.previous_payload), "MediaforceAppBuild": "3"}
    actual_activity = controller_deploy.runtime_activity

    @contextlib.contextmanager
    def another_install_finishes_first(support: Path, **options: float) -> Iterator[bool]:
        mac.plist.write_bytes(plistlib.dumps(newer))
        with actual_activity(support, **options) as acquired:
            yield acquired

    with patch.object(controller_deploy, "runtime_activity", another_install_finishes_first), \
            pytest.raises(RuntimeError, match="older release"):
        _install(mac)
    assert mac.started == []


@pytest.mark.parametrize("controller", [("bundled", False, "scan-running")], indirect=True)
def test_install_refuses_while_any_work_runs_before_changing_anything(controller: Controller) -> None:
    mac = controller
    with pytest.raises(RuntimeError, match="library checks are running"):
        _install(mac)
    assert mac.running == "previous" and mac.started == [] and runtime_install.read_update_intent(mac.support) is None


@pytest.mark.parametrize("controller", [("bundled", True, "healthy")], indirect=True)
def test_install_refuses_while_an_app_update_is_unfinished(controller: Controller) -> None:
    mac = controller
    (mac.support / "runtime").mkdir(parents=True)
    (mac.support / "runtime" / runtime_install.UPDATE_INTENT_NAME).write_text(json.dumps({"phase": "prepared"}))
    with pytest.raises(RuntimeError, match="update or install is unfinished"):
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


@pytest.mark.parametrize("controller", [("bundled", False, "unhealthy"), ("legacy", False, "unhealthy")], indirect=True)
def test_a_failed_release_rolls_back_through_the_app_update_rollback(controller: Controller) -> None:
    mac = controller
    with pytest.raises(RuntimeError):
        _install(mac)
    with open_db(mac.support / "library.sqlite3") as connection:
        connection.exec_driver_sql("create table release_migration (id integer)")
    controller_deploy.rollback_update(controller_deploy.AppUpdate(mac.config, mac.install, mac.home))
    assert mac.running == "previous" and mac.started == ["release", "previous"]
    assert mac.plist.read_bytes() == mac.previous_payload and runtime_install.read_update_intent(mac.support) is None
    # The clone runtime's launch item does not run the app, so only a bundled rollback puts the old app back.
    assert (mac.install / "Contents/MacOS/Mediaforce").read_bytes() == (b"release app" if mac.legacy_root.exists() and
                                                                       Path(plistlib.loads(mac.previous_payload)["ProgramArguments"][0]).is_relative_to(mac.legacy_root)
                                                                       else b"old app")
    with sqlite3.connect(mac.support / "library.sqlite3") as restored:
        assert not restored.execute("select name from sqlite_master where name = 'release_migration'").fetchall()


@pytest.mark.parametrize("controller", [("bundled", False, "healthy")], indirect=True)
def test_an_interrupted_install_continues_as_a_stopped_install(controller: Controller) -> None:
    mac = controller
    with patch.object(controller_deploy, "retain_deploy_stage", side_effect=KeyboardInterrupt), pytest.raises(KeyboardInterrupt):
        _install(mac)
    assert (runtime_install.read_update_intent(mac.support) or {}).get("phase") == controller_deploy.INSTALLING
    mac.disable(mac.previous_paths)
    controller_deploy.install_release(mac.release, mac.support / "deployments/continued", mac.home, evidence={},
                                      install=mac.install, startup_timeout=0, stopped=True)
    assert mac.running == "release" and runtime_install.read_update_intent(mac.support) is None


@pytest.mark.parametrize("controller", [("bundled", False, "healthy")], indirect=True)
def test_a_service_that_will_not_stop_leaves_nothing_held(controller: Controller) -> None:
    mac = controller
    with patch.object(controller_deploy, "disable_login_item", side_effect=OSError("launchctl refused")), pytest.raises(OSError):
        _install(mac)
    assert mac.started == [] and runtime_install.read_update_intent(mac.support) is None


@pytest.mark.parametrize("controller", [("bundled", False, "running-build-3")], indirect=True)
def test_an_unrecorded_build_uses_the_running_servers_build(controller: Controller) -> None:
    mac = controller
    unrecorded = plistlib.loads(mac.previous_payload)
    del unrecorded["MediaforceAppBuild"]
    mac.plist.write_bytes(plistlib.dumps(unrecorded))
    with pytest.raises(RuntimeError, match="older release"):
        _install(mac, allow_unknown_build=True)
    assert mac.started == []


@pytest.mark.parametrize("controller", [("legacy", False, "healthy")], indirect=True)
def test_install_refuses_credentials_others_can_read(controller: Controller) -> None:
    mac = controller
    (mac.legacy_root / ".env").chmod(0o644)
    with pytest.raises(runtime_install.RuntimeProtectionError, match="chmod 600"):
        _install(mac)
    assert mac.running == "previous" and mac.started == []


def test_proof_destination_is_compared_after_resolving_links(tmp_path: Path) -> None:
    applications = tmp_path / "Applications"
    applications.mkdir()
    (tmp_path / "alias").symlink_to(applications, target_is_directory=True)
    app = _app(tmp_path / "Mediaforce.app", build="2", code=b"proof")
    with patch.object(controller_deploy, "APPLICATIONS_APP", applications / "Mediaforce.app"), \
            pytest.raises(RuntimeError, match="only the live app"):
        controller_deploy.install_release(app, tmp_path / "stage", tmp_path, evidence={}, install=tmp_path / "alias/Mediaforce.app")


@pytest.mark.parametrize("controller", [("legacy", False, "unhealthy")], indirect=True)
def test_rollback_to_the_clone_runtime_needs_no_saved_app(controller: Controller) -> None:
    mac = controller
    with pytest.raises(RuntimeError):
        _install(mac)
    shutil.rmtree(mac.support / "deployments/fixture-stage/Mediaforce.app")
    outcome = controller_deploy.rollback_update(controller_deploy.AppUpdate(mac.config, mac.install, mac.home))
    assert outcome and "without the app" in outcome
    assert mac.running == "previous" and runtime_install.read_update_intent(mac.support) is None


@pytest.mark.parametrize("controller", [("bundled", False, "unhealthy")], indirect=True)
def test_rollback_refuses_a_missing_saved_app_before_touching_anything(controller: Controller) -> None:
    mac = controller
    with pytest.raises(RuntimeError):
        _install(mac)
    shutil.rmtree(mac.support / "deployments/fixture-stage/Mediaforce.app")
    with sqlite3.connect(mac.support / "library.sqlite3") as migrated:
        migrated.execute("create table release_migration (id integer)")
    with patch.object(controller_deploy, "_verify_signed_bundle", side_effect=login_item.LoginItemError("no app")), \
            pytest.raises(controller_deploy.UpdateRefused, match="--stopped"):
        controller_deploy.rollback_update(controller_deploy.AppUpdate(mac.config, mac.install, mac.home))
    assert (runtime_install.read_update_intent(mac.support) or {}).get("phase") == controller_deploy.INSTALLING
    with sqlite3.connect(mac.support / "library.sqlite3") as kept:
        assert kept.execute("select name from sqlite_master where name = 'release_migration'").fetchall()
    mac.outcome = "healthy"
    controller_deploy.install_release(mac.release, mac.support / "deployments/forward", mac.home, evidence={},
                                      install=mac.install, startup_timeout=0, stopped=True)
    repaired = runtime_install.retained_deploy_stage(mac.support)
    assert repaired is not None and controller_deploy.usable_saved_app(repaired)
    assert login_item.bundle_build(repaired / controller_deploy.MAC_APP_NAME) == login_item.bundle_build(mac.install)
    with sqlite3.connect(repaired / "before.sqlite") as saved:
        assert saved.execute("select * from release_migration").fetchall() == []
    assert mac.running == "release" and runtime_install.read_update_intent(mac.support) is None


@pytest.mark.parametrize("controller", [("bundled", False, "unhealthy")], indirect=True)
def test_a_release_that_fails_its_checks_is_never_offered_again(controller: Controller) -> None:
    mac = controller
    with pytest.raises(RuntimeError):
        _install(mac)
    assert json.loads((mac.support / "runtime" / controller_deploy.UPDATE_FAILED_NAME).read_text()) == ["2"]


@pytest.mark.parametrize("failure", ["disable", "listener-probe"])
@pytest.mark.parametrize("controller", [("bundled", False, "healthy"), ("legacy", False, "healthy")], indirect=True)
def test_a_failed_stop_before_any_backup_brings_the_old_service_back(controller: Controller, failure: str) -> None:
    mac = controller

    def stopped_then_refused(paths: login_item.LoginItemPaths) -> None:
        mac.disable(paths)
        raise login_item.LoginItemError("could not disable the login item")

    stop = patch.object(controller_deploy, "disable_login_item", side_effect=stopped_then_refused) if failure == "disable" \
        else patch.object(controller_deploy, "_listener_is_closed", side_effect=RuntimeError("cannot verify listener state"))
    with stop, pytest.raises(RuntimeError):
        _install(mac)
    assert mac.started[0] == "previous" and mac.running == "previous"
    assert mac.plist.read_bytes() == mac.previous_payload and runtime_install.read_update_intent(mac.support) is None


def test_publishing_check_reads_the_shared_archive_lock_beside_file_locks(tmp_path: Path) -> None:
    database = tmp_path / "library.sqlite3"
    with archive_activity(database):
        with delivery_lock(database, 7):
            pass
        assert controller_deploy._publishing(database) == 1
    assert (database.parent / "delivery-locks" / "archive-activity.lock").exists()
    assert controller_deploy._publishing(database) == 0
    with delivery_lock(database, 7):
        assert _publishing_in_another_thread(database) == 1


def _publishing_in_another_thread(database: Path) -> int:
    result: list[int] = []
    worker = threading.Thread(target=lambda: result.append(controller_deploy._publishing(database)))
    worker.start()
    worker.join()
    return result[0]


class RunningApps:
    """The Mac's running apps as the relaunch sees them: who is listed, who was asked to quit, what was opened."""

    def __init__(self, apps: dict[int, Path], *, quits: bool = True) -> None:
        self.apps, self.quits = apps, quits
        self.asked: list[int] = []
        self.opened: list[str] = []

    def run(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        if command[0] == "/usr/bin/open":
            self.opened.append(command[1])
        elif command[-2] == "quit":
            self.asked.append(int(command[-1]))
            if self.quits:
                self.apps.pop(int(command[-1]))
        rows = "\n".join(f"{pid}\t{path}" for pid, path in self.apps.items())
        return subprocess.CompletedProcess(command, 0, rows, "")


def test_release_quits_and_reopens_only_the_app_running_from_the_installed_path(tmp_path: Path) -> None:
    """Breaks if a release leaves the old app's window over the replaced bundle, or touches another copy."""
    installed, other = tmp_path / "Applications/Mediaforce.app", tmp_path / "Proof/Mediaforce.app"
    apps = RunningApps({41: installed, 42: other, 43: tmp_path / "Finder.app"})
    assert controller_deploy.relaunch_installed_app(installed, apps.run) == "relaunched"
    assert apps.asked == [41] and apps.opened == [str(installed)] and 42 in apps.apps


def test_an_app_that_does_not_quit_is_left_open_and_named(tmp_path: Path) -> None:
    installed = tmp_path / "Mediaforce.app"
    apps = RunningApps({41: installed}, quits=False)
    with patch.object(controller_deploy.time, "sleep"):
        assert controller_deploy.relaunch_installed_app(installed, apps.run) == "needs a manual restart"
    assert apps.asked == [41] and apps.opened == [] and 41 in apps.apps


def test_no_running_app_is_not_opened(tmp_path: Path) -> None:
    apps = RunningApps({42: tmp_path / "Other.app"})
    assert controller_deploy.relaunch_installed_app(tmp_path / "Mediaforce.app", apps.run) == "not running"
    assert apps.asked == [] and apps.opened == []


@pytest.mark.parametrize("controller", [("bundled", True, "app-install-failed")], indirect=True)
def test_stopped_copy_failure_never_opens_the_original_with_candidate_migrations(controller: Controller) -> None:
    mac = controller
    database = mac.support / "library.sqlite3"
    with _alembic_script_location() as location:
        command.downgrade(_alembic_config(database, location), "-1")
    with sqlite3.connect(database) as db:
        previous = db.execute("select version_num from alembic_version").fetchone()
    with pytest.raises(OSError, match="Applications"):
        _install(mac)
    with sqlite3.connect(database) as db:
        assert db.execute("select version_num from alembic_version").fetchone() == previous
    mac.outcome = "wrong-build"
    with pytest.raises(controller_deploy.UpdateRefused):
        controller_deploy.install_release(mac.release, mac.support / "deployments/retry", mac.home, evidence={},
                                         install=mac.install, stopped=True)
    controller_deploy.rollback_update(controller_deploy.AppUpdate(mac.config, mac.install, mac.home))
    with sqlite3.connect(database) as db:
        assert db.execute("select version_num from alembic_version").fetchone() == previous
    assert mac.plist.read_bytes() == mac.previous_payload and mac.running == "previous"


@pytest.mark.parametrize("controller", [("bundled", True, "healthy")], indirect=True)
def test_a_stopped_repair_leaves_independent_pauses_alone(controller: Controller) -> None:
    mac = controller
    from mediaforce.library.background_work import load_background_work_state, set_background_work_paused
    database = mac.support / "library.sqlite3"
    with open_db(database) as connection:
        set_background_work_paused(connection, is_paused=True)
    _install(mac)
    with open_db(database) as connection:
        assert not load_queue_state(connection)["is_paused"]
        assert load_background_work_state(connection)["is_paused"]


@pytest.mark.parametrize("controller", [("bundled", True, "healthy")], indirect=True)
def test_missing_installed_app_preserves_last_protected_rollback(controller: Controller) -> None:
    mac = controller
    earlier = mac.support / "deployments/earlier"
    earlier.mkdir(parents=True)
    shutil.copytree(mac.install, earlier / mac.install.name)
    (earlier / "launch-item.before.plist").write_bytes(mac.plist.read_bytes())
    controller_deploy.backup_database(mac.support / "library.sqlite3", earlier / "before.sqlite")
    runtime_install.retain_deploy_stage(mac.support, earlier)
    shutil.rmtree(mac.install)
    from scripts import release_app
    release = release_app.VerifiedRelease(mac.release, {"CFBundleVersion": "2"}, {})
    args = argparse.Namespace(release="fixture", feed="fixture", public_key="fixture", install=mac.install,
                              stage=mac.support / "deployments/repair", startup_timeout=0, stopped=True, allow_unknown_build=False)
    with patch.object(release_app, "verify_release", return_value=release), patch.object(Path, "home", return_value=mac.home), \
            patch.object(release_app, "relaunch_installed_app", return_value="not running"):
        result = release_app.install(args)
    assert runtime_install.retained_deploy_stage(mac.support) == earlier
    rollback = result["rollback"]
    assert isinstance(rollback, dict) and rollback["stage"] == str(earlier)
    assert mac.running == "release"
