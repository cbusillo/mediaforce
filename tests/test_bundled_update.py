from __future__ import annotations

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
from mediaforce.ops import bundled_update
from mediaforce.ops.login_item import LoginItemPaths, render_login_item_plist
from mediaforce.ops.runtime_install import retain_deploy_stage, retained_deploy_stage
from mediaforce.web.runtime.queue_actions import (
    fence_for_update_action, release_update_fence_action, resume_encode_queue_action,
)


class FakeServer:
    """The running server as the update sees it: its API, its pause switches and launchd."""

    def __init__(self, database: Path) -> None:
        self.database = database
        self.running = {"encode": 0, "sample": 0, "scan": 0, "evidence": 0}
        self.healthy = True
        self.launchctl: list[list[str]] = []

    def api(self, _base: str, route: str, *, post: bool = False) -> dict[str, Any]:
        _ = post
        if route.startswith("/api/dashboard?"):
            return {"encode_queue": {"running_count": self.running["encode"]},
                    "calibration_queue": {"sample": {"running_count": self.running["sample"]}, "full": {"running_count": 0}}}
        if route == "/api/release/work":
            return {"counts": {"pending_encode": 4, "unfinished_scan_rows": self.running["scan"],
                               "active_evidence": self.running["evidence"]}}
        if not self.healthy:
            raise OSError("connection refused")
        return {}

    def post(self, _context: object, route: str, body: dict[str, Any]) -> dict[str, Any]:
        def connection() -> Any:
            return open_db(self.database)
        if route == "/api/update/fence":
            return fence_for_update_action(connection_factory=connection, now_iso=lambda: "2026-11-12T00:00:00+00:00")
        return release_update_fence_action(body, connection_factory=connection, now_iso=lambda: "2026-11-12T00:00:00+00:00")

    def run(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.launchctl.append(command[1:])
        return subprocess.CompletedProcess(command, 1 if command[1] == "print" else 0, "", "")


Update = tuple[bundled_update.UpdateContext, FakeServer]


@pytest.fixture
def update(tmp_path: Path) -> Iterator[Update]:
    support = tmp_path / "support"
    config = tmp_path / "config.toml"
    config.write_text(
        f'[state]\ndb_path = "{support}/library.sqlite3"\nrun_manifest_dir = "{support}/runs"\n'
        f'web_state_dir = "{support}/web"\n[media]\nsource_roots = {{}}\n'
    )
    support.mkdir()
    database = support / "library.sqlite3"
    with open_db(database) as connection:
        save_queue_state(connection, {**load_queue_state(connection), "is_paused": False, "updated_at": "then"})
    app = _app(tmp_path / "Applications/Mediaforce.app", build="1", code=b"old app")
    home = tmp_path / "home"
    paths = LoginItemPaths(app, home, config)
    paths.plist.parent.mkdir(parents=True)
    paths.plist.write_bytes(render_login_item_plist(paths))
    server = FakeServer(database)
    context = bundled_update.UpdateContext(config, app, home, "http://127.0.0.1:1", 1)
    with patch.object(bundled_update, "_api", server.api), patch.object(bundled_update, "_post", server.post), \
            patch.object(bundled_update, "_verify_signed_bundle"), patch.object(bundled_update, "_listener_is_closed", return_value=True), \
            patch.object(bundled_update, "_copy_app", lambda source, target: shutil.copytree(source, target, symlinks=True)), \
            patch.object(bundled_update, "require_storage_admission"), patch.object(bundled_update.time, "sleep"), \
            patch("mediaforce.ops.login_item._require_supported_platform"), \
            patch("mediaforce.ops.controller_deploy.subprocess.run", return_value=subprocess.CompletedProcess([], 1, b"", b"")):
        yield context, server


def _app(path: Path, *, build: str, code: bytes) -> Path:
    (path / "Contents/MacOS").mkdir(parents=True)
    (path / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleVersion": build}))
    (path / "Contents/MacOS/Mediaforce").write_bytes(code)
    server = path / "Contents/MacOS/mediaforce-server"
    server.write_text("#!/bin/sh\n")
    server.chmod(0o755)
    return path


def _replace_app(context: bundled_update.UpdateContext, *, build: str) -> None:
    """What Sparkle's installer does: the same path now holds the new build."""
    shutil.rmtree(context.app)
    _app(context.app, build=build, code=b"new app")


def _phase(context: bundled_update.UpdateContext) -> str | None:
    intent = bundled_update.read_update_intent(context.support_dir)
    return None if intent is None else str(intent["phase"])


def _paused(database: Path) -> tuple[bool, bool]:
    with open_db(database) as connection:
        return bool(load_queue_state(connection)["is_paused"]), background_work_is_paused(connection)


def test_busy_server_keeps_running_with_work_fenced_until_files_finish(update: Update) -> None:
    context, server = update
    server.running.update(encode=1, sample=1)
    intent = bundled_update.prepare(context, "2", runner=server.run)
    assert intent["phase"] == "fenced"
    assert intent["waiting"] == ["1 files are compressing", "1 samples are running"]
    assert server.launchctl == []
    assert _paused(server.database) == (True, True)
    database = load_config(context.config_path)
    assert resume_encode_queue_action(connection_factory=lambda: open_db(server.database), config=database,
                                      now_iso=lambda: "now")["ok"] is False
    with pytest.raises(bundled_update.UpdateRefused, match="build 2 is unfinished"):
        bundled_update.prepare(context, "3", runner=server.run)
    bundled_update.cancel(context, runner=server.run)
    assert _paused(server.database) == (False, False)
    assert bundled_update.read_update_intent(context.support_dir) is None


def test_idle_server_saves_complete_old_pair_before_sparkle_may_install(update: Update) -> None:
    context, server = update
    previous = context.support_dir / "deployments/earlier"
    retain_deploy_stage(context.support_dir, previous)
    intent = bundled_update.prepare(context, "2", runner=server.run)
    stage = Path(intent["stage"])
    assert intent["phase"] == "prepared" and intent["previous_stage"] == str(previous)
    assert (stage / "Mediaforce.app/Contents/MacOS/Mediaforce").read_bytes() == b"old app"
    assert (stage / "launch-item.before.plist").read_bytes() == context.plist.read_bytes()
    with sqlite3.connect(stage / "before.sqlite") as saved:
        assert saved.execute("pragma quick_check").fetchall() == [("ok",)]
    assert retained_deploy_stage(context.support_dir) == stage
    assert server.launchctl[-1][0] == "disable"
    assert bundled_update.prepare(context, "2", runner=server.run)["stage"] == str(stage)
    with pytest.raises(bundled_update.UpdateRefused):
        bundled_update.require_startup_allowed(context.support_dir, context.app)


def test_backup_failure_restarts_unchanged_old_service_and_keeps_last_pair(update: Update) -> None:
    context, server = update
    previous = context.support_dir / "deployments/earlier"
    retain_deploy_stage(context.support_dir, previous)
    saved_plist = context.plist.read_bytes()
    with patch.object(bundled_update, "backup_database", side_effect=RuntimeError("disk full")), \
            pytest.raises(RuntimeError, match="disk full"):
        bundled_update.prepare(context, "2", runner=server.run)
    assert retained_deploy_stage(context.support_dir) == previous
    assert context.plist.read_bytes() == saved_plist
    assert server.launchctl[-1][0] == "kickstart"
    assert _paused(server.database) == (False, False)
    assert bundled_update.read_update_intent(context.support_dir) is None


def test_failed_migration_stops_service_and_rollback_restores_matching_old_pair(update: Update) -> None:
    context, server = update
    bundled_update.prepare(context, "2", runner=server.run)
    saved_plist = context.plist.read_bytes()
    _replace_app(context, build="2")
    server.healthy = False
    with sqlite3.connect(server.database) as migrated:
        migrated.execute("create table candidate_migration (id integer)")
    with pytest.raises(bundled_update.UpdateRefused, match="did not answer"):
        bundled_update.finish(context, runner=server.run, startup_timeout=0)
    assert server.launchctl[-1][0] == "disable"
    assert _phase(context) == "failed"
    with pytest.raises(bundled_update.UpdateRefused):
        bundled_update.require_startup_allowed(context.support_dir, context.app)

    server.healthy = True
    bundled_update.rollback(context, runner=server.run)
    assert (context.app / "Contents/MacOS/Mediaforce").read_bytes() == b"old app"
    assert context.plist.read_bytes() == saved_plist
    with sqlite3.connect(server.database) as restored:
        assert not restored.execute("select name from sqlite_master where name = 'candidate_migration'").fetchall()
    stage = retained_deploy_stage(context.support_dir)
    assert stage is not None and (stage / "failed/library.sqlite3").exists()
    assert (stage / "failed/Mediaforce.app/Contents/MacOS/Mediaforce").read_bytes() == b"new app"
    assert _paused(server.database) == (False, False)
    assert bundled_update.read_update_intent(context.support_dir) is None
    with pytest.raises(bundled_update.UpdateRefused, match="failed before"):
        bundled_update.prepare(context, "2", runner=server.run)


def test_verified_candidate_records_pair_and_resumes_only_a_running_queue(update: Update) -> None:
    context, server = update
    with open_db(server.database) as connection:
        save_queue_state(connection, {**load_queue_state(connection), "is_paused": True, "updated_at": "then"})
    stage = Path(bundled_update.prepare(context, "2", runner=server.run)["stage"])
    _replace_app(context, build="2")
    bundled_update.finish(context, runner=server.run)
    assert json.loads((stage / "candidate.txt").read_text())["build"] == "2"
    assert plistlib.loads(context.plist.read_bytes())["ProgramArguments"][0] == str(context.app / "Contents/MacOS/mediaforce-server")
    assert _paused(server.database) == (True, False)
    assert bundled_update.read_update_intent(context.support_dir) is None


def test_cancel_refuses_once_the_app_was_replaced(update: Update) -> None:
    context, server = update
    bundled_update.prepare(context, "2", runner=server.run)
    _replace_app(context, build="2")
    with pytest.raises(bundled_update.UpdateRefused, match="already replaced"):
        bundled_update.cancel(context, runner=server.run)
    assert _phase(context) == "prepared"


def test_unfinished_update_holds_cleanup(update: Update) -> None:
    from mediaforce.state_cleanup import purge_transient_artifacts

    context, server = update
    server.running["encode"] = 1
    bundled_update.prepare(context, "2", runner=server.run)
    with patch("mediaforce.state_cleanup.controller_work_allowed", return_value=True), \
            patch("mediaforce.state_cleanup._purge_due_transient_artifacts") as purge:
        assert purge_transient_artifacts(load_config(context.config_path), force=True) is False
    purge.assert_not_called()
