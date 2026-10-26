import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from typing import Any
from pathlib import Path
from unittest.mock import Mock, patch
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient, open_db
from mediaforce.core.db_tables import library_items, source_admissions, source_measurements, source_versions
from mediaforce.library.source_evidence import load_source_evidence, observe_file
from mediaforce.library.source_intake import (
    assess_readable_source,
    commit_readable_source,
    intake_candidate,
    process_source_intake_once as assess_source_intake,
)
from mediaforce.encoding.remote_media import source_observation_script
from test_source_evidence import connection, _insert_item, _config


def _intake_and_measure(
    config: MediaforceConfig,
    *,
    analyze: Callable[..., dict[str, object]],
    target: Callable[..., Any],
    stop_event: threading.Event | None = None,
) -> bool:
    from mediaforce.library.evidence_worker import EvidenceWorkerDeps, process_evidence_queue_once

    assessed = assess_source_intake(config, stop_event=stop_event)
    deps = EvidenceWorkerDeps(
        load_config=lambda _path: config, analyze_evidence=analyze, logger=Mock(), heartbeat_seconds=0.01, retry_base_delay_seconds=0
    )
    with patch("mediaforce.library.evidence_worker._analysis_target", side_effect=lambda *_args: target(None, None)):
        for _index in range(2):
            if stop_event is not None and stop_event.is_set():
                break
            if not process_evidence_queue_once(config_path=config.paths.config_path, config=config, deps=deps):
                break
    return assessed


@pytest.mark.parametrize("size", [0, 1, 65535, 65536, 65537, 131072, 1048577])
def test_remote_samples_equal_descriptor_samples(tmp_path: Path, size: int) -> None:
    path = tmp_path / "source"
    path.write_bytes(bytes(range(251)) * (size // 251) + bytes(range(size % 251)))
    result = subprocess.run(
        [sys.executable, "-c", source_observation_script(), str(path), "1"], check=True, capture_output=True, text=True
    )
    assert json.loads(result.stdout) == observe_file(path, full_digest=True)


def _probe(*, codec: str = "h264", tags: dict[str, str] | None = None, stream_tags: dict[str, str] | None = None) -> dict:
    return {"format": {"tags": tags or {}}, "streams": [{"codec_type": "video", "codec_name": codec, "tags": stream_tags or {}}]}


@pytest.mark.parametrize(
    "probe",
    [
        _probe(tags={"MEDIAFORCE_ENCODED_BY": ""}),
        _probe(stream_tags={"Mediaforce_Encoded_By": "anything"}),
        _probe(codec="av1"),
        _probe(stream_tags={"encoder": "Lavc libsvtav1"}),
    ],
)
def test_outputs_never_become_probable(connection: DBClient, tmp_path: Path, probe: dict) -> None:
    path = tmp_path / "source.mkv"
    path.write_bytes(b"source")
    item_id = _insert_item(connection, path)
    assessment = assess_readable_source(connection, observe_file(path, full_digest=True), probe)
    assert assessment["reason"]
    commit_readable_source(
        connection,
        dict(connection.execute(select(library_items).where(library_items.c.id == item_id)).mappings().one()),
        observe_file(path, full_digest=True),
        probe,
    )
    assert all(trait.tier == "unknown" for trait in load_source_evidence(connection, item_id).traits.values())


def test_same_size_unsampled_mutation_changes_full_identity(tmp_path: Path) -> None:
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


def test_short_reads_do_not_create_observation(tmp_path: Path) -> None:
    path = tmp_path / "source"
    path.write_bytes(b"contents")
    with patch("os.pread", return_value=b""):
        with pytest.raises(OSError):
            observe_file(path, full_digest=True)


@pytest.mark.parametrize("remote", [False, True])
def test_intake_measures_new_original_without_copying_media(tmp_path: Path, remote: bool) -> None:
    config = _config(tmp_path)
    path = tmp_path / "tv/source.mkv"
    path.parent.mkdir()
    path.write_bytes(b"original" * 100)
    with open_db(config.paths.db_path) as database:
        item_id = _insert_item(database, path)
        database.execute(update(library_items).where(library_items.c.id == item_id).values(video_codec="h264"))
    runner = Mock() if remote else None
    if runner:
        runner.observe_source.side_effect = lambda *_args, **_kwargs: observe_file(path)
    target = lambda *_args: SimpleNamespace(source_path=path, command_runner=runner)

    def analyze(*_args: object, **_kwargs: object) -> dict:
        with open_db(config.paths.db_path) as database:
            database.exec_driver_sql("BEGIN IMMEDIATE")
        return {"decision": {"status": "measured", "traits": ["grain"]}}

    original_files = set(tmp_path.rglob("*.mkv"))
    with patch("mediaforce.library.source_intake._probe_payload", return_value=_probe()):
        assert _intake_and_measure(config, analyze=analyze, target=target)
    with open_db(config.paths.db_path) as database:
        evidence = load_source_evidence(database, item_id)
        assert {trait.tier for trait in evidence.traits.values()} == {"probable"}
        assert {trait.binding for trait in evidence.traits.values()} == {"observed"}
        assert {trait.basis for trait in evidence.traits.values()} == {"readable-source"}
        assert intake_candidate(database, config) is None
        version = (
            database.execute(select(source_versions).where(source_versions.c.version_id == evidence.original_version_id))
            .mappings()
            .one()
        )
        database.execute(
            source_versions.insert().values(
                version_id="known-output",
                generation_id=version["generation_id"],
                role="output",
                sha256=version["sha256"],
                size_bytes=1,
                video_codec="av1",
                identity_json="{}",
                profile_json="{}",
                observed_at="now",
            )
        )
        assert {trait.tier for trait in load_source_evidence(database, item_id).traits.values()} == {"unknown"}
    assert set(tmp_path.rglob("*.mkv")) == original_files


def test_revision_race_cannot_admit_source(connection: DBClient, tmp_path: Path) -> None:
    path = tmp_path / "source.mkv"
    path.write_bytes(b"source")
    item_id = _insert_item(connection, path)
    item = dict(connection.execute(select(library_items).where(library_items.c.id == item_id)).mappings().one())
    connection.execute(update(library_items).where(library_items.c.id == item_id).values(source_revision=99))
    with pytest.raises(RuntimeError, match="Source changed"):
        commit_readable_source(connection, item, observe_file(path, full_digest=True), _probe())
    assert not connection.execute(select(source_admissions)).first()


def test_stat_only_remote_reply_never_records_traits(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = tmp_path / "tv/source.mkv"
    path.parent.mkdir()
    path.write_bytes(b"source")
    with open_db(config.paths.db_path) as database:
        item_id = _insert_item(database, path)
        database.execute(update(library_items).where(library_items.c.id == item_id).values(video_codec="h264"))
    runner = Mock()
    runner.observe_source.return_value = {"size_bytes": path.stat().st_size}
    with patch("mediaforce.library.source_intake._probe_payload", return_value=_probe()):
        _intake_and_measure(
            config,
            analyze=Mock(side_effect=AssertionError("no analysis")),
            target=lambda *_args: SimpleNamespace(source_path=path, command_runner=runner),
        )
    with open_db(config.paths.db_path) as database:
        assert not database.execute(select(source_measurements)).first()
        assert {trait.tier for trait in load_source_evidence(database, item_id).traits.values()} == {"unknown"}


@pytest.mark.parametrize("paused,cancelled", [(True, False), (False, True)])
def test_analysis_pause_and_cancel_block_intake(tmp_path: Path, paused: bool, cancelled: bool) -> None:
    from mediaforce.core.db_tables import evidence_queue_state
    from mediaforce.library.evidence_queue import ensure_evidence_queue_state

    config = _config(tmp_path)
    path = tmp_path / "tv/source.mkv"
    path.parent.mkdir()
    path.write_bytes(b"source")
    with open_db(config.paths.db_path) as database:
        item_id = _insert_item(database, path)
        database.execute(update(library_items).where(library_items.c.id == item_id).values(video_codec="h264"))
        ensure_evidence_queue_state(database)
        database.execute(update(evidence_queue_state).values(is_paused=int(paused), cancel_requested=int(cancelled)))
        assert intake_candidate(database, config) is None


def test_retry_measures_only_missing_kind_and_keeps_successful_trait(tmp_path: Path) -> None:
    from mediaforce.library.source_intake import INTAKE_RETRY_SECONDS
    from datetime import UTC, datetime, timedelta

    config = _config(tmp_path)
    path = tmp_path / "tv/source.mkv"
    path.parent.mkdir()
    path.write_bytes(b"source")
    with open_db(config.paths.db_path) as database:
        item_id = _insert_item(database, path)
        database.execute(update(library_items).where(library_items.c.id == item_id).values(video_codec="h264"))
    target = lambda *_args: SimpleNamespace(source_path=path, command_runner=None)
    calls = []

    def first_analysis(_path: Path, kind: str, **_kwargs: object) -> dict:
        calls.append(kind)
        if kind == "media_fingerprint":
            raise RuntimeError("temporary failure")
        return {"decision": {"status": "measured"}, "host": "first-host"}

    with patch("mediaforce.library.source_intake._probe_payload", return_value=_probe()):
        _intake_and_measure(config, analyze=first_analysis, target=target)
    future = datetime.now(UTC) + timedelta(seconds=INTAKE_RETRY_SECONDS + 1)
    calls.clear()

    def second_analysis(_path: Path, kind: str, **_kwargs: object) -> dict:
        calls.append(kind)
        return {"decision": {"status": "measured"}, "host": "other-host"}

    with (
        patch("mediaforce.library.source_intake._probe_payload", return_value=_probe()),
        patch("mediaforce.library.source_intake.datetime", wraps=datetime) as clock,
    ):
        clock.now.return_value = future
        _intake_and_measure(config, analyze=second_analysis, target=target)
    assert calls == ["media_fingerprint"]
    with open_db(config.paths.db_path) as database:
        assert {trait.tier for trait in load_source_evidence(database, item_id).traits.values()} == {"probable"}


def test_remote_script_does_not_evaluate_newer_union_annotations(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import ast
    import typing

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


@pytest.mark.parametrize("stop_mode", ["pause", "shutdown"])
def test_running_intake_stops_during_hash(tmp_path: Path, stop_mode: str) -> None:
    import threading
    from mediaforce.core.db_tables import evidence_queue_state
    from mediaforce.library.evidence_queue import ensure_evidence_queue_state

    config = _config(tmp_path)
    path = tmp_path / "tv/source.mkv"
    path.parent.mkdir()
    path.write_bytes(b"source")
    with open_db(config.paths.db_path) as database:
        item_id = _insert_item(database, path)
        database.execute(update(library_items).where(library_items.c.id == item_id).values(video_codec="h264"))
        ensure_evidence_queue_state(database)
    stop_event = threading.Event()

    def hash_then_stop(path: Path, *, full_digest: bool = False, check_cancelled: object = None) -> dict:
        if full_digest:
            if stop_mode == "shutdown":
                stop_event.set()
            else:
                with open_db(config.paths.db_path) as database:
                    database.execute(update(evidence_queue_state).values(is_paused=1))
            check_cancelled()
        return observe_file(path, full_digest=full_digest)

    with patch("mediaforce.library.source_intake.observe_file", side_effect=hash_then_stop):
        assert not assess_source_intake(config, stop_event=stop_event)
    with open_db(config.paths.db_path) as database:
        assert not database.execute(select(source_measurements)).first()


def test_fresh_queue_receipts_qualify_without_another_analysis(tmp_path: Path) -> None:
    from mediaforce.library.evidence_queue import start_evidence_work, resume_evidence_queue
    from mediaforce.library.evidence_state import rebuild_library_item_evidence_states
    from mediaforce.library.evidence_worker import EvidenceWorkerDeps, process_evidence_queue_once
    from test_evidence_worker import EvidenceWorkerTests

    config = _config(tmp_path)
    path = tmp_path / "tv/source.mkv"
    path.parent.mkdir()
    path.write_bytes(b"source")
    with open_db(config.paths.db_path) as database:
        item_id = _insert_item(database, path)
        database.execute(update(library_items).where(library_items.c.id == item_id).values(video_codec="h264"))
        rebuild_library_item_evidence_states(database)
        start_evidence_work(database, config, "tv/Show/episode.mkv")
        resume_evidence_queue(database)
    calls = []

    def analyze(_path: Path, kind: str, **_kwargs: object) -> dict:
        calls.append(kind)
        summary = (
            EvidenceWorkerTests._cadence_summary_json()
            if kind == "cadence_analysis"
            else EvidenceWorkerTests._fingerprint_summary_json()
        )
        return json.loads(summary)

    deps = EvidenceWorkerDeps(load_config=lambda _path: config, analyze_evidence=analyze, logger=Mock(), heartbeat_seconds=0.01)
    with patch(
        "mediaforce.library.evidence_worker._analysis_target", return_value=SimpleNamespace(source_path=path, command_runner=None)
    ):
        assert process_evidence_queue_once(config_path=config.paths.config_path, config=config, deps=deps)
        assert process_evidence_queue_once(config_path=config.paths.config_path, config=config, deps=deps)
    assert len(calls) == 2
    with patch("mediaforce.library.source_intake._probe_payload", return_value=_probe()):
        assert assess_source_intake(config)
    with open_db(config.paths.db_path) as database:
        assert {trait.tier for trait in load_source_evidence(database, item_id).traits.values()} == {"probable"}
        from mediaforce.library.evidence_queue import runnable_evidence_work_count

        assert runnable_evidence_work_count(database) == 0
    assert len(calls) == 2
