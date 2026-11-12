from __future__ import annotations

import fcntl
import json
import plistlib
import shutil
import sqlite3
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from mediaforce.core.config import load_config
from mediaforce.core.db import open_db
from mediaforce.encoding.encode_queue import load_queue_state, save_queue_state
from mediaforce.library.background_work import background_work_is_paused
from mediaforce.ops import controller_deploy as deploy
from mediaforce.ops.login_item import LoginItemPaths, render_login_item_plist
from mediaforce.ops.runtime_install import SERVER_PORT, SERVICE_LABEL, read_update_intent, retain_deploy_stage, retained_deploy_stage
from mediaforce.web.runtime.queue_actions import fence_for_update_action, release_update_fence_action


class FakeServer:
    """The running server as the update sees it: its API and pause switches, and launchd."""

    def __init__(self, update: deploy.AppUpdate) -> None:
        self.update = update
        self.running = {"encode": 0, "sample": 0}
        self.healthy = True
        self.launchctl: list[list[str]] = []

    def api(self, _base: str, route: str, *, post: bool = False) -> dict[str, Any]:
        _ = post
        if route.startswith("/api/dashboard?"):
            return {"encode_queue": {"running_count": self.running["encode"]},
                    "calibration_queue": {"sample": {"running_count": self.running["sample"]}, "full": {"running_count": 0}}}
        if not self.healthy:
            raise OSError("connection refused")
        server = {"build": deploy.bundle_build(self.update.app), "database": str(self.update.config.paths.db_path.resolve())}
        return {"counts": {"pending_encode": 4, "unfinished_scan_rows": 0, "active_evidence": 0}, "server": server}

    def post(self, _update: object, route: str, body: dict[str, Any]) -> dict[str, Any]:
        config = self.update.config
        def connection() -> Any:
            return open_db(config.paths.db_path)
        if route == "/api/update/fence":
            return fence_for_update_action(connection_factory=connection, now_iso=lambda: "now")
        return release_update_fence_action(body, connection_factory=connection, config=config, now_iso=lambda: "now")

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
        save_queue_state(connection, {**load_queue_state(connection), "is_paused": False, "updated_at": "then"})
    app = _app(tmp_path / "Applications/Mediaforce.app", build="1", code=b"old app")
    item = LoginItemPaths(app, tmp_path / "home", config)
    item.plist.parent.mkdir(parents=True)
    item.plist.write_bytes(render_login_item_plist(item))
    update = deploy.AppUpdate(config, app, tmp_path / "home")
    server = FakeServer(update)
    with patch.object(deploy, "_api", server.api), patch.object(deploy, "_post", server.post), \
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


def _phase(update: deploy.AppUpdate) -> str | None:
    intent = read_update_intent(update.support)
    return None if intent is None else str(intent["phase"])


def _paused(update: deploy.AppUpdate) -> tuple[bool, bool]:
    with open_db(update.config.paths.db_path) as connection:
        return bool(load_queue_state(connection)["is_paused"]), background_work_is_paused(connection)


def test_busy_server_keeps_running_with_new_work_held_until_files_finish(update: Update) -> None:
    app_update, server = update
    server.running.update(encode=1, sample=1)
    deploy.prepare_update(app_update, "2")
    assert _phase(app_update) == "fenced" and len(read_update_intent(app_update.support)["waiting"]) == 2
    assert server.launchctl == [] and _paused(app_update) == (True, True)
    assert server.post(app_update, "/api/update/release", {"encode": False, "background": False})["ok"] is False
    with pytest.raises(deploy.UpdateRefused):
        deploy.prepare_update(app_update, "3")
    deploy.back_out_update(app_update)
    assert _paused(app_update) == (False, False) and _phase(app_update) is None


def test_publication_holding_its_lock_keeps_the_old_server_running(update: Update) -> None:
    app_update, server = update
    (app_update.support / "delivery-locks").mkdir()
    with (app_update.support / "delivery-locks/7.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        deploy.prepare_update(app_update, "2")
        assert _phase(app_update) == "fenced" and server.launchctl == []
    deploy.prepare_update(app_update, "2")
    assert _phase(app_update) == "prepared"


def test_idle_server_saves_complete_old_pair_before_sparkle_may_install(update: Update) -> None:
    app_update, server = update
    deploy.prepare_update(app_update, "2")
    stage = Path(read_update_intent(app_update.support)["stage"])
    assert (stage / "Mediaforce.app/Contents/MacOS/Mediaforce").read_bytes() == b"old app"
    assert (stage / "launch-item.before.plist").read_bytes() == app_update.paths.plist.read_bytes()
    with sqlite3.connect(stage / "before.sqlite") as saved:
        assert saved.execute("pragma quick_check").fetchall() == [("ok",)]
    assert retained_deploy_stage(app_update.support) == stage and server.launchctl[-1][0] == "disable"
    with pytest.raises(deploy.UpdateRefused):
        deploy.require_update_startup_allowed(app_update.support, app_update.app)


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
    assert _paused(app_update) == (False, False) and _phase(app_update) is None


def test_failed_candidate_stays_stopped_and_rolls_back_to_matching_old_pair(update: Update) -> None:
    app_update, server = update
    deploy.prepare_update(app_update, "2")
    saved_plist = app_update.paths.plist.read_bytes()
    _install(app_update, build="2")
    server.healthy = False
    with sqlite3.connect(app_update.config.paths.db_path) as migrated:
        migrated.execute("create table candidate_migration (id integer)")
    with patch.object(deploy, "DEFAULT_STARTUP_TIMEOUT", 0), pytest.raises(RuntimeError):
        deploy.reconcile_update(app_update)
    assert server.launchctl[-1][0] == "disable" and _phase(app_update) == "failed"
    with pytest.raises(deploy.UpdateRefused):
        deploy.require_update_startup_allowed(app_update.support, app_update.app)

    server.healthy = True
    deploy.rollback_update(app_update)
    assert (app_update.app / "Contents/MacOS/Mediaforce").read_bytes() == b"old app"
    assert app_update.paths.plist.read_bytes() == saved_plist
    with sqlite3.connect(app_update.config.paths.db_path) as restored:
        assert not restored.execute("select name from sqlite_master where name = 'candidate_migration'").fetchall()
    failed, = retained_deploy_stage(app_update.support).glob("failed-*")
    assert (failed / "Mediaforce.app/Contents/MacOS/Mediaforce").read_bytes() == b"new app"
    assert _paused(app_update) == (False, False) and _phase(app_update) is None
    with pytest.raises(deploy.UpdateRefused):
        deploy.prepare_update(app_update, "2")


def test_verified_candidate_keeps_launch_item_and_resumes_only_a_running_queue(update: Update) -> None:
    app_update, _server = update
    with open_db(app_update.config.paths.db_path) as connection:
        save_queue_state(connection, {**load_queue_state(connection), "is_paused": True, "updated_at": "then"})
    saved = plistlib.loads(app_update.paths.plist.read_bytes())
    deploy.prepare_update(app_update, "2")
    stage = Path(read_update_intent(app_update.support)["stage"])
    _install(app_update, build="2")
    deploy.reconcile_update(app_update)
    candidate = plistlib.loads(app_update.paths.plist.read_bytes())
    assert candidate["MediaforceAppCodeHash"] != saved["MediaforceAppCodeHash"]
    assert {**candidate, "MediaforceAppCodeHash": None} == {**saved, "MediaforceAppCodeHash": None}
    assert json.loads((stage / "candidate.txt").read_text())["build"] == "2"
    assert _paused(app_update) == (True, False) and _phase(app_update) is None


def test_candidate_answering_as_another_build_is_not_released(update: Update) -> None:
    app_update, _server = update
    deploy.prepare_update(app_update, "2")
    _install(app_update, build="2")
    with patch.object(deploy, "_api", lambda *_a, **_k: {"counts": {"pending_encode": 4}, "server": {"build": "1"}}), \
            pytest.raises(deploy.UpdateRefused):
        deploy.reconcile_update(app_update)
    assert _phase(app_update) == "failed"


def test_proof_build_addresses_only_its_own_service(tmp_path: Path) -> None:
    proof = _app(tmp_path / "Proof.app", build="1", code=b"", MediaforceServiceLabel="com.mediaforce.web.proof",
                 MediaforceServerPort=8787)
    paths = LoginItemPaths(proof, tmp_path)
    assert paths.label != SERVICE_LABEL and paths.plist.name != f"{SERVICE_LABEL}.plist"
    assert deploy.AppUpdate(tmp_path / "config.toml", proof, tmp_path).port != SERVER_PORT
    live_logs = LoginItemPaths(_app(tmp_path / "Live.app", build="1", code=b""), tmp_path).log_dir
    assert not paths.stdout_log.is_relative_to(live_logs) and not paths.stderr_log.is_relative_to(live_logs)


def test_proof_build_gate_refuses_an_app_that_shares_the_live_identity(tmp_path: Path) -> None:
    from scripts.bundle_server import require_proof_identity

    live = _app(tmp_path / "Live.app", build="1", code=b"", CFBundleIdentifier="com.shinycomputers.mediaforce.proof")
    with pytest.raises(ValueError):
        require_proof_identity(live)
    proof = _app(tmp_path / "Proof.app", build="1", code=b"", CFBundleIdentifier="com.shinycomputers.mediaforce.proof",
                 MediaforceServiceLabel="com.mediaforce.web.proof", MediaforceServerPort="8787")
    require_proof_identity(proof)
