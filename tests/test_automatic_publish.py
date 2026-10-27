import json
import threading
from dataclasses import replace
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NoReturn
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import select

from mediaforce import cli
from mediaforce.web.runtime.archive_cleanup import clear_archive_cleanup_action
from mediaforce.web.runtime.episode_progress import folder_episodes_payload
from mediaforce.web.runtime.completed_runtime import list_completed_folders, clear_completed_backups_action
from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db import DBClient, open_db, reset_engine_cache
from mediaforce.core.db_tables import encode_jobs, item_events, library_items, motion_keep_decisions, staged_artifacts
from mediaforce.core.models import ProbeSummary
from mediaforce.core.utils import file_fingerprint, timestamp
from mediaforce.encoding.delivery_lock import delivery_lock
from mediaforce.core.type_defs import object_dict
from mediaforce.encoding.staging import FINAL_SIZE_GOAL_CHECK, PromotionRestoreError, PromotionWaiting
from mediaforce.library.motion_keep_state import library_item_ids_kept_original
from mediaforce.library.staged_integrity import staged_integrity_report
from mediaforce.library.workflow_state import build_folder_workflow_state
from mediaforce.web import app as web_app
from mediaforce.web.runtime import automatic_publish as delivery
from mediaforce.web.runtime import size_held
from mediaforce.web.runtime.folder_tuning_advice import calibration_policy_hash
from mediaforce.web.runtime.production_approval import production_approval_contract


@pytest.fixture
def config(tmp_path: Path) -> Iterator[MediaforceConfig]:
    result = MediaforceConfig(
        raw={
            "validation": {},
            "media": {
                "source_roots": {key: str(tmp_path / "source" / key) for key in ("tv", "movies", "other")},
                "staging_root": str(tmp_path / "staging"),
                "archive_root": str(tmp_path / "archive"),
                "output_container": "mkv",
            },
        },
        paths=ConfigPaths(
            project_root=tmp_path,
            config_path=tmp_path / "config.toml",
            db_path=tmp_path / "state/library.sqlite3",
            run_manifest_dir=tmp_path / "runs",
            web_state_dir=tmp_path / "web",
            review_dir=tmp_path / "review",
            runtime_settings_path=tmp_path / "runtime.json",
            runtime_reservation_dir=tmp_path / "reservations",
        ),
    )
    yield result
    reset_engine_cache()


def _approval() -> dict[str, Any]:
    state = {
        "mode": "sample",
        "policy": {"video": {"preset": 6}},
        "job_id": "sample",
        "accepted_sample_job_id": "sample",
        "accepted_at": timestamp(),
    }
    state["accepted_policy_hash"] = calibration_policy_hash(state)
    return state


def _stage(
    config: MediaforceConfig, rel_path: str, *, checked: bool = False, origin: str = "queue"
) -> tuple[int, dict[str, Any], Path]:
    source = config.source_root_map[rel_path.split("/")[0]] / Path(*Path(rel_path).parts[1:])
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"original media file")
    staged = Path(config.media["staging_root"]) / rel_path
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"av1")
    fingerprint = file_fingerprint(source, source.stat(), 60.0)
    now = timestamp()
    with open_db(config.paths.db_path) as connection:
        item_id = int(
            connection.execute(
                library_items.insert().values(
                    source_path=str(source),
                    rel_path=rel_path,
                    media_root=Path(rel_path).parts[0],
                    parent_dir=Path(rel_path).parent.as_posix(),
                    file_name=source.name,
                    container=".mkv",
                    size_bytes=source.stat().st_size,
                    mtime_ns=source.stat().st_mtime_ns,
                    fingerprint=fingerprint,
                    duration_seconds=60.0,
                    audio_summary_json="[]",
                    subtitle_summary_json="[]",
                    last_scan_id="fixture",
                    discovered_at=now,
                    last_seen_at=now,
                    updated_at=now,
                    status="validated" if checked else "encoded",
                )
            ).inserted_primary_key[0]
        )
        item = {
            "library_item_id": item_id,
            "source_path": str(source),
            "source_fingerprint": fingerprint,
            "source_size_bytes": source.stat().st_size,
            "duration_seconds": 60.0,
            "rel_path": rel_path,
            "media_root": Path(rel_path).parts[0],
            "subtitle_summary": [],
            "resolved_policy": _approval()["policy"],
        }
        manifest_path = config.paths.project_root / f"manifest-{item_id}.json"
        manifest_path.write_text(json.dumps({"run_id": f"run-{item_id}", "items": [item]}))
        connection.execute(
            staged_artifacts.insert().values(
                library_item_id=item_id,
                manifest_run_id=f"run-{item_id}",
                manifest_path=str(manifest_path),
                item_index=0,
                encode_origin=origin,
                staging_path=str(staged),
                staging_size_bytes=staged.stat().st_size,
                staging_mtime_ns=staged.stat().st_mtime_ns,
                source_fingerprint=fingerprint,
                validation_json=json.dumps({"passed": True}) if checked else "{}",
                validated_at=now if checked else None,
                updated_at=now,
            )
        )
        connection.commit()
    return item_id, item, staged


def _probe() -> ProbeSummary:
    return ProbeSummary(
        duration_seconds=60.0,
        video_codec="av1",
        video_bitrate=100,
        width=1920,
        height=1080,
        pix_fmt="yuv420p10le",
        audio_track_count=1,
        subtitle_track_count=0,
        english_audio_count=1,
        english_subtitle_count=0,
        default_audio_language="eng",
        default_subtitle_language=None,
        audio_summary_json="[]",
        subtitle_summary_json="[]",
    )


def _no_remake(_prefix: str, _ids: Any) -> NoReturn:
    raise AssertionError("only a copy that failed a check other than its size goal is made again")


def _remake_refused(_prefix: str, _ids: Any) -> dict[str, Any]:
    return {"ok": False, "message": "Mediaforce is still compressing here."}


def _run(
    config: MediaforceConfig,
    approval: dict[str, Any] | None = None,
    *,
    remake_files: delivery.RemakeFilesFn = _no_remake,
) -> None:
    state = approval if approval is not None else _approval()
    with patch("mediaforce.execution.probe_media", return_value=_probe()):
        delivery.publish_checked_files_once(
            config, load_calibration_state=lambda _config, _prefix: state, remake_files=remake_files
        )


def _status(config: MediaforceConfig, item_id: int) -> str:
    with open_db(config.paths.db_path) as connection:
        return str(connection.execute(select(library_items.c.status).where(library_items.c.id == item_id)).scalar_one())


@pytest.mark.parametrize("root", ["tv/Show/Season 1", "movies/Title", "other/Folder"])
@pytest.mark.parametrize("origin", ["queue", "cli-production"])
def test_checks_and_publishes_each_file_without_manual_action(config: MediaforceConfig, root: str, origin: str) -> None:
    item_id, item, staged = _stage(config, f"{root}/one.mkv", origin=origin)
    _run(config)
    assert _status(config, item_id) == "promoted"
    assert Path(item["source_path"]).read_bytes() == b"av1"
    assert (config.archive_root / item["rel_path"]).read_bytes() == b"original media file"
    assert not staged.exists()
    _run(config)
    assert (config.archive_root / item["rel_path"]).read_bytes() == b"original media file"


def test_ready_file_publishes_beside_running_and_failed_siblings(config: MediaforceConfig) -> None:
    good_id, good, _ = _stage(config, "tv/Show/Season 1/good.mkv")
    failed_id, _, failed_stage = _stage(config, "tv/Show/Season 1/failed.mkv")
    busy_id, busy, _ = _stage(config, "tv/Show/Season 1/busy.mkv")
    with open_db(config.paths.db_path) as connection:
        failed_stage.write_bytes(b"drifted")
        for job_id, kind, item in [("parent", "folder", good), ("child", "shard", busy)]:
            manifest_path = config.paths.project_root / f"active-{job_id}.json"
            manifest_path.write_text(json.dumps({"items": [item]}))
            connection.execute(
                encode_jobs.insert().values(
                    job_id=job_id,
                    job_kind=kind,
                    status="running",
                    prefix="tv/Show/Season 1",
                    manifest_path=str(manifest_path),
                    manifest_indexes_json="[0]",
                    host_json="{}",
                    item_count=1,
                    created_at=timestamp(),
                    updated_at=timestamp(),
                )
            )
        connection.commit()
    _run(config)
    assert _status(config, good_id) == "promoted"
    assert _status(config, failed_id) == "encoded"
    assert _status(config, busy_id) == "encoded"


@pytest.mark.parametrize(
    "problem",
    [
        "approval_changed",
        "approval_revoked",
        "original_changed",
        "validation_failed",
        "calibration",
        "invalid_contract",
        "unknown_origin",
    ],
)
def test_unapproved_or_failed_files_are_not_published(config: MediaforceConfig, problem: str) -> None:
    item_id, item, _ = _stage(
        config, "tv/Show/Season 1/one.mkv", origin="calibration" if problem == "calibration" else "queue"
    )
    approval = _approval()
    if problem == "approval_changed":
        approval["policy"] = {"video": {"preset": 5}}
        approval["accepted_policy_hash"] = calibration_policy_hash(approval)
    if problem == "approval_revoked":
        approval.pop("accepted_at")
    if problem == "original_changed":
        Path(item["source_path"]).write_bytes(b"new original")
    if problem == "invalid_contract":
        path = config.paths.project_root / f"manifest-{item_id}.json"
        manifest = json.loads(path.read_text())
        manifest["selection"] = {"production_approval_contract": {"policy_hash": approval["accepted_policy_hash"]}}
        path.write_text(json.dumps(manifest))
    with open_db(config.paths.db_path) as connection:
        if problem == "validation_failed":
            connection.execute(
                staged_artifacts.update()
                .where(staged_artifacts.c.library_item_id == item_id)
                .values(validation_json='{"passed":false}')
            )
        if problem == "unknown_origin":
            connection.execute(
                staged_artifacts.update()
                .where(staged_artifacts.c.library_item_id == item_id)
                .values(encode_origin=None)
            )
        connection.commit()
    _run(config, approval, remake_files=_remake_refused)
    assert _status(config, item_id) == "encoded"
    assert not (config.archive_root / item["rel_path"]).exists()


def test_temporary_publish_failure_retries_without_holding_siblings(config: MediaforceConfig) -> None:
    waiting_id, _, _ = _stage(config, "tv/Show/Season 1/waiting.mkv", checked=True)
    ready_id, _, _ = _stage(config, "tv/Show/Season 1/ready.mkv", checked=True)
    promote = delivery.promote_one_item

    def sometimes_wait(connection: DBClient, config: MediaforceConfig, item: dict[str, Any], *, force: bool) -> Path:
        if item["library_item_id"] == waiting_id:
            raise PromotionWaiting("Waiting for enough free space.")
        return promote(connection, config, item, force=force)

    with patch.object(delivery, "promote_one_item", side_effect=sometimes_wait):
        _run(config)
        _run(config)
    assert _status(config, waiting_id) == "validated"
    assert _status(config, ready_id) == "promoted"
    with open_db(config.paths.db_path) as connection:
        report = staged_integrity_report(connection, config, "tv/Show/Season 1", discover=False)
        assert "Waiting for enough free space" in next(r.detail for r in report.records if r.item_id == waiting_id)
        events = connection.execute(
            select(item_events).where(
                item_events.c.library_item_id == waiting_id, item_events.c.event_type == "automatic_publish_waiting"
            )
        ).all()
        assert len(events) == 1
    with patch.object(delivery.time, "time", return_value=delivery.time.time() + delivery.PUBLISH_RETRY_DELAY_SECONDS + 1):
        _run(config)
    assert _status(config, waiting_id) == "promoted"


def test_validation_failure_keeps_original_and_other_file_publishes(config: MediaforceConfig) -> None:
    failed_id, _, _ = _stage(config, "tv/Show/Season 1/failed.mkv")
    ready_id, _, _ = _stage(config, "tv/Show/Season 1/ready.mkv")
    validate = delivery.validate_one_item

    def failed_check(connection: DBClient, config: MediaforceConfig, item: dict[str, Any]) -> dict[str, Any]:
        if item["library_item_id"] == failed_id:
            with patch("mediaforce.execution.probe_media", return_value=replace(_probe(), video_codec="h264")):
                return validate(connection, config, item)
        return validate(connection, config, item)

    with patch.object(delivery, "validate_one_item", side_effect=failed_check):
        _run(config, remake_files=_remake_refused)
    assert _status(config, failed_id) == "encoded"
    assert _status(config, ready_id) == "promoted"


def test_retains_preexisting_rollback_copy(config: MediaforceConfig) -> None:
    _, item, _ = _stage(config, "tv/Show/Season 1/one.mkv")
    archive = config.archive_root / item["rel_path"]
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(b"earlier rollback")
    _run(config)
    assert archive.read_bytes() == b"original media file"
    assert any(path.read_bytes() == b"earlier rollback" for path in archive.parent.iterdir() if path != archive)


def test_unsafe_restore_is_recorded_and_not_retried(config: MediaforceConfig) -> None:
    item_id, _, _ = _stage(config, "tv/Show/Season 1/one.mkv", checked=True)
    with patch.object(delivery, "promote_one_item", side_effect=PromotionRestoreError("restore failed")) as promote:
        _run(config)
        _run(config)
    assert promote.call_count == 1
    with open_db(config.paths.db_path) as connection:
        report = staged_integrity_report(connection, config, "tv/Show/Season 1", discover=False)
        assert "could not put the files back" in report.records[0].detail
    assert _status(config, item_id) == "validated"


def test_worker_skips_locked_file_and_publishes_another(config: MediaforceConfig) -> None:
    item_id, _, _ = _stage(config, "tv/Show/Season 1/locked.mkv")
    ready_id, _, _ = _stage(config, "tv/Show/Season 1/ready.mkv")
    started, release = threading.Event(), threading.Event()

    def hold() -> None:
        with delivery_lock(config.paths.db_path, item_id):
            started.set()
            release.wait(5)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert started.wait(5)
        _run(config)
        assert _status(config, item_id) == "encoded"
        assert _status(config, ready_id) == "promoted"
    finally:
        release.set()
        thread.join()


def test_unreadable_check_record_does_not_stop_other_files(config: MediaforceConfig) -> None:
    failed_id, _, _ = _stage(config, "tv/Show/Season 1/broken.mkv")
    ready_id, _, _ = _stage(config, "tv/Show/Season 1/ready.mkv")
    with open_db(config.paths.db_path) as connection:
        connection.execute(
            staged_artifacts.update()
            .where(staged_artifacts.c.library_item_id == failed_id)
            .values(validation_json="broken-json")
        )
        connection.commit()
    _run(config)
    assert _status(config, ready_id) == "promoted"
    assert _status(config, failed_id) == "encoded"
    with open_db(config.paths.db_path) as connection:
        row = connection.execute(
            select(staged_artifacts.c.validation_json).where(staged_artifacts.c.library_item_id == failed_id)
        ).scalar_one()
        assert json.loads(row)["unreadable_validation_json"] == "broken-json"


def test_manual_delivery_after_automatic_delivery_preserves_original(config: MediaforceConfig) -> None:
    _, item, _ = _stage(config, "tv/Show/Season 1/one.mkv")
    _run(config)
    with open_db(config.paths.db_path) as connection:
        path = delivery.promote_one_item(connection, config, item, force=False)
        checked = delivery.validate_one_item(connection, config, item)
    assert checked["passed"]
    assert path.read_bytes() == b"av1"
    assert (config.archive_root / item["rel_path"]).read_bytes() == b"original media file"


def test_approval_revoked_during_validation_prevents_publishing(config: MediaforceConfig) -> None:
    item_id, item, _ = _stage(config, "tv/Show/Season 1/one.mkv")
    approval = _approval()
    validate = delivery.validate_one_item

    def revoke(connection: DBClient, config: MediaforceConfig, item: dict[str, Any]) -> dict[str, Any]:
        result = validate(connection, config, item)
        approval.pop("accepted_at")
        return result

    with patch.object(delivery, "validate_one_item", side_effect=revoke):
        _run(config, approval)
    assert _status(config, item_id) == "validated"
    assert Path(item["source_path"]).read_bytes() == b"original media file"
    assert not (config.archive_root / item["rel_path"]).exists()


def test_app_supervises_automatic_delivery_with_its_leadership(config: MediaforceConfig) -> None:
    sweep = Mock()
    with (
        patch.object(web_app, "_acquire_background_worker_leadership", return_value=Mock()),
        patch.object(web_app, "_start_calibration_queue_worker"),
        patch.object(web_app, "_start_encode_queue_worker"),
        patch.object(web_app, "_start_controller_storage_worker"),
        patch.object(web_app, "_start_host_recovery_worker"),
        patch.object(web_app, "_start_catalog_refresh_worker"),
        patch.object(web_app, "_start_supervised_worker") as start,
    ):
        runtime = web_app._start_background_workers(config, automatic_publish_sweep=sweep)
    assert runtime is not None
    start.call_args.kwargs["process_once_fn"]()
    assert sweep.call_args.args == (start.call_args.kwargs["stop_event"],)


@pytest.mark.parametrize("problem", ["failed_check", "drifted", "missing"])
def test_repeated_integrity_hold_keeps_one_bounded_reason(
    config: MediaforceConfig, problem: str
) -> None:
    item_id, _, staged = _stage(config, "tv/Show/Season 1/held.mkv")
    if problem == "drifted":
        staged.write_bytes(b"changed output")
    elif problem == "missing":
        staged.unlink()
    else:
        with open_db(config.paths.db_path) as connection:
            connection.execute(
                staged_artifacts.update()
                .where(staged_artifacts.c.library_item_id == item_id)
                .values(validation_json='{"passed":false}')
            )
            connection.commit()
    for attempt in range(5):
        with patch.object(delivery.time, "time", return_value=attempt * (delivery.PUBLISH_RETRY_DELAY_SECONDS + 1)):
            _run(config, remake_files=_remake_refused)
    with open_db(config.paths.db_path) as connection:
        events = (
            connection.execute(
                select(item_events.c.details_json).where(
                    item_events.c.library_item_id == item_id,
                    item_events.c.event_type == "automatic_publish_waiting",
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        reason = json.loads(events[0])["reason"]
        report = staged_integrity_report(
            connection, config, "tv/Show/Season 1", discover=False
        )
        assert report.records[0].detail.count(reason) == 1


@pytest.mark.parametrize("intermittent_storage_wait", [False, True])
def test_unknown_failure_retries_then_holds_without_stopping_siblings(
    config: MediaforceConfig, intermittent_storage_wait: bool,
) -> None:
    failed_id, failed_item, _ = _stage(
        config, "tv/Show/Season 1/failed.mkv", checked=True
    )
    ready_id, _, _ = _stage(config, "tv/Show/Season 1/ready.mkv", checked=True)
    promote = delivery.promote_one_item
    failed_calls = 0

    def fail(
        connection: DBClient,
        config: MediaforceConfig,
        item: dict[str, Any],
        *,
        force: bool,
    ) -> Path:
        nonlocal failed_calls
        if item["library_item_id"] == failed_id:
            failed_calls += 1
            if intermittent_storage_wait and failed_calls % 2 == 0:
                raise ConnectionError("storage unavailable")
            raise RuntimeError("unclassified failure")
        return promote(connection, config, item, force=force)

    with patch.object(delivery, "promote_one_item", side_effect=fail) as attempts:
        for _ in range(delivery.UNKNOWN_FAILURE_RETRY_LIMIT * 2 + 2):
            with patch.object(delivery.time, "time", return_value=_ * (delivery.PUBLISH_RETRY_DELAY_SECONDS * delivery.UNKNOWN_FAILURE_RETRY_LIMIT + 1)):
                _run(config)
    expected_failures = delivery.UNKNOWN_FAILURE_RETRY_LIMIT * 2 - 1 if intermittent_storage_wait else delivery.UNKNOWN_FAILURE_RETRY_LIMIT
    assert attempts.call_count == expected_failures + 1
    assert _status(config, ready_id) == "promoted"
    with open_db(config.paths.db_path) as connection:
        saved = json.loads(
            connection.execute(
                select(staged_artifacts.c.validation_json).where(
                    staged_artifacts.c.library_item_id == failed_id
                )
            ).scalar_one()
        )
        assert saved["automatic_publish"]["state"] == "failed"
    # A deliberate new check clears the terminal hold and supports a manual retry.
    with (
        open_db(config.paths.db_path) as connection,
        patch("mediaforce.execution.probe_media", return_value=_probe()),
    ):
        delivery.validate_one_item(connection, config, failed_item)
    _run(config)
    assert _status(config, failed_id) == "promoted"


def test_shutdown_stops_between_files(config: MediaforceConfig) -> None:
    first, _, _ = _stage(config, "tv/Show/Season 1/one.mkv", checked=True)
    second, _, _ = _stage(config, "tv/Show/Season 1/two.mkv", checked=True)
    stop = threading.Event()
    promote = delivery.promote_one_item

    def finish_one(
        connection: DBClient,
        config: MediaforceConfig,
        item: dict[str, Any],
        *,
        force: bool,
    ) -> Path:
        result = promote(connection, config, item, force=force)
        stop.set()
        return result

    with (
        patch.object(delivery, "promote_one_item", side_effect=finish_one),
        patch("mediaforce.execution.probe_media", return_value=_probe()),
    ):
        delivery.publish_checked_files_once(
            config, load_calibration_state=lambda *_args: _approval(), remake_files=_no_remake, stop_event=stop
        )
    assert _status(config, first) == "promoted"
    assert _status(config, second) == "validated"


def test_cli_review_and_legacy_cli_output_do_not_auto_publish(
    config: MediaforceConfig,
) -> None:
    item_id, item, staged = _stage(config, "tv/Show/Season 1/review.mkv", checked=True)

    def encode(*_args: Any, **kwargs: Any) -> list[Any]:
        with open_db(config.paths.db_path) as connection:
            connection.execute(
                staged_artifacts.update()
                .where(staged_artifacts.c.library_item_id == item_id)
                .values(encode_origin=kwargs["encode_context"]["origin"])
            )
            connection.commit()
        return [
            SimpleNamespace(
                staging_path=staged,
                staging_size_bytes=3,
                source_size_bytes=18,
                chosen_crf=30,
                quality_metric="ssim",
                quality_score=1,
            )
        ]

    with (
        open_db(config.paths.db_path) as connection,
        patch.object(cli, "encode_manifest_items", side_effect=encode),
        patch.object(cli, "_print_manifest_item"),
        patch.object(
            cli,
            "validate_manifest_items",
            return_value=[
                {
                    "passed": True,
                    "source_size_bytes": 18,
                    "staged_size_bytes": 3,
                    "bytes_saved": 15,
                    "checks": [],
                }
            ],
        ),
        patch.object(cli, "generate_compare_clips", return_value=[]),
    ):
        cli._run_review(
            connection,
            config,
            config.paths.project_root / f"manifest-{item_id}.json",
            {"items": [item]},
            index=0,
            overwrite=True,
            duration=8,
            timestamps=None,
            output_dir=config.paths.review_dir,
            play=False,
        )
    _run(config)
    assert _status(config, item_id) == "validated"
    with open_db(config.paths.db_path) as connection:
        connection.execute(
            staged_artifacts.update()
            .where(staged_artifacts.c.library_item_id == item_id)
            .values(encode_origin="cli")
        )
        connection.commit()
    _run(config)
    assert _status(config, item_id) == "validated"
    assert Path(item["source_path"]).read_bytes() == b"original media file"


def test_unapproved_draft_does_not_hide_accepted_parent(
    config: MediaforceConfig,
) -> None:
    item_id, _, _ = _stage(config, "tv/Show/Season 1/one.mkv")

    def load(_config: MediaforceConfig, prefix: str) -> dict[str, Any] | None:
        return _approval() if prefix == "tv/Show" else {"policy": {}, "mode": "sample"}

    with patch("mediaforce.execution.probe_media", return_value=_probe()):
        delivery.publish_checked_files_once(config, load_calibration_state=load, remake_files=_no_remake)
    assert _status(config, item_id) == "promoted"


@pytest.mark.parametrize("season_approval", ["none", "own_sample", "too_old"])
def test_a_show_run_file_publishes_only_while_the_show_approval_governs_it(
    config: MediaforceConfig, season_approval: str
) -> None:
    item_id, item, _ = _stage(config, "tv/Show/Season 1/one.mkv")
    show = {**_approval(), "sample_item": {"resolved_operator_intent": {"request": {"compression_intent": "balanced"}}}}
    manifest_path = config.paths.project_root / f"manifest-{item_id}.json"
    manifest_path.write_text(json.dumps({
        "run_id": f"run-{item_id}",
        "items": [item],
        "selection": {"production_approval_contract": production_approval_contract(show)},
    }))
    season = {**show, "job_id": "season-sample", "accepted_sample_job_id": "season-sample"}
    if season_approval == "too_old":
        season.pop("accepted_policy_hash")
    approvals = {"tv/Show": show, **({"tv/Show/Season 1": season} if season_approval != "none" else {})}

    with patch("mediaforce.execution.probe_media", return_value=_probe()):
        delivery.publish_checked_files_once(
            config, load_calibration_state=lambda _config, prefix: approvals.get(prefix), remake_files=_no_remake
        )

    # The season's own approval governs its files even when it is too old to check against.
    assert (_status(config, item_id) == "promoted") == (season_approval == "none")


def test_retained_rollback_is_counted_and_owner_cleanup_reaches_it(
    config: MediaforceConfig,
) -> None:
    _, item, _ = _stage(config, "tv/Show/Season 1/one.mkv")
    archive = config.archive_root / item["rel_path"]
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(b"earlier rollback")
    _run(config)
    with open_db(config.paths.db_path) as connection:
        folders = list_completed_folders(
            connection,
            archive_root=config.archive_root,
            folder_group=web_app._folder_group,
        )
    assert folders[0].archived_backup_count == 2
    assert folders[0].archived_backup_size_bytes == len(b"earlier rollback") + len(
        b"original media file"
    )
    result = clear_completed_backups_action(
        config, folder_group=web_app._folder_group, prefixes=[folders[0].prefix]
    )
    assert result["removed_count"] == folders[0].archived_backup_count
    assert not any(path.is_file() for path in config.archive_root.rglob("*"))
    assert Path(item["source_path"]).read_bytes() == b"av1"


def test_short_storage_outage_does_not_exhaust_retries(config: MediaforceConfig) -> None:
    item_id, item, _ = _stage(config, "tv/Show/Season 1/one.mkv", checked=True)
    source = Path(item["source_path"])
    parked = source.with_suffix(".unavailable")
    source.rename(parked)
    for now in (0, 2, 4):
        with patch.object(delivery.time, "time", return_value=now):
            _run(config)
    parked.rename(source)
    with patch.object(delivery.time, "time", return_value=6):
        _run(config)
    assert _status(config, item_id) == "validated"
    with patch.object(delivery.time, "time", return_value=delivery.PUBLISH_RETRY_DELAY_SECONDS + 1):
        _run(config)
    assert _status(config, item_id) == "promoted"


def test_changing_free_space_counts_do_not_create_wait_events(config: MediaforceConfig) -> None:
    item_id, _, _ = _stage(config, "tv/Show/Season 1/one.mkv", checked=True)
    with patch.object(delivery, "promote_one_item", side_effect=[
        PromotionWaiting("Need 100 bytes but only 4 is available", reason_code="space_reserve"),
        PromotionWaiting("Need 100 bytes but only 3 is available", reason_code="space_reserve"),
        PromotionWaiting("Need 100 bytes but only 2 is available", reason_code="space_reserve"),
    ]):
        for attempt in range(3):
            with patch.object(delivery.time, "time", return_value=attempt * (delivery.PUBLISH_RETRY_DELAY_SECONDS + 1)):
                _run(config)
    with open_db(config.paths.db_path) as connection:
        events = connection.execute(select(item_events.c.details_json).where(
            item_events.c.library_item_id == item_id,
            item_events.c.event_type == "automatic_publish_waiting",
        )).scalars().all()
    assert len(events) == 1


def test_reports_changed_approval_and_source_together(config: MediaforceConfig) -> None:
    item_id, item, _ = _stage(config, "tv/Show/Season 1/one.mkv")
    approval = _approval()
    approval.pop("accepted_at")
    Path(item["source_path"]).write_bytes(b"replaced original")
    _run(config, approval)
    with open_db(config.paths.db_path) as connection:
        saved = json.loads(connection.execute(select(staged_artifacts.c.validation_json).where(
            staged_artifacts.c.library_item_id == item_id)).scalar_one())
    reason = saved["automatic_publish"]["reason"]
    assert "matching sample approval" in reason
    assert "original changed" in reason


def test_retained_rollback_count_survives_best_effort_history_failure(config: MediaforceConfig) -> None:
    from mediaforce import execution
    _, item, _ = _stage(config, "tv/Show/Season 1/one.mkv")
    archive = config.archive_root / item["rel_path"]
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(b"earlier rollback")
    record = execution._record_event
    def fail_history(connection: DBClient, item_id: int, event_type: str, details: dict[str, Any]) -> None:
        if event_type == "promotion_completed":
            raise RuntimeError("history write unavailable")
        record(connection, item_id, event_type, details)
    with patch.object(execution, "_record_event", side_effect=fail_history):
        _run(config)
    with open_db(config.paths.db_path) as connection:
        folders = list_completed_folders(connection, archive_root=config.archive_root, folder_group=web_app._folder_group)
    assert folders[0].archived_backup_count == 2
    assert Path(item["source_path"]).read_bytes() == b"av1"


def test_manual_publish_waiting_for_encode_rechecks_validation(config: MediaforceConfig) -> None:
    item_id, item, staged = _stage(config, "tv/Show/Season 1/one.mkv", checked=True)
    started = threading.Event()
    failures: list[Exception] = []
    def publish() -> None:
        with open_db(config.paths.db_path) as connection:
            started.set()
            try:
                delivery.promote_one_item(connection, config, item, force=False)
            except Exception as exc:
                failures.append(exc)
    with delivery_lock(config.paths.db_path, item_id):
        thread = threading.Thread(target=publish)
        thread.start()
        assert started.wait(5)
        staged.write_bytes(b"unchecked replacement")
        with open_db(config.paths.db_path) as connection:
            connection.execute(staged_artifacts.update().where(staged_artifacts.c.library_item_id == item_id)
                               .values(validation_json='{"passed":true}', validated_at=None))
            connection.commit()
    thread.join(5)
    assert not thread.is_alive()
    assert len(failures) == 1
    assert "must be validated" in str(failures[0])
    assert Path(item["source_path"]).read_bytes() == b"original media file"
    assert staged.read_bytes() == b"unchecked replacement"


@pytest.mark.parametrize("rel_path", ["movies/Title.avi", "tv/Show/Episode 01.avi"])
def test_loose_file_rollback_counts_and_selected_cleanup_follow_owner(config: MediaforceConfig, rel_path: str) -> None:
    _, item, _ = _stage(config, rel_path)
    archive = config.archive_root / item["rel_path"]
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(b"earlier rollback")
    _run(config)
    with open_db(config.paths.db_path) as connection:
        folders = list_completed_folders(connection, archive_root=config.archive_root, folder_group=web_app._folder_group)
    assert folders[0].archived_backup_count == 2
    result = clear_completed_backups_action(config, folder_group=web_app._folder_group, prefixes=[folders[0].prefix])
    assert result["removed_count"] == 2
    assert result["removed_prefix_count"] == 1
    assert not any(path.is_file() for path in config.archive_root.rglob("*"))
    assert Path(item["source_path"]).with_suffix(".mkv").read_bytes() == b"av1"


def test_collision_holds_both_files_for_a_manual_choice(config: MediaforceConfig) -> None:
    first, first_item, _ = _stage(config, "tv/Show/Season 1/one.mp4", checked=True)
    second, second_item, _ = _stage(config, "tv/Show/Season 1/one.avi", checked=True)
    _run(config)
    assert _status(config, first) == "validated"
    assert _status(config, second) == "validated"
    assert not Path(first_item["source_path"]).with_suffix(".mkv").exists()
    assert Path(second_item["source_path"]).read_bytes() == b"original media file"


def test_missing_source_has_a_specific_wait_when_parent_is_reachable(config: MediaforceConfig) -> None:
    item_id, item, _ = _stage(config, "tv/Show/Season 1/one.mkv", checked=True)
    Path(item["source_path"]).unlink()
    _run(config)
    with open_db(config.paths.db_path) as connection:
        saved = json.loads(connection.execute(select(staged_artifacts.c.validation_json).where(
            staged_artifacts.c.library_item_id == item_id)).scalar_one())
    assert "missing from a reachable folder" in saved["automatic_publish"]["reason"]
    assert "retry this file" not in saved["automatic_publish"]["reason"]


def test_malformed_sibling_job_does_not_hold_a_finished_file(config: MediaforceConfig) -> None:
    ready_id, _, _ = _stage(config, "tv/Show/Season 1/ready.mkv")
    manifest = config.paths.project_root / "malformed-job.json"
    manifest.write_text('{"items":[]}')
    with open_db(config.paths.db_path) as connection:
        connection.execute(encode_jobs.insert().values(job_id="broken", job_kind="shard", status="running",
            prefix="tv/Show/Season 1", manifest_path=str(manifest), manifest_indexes_json="[0]", host_json="{}",
            item_count=1, created_at=timestamp(), updated_at=timestamp()))
        connection.commit()
    _run(config)
    assert _status(config, ready_id) == "promoted"


def test_wait_reason_is_public_without_duplicate_details(config: MediaforceConfig) -> None:
    _, _, staged = _stage(config, "tv/Show/Season 1/one.mkv")
    staged.unlink()
    _run(config)
    with open_db(config.paths.db_path) as connection:
        record = staged_integrity_report(connection, config, "tv/Show/Season 1", discover=False).records[0]
    assert record.publish_wait_reason is not None
    assert record.to_payload()["publish_wait_reason"] == record.publish_wait_reason
    assert record.detail.count(record.publish_wait_reason) == 1


@pytest.mark.parametrize("settings_cleanup", [False, True])
def test_owner_cleanup_preserves_an_original_during_publication(config: MediaforceConfig, settings_cleanup: bool) -> None:
    from mediaforce import execution
    _stage(config, "tv/Show/Season 1/done.mkv", checked=True)
    _run(config)
    _, item, _ = _stage(config, "tv/Show/Season 1/publishing.mkv", checked=True)
    moved, release = threading.Event(), threading.Event()
    failures: list[Exception] = []
    def probe(_path: Path) -> ProbeSummary:
        moved.set()
        assert release.wait(5)
        raise RuntimeError("final probe failed")
    def publish() -> None:
        with open_db(config.paths.db_path) as connection:
            try:
                execution.promote_one_item(connection, config, item, force=False)
            except Exception as exc:
                failures.append(exc)
    with patch.object(execution, "probe_media", side_effect=probe):
        thread = threading.Thread(target=publish)
        thread.start()
        try:
            assert moved.wait(5)
            result = (clear_archive_cleanup_action(config) if settings_cleanup else
                clear_completed_backups_action(config, folder_group=web_app._folder_group,
                    prefixes=["tv/Show/Season 1"], valid_prefixes={"tv/Show/Season 1"}))
            assert result["ok"] is False
            assert result["removed_count"] == 0
        finally:
            release.set()
            thread.join(5)
    assert not thread.is_alive()
    assert len(failures) == 1
    assert not isinstance(failures[0], PromotionRestoreError)
    assert Path(item["source_path"]).read_bytes() == b"original media file"
    result = clear_completed_backups_action(config, folder_group=web_app._folder_group,
        prefixes=["tv/Show/Season 1"], valid_prefixes={"tv/Show/Season 1"})
    assert result["ok"] is True
    assert result["removed_count"] == 1


@pytest.mark.parametrize("settings_cleanup", [False, True])
def test_cleanup_keeps_originals_needed_by_an_unpublished_sibling(config: MediaforceConfig, settings_cleanup: bool) -> None:
    _stage(config, "tv/Show/Season 1/done.mkv", checked=True)
    _run(config)
    item_id, item, _ = _stage(config, "tv/Show/Season 1/unsafe.mkv", checked=True)
    original = Path(item["source_path"])
    archived = config.archive_root / item["rel_path"]
    archived.parent.mkdir(parents=True, exist_ok=True)
    original.rename(archived)
    retained = archived.with_name(f".{archived.name}.promotion-backup-fixture")
    retained.write_bytes(b"previous original")
    with open_db(config.paths.db_path) as connection:
        delivery._record_wait(connection, item_id, "unsafe", "Inspect and restore this file.")
    result = (clear_archive_cleanup_action(config) if settings_cleanup else
        clear_completed_backups_action(config, folder_group=web_app._folder_group,
            prefixes=["tv/Show/Season 1"], valid_prefixes={"tv/Show/Season 1"}))
    assert result["removed_count"] == 1
    assert result["preserved_count"] == 2
    assert "Inspect and restore" in result["message"]
    assert archived.read_bytes() == b"original media file"
    assert retained.read_bytes() == b"previous original"
    assert not (config.archive_root / "tv/Show/Season 1/done.mkv").exists()
    archived.rename(original)
    _run(config)
    assert _status(config, item_id) != "promoted"


def _encoder_size_record(
    config: MediaforceConfig, rel_path: str, *, lower: int, upper: int, band_in_plan: bool = True
) -> tuple[int, dict[str, Any]]:
    """Stage a file the way the encoder leaves it after its final size check against that band."""
    item_id, item, staged = _stage(config, rel_path)
    staged_bytes = staged.stat().st_size
    status = "over_target" if staged_bytes > upper else "under_target" if staged_bytes < lower else "inside_target_band"
    if band_in_plan:
        item["resolved_operator_intent"] = {
            "size_goal": {"final_lower_bound_bytes": lower, "final_upper_bound_bytes": upper, "target_size_bytes": lower}
        }
    manifest_path = config.paths.project_root / f"manifest-{item_id}.json"
    manifest_path.write_text(json.dumps({"run_id": f"run-{item_id}", "items": [item]}))
    passed = status == "inside_target_band"
    encoder_record = {
        "passed": passed,
        "target_size_verification": {
            "status": status, "passed": passed, "lower_bound_bytes": lower, "upper_bound_bytes": upper,
            "actual_output_bytes": staged_bytes, "retry_allowed": False,
            "retry_reason": None if passed else "The final-size retry budget is used up.",
        },
        "target_size_trace": None,
    }
    with open_db(config.paths.db_path) as connection:
        connection.execute(
            staged_artifacts.update()
            .where(staged_artifacts.c.library_item_id == item_id)
            .values(validation_json=json.dumps(encoder_record))
        )
        connection.commit()
    return item_id, item


def _keep(config: MediaforceConfig, item_id: int, item: dict[str, Any]) -> dict[str, Any]:
    def validate_items(_prefix: str, _ids: Any) -> dict[str, Any]:
        with patch("mediaforce.execution.probe_media", return_value=_probe()), open_db(config.paths.db_path) as connection:
            result = delivery.validate_one_item(connection, config, item)
        return {"ok": True, "validated_count": int(bool(result["passed"]))}

    return size_held.decide_size_held_file(
        config, "tv/Show/Season 1", item_id, keep=True, now_iso=timestamp,
        validate_items=validate_items, queue_items=lambda *_args: pytest.fail("keeping must not queue anything"),
    )


def _remake_reason(config: MediaforceConfig, item_id: int) -> str | None:
    with open_db(config.paths.db_path) as connection:
        report = staged_integrity_report(connection, config, "tv/Show/Season 1", discover=False)
        records = [record.to_payload() for record in report.records if record.item_id == item_id]
        size_held.staged_remake_records(connection, records, "tv/Show/Season 1", current_approval=lambda _p: None)
    return object_dict(records[0].get("remake")).get("reason")


@pytest.mark.parametrize("band_in_plan", [True, False], ids=["band_in_plan", "no_band_in_plan"])
def test_encoder_size_only_miss_is_checked_then_asks_keep_or_make_again(
    config: MediaforceConfig, band_in_plan: bool
) -> None:
    item_id, item = _encoder_size_record(
        config, "tv/Show/Season 1/too-big.mkv", lower=1, upper=2, band_in_plan=band_in_plan
    )

    _run(config)

    assert _status(config, item_id) == "encoded"
    assert not (config.archive_root / item["rel_path"]).exists()
    with open_db(config.paths.db_path) as connection:
        saved = json.loads(str(connection.execute(
            select(staged_artifacts.c.validation_json).where(staged_artifacts.c.library_item_id == item_id)
        ).scalar_one()))
    assert [check["message"] for check in saved["checks"] if not check["passed"]] == [FINAL_SIZE_GOAL_CHECK]
    assert _remake_reason(config, item_id) == "final_size"

    result = _keep(config, item_id, item)
    assert result["ok"], result
    _run(config)
    assert _status(config, item_id) == "promoted"


def test_encoder_size_miss_with_another_failure_stays_failed(config: MediaforceConfig) -> None:
    item_id, item = _encoder_size_record(config, "tv/Show/Season 1/broken.mkv", lower=1, upper=2)

    with patch("mediaforce.execution.probe_media", return_value=replace(_probe(), video_codec="h264")):
        delivery.publish_checked_files_once(
            config, load_calibration_state=lambda _config, _prefix: _approval(), remake_files=_remake_refused
        )

    with open_db(config.paths.db_path) as connection:
        report = staged_integrity_report(connection, config, "tv/Show/Season 1", discover=False)
    assert [record.disposition for record in report.records if record.item_id == item_id] == ["validation_failed"]
    assert _remake_reason(config, item_id) is None
    assert not _keep(config, item_id, item)["ok"]
    assert _status(config, item_id) == "encoded"


def test_encoder_copy_inside_its_size_band_publishes_without_a_question(config: MediaforceConfig) -> None:
    item_id, item = _encoder_size_record(config, "tv/Show/Season 1/fits.mkv", lower=1, upper=100)
    _run(config)
    assert _status(config, item_id) == "promoted"
    assert Path(item["source_path"]).read_bytes() == b"av1"


def _restage(config: MediaforceConfig, item_id: int, item: dict[str, Any]) -> None:
    """Leave a new copy of an existing file, the way the queue does after it is made again."""
    staged = Path(config.media["staging_root"]) / item["rel_path"]
    staged.write_bytes(b"av1")
    with open_db(config.paths.db_path) as connection:
        connection.execute(
            staged_artifacts.insert().values(
                library_item_id=item_id,
                manifest_run_id=f"run-{item_id}",
                manifest_path=str(config.paths.project_root / f"manifest-{item_id}.json"),
                item_index=0,
                encode_origin="queue",
                staging_path=str(staged),
                staging_size_bytes=staged.stat().st_size,
                staging_mtime_ns=staged.stat().st_mtime_ns,
                source_fingerprint=item["source_fingerprint"],
                validation_json="{}",
                updated_at=timestamp(),
            )
        )
        connection.execute(library_items.update().where(library_items.c.id == item_id).values(status="encoded"))
        connection.commit()


def _kept_reason(config: MediaforceConfig, item_id: int) -> str | None:
    with open_db(config.paths.db_path) as connection:
        if not library_item_ids_kept_original(connection, [item_id]):
            return None
        return connection.execute(
            select(motion_keep_decisions.c.reason_code).where(motion_keep_decisions.c.library_item_id == item_id)
        ).scalar_one()


def test_failed_check_is_made_again_until_the_limit_then_the_original_is_kept(config: MediaforceConfig) -> None:
    item_id, item, staged = _stage(config, "tv/Show/Season 1/broken.mkv")
    queued: list[list[int]] = []

    def queue_items(_prefix: str, _mode: str, ids: Any, _approval: dict[str, Any]) -> dict[str, Any]:
        queued.append(list(ids))
        return {"ok": True, "queued_library_item_ids": list(ids)}

    def remake(prefix: str, ids: Any) -> dict[str, Any]:
        return size_held.remake_staged_files(
            config, prefix, ids, now_iso=timestamp, queue_items=queue_items,
            current_approval=lambda _prefix: {"approval": "current"}, retry_failed_check=True,
        )

    for attempt in range(1, delivery.CHECK_FAILURE_ATTEMPT_LIMIT + 1):
        with patch("mediaforce.execution.probe_media", return_value=replace(_probe(), video_codec="h264")):
            delivery.publish_checked_files_once(
                config, load_calibration_state=lambda _config, _prefix: _approval(), remake_files=remake
            )
        assert not staged.exists()
        assert Path(item["source_path"]).read_bytes() == b"original media file"
        if attempt < delivery.CHECK_FAILURE_ATTEMPT_LIMIT:
            assert queued == [[item_id]] * attempt
            assert _status(config, item_id) == "planned"
            assert _kept_reason(config, item_id) is None
            _restage(config, item_id, item)

    assert len(queued) == delivery.CHECK_FAILURE_ATTEMPT_LIMIT - 1
    assert _kept_reason(config, item_id) == size_held.CHECK_FAILED_KEEP_REASON
    with open_db(config.paths.db_path) as connection:
        assert connection.execute(
            select(staged_artifacts.c.library_item_id).where(staged_artifacts.c.library_item_id == item_id)
        ).first() is None
    [episode] = folder_episodes_payload(config, "tv/Show/Season 1")["episodes"]
    assert episode["stage"] == "kept_original"
    with open_db(config.paths.db_path) as connection:
        # A changed source lapses the keep, so the new source is worked on like any other file.
        connection.execute(library_items.update().where(library_items.c.id == item_id).values(fingerprint="new"))
        connection.commit()
    assert _kept_reason(config, item_id) is None


def test_failed_copy_waiting_to_be_made_again_does_not_wait_on_the_owner(config: MediaforceConfig) -> None:
    item_id, _, staged = _stage(config, "tv/Show/Season 1/broken.mkv")

    with patch("mediaforce.execution.probe_media", return_value=replace(_probe(), video_codec="h264")):
        delivery.publish_checked_files_once(
            config, load_calibration_state=lambda _config, _prefix: _approval(), remake_files=_remake_refused
        )

    assert staged.exists()
    assert _status(config, item_id) == "encoded"
    with open_db(config.paths.db_path) as connection:
        [state] = build_folder_workflow_state(connection, "tv/Show/Season 1").items
    assert state.lane != "blocked"
    [episode] = folder_episodes_payload(config, "tv/Show/Season 1")["episodes"]
    assert (episode["stage"], episode["validation"], episode["owner_action"]) == ("waiting", "remaking", None)
