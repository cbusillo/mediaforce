from __future__ import annotations

import plistlib
from datetime import datetime, timedelta, timezone
import shutil
import sqlite3
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from mediaforce.core.db import open_db
from mediaforce.encoding.delivery_lock import delivery_lock
from mediaforce.encoding.encode_queue import load_queue_state, save_queue_state
from mediaforce.library.background_work import background_work_is_paused, load_background_work_state
from mediaforce.ops import controller_deploy as deploy
from mediaforce.ops.login_item import LoginItemPaths, render_login_item_plist
from mediaforce.ops.runtime_install import read_update_intent, retain_deploy_stage, retained_deploy_stage
from mediaforce.web.runtime.queue_actions import resume_encode_queue_action


class FakeServer:
    """The running server's API as the update sees it, and launchd."""

    def __init__(self, update: deploy.AppUpdate) -> None:
        self.update = update
        self.running = {"encode": 0, "sample": 0}
        self.finishes_in: int | None = None
        self.failing_build: str | None = None
        self.answering_build: str | None = None
        self.launchctl: list[list[str]] = []

    def api(self, _base: str, route: str, *, post: bool = False) -> dict[str, Any]:
        _ = post
        if route.startswith("/api/dashboard?"):
            return {"encode_queue": {"running_count": self.running["encode"]},
                    "calibration_queue": {"sample": {"running_count": self.running["sample"]}, "full": {"running_count": 0}}}
        if route == "/api/app/activity":
            finishes = (datetime.now(timezone.utc) + timedelta(seconds=self.finishes_in)).isoformat() if self.finishes_in else None
            return {"computers": [{"running": [{"finishes_at": finishes}] * self.running["encode"]}]}
        build = deploy.bundle_build(self.update.app)
        if route == "/api/dashboard/scan-job" and build == self.failing_build:
            raise OSError("connection refused")
        server = {"build": self.answering_build or build, "database": str(self.update.config.paths.db_path.resolve())}
        return {"counts": {"pending_encode": 4, "unfinished_scan_rows": 0, "active_evidence": 0}, "server": server}

    def run(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.launchctl.append(command[1:])
        return subprocess.CompletedProcess(command, 1 if command[1] == "print" else 0, "", "")


Update = tuple[deploy.AppUpdate, FakeServer]


@pytest.fixture
def update(tmp_path: Path) -> Iterator[Update]:
    support = tmp_path / "support"
    config = tmp_path / "config.toml"
    config.write_text(
        f'[state]\ndb_path = "{support}/library.sqlite3"\nrun_manifest_dir = "{support}/runs"\n'
        f'web_state_dir = "{support}/web"\n[media]\nsource_roots = {{}}\n'
    )
    support.mkdir()
    with open_db(support / "library.sqlite3") as connection:
        save_queue_state(connection, {**load_queue_state(connection), "is_paused": True, "updated_at": "then"})
    app = _app(tmp_path / "Applications/Mediaforce.app", build="1", code=b"old app")
    item = LoginItemPaths(app, tmp_path / "home", config)
    item.plist.parent.mkdir(parents=True)
    item.plist.write_bytes(render_login_item_plist(item))
    update = deploy.AppUpdate(config, app, tmp_path / "home")
    server = FakeServer(update)
    with patch.object(deploy, "_api", server.api), \
            patch.object(deploy, "_run_command", server.run), patch("mediaforce.ops.login_item._run_command", server.run), \
            patch.object(deploy, "_verify_signed_bundle"), patch.object(deploy, "_listener_is_closed", return_value=True), \
            patch.object(deploy, "_copy_app", lambda source, target: shutil.copytree(source, target, symlinks=True)), \
            patch.object(deploy, "require_storage_admission"), patch.object(deploy.time, "sleep"), \
            patch("mediaforce.ops.login_item._require_supported_platform"):
        yield update, server


def _app(path: Path, *, build: str, code: bytes, **identity: object) -> Path:
    (path / "Contents/MacOS").mkdir(parents=True)
    (path / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleVersion": build, **identity}))
    (path / "Contents/MacOS/Mediaforce").write_bytes(code)
    (path / "Contents/MacOS/mediaforce-server").write_text("#!/bin/sh\n")
    return path


def _install(update: deploy.AppUpdate, *, build: str) -> None:
    """What Sparkle's installer does: the same path now holds the new build."""
    shutil.rmtree(update.app)
    _app(update.app, build=build, code=b"new app")


def _intent(update: deploy.AppUpdate) -> dict[str, Any]:
    intent = read_update_intent(update.support)
    assert intent is not None
    return intent


def _phase(update: deploy.AppUpdate) -> str | None:
    intent = read_update_intent(update.support)
    return None if intent is None else str(intent["phase"])


def _work(update: deploy.AppUpdate) -> tuple[bool, bool, bool]:
    """The saved encode and background pauses a person set, and whether background work is held now."""
    with open_db(update.config.paths.db_path) as connection:
        return (bool(load_queue_state(connection)["is_paused"]), bool(load_background_work_state(connection)["is_paused"]),
                background_work_is_paused(connection))


def test_long_running_work_is_not_held_and_short_work_drains_before_the_update(update: Update) -> None:
    app_update, server = update
    server.running["encode"] = 1
    deploy.prepare_update(app_update, "2")
    # Nothing waits merely because an update exists.
    assert _phase(app_update) is None and _work(app_update) == (True, False, False) and server.launchctl == []
    server.finishes_in = 60
    deploy.prepare_update(app_update, "2")
    assert _phase(app_update) == "draining" and _work(app_update) == (True, False, True) and server.launchctl == []
    resumed = resume_encode_queue_action(connection_factory=lambda: open_db(app_update.config.paths.db_path),
                                         config=app_update.config, now_iso=lambda: "now")
    assert resumed["ok"] is False
    server.finishes_in = 3600
    deploy.prepare_update(app_update, "2")
    assert _phase(app_update) == "draining"
    server.running["encode"] = 0
    deploy.prepare_update(app_update, "2")
    assert _phase(app_update) == "prepared" and server.launchctl[-1][0] == "disable"


def test_publication_holding_its_lock_keeps_the_old_server_running(update: Update) -> None:
    app_update, server = update
    (app_update.support / "delivery-locks").mkdir()
    (app_update.support / "delivery-locks/7.lock").touch()
    holding, done = threading.Event(), threading.Event()

    def publish() -> None:
        with delivery_lock(app_update.config.paths.db_path, 7):
            holding.set()
            done.wait(10)

    publisher = threading.Thread(target=publish)
    publisher.start()
    holding.wait(10)
    deploy.prepare_update(app_update, "2")
    assert _phase(app_update) != "prepared" and server.launchctl == []
    done.set()
    publisher.join()
    deploy.prepare_update(app_update, "2")
    assert _phase(app_update) == "prepared"


def test_quiet_server_saves_complete_old_pair_before_sparkle_may_install(update: Update) -> None:
    app_update, server = update
    deploy.prepare_update(app_update, "2")
    stage = Path(_intent(app_update)["stage"])
    assert (stage / "Mediaforce.app/Contents/MacOS/Mediaforce").read_bytes() == b"old app"
    assert (stage / "launch-item.before.plist").read_bytes() == app_update.paths.plist.read_bytes()
    with sqlite3.connect(stage / "before.sqlite") as saved:
        assert saved.execute("pragma quick_check").fetchall() == [("ok",)]
    assert retained_deploy_stage(app_update.support) == stage and server.launchctl[-1][0] == "disable"
    with pytest.raises(deploy.UpdateRefused):
        deploy.require_update_startup_allowed(app_update.support, app_update.app)


def test_newer_offer_while_prepared_reuses_the_saved_pair(update: Update) -> None:
    app_update, server = update
    deploy.prepare_update(app_update, "2")
    stage, calls = _intent(app_update)["stage"], len(server.launchctl)
    deploy.prepare_update(app_update, "3")
    assert _intent(app_update)["to"] == "3" and _intent(app_update)["stage"] == stage and len(server.launchctl) == calls
    _install(app_update, build="3")
    assert deploy.reconcile_update(app_update) == "updated" and _phase(app_update) is None


@pytest.mark.parametrize("interrupted", [False, True])
def test_failure_before_replacement_restarts_unchanged_old_service(update: Update, interrupted: bool) -> None:
    app_update, server = update
    previous = app_update.support / "deployments/earlier"
    retain_deploy_stage(app_update.support, previous)
    saved_plist = app_update.paths.plist.read_bytes()
    failure = KeyboardInterrupt if interrupted else RuntimeError
    with patch.object(deploy, "backup_database", side_effect=failure("disk")), pytest.raises(failure):
        deploy.prepare_update(app_update, "2")
    if interrupted:
        # A quit or reboot mid-save leaves the record; the next launch backs out from it.
        assert _phase(app_update) == "saving"
        deploy.reconcile_update(app_update)
    assert retained_deploy_stage(app_update.support) == previous
    assert app_update.paths.plist.read_bytes() == saved_plist and server.launchctl[-1][0] == "kickstart"
    assert _work(app_update) == (True, False, False) and _phase(app_update) is None


def test_failed_restart_keeps_the_original_cause(update: Update) -> None:
    app_update, server = update
    server.failing_build = "1"
    with patch.object(deploy, "backup_database", side_effect=RuntimeError("disk full")), \
            patch.object(deploy, "DEFAULT_STARTUP_TIMEOUT", 0), pytest.raises(RuntimeError, match="disk full"):
        deploy.prepare_update(app_update, "2")


def test_failed_candidate_rolls_back_on_its_own_to_the_matching_old_pair(update: Update) -> None:
    app_update, server = update
    deploy.prepare_update(app_update, "2")
    saved_plist = app_update.paths.plist.read_bytes()
    _install(app_update, build="2")
    server.failing_build = "2"
    with sqlite3.connect(app_update.config.paths.db_path) as migrated:
        migrated.execute("create table candidate_migration (id integer)")
    with patch.object(deploy, "DEFAULT_STARTUP_TIMEOUT", 0):
        assert deploy.reconcile_update(app_update) == "rolled back"
    assert (app_update.app / "Contents/MacOS/Mediaforce").read_bytes() == b"old app"
    assert app_update.paths.plist.read_bytes() == saved_plist
    with sqlite3.connect(app_update.config.paths.db_path) as restored:
        assert not restored.execute("select name from sqlite_master where name = 'candidate_migration'").fetchall()
    stage = retained_deploy_stage(app_update.support)
    assert stage is not None
    failed, = stage.glob("failed-*")
    assert (failed / "Mediaforce.app/Contents/MacOS/Mediaforce").read_bytes() == b"new app"
    assert _work(app_update) == (True, False, False) and _phase(app_update) is None
    with pytest.raises(deploy.UpdateRefused):
        deploy.prepare_update(app_update, "2")


def test_interrupted_rollback_finishes_from_the_saved_pair_on_next_launch(update: Update) -> None:
    app_update, server = update
    deploy.prepare_update(app_update, "2")
    _install(app_update, build="2")
    server.failing_build = "2"
    with patch.object(deploy, "DEFAULT_STARTUP_TIMEOUT", 0), patch.object(deploy, "_copy_app", side_effect=KeyboardInterrupt), \
            pytest.raises(KeyboardInterrupt):
        deploy.reconcile_update(app_update)
    assert _phase(app_update) == "rolling-back"
    assert deploy.reconcile_update(app_update) == "rolled back"
    assert (app_update.app / "Contents/MacOS/Mediaforce").read_bytes() == b"old app" and _phase(app_update) is None


def test_verified_candidate_keeps_launch_item_and_every_earlier_pause(update: Update) -> None:
    app_update, _server = update
    saved = plistlib.loads(app_update.paths.plist.read_bytes())
    deploy.prepare_update(app_update, "2")
    _install(app_update, build="2")
    assert deploy.reconcile_update(app_update) == "updated"
    candidate = plistlib.loads(app_update.paths.plist.read_bytes())
    assert candidate["MediaforceAppCodeHash"] != saved["MediaforceAppCodeHash"]
    assert {**candidate, "MediaforceAppCodeHash": None} == {**saved, "MediaforceAppCodeHash": None}
    assert _work(app_update) == (True, False, False) and _phase(app_update) is None


def test_candidate_answering_as_another_build_is_rolled_back(update: Update) -> None:
    app_update, server = update
    deploy.prepare_update(app_update, "2")
    _install(app_update, build="2")
    server.answering_build = "1"
    assert deploy.reconcile_update(app_update) == "rolled back"
    assert deploy.bundle_build(app_update.app) == "1" and "2" in deploy._failed(app_update)


def test_proof_build_refuses_the_live_support_folder(tmp_path: Path) -> None:
    from mediaforce.ops.runtime_install import RuntimeProtectionError, require_own_support

    proof = _app(tmp_path / "Proof.app", build="1", code=b"", MediaforceServiceLabel="com.mediaforce.web.proof")
    live_database = tmp_path / "Library/Application Support/mediaforce/library.sqlite3"
    with pytest.raises(RuntimeProtectionError):
        require_own_support(proof, live_database, tmp_path)
    require_own_support(proof, tmp_path / "proof/library.sqlite3", tmp_path)


def test_bundle_without_its_info_never_takes_the_live_identity(tmp_path: Path) -> None:
    from mediaforce.ops.runtime_install import bundle_service

    (tmp_path / "Broken.app/Contents").mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        bundle_service(tmp_path / "Broken.app")
