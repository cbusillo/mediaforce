import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from mediaforce.core.db import DBClient, open_db
from mediaforce.core.db_tables import library_items, source_admissions, source_measurements, source_versions
from mediaforce.library.source_evidence import load_source_evidence, observe_file
from mediaforce.library.source_intake import (
    assess_readable_source,
    commit_readable_source,
    intake_candidate,
    process_source_intake_once,
)
from mediaforce.encoding.remote_media import source_observation_script
from test_source_evidence import connection, _insert_item, _config


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
        assert process_source_intake_once(config, analyze=analyze, target=target)
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
        process_source_intake_once(
            config,
            analyze=Mock(side_effect=AssertionError("no analysis")),
            target=lambda *_args: SimpleNamespace(source_path=path, command_runner=runner),
        )
    with open_db(config.paths.db_path) as database:
        assert not database.execute(select(source_measurements)).first()
        assert {trait.tier for trait in load_source_evidence(database, item_id).traits.values()} == {"unknown"}
