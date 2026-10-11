import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from sqlalchemy import select, update
from starlette.types import Message, Scope

from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db import DBClient, open_db
from mediaforce.core.db_tables import calibration_jobs, encode_jobs, library_item_evidence_state, library_items, scan_runs
from mediaforce.web.runtime.release_work import ReleaseWorkCounts, ReleaseWorkSnapshot, release_work_payload, \
    release_work_snapshot
from mediaforce.web.routes.releases import register_release_routes
from mediaforce.web import app as web_app
from mediaforce.web.runtime import release_work


def add_encode(connection: DBClient, job_id: str, status: str, *, kind: str = "single", parent: str | None = None) -> None:
    connection.execute(encode_jobs.insert().values(
        job_id=job_id, prefix="tv/show", status=status, job_kind=kind, parent_job_id=parent,
        manifest_path="manifest.json", host_json="{}", created_at="2026-10-09T00:00:00Z",
        updated_at="2026-10-09T00:00:00Z",
    ))


@pytest.mark.parametrize("child_status", ["queued", "retry_backoff", "running"])
def test_folder_attention_does_not_hide_work(tmp_path: Path, child_status: str) -> None:
    with open_db(tmp_path / "library.sqlite3") as connection:
        add_encode(connection, "parent", "needs_attention", kind="folder")
        add_encode(connection, "child", child_status, kind="shard", parent="parent")
        before = connection.execute(select(encode_jobs)).mappings().all()
        snapshot = release_work_snapshot(connection)
        assert snapshot["counts"]["active_encode"] == 1
        assert snapshot["counts"]["pending_encode"] == (0 if child_status == "running" else 1)
        assert connection.execute(select(encode_jobs)).mappings().all() == before


@pytest.mark.parametrize("status", ["queued", "starting", "running"])
def test_sample_or_full_work_is_counted(tmp_path: Path, status: str) -> None:
    with open_db(tmp_path / "library.sqlite3") as connection:
        connection.execute(calibration_jobs.insert().values(
            job_id="sample", prefix="tv/show", status=status, lane="sample", action="sample",
            host_json="{}", policy_json="{}", sample_item_json="{}",
            created_at="2026-10-09T00:00:00Z", updated_at="2026-10-09T00:00:00Z",
        ))
        assert release_work_snapshot(connection)["counts"]["active_calibration"] == 1


def test_running_catalog_scan_is_counted(tmp_path: Path) -> None:
    with open_db(tmp_path / "library.sqlite3") as connection:
        connection.execute(scan_runs.insert().values(
            scan_id="scan", status="running", started_at="2026-10-09T00:00:00Z", roots_json="[]",
        ))
        assert release_work_snapshot(connection)["counts"]["unfinished_scan_rows"] == 1


@pytest.mark.parametrize("status", ["queued", "running", "retry_wait", "waiting_source", "waiting_host"])
def test_deferred_evidence_is_counted(tmp_path: Path, status: str) -> None:
    with open_db(tmp_path / "library.sqlite3") as connection:
        result = connection.execute(library_items.insert().values(
            source_path="/fixture/file.mkv", rel_path="show/file.mkv", media_root="/fixture",
            parent_dir="show", file_name="file.mkv", container="mkv", fingerprint="fixture",
            audio_summary_json="[]", subtitle_summary_json="[]", last_scan_id="fixture",
            discovered_at="2026-10-09T00:00:00Z", last_seen_at="2026-10-09T00:00:00Z",
            size_bytes=100, mtime_ns=1, updated_at="2026-10-09T00:00:00Z",
        ))
        connection.execute(library_item_evidence_state.insert().values(
            library_item_id=result.inserted_primary_key[0], evidence_kind="cadence", state="missing",
            policy_hash="fixture", work_status=status, updated_at="2026-10-09T00:00:00Z",
        ))
        assert release_work_snapshot(connection)["counts"]["active_evidence"] == 1


def test_finished_and_attention_only_jobs_have_no_counted_work(tmp_path: Path) -> None:
    with open_db(tmp_path / "library.sqlite3") as connection:
        for status in ("completed", "failed", "stopped", "needs_attention"):
            add_encode(connection, status, status)
        snapshot = release_work_snapshot(connection)
        assert not any(snapshot["counts"].values())
        assert snapshot["observed_at"]


def test_work_route_returns_uncached_observations() -> None:
    app = FastAPI()
    counts: ReleaseWorkCounts = {
        "pending_encode": 0, "active_encode": 0, "active_calibration": 0, "unfinished_scan_rows": 0, "active_evidence": 0,
    }
    snapshots: list[ReleaseWorkSnapshot] = [
        {"observed_at": "first", "counts": counts},
        {"observed_at": "second", "counts": {**counts, "active_encode": 1}},
    ]
    observations = iter(snapshots)
    register_release_routes(app, work_snapshot=lambda: next(observations))
    route = next(route for route in app.routes if isinstance(route, APIRoute) and route.path == "/api/release/work")
    first = route.endpoint()
    second = route.endpoint()
    assert json.loads(first.body) == snapshots[0]
    assert json.loads(second.body) == snapshots[1]
    assert second.headers["cache-control"] == "no-store"


def test_work_payload_does_not_create_a_missing_database(tmp_path: Path) -> None:
    database = tmp_path / "missing.sqlite3"
    with pytest.raises(FileNotFoundError):
        release_work_payload(database)
    assert not database.exists()


def test_work_payload_reads_committed_work_without_writing(tmp_path: Path) -> None:
    database = tmp_path / "library.sqlite3"
    with open_db(database) as connection:
        add_encode(connection, "busy", "queued")
    before = database.read_bytes()
    snapshot = release_work_payload(database)
    assert snapshot["counts"]["pending_encode"] == 1
    assert database.read_bytes() == before


async def get_work_over_http(app: FastAPI, messages: list[Message]) -> None:
    scope: Scope = {
        "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": "/api/release/work", "raw_path": b"/api/release/work", "query_string": b"",
        "root_path": "", "headers": [], "server": ("fixture", 80), "client": ("fixture", 1),
    }

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        messages.append(message)

    await app(scope, receive, send)


def test_real_app_registers_readonly_http_evidence_and_fails_without_database(tmp_path: Path) -> None:
    config = MediaforceConfig(raw={}, paths=ConfigPaths(
        project_root=tmp_path, config_path=tmp_path / "config.toml", db_path=tmp_path / "library.sqlite3",
        run_manifest_dir=tmp_path / "runs", web_state_dir=tmp_path / "web", review_dir=tmp_path / "review",
        runtime_settings_path=tmp_path / "settings.json",
    ))
    with patch.dict(vars(web_app), load_config=lambda _path: config):
        app = web_app.create_app(config.paths.config_path)
    with open_db(config.paths.db_path) as connection:
        add_encode(connection, "busy", "queued")
    messages: list[Message] = []
    asyncio.run(get_work_over_http(app, messages))
    assert messages[0]["status"] == 200
    assert (b"cache-control", b"no-store") in messages[0]["headers"]
    body = json.loads(b"".join(message.get("body", b"") for message in messages))
    assert body["counts"]["pending_encode"] == 1
    assert "idle" not in body
    assert "database_idle" not in body
    config.paths.db_path.unlink()
    failed: list[Message] = []
    with pytest.raises(FileNotFoundError):
        asyncio.run(get_work_over_http(app, failed))
    assert failed[0]["status"] == 500
    assert not config.paths.db_path.exists()


def test_counts_share_one_snapshot_during_work_handoff(tmp_path: Path) -> None:
    database = tmp_path / "library.sqlite3"
    with open_db(database) as sample_connection:
        sample_connection.execute(calibration_jobs.insert().values(
            job_id="sample", prefix="tv/show", status="running", lane="sample", action="sample",
            host_json="{}", policy_json="{}", sample_item_json="{}",
            created_at="2026-10-09T00:00:00Z", updated_at="2026-10-09T00:00:00Z",
        ))
    original_counter = release_work.count_pending_encode_work

    def handoff_after_first_read(connection: DBClient) -> int:
        count = original_counter(connection)
        with open_db(database) as producer:
            producer.execute(update(calibration_jobs).values(status="completed"))
            add_encode(producer, "next", "queued")
        return count

    with patch.dict(vars(release_work), count_pending_encode_work=handoff_after_first_read):
        snapshot = release_work_payload(database)
    assert snapshot["counts"]["active_calibration"] == 1
    assert snapshot["counts"]["active_encode"] == 0
    assert snapshot["counts"]["pending_encode"] == 0
