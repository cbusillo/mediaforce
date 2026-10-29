import ast
import json
import os
import subprocess
import sys
import threading
import typing
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import func, select, update

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient, open_db
from mediaforce.core.db_tables import (
    library_item_evidence_state,
    library_items,
    source_admissions,
    source_measurements,
)
from mediaforce.encoding.remote_media import source_observation_script
from mediaforce.library.evidence_queue import (
    DEFAULT_EVIDENCE_BATCH_LIMIT,
    EVIDENCE_WORK_PRIORITY_REPRESENTATIVE,
    EVIDENCE_WORK_REASON_ORIGINAL_SOURCE,
    EvidenceWorkClaim,
    cancel_evidence_queue,
    claim_next_evidence_work,
    resume_evidence_queue,
    original_intake_hold,
    refresh_controller_digest_waits,
    original_check_wait_reasons,
    queue_decision_evidence_work,
    runnable_evidence_work_count,
    start_evidence_work,
)
from mediaforce.library.evidence_state import EVIDENCE_KINDS, EVIDENCE_STATE_CURRENT, EVIDENCE_STATE_CLASSIFICATION_REQUIRED, EvidenceKind, rebuild_library_item_evidence_states
from mediaforce.library.evidence_worker import EvidenceWorkerDeps, process_evidence_queue_once
from mediaforce.library.source_evidence import load_source_evidence, observe_file, record_current_measurement
from mediaforce.library.source_intake import assess_readable_source, commit_readable_source, queue_original_intake
from test_source_evidence import connection as connection, _config, _insert_item
import test_evidence_worker as worker_fixtures


def _probe(*, codec: str = "h264", tags: dict[str, str] | None = None, stream_tags: dict[str, str] | None = None) -> dict[str, Any]:
    return {"format": {"tags": tags or {}}, "streams": [{"codec_type": "video", "codec_name": codec, "tags": stream_tags or {}}]}


def _source(config: MediaforceConfig, name: str = "source") -> tuple[int, Path]:
    path = config.source_root_map["tv"] / f"{name}.mkv"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(name.encode() * 100)
    with open_db(config.paths.db_path) as database:
        item_id = _insert_item(database, path)
        database.execute(
            update(library_items)
            .where(library_items.c.id == item_id)
            .values(video_codec="h264", rel_path=f"tv/{name}.mkv", file_name=path.name)
        )
    return item_id, path


def _summary(kind: str, host: str = "first") -> dict[str, Any]:
    raw = (
        worker_fixtures.EvidenceWorkerTests._cadence_summary_json()
        if kind == "cadence_analysis"
        else worker_fixtures.EvidenceWorkerTests._fingerprint_summary_json()
    )
    result = json.loads(raw)
    result.setdefault("analysis", {}).setdefault("tool", {})["host"] = host
    return result


def _work(
    config: MediaforceConfig,
    analyze: Callable[..., dict[str, object]] | None = None,
    *,
    stop_event: threading.Event | None = None,
    max_attempts: int | None = None,
    max_work_items: int = 1,
) -> int:
    deps = EvidenceWorkerDeps(read_probe=lambda *_args, **_kw: _probe(),
        load_config=lambda _path: config,
        analyze_evidence=analyze or (lambda _path, kind, **_kwargs: _summary(kind)),
        logger=Mock(),
        heartbeat_seconds=0.01,
        retry_base_delay_seconds=0,
        shutdown_event=stop_event,
        **({"max_attempts": max_attempts} if max_attempts is not None else {}),
    )
    with (
        patch("mediaforce.library.source_intake.probe_evidence_source", return_value=_probe()),
        patch(
            "mediaforce.library.evidence_worker._analysis_target",
            side_effect=lambda _config, _claim, path, *_args: SimpleNamespace(source_path=path, command_runner=None),
        ),
    ):
        return process_evidence_queue_once(config_path=config.paths.config_path, config=config, deps=deps, max_work_items=max_work_items)


def _measure_source_traits(config: MediaforceConfig) -> None:
    with open_db(config.paths.db_path) as database:
        rebuild_library_item_evidence_states(database)
        start_evidence_work(database, config, "tv/source.mkv")
        resume_evidence_queue(database)
    assert _work(config)
    assert _work(config)


def _discover(config: MediaforceConfig) -> int:
    with open_db(config.paths.db_path) as database:
        return queue_original_intake(database, config)


def _cache_unadmitted_original(config: MediaforceConfig, item_id: int) -> None:
    from mediaforce.core.process_control import ManagedProcessController
    from mediaforce.library.source_intake import assess_source_input

    with patch("mediaforce.library.source_intake.probe_evidence_source", side_effect=RuntimeError("probe failed")):
        with pytest.raises(RuntimeError, match="probe failed"):
            assess_source_input(config, item_id, ManagedProcessController(), lambda: None)


def _touch_catalog_source(config: MediaforceConfig, item_id: int, path: Path) -> None:
    info = path.stat()
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))
    with open_db(config.paths.db_path) as database:
        database.execute(update(library_items).where(library_items.c.id == item_id).values(mtime_ns=path.stat().st_mtime_ns))


def _assert_unadmitted_window_wait(database: DBClient, item_id: int, *, status: str = "waiting_window") -> None:
    state = database.execute(select(library_item_evidence_state).where(
        library_item_evidence_state.c.library_item_id == item_id)).mappings().one()
    assert state["work_status"] == status
    assert state["attempt_count"] == 0
    assert database.execute(select(source_admissions)).first() is None


@pytest.mark.parametrize("size", [0, 1, 65535, 65536, 65537, 131072, 1048577])
def test_remote_samples_equal_descriptor_samples(tmp_path: Path, size: int) -> None:
    path = tmp_path / "source"
    path.write_bytes(bytes(range(251)) * (size // 251) + bytes(range(size % 251)))
    result = subprocess.run(
        [sys.executable, "-c", source_observation_script(), str(path), "1"], check=True, capture_output=True, text=True
    )
    assert json.loads(result.stdout) == observe_file(path, full_digest=True)


def test_remote_script_does_not_evaluate_newer_union_annotations(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    class OldCallable:
        def __getitem__(self, _item: object) -> object:
            return object()

    path = tmp_path / "source"
    path.write_bytes(b"source")
    script = source_observation_script()
    ast.parse(script, feature_version=(3, 9))
    with patch.object(typing, "Callable", OldCallable()), patch.object(sys, "argv", ["probe", str(path), "0"]):
        exec(compile(script, "remote-observation", "exec"), {})
    assert json.loads(capsys.readouterr().out)["sample_fingerprint"] == observe_file(path)["sample_fingerprint"]


@pytest.mark.parametrize(
    "probe",
    [
        _probe(tags={"MEDIAFORCE_ENCODED_BY": ""}),
        _probe(stream_tags={"Mediaforce_Encoded_By": "anything"}),
        _probe(codec="av1"),
        _probe(stream_tags={"encoder": "Lavc libsvtav1"}),
    ],
)
def test_outputs_never_become_probable(connection: DBClient, tmp_path: Path, probe: dict[str, Any]) -> None:
    path = tmp_path / "source.mkv"
    path.write_bytes(b"source")
    item_id = _insert_item(connection, path)
    identity = observe_file(path, full_digest=True)
    assert assess_readable_source(connection, identity, probe)["reason"]
    item = cast(
        dict[str, Any], dict(connection.execute(select(library_items).where(library_items.c.id == item_id)).mappings().one())
    )
    commit_readable_source(connection, item, identity, probe)
    assert {trait.tier for trait in load_source_evidence(connection, item_id).traits.values()} == {"unknown"}


def test_no_computer_waits_before_reading_an_original_digest(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.raw["remote_hosts"] = [{"host": "offline.example", "mode": "ssh", "source_roots": {"tv": str(config.source_root_map["tv"])}}]
    item_id, _path = _source(config)
    _discover(config)
    with patch("mediaforce.encoding.remote_media.run_remote_command", side_effect=AssertionError("no real SSH")), patch(
        "mediaforce.library.source_intake.probe_evidence_source"
    ) as probe:
        assert not process_evidence_queue_once(config_path=config.paths.config_path, config=config,
            deps=EvidenceWorkerDeps(read_probe=lambda *_args, **_kw: _probe(), load_config=lambda _path: config, analyze_evidence=Mock(), logger=Mock(),
                evidence_host_rows=lambda _config: [{"key": "offline.example", "available": False}]))
    with open_db(config.paths.db_path) as db:
        state = db.execute(select(library_item_evidence_state).where(
            library_item_evidence_state.c.library_item_id == item_id)).mappings().one()
        assert state["work_status"] == "waiting_host"
        assert state["attempt_count"] == 0
        assert db.execute(select(source_admissions)).first() is None
    probe.assert_not_called()


@pytest.mark.parametrize("controller_open", [False, None])
def test_remote_measuring_window_does_not_admit_a_new_controller_digest(tmp_path: Path, controller_open: bool | None) -> None:
    config = _config(tmp_path)
    config.raw["remote_hosts"] = [
        {"host": "localhost", "label": "Controller", "mode": "local"},
        {"host": "measuring.example", "mode": "ssh", "source_roots": {"tv": str(config.source_root_map["tv"])}},
    ]
    item_id, path = _source(config)
    _discover(config)
    controller_row: dict[str, Any] = {"key": "localhost", "label": "Controller", "available": True}
    if controller_open is not None:
        controller_row["schedule_open"] = controller_open
    rows = [controller_row, {"key": "measuring.example", "available": True,
                             "capabilities": ["encode_queue"], "schedule_open": True}]
    deps = EvidenceWorkerDeps(load_config=lambda _path: config, analyze_evidence=lambda _path, kind, **_kw: _summary(kind),
                              logger=Mock(), evidence_host_rows=lambda _config: rows)
    with patch("mediaforce.hosts.config._host_lookup_targets_current_machine", side_effect=lambda host: host == "localhost"), patch(
        "mediaforce.library.source_intake.observe_file", wraps=observe_file
    ) as observe, patch("mediaforce.library.source_intake.probe_evidence_source", return_value=_probe()), patch(
        "mediaforce.library.evidence_worker._analysis_target",
        side_effect=lambda _config, _claim, source, *_args: SimpleNamespace(source_path=source, command_runner=None),
    ), patch("mediaforce.encoding.remote_media.run_remote_command", side_effect=AssertionError("no real SSH")):
        assert process_evidence_queue_once(config_path=config.paths.config_path, config=config, deps=deps) == 0
        assert not any(call.kwargs.get("full_digest") for call in observe.call_args_list)
        with open_db(config.paths.db_path) as database:
            _assert_unadmitted_window_wait(database, item_id)
            assert runnable_evidence_work_count(database) == 0
            from mediaforce.library.evidence_hosts import controller_digest_window
            from test_app_window import _payload
            reason = controller_digest_window(config, deps.evidence_host_rows).waiting_reason
            assert _payload(database)["work"]["original_check_wait_reasons"] == [reason]
        controller_row["schedule_open"] = True
        assert process_evidence_queue_once(config_path=config.paths.config_path, config=config, deps=deps) == 1
        assert any(call.args[0] == path and call.kwargs.get("full_digest") for call in observe.call_args_list)
    with open_db(config.paths.db_path) as database:
        assert load_source_evidence(database, item_id).original_version_id is not None


@pytest.mark.parametrize("changed_stat", [False, True])
def test_cached_controller_digest_can_feed_remote_measurement_outside_controller_window(tmp_path: Path, changed_stat: bool) -> None:
    config = _config(tmp_path)
    blocked, _ = _source(config, "unread")
    cached, cached_path = _source(config, "cached")
    _cache_unadmitted_original(config, cached)
    if changed_stat:
        _touch_catalog_source(config, cached, cached_path)
    config.raw["remote_hosts"] = [{"host": "localhost", "mode": "local"},
        {"host": "measuring.example", "mode": "ssh", "source_roots": {"tv": str(config.source_root_map["tv"])}}]
    _discover(config)
    measured: list[Path] = []
    deps = EvidenceWorkerDeps(load_config=lambda _path: config,
        analyze_evidence=lambda path, kind, **_kw: (measured.append(path), _summary(kind))[1], logger=Mock(),
        evidence_host_rows=lambda _config: [{"key": "localhost", "schedule_open": False},
            {"key": "measuring.example", "available": True, "schedule_open": True, "capabilities": ["encode_queue"]}])
    with patch("mediaforce.library.source_intake.observe_file", wraps=observe_file) as observe, patch(
        "mediaforce.library.source_intake.probe_evidence_source", return_value=_probe()
    ), patch("mediaforce.library.evidence_worker._analysis_target",
        side_effect=lambda _config, _claim, path, *_args: SimpleNamespace(source_path=path, command_runner=None)):
        assert process_evidence_queue_once(config_path=config.paths.config_path, config=config, deps=deps) == (0 if changed_stat else 1)
    assert measured == ([] if changed_stat else [cached_path])
    assert not any(call.kwargs.get("full_digest") for call in observe.call_args_list)
    with open_db(config.paths.db_path) as database:
        blocked_state = database.execute(select(library_item_evidence_state).where(
            library_item_evidence_state.c.library_item_id == blocked)).mappings().one()
        assert blocked_state["work_status"] == "waiting_window"
        assert blocked_state["attempt_count"] == 0
        assert (load_source_evidence(database, cached).original_version_id is not None) == (not changed_stat)
        if changed_stat:
            state = database.execute(select(library_item_evidence_state).where(
                library_item_evidence_state.c.library_item_id == cached)).mappings().one()
            assert state["work_status"] == "waiting_window"
            assert state["attempt_count"] == 0


@pytest.mark.parametrize("finish_current_file", [True, False])
def test_controller_digest_obeys_the_current_file_close_policy(tmp_path: Path, finish_current_file: bool) -> None:
    config = _config(tmp_path)
    config.raw["remote_hosts"] = [{"host": "localhost", "mode": "local"}]
    item_id, _path = _source(config)
    _discover(config)
    opened = datetime.now(UTC)
    closed = opened + timedelta(minutes=1)
    deps = EvidenceWorkerDeps(load_config=lambda _path: config, analyze_evidence=lambda _path, kind, **_kw: _summary(kind),
        logger=Mock(), evidence_host_rows=lambda _config: [{"key": "localhost", "schedule_open": True,
            "schedule_closes_at": closed.isoformat(), "finish_current_file": finish_current_file}])
    with patch("mediaforce.library.evidence_hosts.datetime") as clock:
        clock.now.return_value = opened

        def observe(path: Path, **kwargs: Any) -> dict[str, Any]:
            if kwargs.get("full_digest"):
                clock.now.return_value = closed
                kwargs["check_cancelled"]()
            return observe_file(path, **kwargs)

        with patch("mediaforce.library.source_intake.observe_file", side_effect=observe), patch(
            "mediaforce.library.source_intake.probe_evidence_source", return_value=_probe()
        ), patch("mediaforce.library.evidence_worker._analysis_target",
            side_effect=lambda _config, _claim, path, *_args: SimpleNamespace(source_path=path, command_runner=None)):
            assert process_evidence_queue_once(config_path=config.paths.config_path, config=config, deps=deps)
    with open_db(config.paths.db_path) as database:
        assert (load_source_evidence(database, item_id).original_version_id is not None) == finish_current_file
        if not finish_current_file:
            state = database.execute(select(library_item_evidence_state).where(
                library_item_evidence_state.c.library_item_id == item_id)).mappings().one()
            assert state["work_status"] == "retry_wait"
            assert state["attempt_count"] == 0


def test_controller_window_closing_after_claim_does_not_spend_an_attempt(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.raw["remote_hosts"] = [{"host": "localhost", "mode": "local"}]
    item_id, _path = _source(config)
    _discover(config)
    def computer_rows(_config: MediaforceConfig) -> list[dict[str, Any]]:
        with open_db(config.paths.db_path) as claim_database:
            claimed = claim_database.execute(select(library_item_evidence_state.c.work_status).where(
                library_item_evidence_state.c.library_item_id == item_id)).scalar_one() == "running"
        return [{"key": "localhost", "schedule_open": not claimed}]

    deps = EvidenceWorkerDeps(load_config=lambda _path: config, analyze_evidence=Mock(), logger=Mock(),
                              evidence_host_rows=computer_rows)
    with patch("mediaforce.library.source_intake.observe_file", wraps=observe_file) as observe:
        assert process_evidence_queue_once(config_path=config.paths.config_path, config=config, deps=deps)
    assert not any(call.kwargs.get("full_digest") for call in observe.call_args_list)
    with open_db(config.paths.db_path) as database:
        _assert_unadmitted_window_wait(database, item_id, status="retry_wait")


def test_window_waits_do_not_unpair_eligible_remote_measurements(tmp_path: Path) -> None:
    config = _config(tmp_path)
    waiting, _ = _source(config, "waiting")
    ready, ready_path = _source(config, "ready")
    _discover(config)
    with open_db(config.paths.db_path) as database:
        rebuild_library_item_evidence_states(database)
        for requested_kind in EVIDENCE_KINDS:
            queue_decision_evidence_work(database, config, "tv/ready.mkv", library_item_ids=[ready], evidence_kind=requested_kind,
                work_reason="operator_scope", require_measurement=True, priority=50, start_new_batch=True)
        database.execute(update(library_item_evidence_state).where(
            library_item_evidence_state.c.library_item_id == waiting).values(work_priority=0))
    config.raw["remote_hosts"] = [{"host": "localhost", "mode": "local"}]
    measured: list[str] = []
    read_probe = Mock(return_value=_probe())

    def analyze(path: Path, kind: str, **kwargs: Any) -> dict[str, object]:
        assert path == ready_path and kwargs["probe_payload"] is read_probe.return_value
        measured.append(kind)
        return _summary(kind)

    deps = EvidenceWorkerDeps(load_config=lambda _path: config, analyze_evidence=analyze, logger=Mock(), read_probe=read_probe,
        evidence_host_rows=lambda _config: [{"key": "localhost", "schedule_open": False}])
    with patch("mediaforce.library.evidence_worker._analysis_target",
        side_effect=lambda _config, _claim, path, *_args: SimpleNamespace(source_path=path, command_runner=None)):
        assert process_evidence_queue_once(config_path=config.paths.config_path, config=config, deps=deps, max_work_items=2) == 2
    assert set(measured) == set(EVIDENCE_KINDS)
    read_probe.assert_called_once()
    with open_db(config.paths.db_path) as database:
        state = database.execute(select(library_item_evidence_state).where(
            library_item_evidence_state.c.library_item_id == waiting,
            library_item_evidence_state.c.evidence_kind == "media_fingerprint")).mappings().one()
        assert state["work_status"] == "waiting_window"
        assert state["attempt_count"] == 0


def test_autostart_waits_without_a_runner_and_wakes_when_the_controller_window_opens(tmp_path: Path) -> None:
    from mediaforce.web.runtime.operator_work import BoundedEvidenceRunner, start_ready_evidence_work

    config = _config(tmp_path)
    item_id, _path = _source(config)
    _discover(config)
    config.raw["remote_hosts"] = [{"host": "localhost", "mode": "local"}]
    row = {"key": "localhost", "schedule_open": False}
    deps = EvidenceWorkerDeps(load_config=lambda _path: config, analyze_evidence=Mock(), logger=Mock(),
                              evidence_host_rows=lambda _config: [row])
    runner = BoundedEvidenceRunner(config.paths.config_path, deps=deps)
    with patch.object(runner, "wake", return_value=False), patch.object(runner, "start", return_value=True) as start:
        for _ in range(2):
            assert not start_ready_evidence_work(config.paths.db_path, runner)
        start.assert_not_called()
        with open_db(config.paths.db_path) as database:
            state = database.execute(select(library_item_evidence_state).where(
                library_item_evidence_state.c.library_item_id == item_id)).mappings().one()
            assert state["work_status"] == "waiting_window"
            assert state["attempt_count"] == 0
        row["schedule_open"] = True
        assert start_ready_evidence_work(config.paths.db_path, runner)
        start.assert_called_once_with(max_work_items=None)


@pytest.mark.parametrize("earlier_batch", [False, True])
@pytest.mark.parametrize("claimed_wait", [False, True])
def test_a_remote_decision_takes_over_an_original_window_wait(tmp_path: Path, earlier_batch: bool, claimed_wait: bool) -> None:
    from mediaforce.library.evidence_hosts import ControllerDigestWindow

    config = _config(tmp_path)
    item_id, path = _source(config)
    _discover(config)
    window = ControllerDigestWindow(admitted=False, next_opens_at=datetime.now(UTC) + timedelta(hours=1))
    with open_db(config.paths.db_path) as database:
        refresh_controller_digest_waits(database, window)
        if claimed_wait:
            database.execute(update(library_item_evidence_state).values(work_status="retry_wait", attempt_count=2))
        if earlier_batch:
            database.execute(update(library_item_evidence_state).values(work_batch_id="earlier-batch"))
        queue_decision_evidence_work(database, config, "tv/source.mkv", library_item_ids=[item_id],
            evidence_kind="media_fingerprint", work_reason="decision_required", priority=0,
            require_measurement=True, start_new_batch=True)
        assert original_check_wait_reasons(database) == []
        assert runnable_evidence_work_count(database) == 1
        assert database.execute(select(library_item_evidence_state.c.attempt_count)).scalar_one() == (2 if claimed_wait else 0)
    config.raw["remote_hosts"] = [{"host": "localhost", "mode": "local"}]
    analyze = Mock(return_value=_summary("media_fingerprint"))
    deps = EvidenceWorkerDeps(load_config=lambda _path: config, analyze_evidence=analyze, logger=Mock(),
        evidence_host_rows=lambda _config: [{"key": "localhost", "schedule_open": False}])
    with patch("mediaforce.library.source_intake.observe_file", side_effect=AssertionError("a decision needs no new local hash")), patch(
        "mediaforce.library.evidence_worker._analysis_target",
        side_effect=lambda _config, _claim, source, *_args: SimpleNamespace(source_path=source, command_runner=None),
    ):
        assert process_evidence_queue_once(config_path=config.paths.config_path, config=config, deps=deps) == 1
    assert analyze.call_args.args[0] == path
    with open_db(config.paths.db_path) as database:
        assert original_check_wait_reasons(database) == []
        state = database.execute(select(library_item_evidence_state).where(
            library_item_evidence_state.c.library_item_id == item_id)).mappings().one()
        assert state["work_status"] == "completed"


def test_a_replaced_cached_file_keeps_its_window_retry_cooldown(tmp_path: Path) -> None:
    from mediaforce.library.evidence_hosts import ControllerDigestWindow

    config = _config(tmp_path)
    item_id, path = _source(config)
    _cache_unadmitted_original(config, item_id)
    _discover(config)
    original_stat = path.stat()
    replacement = path.with_suffix(".replacement")
    replacement.write_bytes(path.read_bytes())
    os.utime(replacement, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    replacement.replace(path)
    config.raw["remote_hosts"] = [{"host": "localhost", "mode": "local"}]
    deps = EvidenceWorkerDeps(load_config=lambda _path: config, analyze_evidence=Mock(), logger=Mock(),
        evidence_host_rows=lambda _config: [{"key": "localhost", "schedule_open": False}])
    with patch("mediaforce.library.source_intake.observe_file", wraps=observe_file) as observe:
        assert process_evidence_queue_once(config_path=config.paths.config_path, config=config, deps=deps) == 1
        assert process_evidence_queue_once(config_path=config.paths.config_path, config=config, deps=deps) == 0
    assert not any(call.kwargs.get("full_digest") for call in observe.call_args_list)
    with open_db(config.paths.db_path) as database:
        state = database.execute(select(library_item_evidence_state).where(
            library_item_evidence_state.c.library_item_id == item_id)).mappings().one()
        assert state["work_status"] == "retry_wait"
        assert state["attempt_count"] == 0
        assert original_check_wait_reasons(database) == [ControllerDigestWindow().waiting_reason]


@pytest.mark.parametrize("earlier_batch", [False, True])
def test_remote_check_failure_keeps_its_backoff_when_a_request_changes_priority(tmp_path: Path, earlier_batch: bool) -> None:
    config = _config(tmp_path)
    item_id, _path = _source(config)
    _discover(config)
    retry_at = (datetime.now(UTC) + timedelta(minutes=1)).isoformat(timespec="seconds")
    with open_db(config.paths.db_path) as database:
        database.execute(update(library_item_evidence_state).values(work_reason="operator_scope", work_status="retry_wait",
            retry_not_before=retry_at, last_error="The computer is busy.", attempt_count=2))
        if earlier_batch:
            database.execute(update(library_item_evidence_state).values(work_batch_id="earlier-batch"))
        queue_decision_evidence_work(database, config, "tv/source.mkv", library_item_ids=[item_id],
            evidence_kind="media_fingerprint", work_reason="decision_required", priority=0,
            require_measurement=True, start_new_batch=True)
        state = database.execute(select(library_item_evidence_state)).mappings().one()
        assert state["work_status"] == "retry_wait"
        assert state["retry_not_before"] == retry_at
        assert state["attempt_count"] == 2


def test_original_retry_reasons_use_the_existing_plain_failure_formatter(tmp_path: Path) -> None:
    from mediaforce.encoding.plain_failure import plain_failure_summary

    config = _config(tmp_path)
    _source(config)
    _discover(config)
    error = "RuntimeError: ffprobe could not inspect /private/technical/path"
    with open_db(config.paths.db_path) as database:
        database.execute(update(library_item_evidence_state).values(work_status="retry_wait", last_error=error))
        assert original_check_wait_reasons(database) == [plain_failure_summary(error)]


@pytest.mark.parametrize("qualification", ["cache", "stale_cache", "receipt"])
def test_window_wait_releases_new_source_qualification_but_keeps_a_stale_cache(tmp_path: Path, qualification: str) -> None:
    from mediaforce.library.evidence_hosts import ControllerDigestWindow

    config = _config(tmp_path)
    item_id, path = _source(config)
    _discover(config)
    window = ControllerDigestWindow(admitted=False, next_opens_at=datetime.now(UTC) + timedelta(hours=1))
    with open_db(config.paths.db_path) as database:
        refresh_controller_digest_waits(database, window)
    if qualification == "receipt":
        with open_db(config.paths.db_path) as database:
            item = database.execute(select(library_items).where(library_items.c.id == item_id)).mappings().one()
            observation = observe_file(path)
            record_current_measurement(database, item, kind="media_fingerprint", summary=_summary("media_fingerprint"),
                provenance={"before": observation, "after": observation}, binding="observed")
    else:
        _cache_unadmitted_original(config, item_id)
        if qualification == "stale_cache":
            _touch_catalog_source(config, item_id, path)
    with open_db(config.paths.db_path) as database:
        refresh_controller_digest_waits(database, window)
        state = database.execute(select(library_item_evidence_state).where(
            library_item_evidence_state.c.library_item_id == item_id)).mappings().one()
        if qualification == "stale_cache":
            assert state["work_status"] == "waiting_window"
            assert runnable_evidence_work_count(database) == 0
        else:
            assert state["work_status"] == "queued"
            assert state["retry_not_before"] is None
            assert original_check_wait_reasons(database) == []
            assert runnable_evidence_work_count(database) == 1


def test_loop_discovery_marks_original_window_waits_before_returning(tmp_path: Path) -> None:
    from mediaforce.library.evidence_worker import _discover_originals

    config = _config(tmp_path)
    item_id, _path = _source(config)
    config.raw["remote_hosts"] = [{"host": "localhost", "mode": "local"}]
    deps = EvidenceWorkerDeps(load_config=lambda _path: config, analyze_evidence=Mock(), logger=Mock(),
        evidence_host_rows=lambda _config: [{"key": "localhost", "schedule_open": False}])
    assert _discover_originals(config, deps) == 1
    with open_db(config.paths.db_path) as database:
        _assert_unadmitted_window_wait(database, item_id)
        assert runnable_evidence_work_count(database) == 0


def test_a_finished_digest_is_kept_when_the_window_closes_after_its_last_read(tmp_path: Path) -> None:
    import hashlib
    from mediaforce.core.process_control import ManagedProcessController
    from mediaforce.library.evidence_hosts import ControllerDigestWindow

    config = _config(tmp_path)
    _item_id, path = _source(config)
    opened = datetime.now(UTC)
    closed = opened + timedelta(seconds=1)
    controller = ManagedProcessController()
    window = ControllerDigestWindow(closes_at=closed)
    with patch("mediaforce.library.evidence_hosts.datetime") as clock:
        clock.now.return_value = opened
        with window.guard(controller):
            observed = observe_file(path, full_digest=True, check_cancelled=controller.throw_if_cancelled)
            clock.now.return_value = closed
    assert observed["sha256"] == f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _controller_prefetch_input(config: MediaforceConfig) -> tuple[EvidenceWorkClaim, int]:
    _source(config, "current")
    waiting, _path = _source(config, "next")
    _discover(config)
    with open_db(config.paths.db_path) as database:
        claim = claim_next_evidence_work(database, worker_id="test", lease_seconds=60)
    assert claim is not None
    config.raw["remote_hosts"] = [{"host": "localhost", "mode": "local"}]
    return claim, waiting


@pytest.mark.parametrize("closes_after_selection", [False, True])
def test_prefetch_checks_controller_window_before_a_new_digest(tmp_path: Path, closes_after_selection: bool) -> None:
    from mediaforce.library.source_prefetch import SourceAdmissionPrefetcher

    config = _config(tmp_path)
    claim, waiting = _controller_prefetch_input(config)
    observed = threading.Event()
    logger = Mock()

    def rows(_config: MediaforceConfig) -> list[dict[str, Any]]:
        return [{"key": "localhost", "schedule_open": closes_after_selection and not observed.is_set()}]

    def observe_source(path: Path, **kwargs: Any) -> dict[str, Any]:
        observed.set()
        return observe_file(path, **kwargs)

    with patch("mediaforce.library.source_intake.observe_file", side_effect=observe_source) as observe, SourceAdmissionPrefetcher(
        stop_event=None, logger=logger, evidence_host_rows=rows
    ) as prefetch:
        prefetch.start(config, claim)
        prefetch.wait()
    assert not any(call.kwargs.get("full_digest") for call in observe.call_args_list)
    logger.error.assert_not_called()
    with open_db(config.paths.db_path) as database:
        assert load_source_evidence(database, waiting).original_version_id is None
        state = database.execute(select(library_item_evidence_state).where(
            library_item_evidence_state.c.library_item_id == waiting)).mappings().one()
        assert state["work_status"] == ("queued" if closes_after_selection else "waiting_window")
        assert state["attempt_count"] == 0


@pytest.mark.parametrize("finish_current_file", [True, False])
def test_prefetched_digest_obeys_window_close_policy(tmp_path: Path, finish_current_file: bool) -> None:
    from mediaforce.library.source_prefetch import SourceAdmissionPrefetcher

    config = _config(tmp_path)
    claim, waiting = _controller_prefetch_input(config)
    opened, closed = datetime.now(UTC), datetime.now(UTC) + timedelta(minutes=1)
    rows = lambda _config: [{"key": "localhost", "schedule_open": True,
        "schedule_closes_at": closed.isoformat(), "finish_current_file": finish_current_file}]
    with patch("mediaforce.library.evidence_hosts.datetime") as clock:
        clock.now.return_value = opened

        def observe(path: Path, **kwargs: Any) -> dict[str, Any]:
            if kwargs.get("full_digest"):
                clock.now.return_value = closed
                kwargs["check_cancelled"]()
            return observe_file(path, **kwargs)

        with patch("mediaforce.library.source_intake.observe_file", side_effect=observe), patch(
            "mediaforce.library.source_intake.probe_evidence_source", return_value=_probe()
        ), SourceAdmissionPrefetcher(stop_event=None, logger=Mock(), evidence_host_rows=rows) as prefetch:
            prefetch.start(config, claim)
            prefetch.wait()
    with open_db(config.paths.db_path) as database:
        assert (load_source_evidence(database, waiting).original_version_id is not None) == finish_current_file


def test_computer_schedule_is_checked_again_after_original_admission(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.raw["remote_hosts"] = [{"host": "computer.example", "mode": "ssh", "source_roots": {"tv": str(config.source_root_map["tv"])}}]
    item_id, _path = _source(config)
    _discover(config)
    rows = [{"key": "computer.example", "available": True, "capabilities": ["encode_queue"], "schedule_open": opened}
            for opened in (True, False)]
    analyze = Mock(side_effect=AssertionError("closed schedule"))
    with patch("mediaforce.encoding.remote_media.run_remote_command", side_effect=AssertionError("no real SSH")), patch(
        "mediaforce.library.source_intake.probe_evidence_source", return_value=_probe()
    ):
        assert process_evidence_queue_once(config_path=config.paths.config_path, config=config,
            deps=EvidenceWorkerDeps(read_probe=lambda *_args, **_kw: _probe(), load_config=lambda _path: config, analyze_evidence=analyze, logger=Mock(),
                evidence_host_rows=Mock(side_effect=[[row] for row in rows])))
    with open_db(config.paths.db_path) as db:
        state = db.execute(select(library_item_evidence_state).where(
            library_item_evidence_state.c.library_item_id == item_id,
            library_item_evidence_state.c.evidence_kind == "media_fingerprint")).mappings().one()
        assert state["work_status"] == "waiting_host"
        assert state["attempt_count"] == 0
        assert db.execute(select(source_admissions)).first() is not None
    analyze.assert_not_called()


def test_current_original_receipts_finish_without_a_computer(tmp_path: Path) -> None:
    config = _config(tmp_path)
    item_id, _path = _source(config)
    _measure_source_traits(config)
    config.raw["remote_hosts"] = [{"host": "offline.example", "mode": "ssh", "source_roots": {"tv": str(config.source_root_map["tv"])}}]
    assert _discover(config) == 1
    for qualification in range(2):
        with patch("mediaforce.library.evidence_worker._analysis_target", side_effect=AssertionError("stored result needs no computer")), patch(
            "mediaforce.library.source_intake.probe_evidence_source", return_value=_probe()
        ):
            assert process_evidence_queue_once(config_path=config.paths.config_path, config=config,
                deps=EvidenceWorkerDeps(read_probe=lambda *_args, **_kw: _probe(), load_config=lambda _path: config, analyze_evidence=Mock(), logger=Mock(),
                    evidence_host_rows=lambda _config: [{"key": "offline.example", "available": False}]))
        with open_db(config.paths.db_path) as db:
            state = db.execute(select(library_item_evidence_state).where(library_item_evidence_state.c.library_item_id == item_id,
                library_item_evidence_state.c.evidence_kind == "media_fingerprint")).mappings().one()
            assert state["work_status"] == "completed"
            if qualification == 0:
                queue_decision_evidence_work(db, config, "tv/source.mkv", library_item_ids=[item_id], evidence_kind="media_fingerprint",
                    work_reason=EVIDENCE_WORK_REASON_ORIGINAL_SOURCE, require_measurement=True, restart_terminal=True, start_new_batch=True)


def test_prefetch_skips_libraries_waiting_for_a_computer(tmp_path: Path) -> None:
    from mediaforce.library.source_prefetch import SourceAdmissionPrefetcher
    config = _config(tmp_path)
    config.raw["media"]["source_roots"]["movies"] = str(config.source_root_map["tv"])
    blocked, _ = _source(config, "blocked")
    current, _ = _source(config, "current")
    ready, _ready_path = _source(config, "ready")
    _discover(config)
    with open_db(config.paths.db_path) as db:
        db.execute(update(library_items).where(library_items.c.id.in_([current, ready])).values(media_root="movies"))
    with open_db(config.paths.db_path) as db:
        claim = claim_next_evidence_work(db, worker_id="test", lease_seconds=60, excluded_media_roots={"tv"})
    assert claim is not None and claim.library_item_id == current
    with patch("mediaforce.library.source_intake.probe_evidence_source", return_value=_probe()), SourceAdmissionPrefetcher(
        stop_event=None, logger=Mock()
    ) as prefetch:
        prefetch.start(config, claim, excluded_media_roots={"tv"})
        prefetch.wait()
    with open_db(config.paths.db_path) as db:
        assert load_source_evidence(db, ready).availability == "available"
        assert load_source_evidence(db, blocked).availability != "available"


def test_automatic_loop_overlaps_eligible_inputs_and_discovers_without_a_poll(tmp_path: Path) -> None:
    from mediaforce.library.evidence_worker import run_evidence_queue_until_blocked
    config = _config(tmp_path)
    config.raw["media"]["source_roots"]["movies"] = str(config.source_root_map["tv"])
    config.raw["remote_hosts"] = [{"host": "tv.example", "mode": "ssh", "allowed_libraries": ["tv"],
        "source_roots": {"tv": str(config.source_root_map["tv"])}}]
    first, first_path = _source(config, "first")
    offline, offline_path = _source(config, "offline")
    eligible = [_source(config, f"ready-{index}") for index in range(DEFAULT_EVIDENCE_BATCH_LIMIT)]
    with open_db(config.paths.db_path) as db:
        db.execute(update(library_items).where(library_items.c.id == offline).values(media_root="movies"))
    _discover(config)
    next_prepared = threading.Event()
    digests: list[tuple[Path, str]] = []
    measured: list[tuple[Path, str]] = []
    late: list[int] = []

    def observe(path: Path, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("full_digest"):
            digests.append((path, threading.current_thread().name))
            if path == eligible[0][1]:
                next_prepared.set()
        return observe_file(path, **kwargs)

    def analyze(path: Path, kind: str, **_kwargs: Any) -> dict[str, Any]:
        if path == first_path and not late:
            late.append(_source(config, "late-download")[0])
            assert next_prepared.wait(5)
        measured.append((path, kind))
        return _summary(kind)

    deps = EvidenceWorkerDeps(read_probe=lambda *_args, **_kw: _probe(), load_config=lambda _path: config, analyze_evidence=analyze, logger=Mock(),
        evidence_host_rows=lambda _config: [{"key": "tv.example", "available": True, "capabilities": ["encode_queue"]}])
    with patch("mediaforce.library.source_intake.probe_evidence_source", return_value=_probe()), patch(
        "mediaforce.library.source_intake.observe_file", side_effect=observe
    ), patch("mediaforce.library.evidence_worker._analysis_target",
             side_effect=lambda _config, _claim, path, *_args: SimpleNamespace(source_path=path, command_runner=None)):
        result = run_evidence_queue_until_blocked(config_path=config.paths.config_path, config=config, deps=deps, max_work_items=None)
    with open_db(config.paths.db_path) as db:
        for item_id in [first, *(item_id for item_id, _path in eligible), *late]:
            assert {trait.tier for trait in load_source_evidence(db, item_id).traits.values()} == {"probable"}
        assert load_source_evidence(db, offline).availability != "available"
        pending = db.execute(select(library_item_evidence_state).where(library_item_evidence_state.c.library_item_id == offline)).mappings().one()
        assert pending["work_status"] == "waiting_host"
        assert pending["attempt_count"] == 0
    assert offline_path not in {path for path, _thread in digests}
    assert any(path == eligible[0][1] and thread.startswith("original-admission") for path, thread in digests)
    assert len(measured) == (len(eligible) + 1 + len(late)) * len(EVIDENCE_KINDS)
    assert result["processed_count"] == len(measured)


@pytest.mark.parametrize("control", ["pause", "cancel", "shutdown"])
def test_automatic_loop_preserves_real_stop_controls(tmp_path: Path, control: str) -> None:
    from mediaforce.library.evidence_queue import pause_evidence_queue
    from mediaforce.library.evidence_worker import run_evidence_queue_until_blocked
    config = _config(tmp_path)
    _source(config, "first")
    _source(config, "second")
    _discover(config)
    late, _path = _source(config, "late")
    stop = threading.Event()
    measured: list[Path] = []
    with open_db(config.paths.db_path) as db:
        batch_id = db.execute(select(library_item_evidence_state.c.work_batch_id).limit(1)).scalar_one()

    def analyze(path: Path, kind: str, **_kwargs: Any) -> dict[str, Any]:
        measured.append(path)
        if control == "shutdown":
            stop.set()
        else:
            with open_db(config.paths.db_path) as database:
                (pause_evidence_queue if control == "pause" else cancel_evidence_queue)(database)
        return _summary(kind)

    deps = EvidenceWorkerDeps(read_probe=lambda *_args, **_kw: _probe(), load_config=lambda _path: config, analyze_evidence=analyze, logger=Mock())
    with patch("mediaforce.library.source_intake.probe_evidence_source", return_value=_probe()), patch(
        "mediaforce.library.evidence_worker._analysis_target",
        side_effect=lambda _config, _claim, path, *_args: SimpleNamespace(source_path=path, command_runner=None)
    ):
        run_evidence_queue_until_blocked(config_path=config.paths.config_path, config=config, deps=deps,
            max_work_items=None, stop_event=stop)
    assert len(measured) == 1
    with open_db(config.paths.db_path) as db:
        assert db.execute(select(library_item_evidence_state.c.work_batch_id).where(
            library_item_evidence_state.c.work_batch_id.is_not(None)).distinct()).scalar_one() == batch_id
        assert db.execute(select(library_item_evidence_state.c.library_item_id).where(
            library_item_evidence_state.c.library_item_id == late, library_item_evidence_state.c.work_batch_id.is_not(None)
        )).first() is None


def test_automatic_loop_stops_after_one_missing_root_observation(tmp_path: Path) -> None:
    from mediaforce.library.evidence_worker import run_evidence_queue_until_blocked
    config = _config(tmp_path)
    _source(config, "first")
    _source(config, "second")
    _discover(config)
    stop = threading.Event()
    observations: list[int] = []

    def missing_root(_config: MediaforceConfig, claim: EvidenceWorkClaim, _path: Path) -> tuple[str, None]:
        observations.append(claim.library_item_id)
        if len(observations) > 1:
            stop.set()
        return "root_unavailable", None

    deps = EvidenceWorkerDeps(read_probe=lambda *_args, **_kw: _probe(), load_config=lambda _path: config, analyze_evidence=Mock(), logger=Mock())
    clock = Mock(wraps=datetime)
    clock.now.return_value = datetime.now(UTC).replace(microsecond=0)
    with patch("mediaforce.library.evidence_worker.datetime", clock), patch(
        "mediaforce.library.evidence_queue.datetime", clock
    ), patch("mediaforce.library.evidence_worker._source_fingerprint", side_effect=missing_root):
        result = run_evidence_queue_until_blocked(config_path=config.paths.config_path, config=config, deps=deps,
            max_work_items=None, max_seconds=5, stop_event=stop)
    assert len(observations) == 1
    assert result["processed_count"] == 1
    with open_db(config.paths.db_path) as db:
        rows = db.execute(select(library_item_evidence_state)).mappings().all()
        assert {row["work_status"] for row in rows} == {"waiting_source"}
        assert {row["attempt_count"] for row in rows} == {0}


def test_missing_root_does_not_hold_a_healthy_original_at_lower_priority(tmp_path: Path) -> None:
    from mediaforce.library.evidence_worker import run_evidence_queue_until_blocked
    config = _config(tmp_path)
    config.raw["media"]["source_roots"]["movies"] = str(tmp_path / "missing-mount")
    offline = [_source(config, f"offline-{index}")[0] for index in range(2)]
    healthy, _path = _source(config, "healthy")
    with open_db(config.paths.db_path) as db:
        db.execute(update(library_items).where(library_items.c.id.in_(offline)).values(media_root="movies"))
    _discover(config)
    with open_db(config.paths.db_path) as db:
        db.execute(update(library_item_evidence_state).where(library_item_evidence_state.c.library_item_id.in_(offline)).values(work_priority=0))
    deps = EvidenceWorkerDeps(read_probe=lambda *_args, **_kw: _probe(), load_config=lambda _path: config,
        analyze_evidence=lambda _path, kind, **_kwargs: _summary(kind), logger=Mock())
    with patch("mediaforce.library.source_intake.probe_evidence_source", return_value=_probe()):
        run_evidence_queue_until_blocked(config_path=config.paths.config_path, config=config, deps=deps, max_work_items=None)
    with open_db(config.paths.db_path) as db:
        assert {trait.tier for trait in load_source_evidence(db, healthy).traits.values()} == {"probable"}
        waiting = db.execute(select(library_item_evidence_state).where(library_item_evidence_state.c.library_item_id.in_(offline))).mappings().all()
        assert {row["work_status"] for row in waiting} == {"waiting_source"}
        assert {row["attempt_count"] for row in waiting} == {0}


@pytest.mark.parametrize("state,original,can_claim", [
    (EVIDENCE_STATE_CURRENT, True, False),
    (EVIDENCE_STATE_CURRENT, False, True),
    (EVIDENCE_STATE_CLASSIFICATION_REQUIRED, True, False),
    (EVIDENCE_STATE_CLASSIFICATION_REQUIRED, False, True),
])
def test_source_wait_preserves_only_metadata_fast_paths(tmp_path: Path, state: str, original: bool, can_claim: bool) -> None:
    config = _config(tmp_path)
    anchor, _path = _source(config, "anchor")
    candidate, _other = _source(config, "candidate")
    _discover(config)
    with open_db(config.paths.db_path) as db:
        db.execute(update(library_item_evidence_state).where(library_item_evidence_state.c.library_item_id == anchor).values(
            work_status="waiting_source", retry_not_before="9999-01-01T00:00:00+00:00"))
        db.execute(update(library_item_evidence_state).where(library_item_evidence_state.c.library_item_id == candidate).values(
            state=state, work_reason="original_source_measurement" if original else "operator_scope"))
    with open_db(config.paths.db_path) as db:
        claim = claim_next_evidence_work(db, worker_id="test", lease_seconds=60)
    assert (claim is not None) == can_claim
    if claim is not None:
        assert claim.library_item_id == candidate


@pytest.mark.parametrize("offline", [True, False])
def test_current_unreceipted_originals_wait_or_prefetch_by_computer_readiness(tmp_path: Path, offline: bool) -> None:
    from mediaforce.library.evidence_worker import run_evidence_queue_until_blocked
    config = _config(tmp_path)
    config.raw["media"]["source_roots"]["movies"] = str(config.source_root_map["tv"])
    first, first_path = _source(config, "first")
    legacy, legacy_path = _source(config)
    with open_db(config.paths.db_path) as db:
        db.execute(update(library_items).where(library_items.c.id == legacy).values(
            media_root="movies", media_fingerprint_json=json.dumps(_summary("media_fingerprint"))))
    config.raw["remote_hosts"] = [{"host": "worker.example", "mode": "ssh", "allowed_libraries": ["tv"] if offline else ["tv", "movies"],
        "source_roots": {"tv": str(config.source_root_map["tv"]), "movies": str(config.source_root_map["movies"])}}]
    _discover(config)
    with open_db(config.paths.db_path) as db:
        assert db.execute(select(library_item_evidence_state.c.state).where(library_item_evidence_state.c.library_item_id == legacy,
            library_item_evidence_state.c.evidence_kind == "media_fingerprint")).scalar_one() == EVIDENCE_STATE_CURRENT
    prepared = threading.Event()
    digests: list[tuple[Path, str]] = []

    def observe(path: Path, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("full_digest"):
            digests.append((path, threading.current_thread().name))
            if path == legacy_path:
                prepared.set()
        return observe_file(path, **kwargs)

    def analyze(path: Path, kind: str, **_kwargs: Any) -> dict[str, Any]:
        if path == first_path and not offline:
            assert prepared.wait(5)
        return _summary(kind)

    deps = EvidenceWorkerDeps(read_probe=lambda *_args, **_kw: _probe(), load_config=lambda _path: config, analyze_evidence=analyze, logger=Mock(),
        evidence_host_rows=lambda _config: [{"key": "worker.example", "available": True, "capabilities": ["encode_queue"]}])
    with patch("mediaforce.library.source_intake.probe_evidence_source", return_value=_probe()), patch(
        "mediaforce.library.source_intake.observe_file", side_effect=observe
    ), patch("mediaforce.library.evidence_worker._analysis_target",
        side_effect=lambda _config, _claim, path, *_args: SimpleNamespace(source_path=path, command_runner=None)):
        run_evidence_queue_until_blocked(config_path=config.paths.config_path, config=config, deps=deps, max_work_items=None)
    with open_db(config.paths.db_path) as db:
        row = db.execute(select(library_item_evidence_state).where(library_item_evidence_state.c.library_item_id == legacy,
            library_item_evidence_state.c.evidence_kind == "media_fingerprint")).mappings().one()
        assert row["work_status"] == ("waiting_host" if offline else "completed")
        if offline:
            assert row["attempt_count"] == 0
            assert legacy_path not in {path for path, _thread in digests}
        else:
            assert any(path == legacy_path and thread.startswith("original-admission") for path, thread in digests)


def test_unavailable_urgent_work_queued_during_admission_does_not_interrupt_it(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _source(config, "first")
    urgent, _path = _source(config, "urgent")
    _discover(config)
    with open_db(config.paths.db_path) as db:
        db.execute(update(library_items).where(library_items.c.id == urgent).values(media_root="movies"))
        db.execute(update(library_item_evidence_state).where(library_item_evidence_state.c.library_item_id == urgent).values(work_priority=0))
        batch = db.execute(select(library_item_evidence_state.c.work_batch_id).limit(1)).scalar_one()
        assert original_intake_hold(db, batch_id=batch, priority=EVIDENCE_WORK_PRIORITY_REPRESENTATIVE, excluded_media_roots={"movies"}) is None
        assert original_intake_hold(db, batch_id=batch, priority=EVIDENCE_WORK_PRIORITY_REPRESENTATIVE) == "higher_priority"


def test_shutdown_during_computer_readiness_does_not_claim_or_mark_work(tmp_path: Path) -> None:
    from mediaforce.library.evidence_worker import run_evidence_queue_until_blocked
    config = _config(tmp_path)
    _source(config)
    _discover(config)
    config.raw["remote_hosts"] = [{"host": "worker.example", "mode": "ssh", "source_roots": {"tv": str(config.source_root_map["tv"])}}]
    stop = threading.Event()

    def rows(_config: MediaforceConfig) -> list[dict[str, Any]]:
        stop.set()
        return []

    result = run_evidence_queue_until_blocked(config_path=config.paths.config_path, config=config,
        deps=EvidenceWorkerDeps(read_probe=lambda *_args, **_kw: _probe(), load_config=lambda _path: config, analyze_evidence=Mock(), logger=Mock(), evidence_host_rows=rows),
        max_work_items=None, stop_event=stop)
    assert result["stop_reason"] == "shutdown"
    assert result["processed_count"] == 0
    with open_db(config.paths.db_path) as db:
        assert db.execute(select(library_item_evidence_state.c.work_status)).scalar_one() == "queued"


def test_original_assessment_is_final_and_measurements_use_one_queue(tmp_path: Path) -> None:
    config = _config(tmp_path)
    item_id, _path = _source(config)
    assert _discover(config) == 1
    assert _work(config)
    assert _work(config)
    with open_db(config.paths.db_path) as database:
        assert {trait.tier for trait in load_source_evidence(database, item_id).traits.values()} == {"probable"}
        before = database.execute(select(func.count()).select_from(source_admissions)).scalar_one()
        revision = database.execute(select(library_items.c.source_revision)).scalar_one()
        assert queue_original_intake(database, config) == 0
        assert runnable_evidence_work_count(database) == 0
        assert database.execute(select(func.count()).select_from(source_admissions)).scalar_one() == before
        assert database.execute(select(library_items.c.source_revision)).scalar_one() == revision


@pytest.mark.parametrize("prior_success", [True, False])
def test_unknown_fingerprint_never_replaces_successful_original_traits(tmp_path: Path, prior_success: bool) -> None:
    config = _config(tmp_path)
    item_id, path = _source(config)
    assert _discover(config) == 1
    unknown = _summary("media_fingerprint")
    unknown["analysis"]["interrupted_by_timeout"] = True
    unknown["decision"] = {"status": "unknown"}
    def analyzer(_path: Path, kind: str, **_kwargs: object) -> dict[str, Any]:
        return unknown if kind == "media_fingerprint" and not prior_success else _summary(kind)
    assert _work(config, analyzer, max_attempts=1)
    assert _work(config, analyzer, max_attempts=1)
    with open_db(config.paths.db_path) as database:
        earlier = load_source_evidence(database, item_id).traits["media_fingerprint"]
        item = cast(dict[str, Any], dict(database.execute(select(library_items).where(library_items.c.id == item_id)).mappings().one()))
        observation = observe_file(path)
        record_current_measurement(database, item, kind="media_fingerprint", summary=unknown, binding="observed",
                                   provenance={"before": observation, "after": observation})
        selected = load_source_evidence(database, item_id).traits["media_fingerprint"]

    assert selected == earlier
    assert selected.tier == ("probable" if prior_success else "unknown")


def test_fresh_queue_receipts_are_frozen_and_not_reanalyzed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    item_id, _path = _source(config)
    _measure_source_traits(config)
    with open_db(config.paths.db_path) as database:
        receipts = set(database.execute(select(source_measurements.c.measurement_id)).scalars())
    assert _discover(config) == 1
    assert _work(config, Mock(side_effect=AssertionError("must not reanalyze")))
    with open_db(config.paths.db_path) as database:
        assert {trait.tier for trait in load_source_evidence(database, item_id).traits.values()} == {"probable"}
        checks = json.loads(database.execute(select(source_admissions.c.checks_json)).scalar_one())
        assert set(checks["measurement_ids"]) == receipts
        assert queue_original_intake(database, config) == 0


def test_stale_catalog_uses_queue_retry_and_does_not_hold_other_files(tmp_path: Path) -> None:
    config = _config(tmp_path)
    first_id, path = _source(config, "first")
    second_id, _second = _source(config, "second")
    path.write_bytes(b"changed since scan")
    assert _discover(config) == 2
    assert _work(config)
    assert _work(config)
    with open_db(config.paths.db_path) as database:
        first = (
            database.execute(select(library_item_evidence_state).where(library_item_evidence_state.c.library_item_id == first_id))
            .mappings()
            .one()
        )
        assert first["work_status"] == "failed"
        assert load_source_evidence(database, second_id).original_version_id is not None


def test_backoff_and_finished_cancel_do_not_starve_new_downloads(tmp_path: Path) -> None:
    config = _config(tmp_path)
    first_id, _path = _source(config, "first")
    assert _discover(config) == 1
    with open_db(config.paths.db_path) as database:
        database.execute(
            update(library_item_evidence_state).values(work_status="waiting_host", retry_not_before="9999-01-01T00:00:00+00:00")
        )
        assert runnable_evidence_work_count(database) == 0
    second_id, _second = _source(config, "second")
    assert _discover(config) == 1
    assert _work(config)
    with open_db(config.paths.db_path) as database:
        assert load_source_evidence(database, second_id).original_version_id is not None
        cancel_evidence_queue(database)
    third_id, _third = _source(config, "third")
    assert _discover(config) == 1
    assert _work(config)
    with open_db(config.paths.db_path) as database:
        assert load_source_evidence(database, third_id).original_version_id is not None
        assert (
            database.execute(
                select(library_item_evidence_state.c.work_status).where(library_item_evidence_state.c.library_item_id == first_id)
            ).scalar_one()
            == "cancelled"
        )


def test_prepare_preserves_background_lease_and_retry_state(tmp_path: Path) -> None:
    config = _config(tmp_path)
    first_id, _path = _source(config, "first")
    second_id, _second = _source(config, "second")
    assert _discover(config) == 2
    with open_db(config.paths.db_path) as database:
        claim = claim_next_evidence_work(database, worker_id="fixture-worker", lease_seconds=60)
        assert claim is not None
        database.execute(
            update(library_item_evidence_state)
            .where(library_item_evidence_state.c.library_item_id == second_id)
            .values(work_status="retry_wait", attempt_count=2, retry_not_before="9999-01-01T00:00:00+00:00")
        )
        before = dict(
            database.execute(select(library_item_evidence_state).where(library_item_evidence_state.c.library_item_id == first_id))
            .mappings()
            .one()
        )
        report = start_evidence_work(database, config, "tv/second.mkv")
        after = dict(
            database.execute(select(library_item_evidence_state).where(library_item_evidence_state.c.library_item_id == first_id))
            .mappings()
            .one()
        )
        retry = (
            database.execute(
                select(library_item_evidence_state).where(
                    library_item_evidence_state.c.library_item_id == second_id,
                    library_item_evidence_state.c.evidence_kind == "media_fingerprint",
                )
            )
            .mappings()
            .one()
        )
        assert before == after
        assert retry["attempt_count"] == 2 and retry["work_status"] == "retry_wait"
        assert report["is_paused"]


def test_newer_measurement_after_stat_change_replaces_old_lineage(tmp_path: Path) -> None:
    config = _config(tmp_path)
    item_id, path = _source(config)
    _discover(config)
    _work(config)
    _work(config)
    os.chmod(path, 0o640)
    with open_db(config.paths.db_path) as database:
        from mediaforce.library.evidence_queue import EVIDENCE_WORK_REASON_ORIGINAL_SOURCE, queue_decision_evidence_work

        queue_decision_evidence_work(
            database,
            config,
            "tv/source.mkv",
            library_item_ids=[item_id],
            evidence_kind="media_fingerprint",
            work_reason=EVIDENCE_WORK_REASON_ORIGINAL_SOURCE,
            require_measurement=True,
            start_new_batch=True,
        )
        database.execute(
            update(library_item_evidence_state)
            .where(library_item_evidence_state.c.evidence_kind == "media_fingerprint")
            .values(state="analysis_required")
        )
    _work(config, lambda _path, kind, **_kwargs: _summary(kind, "new-host"))
    with open_db(config.paths.db_path) as database:
        trait = load_source_evidence(database, item_id).traits["media_fingerprint"]
        assert trait.tier == "probable"
        assert trait.summary is not None
        assert trait.summary["analysis"]["tool"]["host"] == "new-host"
        assert database.execute(select(func.count()).select_from(source_measurements)).scalar_one() >= 3


def test_short_reads_and_outside_sample_mutations_are_not_stable_observations(tmp_path: Path) -> None:
    path = tmp_path / "source"
    path.write_bytes(b"x" * 1048576)
    before = observe_file(path, full_digest=True)
    stat = path.stat()
    with path.open("r+b") as handle:
        handle.seek(100000)
        handle.write(b"y")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    after = observe_file(path, full_digest=True)
    assert before["sample_fingerprint"] == after["sample_fingerprint"]
    assert before["sha256"] != after["sha256"]
    with patch("os.pread", return_value=b""):
        with pytest.raises(OSError):
            observe_file(path, full_digest=True)


def test_assessment_commit_failure_retries_once_without_a_second_admission_attempt(tmp_path: Path) -> None:
    import mediaforce.library.source_intake as intake

    config = _config(tmp_path)
    first_id, _first = _source(config, "first")
    second_id, _second = _source(config, "second")
    _discover(config)
    original_commit = intake.commit_readable_source
    attempts = []

    def fail_first(db: DBClient, item: dict[str, Any], identity: dict[str, Any], probe: dict[str, Any], **kwargs: Any) -> str:
        attempts.append(item["id"])
        if item["id"] == first_id:
            raise RuntimeError("file changed before commit")
        return original_commit(db, item, identity, probe, **kwargs)

    with patch.object(intake, "commit_readable_source", side_effect=fail_first):
        _work(config)
    assert attempts == [first_id]
    with open_db(config.paths.db_path) as database:
        database.execute(
            update(library_item_evidence_state)
            .where(library_item_evidence_state.c.library_item_id == first_id)
            .values(retry_not_before="9999-01-01T00:00:00+00:00")
        )
    _work(config)
    with open_db(config.paths.db_path) as database:
        assert load_source_evidence(database, second_id).original_version_id is not None
        assert database.execute(select(func.count()).select_from(source_admissions)).scalar_one() == 1


def test_pre_admission_receipt_with_changed_stat_is_not_frozen(tmp_path: Path) -> None:
    config = _config(tmp_path)
    item_id, path = _source(config)
    with open_db(config.paths.db_path) as database:
        rebuild_library_item_evidence_states(database)
        start_evidence_work(database, config, "tv/source.mkv")
        resume_evidence_queue(database)
    _work(config)
    _work(config)
    os.chmod(path, 0o640)
    _discover(config)
    _work(config)
    with open_db(config.paths.db_path) as database:
        checks = json.loads(database.execute(select(source_admissions.c.checks_json)).scalar_one())
        assert not checks["measurement_ids"]
        assert load_source_evidence(database, item_id).traits["media_fingerprint"].tier == "probable"


def test_digest_is_bounded_by_observed_length_even_while_file_grows(tmp_path: Path) -> None:
    import hashlib
    from mediaforce.core.utils import descriptor_sha256, FILE_DIGEST_CHUNK_BYTES

    path = tmp_path / "growing"
    original = b"x" * (FILE_DIGEST_CHUNK_BYTES + 3)
    path.write_bytes(original)
    read = os.read

    def read_and_extend(descriptor: int, count: int) -> bytes:
        chunk = read(descriptor, count)
        with path.open("ab") as append:
            append.write(b"new bytes")
        return chunk

    with path.open("rb") as handle, patch("os.read", side_effect=read_and_extend):
        assert descriptor_sha256(handle.fileno(), size_bytes=len(original)) == f"sha256:{hashlib.sha256(original).hexdigest()}"


def test_hash_has_no_wall_clock_ceiling(tmp_path: Path) -> None:
    config = _config(tmp_path)
    item_id, _path = _source(config)
    _discover(config)
    ticks = iter([index * 1800.0 for index in range(100)])
    with patch("time.monotonic", side_effect=lambda: next(ticks)):
        assert _work(config)
    with open_db(config.paths.db_path) as database:
        assert load_source_evidence(database, item_id).traits["media_fingerprint"].tier == "probable"


def test_new_download_is_claimed_before_a_due_host_retry(tmp_path: Path) -> None:
    config = _config(tmp_path)
    first_id, _first = _source(config, "first")
    _discover(config)
    with open_db(config.paths.db_path) as database:
        database.execute(
            update(library_item_evidence_state)
            .where(library_item_evidence_state.c.library_item_id == first_id)
            .values(work_status="waiting_host", retry_not_before="2000-01-01T00:00:00+00:00")
        )
    second_id, _second = _source(config, "second")
    _discover(config)
    with open_db(config.paths.db_path) as database:
        claim = claim_next_evidence_work(database, worker_id="fixture", lease_seconds=60)
        assert claim is not None and claim.library_item_id == second_id


@pytest.mark.parametrize("kind", ["media_fingerprint", "cadence_analysis"])
def test_cancelled_background_checks_retry_after_cooldown(tmp_path: Path, kind: EvidenceKind) -> None:
    config = _config(tmp_path)
    item_id, _path = _source(config)
    _discover(config)

    def analyze(_path: Path, requested: str, **_kwargs: object) -> dict[str, Any]:
        if requested == kind:
            raise RuntimeError("temporary analysis failure")
        return _summary(requested)

    _work(config, analyze)
    _work(config, analyze)
    with open_db(config.paths.db_path) as database:
        database.execute(
            update(library_item_evidence_state)
            .where(library_item_evidence_state.c.evidence_kind == kind)
            .values(work_status="cancelled", retry_not_before="9999-01-01T00:00:00+00:00", attempt_count=3)
        )
    assert _discover(config) == 0
    with open_db(config.paths.db_path) as database:
        database.execute(
            update(library_item_evidence_state)
            .where(library_item_evidence_state.c.evidence_kind == kind)
            .values(retry_not_before="2000-01-01T00:00:00+00:00")
        )
    assert _discover(config) == 1
    assert _work(config)
    with open_db(config.paths.db_path) as database:
        assert load_source_evidence(database, item_id).traits[kind].tier == "probable"
        assert (
            database.execute(
                select(library_item_evidence_state.c.work_status).where(library_item_evidence_state.c.evidence_kind == kind)
            ).scalar_one()
            == "completed"
        )


def test_failed_probe_reuses_digest_without_admitting_original(tmp_path: Path) -> None:
    import mediaforce.library.source_intake as intake
    from mediaforce.core.process_control import ManagedProcessController

    config = _config(tmp_path)
    item_id, _path = _source(config)
    observations = Mock(wraps=observe_file)
    with (
        patch("mediaforce.library.source_intake.observe_file", observations),
        patch("mediaforce.library.source_intake.probe_evidence_source", side_effect=RuntimeError("probe failed")),
    ):
        with pytest.raises(RuntimeError, match="probe failed"):
            intake.assess_source_input(config, item_id, ManagedProcessController(), lambda: None)
    with open_db(config.paths.db_path) as database:
        assert database.execute(select(func.count()).select_from(source_admissions)).scalar_one() == 0
    observations.reset_mock()
    with (
        patch("mediaforce.library.source_intake.observe_file", observations),
        patch("mediaforce.library.source_intake.probe_evidence_source", return_value=_probe()),
    ):
        intake.assess_source_input(config, item_id, ManagedProcessController(), lambda: None)
    assert not any(call.kwargs.get("full_digest") for call in observations.call_args_list)


def test_next_original_admission_overlaps_analysis_and_digest_is_reused(tmp_path: Path) -> None:
    from mediaforce.library.evidence_worker import run_evidence_queue_until_blocked

    config = _config(tmp_path)
    first_id, first_path = _source(config, "first")
    second_id, second_path = _source(config, "second")
    _discover(config)
    analyzing = threading.Event()
    prefetched = threading.Event()
    digests: list[Path] = []
    measured: list[tuple[Path, str]] = []

    def observe(path: Path, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("full_digest"):
            digests.append(path)
            if path == second_path:
                assert analyzing.wait(5)
                prefetched.set()
        return observe_file(path, **kwargs)

    def analyze(path: Path, kind: str, **_kwargs: Any) -> dict[str, Any]:
        if path == first_path:
            analyzing.set()
            assert prefetched.wait(5)
        measured.append((path, kind))
        return _summary(kind)

    deps = EvidenceWorkerDeps(read_probe=lambda *_args, **_kw: _probe(), load_config=lambda _path: config, analyze_evidence=analyze, logger=Mock())
    with (
        patch("mediaforce.library.source_intake.observe_file", side_effect=observe),
        patch("mediaforce.library.source_intake.probe_evidence_source", return_value=_probe()),
        patch("mediaforce.library.evidence_worker._analysis_target",
              side_effect=lambda _config, _claim, path, *_args: SimpleNamespace(source_path=path, command_runner=None)),
    ):
        result = run_evidence_queue_until_blocked(config_path=config.paths.config_path, config=config, deps=deps, max_work_items=4)

    assert result["processed_count"] == 4
    assert digests == [first_path, second_path]
    assert set(measured) == {(path, kind) for path in (first_path, second_path) for kind in ("media_fingerprint", "cadence_analysis")}
    with open_db(config.paths.db_path) as database:
        for item_id in (first_id, second_id):
            assert load_source_evidence(database, item_id).availability == "available"
        receipts = database.execute(select(source_measurements.c.summary_json)).scalars().all()
    assert sorted(json.dumps(json.loads(raw)["analysis"], sort_keys=True) for raw in receipts) == sorted(
        json.dumps(_summary(kind)["analysis"], sort_keys=True) for _, kind in measured
    )


def test_shutdown_interrupts_prefetched_digest_without_admission_or_queue_transition(tmp_path: Path) -> None:
    from mediaforce.library.source_prefetch import SourceAdmissionPrefetcher

    config = _config(tmp_path)
    _source(config, "first")
    second_id, _path = _source(config, "second")
    _discover(config)
    with open_db(config.paths.db_path) as database:
        claim = claim_next_evidence_work(database, worker_id="fixture", lease_seconds=60)
    assert claim is not None
    stopped = threading.Event()

    def observe(path: Path, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("full_digest"):
            stopped.set()
            kwargs["check_cancelled"]()
        return observe_file(path, **kwargs)

    with patch("mediaforce.library.source_intake.observe_file", side_effect=observe):
        with SourceAdmissionPrefetcher(stop_event=stopped, logger=Mock()) as prefetch:
            prefetch.start(config, claim)
            prefetch.wait()
    assert stopped.is_set()
    with open_db(config.paths.db_path) as database:
        assert database.execute(select(func.count()).select_from(source_admissions)).scalar_one() == 0
        assert database.execute(select(library_item_evidence_state.c.work_status).where(
            library_item_evidence_state.c.library_item_id == second_id,
        )).scalars().all() == ["queued"]


@pytest.mark.parametrize("interruption", ["pause", "cancel", "priority", "other_runner"])
def test_prefetch_rechecks_runnability_under_commit_lock(tmp_path: Path, interruption: str) -> None:
    from mediaforce.core.db_tables import source_versions
    from mediaforce.library.evidence_queue import pause_evidence_queue, transition_evidence_work
    from mediaforce.library.source_prefetch import SourceAdmissionPrefetcher

    config = _config(tmp_path)
    _source(config, "first")
    second_id, second_path = _source(config, "second")
    _discover(config)
    with open_db(config.paths.db_path) as database:
        claim = claim_next_evidence_work(database, worker_id="first-runner", lease_seconds=60)
        versions_before = database.execute(select(func.count()).select_from(source_versions)).scalar_one()
    assert claim is not None
    owned_claim: EvidenceWorkClaim = claim
    interrupted = threading.Event()

    def observe(path: Path, **kwargs: Any) -> dict[str, Any]:
        result = observe_file(path, **kwargs)
        if path == second_path and kwargs.get("full_digest"):
            with open_db(config.paths.db_path) as mutation_db:
                if interruption == "pause":
                    pause_evidence_queue(mutation_db)
                elif interruption == "cancel":
                    cancel_evidence_queue(mutation_db)
                elif interruption == "priority":
                    mutation_db.execute(update(library_item_evidence_state).where(
                        library_item_evidence_state.c.library_item_id == second_id,
                    ).values(work_priority=owned_claim.work_priority - 1))
                else:
                    transition_evidence_work(mutation_db, owned_claim, work_status="completed")
                    mutation_db.commit()
                    assert claim_next_evidence_work(mutation_db, worker_id="second-runner", lease_seconds=60) is not None
            interrupted.set()
        return result

    with patch("mediaforce.library.source_intake.observe_file", side_effect=observe):
        with SourceAdmissionPrefetcher(stop_event=None, logger=Mock()) as prefetch:
            prefetch.start(config, claim)
            prefetch.wait()
    assert interrupted.is_set()
    with open_db(config.paths.db_path) as database:
        assert database.execute(select(func.count()).select_from(source_versions)).scalar_one() == versions_before
        assert database.execute(select(func.count()).select_from(source_admissions)).scalar_one() == 0


def test_prefetch_does_not_record_bytes_changed_since_scan(tmp_path: Path) -> None:
    from mediaforce.core.db_tables import source_versions
    from mediaforce.library.source_prefetch import SourceAdmissionPrefetcher

    config = _config(tmp_path)
    _source(config, "first")
    _second_id, path = _source(config, "second")
    _discover(config)
    with open_db(config.paths.db_path) as database:
        claim = claim_next_evidence_work(database, worker_id="fixture", lease_seconds=60)
        before = database.execute(select(func.count()).select_from(source_versions)).scalar_one()
    assert claim is not None
    path.write_bytes(b"replacement bytes")
    with SourceAdmissionPrefetcher(stop_event=None, logger=Mock()) as prefetch:
        prefetch.start(config, claim)
        prefetch.wait()
    with open_db(config.paths.db_path) as database:
        assert database.execute(select(func.count()).select_from(source_versions)).scalar_one() == before
        assert database.execute(select(func.count()).select_from(source_admissions)).scalar_one() == 0


def test_prefetch_setup_failure_does_not_fail_current_file(tmp_path: Path) -> None:
    from mediaforce.library.source_prefetch import SourceAdmissionPrefetcher

    config = _config(tmp_path)
    _source(config)
    _discover(config)
    with open_db(config.paths.db_path) as database:
        claim = claim_next_evidence_work(database, worker_id="fixture", lease_seconds=60)
    assert claim is not None
    logger = Mock()
    with patch("mediaforce.library.source_prefetch.open_db", side_effect=RuntimeError("fixture setup failed")):
        with SourceAdmissionPrefetcher(stop_event=None, logger=logger) as prefetch:
            prefetch.start(config, claim)
            prefetch.wait()
    logger.error.assert_called_once()
    with open_db(config.paths.db_path) as database:
        assert database.execute(select(library_item_evidence_state.c.attempt_count)).scalar_one() == 0


def test_prefetch_exit_interrupts_its_digest_after_unexpected_runner_error(tmp_path: Path) -> None:
    from mediaforce.core.process_control import ProcessCancelledError
    from mediaforce.library.source_prefetch import SourceAdmissionPrefetcher

    config = _config(tmp_path)
    _source(config, "first")
    _source(config, "second")
    _discover(config)
    with open_db(config.paths.db_path) as database:
        claim = claim_next_evidence_work(database, worker_id="fixture", lease_seconds=60)
    assert claim is not None
    owned_claim: EvidenceWorkClaim = claim
    started = threading.Event()
    interrupted = threading.Event()

    def observe(path: Path, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("full_digest"):
            started.set()
            try:
                for _ in range(100):
                    kwargs["check_cancelled"]()
                    interrupted.wait(.01)
            except ProcessCancelledError:
                interrupted.set()
                raise
        return observe_file(path, **kwargs)

    def fail_runner() -> None:
        with SourceAdmissionPrefetcher(stop_event=None, logger=Mock()) as prefetch:
            prefetch.start(config, owned_claim)
            assert started.wait(5)
            raise RuntimeError("unexpected runner failure")

    with patch("mediaforce.library.source_intake.observe_file", side_effect=observe):
        with pytest.raises(RuntimeError):
            fail_runner()
    assert interrupted.is_set()


def test_one_item_run_does_not_prepare_an_extra_original(tmp_path: Path) -> None:
    from mediaforce.library.evidence_worker import run_evidence_queue_until_blocked

    config = _config(tmp_path)
    _source(config, "first")
    _source(config, "second")
    _discover(config)
    deps = EvidenceWorkerDeps(read_probe=lambda *_args, **_kw: _probe(), load_config=lambda _path: config, analyze_evidence=lambda _path, kind, **_kw: _summary(kind), logger=Mock())
    with (
        patch("mediaforce.library.source_intake.observe_file", wraps=observe_file) as observed,
        patch("mediaforce.library.source_intake.probe_evidence_source", return_value=_probe()),
        patch("mediaforce.library.evidence_worker._analysis_target",
              side_effect=lambda _config, _claim, path, *_args: SimpleNamespace(source_path=path, command_runner=None)),
    ):
        run_evidence_queue_until_blocked(config_path=config.paths.config_path, config=config, deps=deps)
    assert sum(bool(call.kwargs.get("full_digest")) for call in observed.call_args_list) == 1


@pytest.mark.parametrize("interruption", ["cancel", "cancel_shutdown", "pause", "priority", "shutdown"])
def test_original_digest_preserves_terminal_and_resumable_interruptions(tmp_path: Path, interruption: str) -> None:
    from mediaforce.library.evidence_queue import EVIDENCE_BACKGROUND_RETRY_SECONDS, load_evidence_queue_state, pause_evidence_queue

    config = _config(tmp_path)
    item_id, _path = _source(config)
    if interruption == "priority":
        _source(config, "urgent")
    _discover(config)
    stopped = threading.Event()
    analyzer = Mock()

    def observe(path: Path, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("full_digest"):
            with open_db(config.paths.db_path) as mutation_db:
                if interruption.startswith("cancel"):
                    cancel_evidence_queue(mutation_db)
                elif interruption == "pause":
                    pause_evidence_queue(mutation_db)
                elif interruption == "priority":
                    mutation_db.execute(update(library_item_evidence_state).where(
                        library_item_evidence_state.c.library_item_id != item_id,
                    ).values(work_priority=0))
            if interruption.endswith("shutdown"):
                stopped.set()
            kwargs["check_cancelled"]()
        return observe_file(path, **kwargs)

    deps = EvidenceWorkerDeps(read_probe=lambda *_args, **_kw: _probe(), load_config=lambda _path: config, analyze_evidence=analyzer, logger=Mock(),
                              heartbeat_seconds=60, shutdown_event=stopped)
    with patch("mediaforce.library.source_intake.observe_file", side_effect=observe):
        assert process_evidence_queue_once(config_path=config.paths.config_path, config=config, deps=deps)
    analyzer.assert_not_called()
    with open_db(config.paths.db_path) as database:
        row = database.execute(select(library_item_evidence_state).where(
            library_item_evidence_state.c.library_item_id == item_id,
        )).mappings().one()
        state = load_evidence_queue_state(database)
        assert row["attempt_count"] == 0
        if interruption.startswith("cancel"):
            assert row["work_status"] == state["status"] == "cancelled"
            delay = datetime.fromisoformat(row["retry_not_before"]) - datetime.fromisoformat(row["updated_at"])
            assert delay.total_seconds() == EVIDENCE_BACKGROUND_RETRY_SECONDS
        else:
            assert row["work_status"] == "retry_wait"
            assert not state["cancel_requested"]
        database.execute(update(library_item_evidence_state).where(
            library_item_evidence_state.c.library_item_id == item_id,
        ).values(retry_not_before="2000-01-01T00:00:00+00:00"))
    if interruption.startswith("cancel"):
        assert _discover(config) == 1


def test_background_discovery_preserves_higher_priority_reason(tmp_path: Path) -> None:
    from mediaforce.library.evidence_queue import queue_decision_evidence_work

    config = _config(tmp_path)
    item_id, _path = _source(config)
    _discover(config)
    with open_db(config.paths.db_path) as database:
        queue_decision_evidence_work(
            database,
            config,
            "tv/source.mkv",
            library_item_ids=[item_id],
            evidence_kind="media_fingerprint",
            work_reason="sample_safety",
            priority=0,
            require_measurement=True,
        )
        queue_decision_evidence_work(
            database,
            config,
            "tv/source.mkv",
            library_item_ids=[item_id],
            evidence_kind="media_fingerprint",
            work_reason="original_source_measurement",
            priority=70,
            require_measurement=True,
        )
        row = database.execute(select(library_item_evidence_state)).mappings().one()
        assert row["work_reason"] == "sample_safety" and row["work_priority"] == 0


def test_completed_operator_rows_do_not_block_later_prepare(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _source(config, "first")
    second_id, _path = _source(config, "second")
    _discover(config)
    with open_db(config.paths.db_path) as database:
        database.execute(
            update(library_item_evidence_state)
            .where(library_item_evidence_state.c.library_item_id != second_id)
            .values(work_status="completed", work_reason="operator_scope")
        )
        result = start_evidence_work(database, config, "tv/second.mkv")
        assert result["is_paused"]


def test_empty_prepare_does_not_pause_background_work(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _source(config, "measured")
    _discover(config)
    _work(config)
    _work(config)
    _source(config, "new")
    _discover(config)
    with open_db(config.paths.db_path) as database:
        result = start_evidence_work(database, config, "tv/measured.mkv")
        assert not result["is_paused"]
        assert result["prepared_count"] == 0
        assert runnable_evidence_work_count(database) > 0


def test_fully_measured_ambiguous_cadence_is_retained(tmp_path: Path) -> None:
    config = _config(tmp_path)
    item_id, _path = _source(config)
    _discover(config)

    def analyze(_path: Path, kind: str, **_kwargs: object) -> dict[str, Any]:
        summary = _summary(kind)
        if kind == "cadence_analysis":
            summary = json.loads(worker_fixtures.EvidenceWorkerTests._blocked_cadence_summary_json())
        return summary

    _work(config, analyze)
    _work(config, analyze)
    with open_db(config.paths.db_path) as database:
        trait = load_source_evidence(database, item_id).traits["cadence_analysis"]
        assert trait.tier == "probable"
        assert trait.summary is not None
        assert trait.summary["decision"]["status"] == "blocked"


def test_unknown_result_exhausts_existing_budget_then_prepare_retries(tmp_path: Path) -> None:
    config = _config(tmp_path)
    item_id, _path = _source(config)
    _discover(config)
    calls = []

    def analyze(_path: Path, kind: str, **_kwargs: object) -> dict[str, Any]:
        calls.append(kind)
        summary = _summary(kind)
        if kind == "media_fingerprint":
            summary["decision"]["status"] = "unknown"
            summary["analysis"]["coverage"] = 0.0
        return summary

    for _ in range(5):
        _work(config, analyze, max_attempts=2)
    assert calls.count("media_fingerprint") == 2
    assert _discover(config) == 0
    with open_db(config.paths.db_path) as database:
        row = (
            database.execute(
                select(library_item_evidence_state).where(library_item_evidence_state.c.evidence_kind == "media_fingerprint")
            )
            .mappings()
            .one()
        )
        assert row["work_status"] == "failed" and row["retry_not_before"] is None
    _source(config, "new")
    _discover(config)
    with open_db(config.paths.db_path) as database:
        result = start_evidence_work(database, config, "tv/source.mkv", evidence_kinds=["media_fingerprint"])
        row = (
            database.execute(
                select(library_item_evidence_state).where(
                    library_item_evidence_state.c.library_item_id == item_id,
                    library_item_evidence_state.c.evidence_kind == "media_fingerprint",
                )
            )
            .mappings()
            .one()
        )
        assert row["work_status"] == "queued" and row["attempt_count"] == 0
        assert result["prepared_count"] == 1 and result["item_count"] > result["prepared_count"]
        resume_evidence_queue(database)
    _work(config)
    with open_db(config.paths.db_path) as database:
        assert load_source_evidence(database, item_id).traits["media_fingerprint"].tier == "probable"


@pytest.mark.parametrize("status", ["failed", "retry_wait", "waiting_host"])
def test_background_followup_preserves_failed_or_waiting_safety_request(tmp_path: Path, status: str) -> None:
    from mediaforce.library.evidence_queue import queue_decision_evidence_work

    config = _config(tmp_path)
    item_id, _path = _source(config)
    _discover(config)
    with open_db(config.paths.db_path) as database:
        database.execute(
            update(library_item_evidence_state).values(work_status=status, work_priority=0, work_reason="sample_safety")
        )
        queue_decision_evidence_work(
            database,
            config,
            "tv/source.mkv",
            library_item_ids=[item_id],
            evidence_kind="media_fingerprint",
            work_reason="original_source_measurement",
            priority=70,
            require_measurement=True,
        )
        row = database.execute(select(library_item_evidence_state)).mappings().one()
        assert row["work_status"] == status
        assert row["work_reason"] == "sample_safety" and row["work_priority"] == 0


def test_cancelled_kind_with_ineligible_original_does_not_enqueue_another_kind(tmp_path: Path) -> None:
    from dataclasses import replace

    config = _config(tmp_path)
    item_id, _path = _source(config)
    _discover(config)
    _work(config)
    _work(config)
    with open_db(config.paths.db_path) as database:
        evidence = load_source_evidence(database, item_id)
        database.execute(
            update(library_item_evidence_state)
            .where(library_item_evidence_state.c.evidence_kind == "cadence_analysis")
            .values(work_status="cancelled", retry_not_before="2000-01-01T00:00:00+00:00")
        )
    with patch("mediaforce.library.source_intake.load_source_evidence", return_value=replace(evidence, availability="unavailable")):
        assert _discover(config) == 0
    with open_db(config.paths.db_path) as database:
        statuses = dict(
            database.execute(select(library_item_evidence_state.c.evidence_kind, library_item_evidence_state.c.work_status)).all()
        )
        assert statuses == {"cadence_analysis": "failed", "media_fingerprint": "completed"}


def test_paired_original_records_two_observed_receipts_from_one_probe(tmp_path: Path) -> None:
    config = _config(tmp_path)
    item_id, _path = _source(config)
    _discover(config)
    probe = Mock(return_value=_probe())
    measured: list[tuple[str, object]] = []
    def analyze(_path: Path, kind: str, **kwargs: Any) -> dict[str, Any]:
        measured.append((kind, kwargs["probe_payload"]))
        return _summary(kind)
    from mediaforce.library.evidence_worker import run_evidence_queue_until_blocked
    deps = EvidenceWorkerDeps(load_config=lambda _: config, analyze_evidence=analyze, logger=Mock(), read_probe=probe)
    with patch("mediaforce.library.source_intake.probe_evidence_source", return_value=_probe()):
        result = run_evidence_queue_until_blocked(config_path=config.paths.config_path, config=config, deps=deps, max_work_items=2)
    assert result["processed_count"] == 2
    probe.assert_called_once()
    assert len(measured) == 2 and measured[0][1] is measured[1][1]
    with open_db(config.paths.db_path) as db:
        receipts = db.execute(select(source_measurements)).mappings().all()
        evidence = load_source_evidence(db, item_id)
    assert {row["kind"] for row in receipts} == set(EVIDENCE_KINDS)
    assert {row["binding"] for row in receipts} == {"observed"}
    assert len(receipts) == 2
    assert {trait.tier for trait in evidence.traits.values()} == {"probable"}


@pytest.mark.parametrize("action", ["cancel", "pause", "shutdown"])
def test_pair_interrupt_restores_each_started_attempt(tmp_path: Path, action: str) -> None:
    from mediaforce.core.process_control import ProcessCancelledError
    from mediaforce.library.evidence_queue import pause_evidence_queue
    from mediaforce.library.evidence_worker import EvidenceWorkDeferred
    config = _config(tmp_path)
    _source(config)
    _discover(config)
    def analyze(_path: Path, _kind: str, **_kwargs: Any) -> dict[str, Any]:
        with open_db(config.paths.db_path) as controls:
            if action == "cancel":
                cancel_evidence_queue(controls)
            elif action == "pause":
                pause_evidence_queue(controls)
        if action == "cancel":
            raise ProcessCancelledError("cancelled")
        raise EvidenceWorkDeferred(action)
    assert _work(config, analyze, max_work_items=2) == 2
    with open_db(config.paths.db_path) as db:
        rows = db.execute(select(library_item_evidence_state)).mappings().all()
        assert db.execute(select(func.count()).select_from(source_measurements)).scalar_one() == 0
    assert {row["attempt_count"] for row in rows} == {0}
    assert {row["work_status"] for row in rows} == ({"cancelled"} if action == "cancel" else {"retry_wait"})


@pytest.mark.parametrize("fence", ["cancel", "source_operation"])
def test_pair_fence_between_commits_preserves_first_receipt(tmp_path: Path, fence: str) -> None:
    from mediaforce.library import evidence_worker
    config = _config(tmp_path)
    _source(config)
    _discover(config)
    commit = evidence_worker._commit_evidence_summary
    committed: list[str] = []
    def commit_and_cancel(current: MediaforceConfig, claim: EvidenceWorkClaim, *args: Any, **kwargs: Any) -> None:
        commit(current, claim, *args, **kwargs)
        committed.append(claim.evidence_kind)
        if fence == "cancel":
            with open_db(config.paths.db_path) as controls:
                cancel_evidence_queue(controls)
    with patch("mediaforce.library.evidence_worker._commit_evidence_summary", side_effect=commit_and_cancel), patch(
        "mediaforce.library.evidence_worker.pending_source_operation",
        side_effect=lambda *_: fence == "source_operation" and bool(committed),
    ):
        assert _work(config, max_work_items=2) == 2
    with open_db(config.paths.db_path) as db:
        receipts = db.execute(select(source_measurements.c.kind)).scalars().all()
        rows = db.execute(select(library_item_evidence_state.c.work_status)).scalars().all()
    assert receipts == committed[:1]
    assert sorted(rows) == sorted(["cancelled" if fence == "cancel" else "failed", "completed"])


@pytest.mark.parametrize("hold", ["pause", "shutdown"])
def test_pair_retains_finished_kind_when_next_start_is_held(tmp_path: Path, hold: str) -> None:
    from mediaforce.library.evidence_queue import pause_evidence_queue
    config = _config(tmp_path)
    _source(config)
    _discover(config)
    shutdown = threading.Event()
    measured: list[str] = []
    def analyze(_path: Path, kind: str, **_kwargs: Any) -> dict[str, Any]:
        measured.append(kind)
        if hold == "pause":
            with open_db(config.paths.db_path) as controls:
                pause_evidence_queue(controls)
        else:
            shutdown.set()
        return _summary(kind)
    assert _work(config, analyze, stop_event=shutdown, max_work_items=2) == 1
    with open_db(config.paths.db_path) as db:
        receipts = db.execute(select(source_measurements.c.kind)).scalars().all()
        rows = db.execute(select(library_item_evidence_state)).mappings().all()
    assert receipts == measured
    assert len(measured) == 1
    assert {row["attempt_count"] for row in rows} == {0}
    assert {row["work_status"] for row in rows} == {"queued", "completed"}


def test_pair_lost_companion_cannot_publish_without_shared_validation(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _source(config)
    _discover(config)
    def analyze(_path: Path, kind: str, **_kwargs: Any) -> dict[str, Any]:
        with open_db(config.paths.db_path) as controls:
            controls.execute(update(library_item_evidence_state).where(
                library_item_evidence_state.c.evidence_kind != kind,
            ).values(worker_id="replacement-worker"))
        return _summary(kind)
    assert _work(config, analyze, max_work_items=2) == 2
    with open_db(config.paths.db_path) as db:
        assert db.execute(select(func.count()).select_from(source_measurements)).scalar_one() == 0
        rows = db.execute(select(library_item_evidence_state)).mappings().all()
    assert {row["attempt_count"] for row in rows} == {0}
    assert {row["work_status"] for row in rows} == {"running", "cancelled"}
    assert next(row for row in rows if row["work_status"] == "running")["worker_id"] == "replacement-worker"


def test_urgent_sibling_remains_visible_while_original_admission_owns_the_file(tmp_path: Path) -> None:
    config = _config(tmp_path)
    item_id, _path = _source(config)
    _discover(config)
    with open_db(config.paths.db_path) as database:
        primary = claim_next_evidence_work(database, worker_id="intake-owner", lease_seconds=60)
    assert primary is not None
    with open_db(config.paths.db_path) as database:
        item = cast(dict[str, Any], dict(database.execute(select(library_items).where(library_items.c.id == item_id)).mappings().one()))
        from mediaforce.library.evidence_state import sync_library_item_evidence_state
        sync_library_item_evidence_state(database, item, "cadence_analysis")
        queue_decision_evidence_work(database, config, item["rel_path"], library_item_ids=[item_id],
                                    evidence_kind="cadence_analysis", work_reason="sample_safety")
    with open_db(config.paths.db_path) as database:
        from mediaforce.library.evidence_queue import next_evidence_work_query
        assert database.execute(next_evidence_work_query(primary.batch_id, datetime.now(UTC).isoformat())).first() is None
        assert original_intake_hold(database, batch_id=primary.batch_id,
            priority=primary.work_priority, allowed_running_item=primary.library_item_id) == "higher_priority"
