import ast
import json
import os
import subprocess
import sys
import threading
import typing
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import func, select, update

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient, open_db
from mediaforce.core.db_tables import (
    evidence_queue_state,
    library_item_evidence_state,
    library_items,
    source_admissions,
    source_measurements,
    source_versions,
)
from mediaforce.encoding.remote_media import source_observation_script
from mediaforce.library.evidence_queue import (
    cancel_evidence_queue,
    claim_next_evidence_work,
    pause_evidence_queue,
    resume_evidence_queue,
    runnable_evidence_work_count,
    start_evidence_work,
)
from mediaforce.library.evidence_state import rebuild_library_item_evidence_states
from mediaforce.library.evidence_worker import EvidenceWorkerDeps, process_evidence_queue_once
from mediaforce.library.source_evidence import load_source_evidence, observe_file
from mediaforce.library.source_intake import assess_readable_source, commit_readable_source, queue_original_intake
from test_source_evidence import connection, _config, _insert_item
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
    config: MediaforceConfig, analyze: Callable[..., dict[str, object]] | None = None, *, stop_event: threading.Event | None = None
) -> bool:
    deps = EvidenceWorkerDeps(
        load_config=lambda _path: config,
        analyze_evidence=analyze or (lambda _path, kind, **_kwargs: _summary(kind)),
        logger=Mock(),
        heartbeat_seconds=0.01,
        retry_base_delay_seconds=0,
        shutdown_event=stop_event,
    )
    with (
        patch("mediaforce.library.source_intake._probe_payload", return_value=_probe()),
        patch(
            "mediaforce.library.evidence_worker._analysis_target",
            side_effect=lambda _config, _claim, path, *_args: SimpleNamespace(source_path=path, command_runner=None),
        ),
    ):
        return process_evidence_queue_once(config_path=config.paths.config_path, config=config, deps=deps)


def _discover(config: MediaforceConfig) -> int:
    with open_db(config.paths.db_path) as database:
        return queue_original_intake(database, config)


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
    item = dict(connection.execute(select(library_items).where(library_items.c.id == item_id)).mappings().one())
    commit_readable_source(connection, item, identity, probe)
    assert {trait.tier for trait in load_source_evidence(connection, item_id).traits.values()} == {"unknown"}


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


def test_fresh_queue_receipts_are_frozen_and_not_reanalyzed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    item_id, _path = _source(config)
    with open_db(config.paths.db_path) as database:
        rebuild_library_item_evidence_states(database)
        start_evidence_work(database, config, "tv/source.mkv")
        resume_evidence_queue(database)
    assert _work(config)
    assert _work(config)
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

    def fail_first(database: DBClient, item: dict[str, Any], identity: dict[str, Any], probe: dict[str, Any]) -> str:
        attempts.append(item["id"])
        if item["id"] == first_id:
            raise RuntimeError("file changed before commit")
        return original_commit(database, item, identity, probe)

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
    import mediaforce.library.evidence_worker as worker

    config = _config(tmp_path)
    item_id, _path = _source(config)
    _discover(config)
    ticks = iter([index * 1800.0 for index in range(100)])
    with patch.object(worker, "time", SimpleNamespace(monotonic=lambda: next(ticks))):
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
