import shutil
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select

from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import library_items, metadata, motion_keep_decisions, staged_artifacts
from mediaforce.core.utils import content_version_fingerprint, file_fingerprint, timestamp
from mediaforce.encoding.free_space import ReservePreflight
from mediaforce.encoding.put_back import PUT_BACK_EVENT, PutBackRefused, PutBackRestoreError, put_original_back, \
    reconcile_unfinished_put_backs
from mediaforce.encoding.source_publication import path_in_pending_move
from mediaforce.encoding.staging import promote_one_item
from mediaforce.library.evidence_state import library_item_source_fingerprint
from mediaforce.library.motion_keep_state import library_item_ids_kept_original
from mediaforce.web.runtime.episode_progress import put_back_rel_paths

ORIGINAL = b"S" * 1600
COMPRESSED = b"C" * 200


@pytest.fixture
def connection() -> Iterator[DBClient]:
    engine = create_engine("sqlite://")
    with engine.connect() as database:
        database.exec_driver_sql("PRAGMA foreign_keys=ON")
        metadata.create_all(database)
        database.commit()
        yield database
    engine.dispose()


def _config(root: Path) -> MediaforceConfig:
    return MediaforceConfig(raw={"media": {"source_roots": {"tv": str(root / "tv")},
                                           "staging_root": str(root / "staging"), "archive_root": str(root / "archive"),
                                           "output_container": "mkv"}},
                            paths=ConfigPaths(project_root=root, config_path=root / "config.toml", db_path=root / "db.sqlite3",
                                              run_manifest_dir=root / "runs", web_state_dir=root / "web", review_dir=root / "review",
                                              runtime_settings_path=root / "settings.json"))


def _probe() -> SimpleNamespace:
    return SimpleNamespace(duration_seconds=60, video_codec="h264", video_bitrate=4000, width=1920, height=1080, pix_fmt="yuv420p",
                           audio_track_count=1, subtitle_track_count=0, english_audio_count=1, english_subtitle_count=0,
                           default_audio_language="eng", default_subtitle_language=None, audio_summary_json="[]",
                           subtitle_summary_json="[]", attachment_summary_json="[]")


def _published(connection: DBClient, root: Path, original_name: str) -> dict[str, Any]:
    """One episode compressed and published the normal way, its original kept as a backup."""
    source = root / "tv/Show/Season 1" / original_name
    source.parent.mkdir(parents=True)
    source.write_bytes(ORIGINAL)
    now = timestamp()
    item_id = int(connection.execute(library_items.insert().values(
        source_path=str(source), rel_path=f"tv/Show/Season 1/{original_name}", media_root="tv", parent_dir="tv/Show/Season 1",
        file_name=source.name, container=source.suffix, size_bytes=source.stat().st_size, mtime_ns=source.stat().st_mtime_ns,
        fingerprint=file_fingerprint(source, source.stat(), 60),
        content_version_fingerprint=content_version_fingerprint(source, source.stat()),
        video_codec="h264", width=1920, height=1080, duration_seconds=60, audio_summary_json="[]", subtitle_summary_json="[]",
        last_scan_id="fixture", discovered_at=now, last_seen_at=now, updated_at=now,
    )).inserted_primary_key[0])
    staging = root / "staging/tv/Show/Season 1/episode.mkv"
    staging.parent.mkdir(parents=True)
    staging.write_bytes(COMPRESSED)
    connection.execute(staged_artifacts.insert().values(
        library_item_id=item_id, staging_path=str(staging), source_path=str(source), validation_json='{"passed":true}',
        validated_at=now, bytes_saved=len(ORIGINAL) - len(COMPRESSED), updated_at=now,
    ))
    connection.commit()
    promote_one_item(connection, _config(root), {"library_item_id": item_id, "source_path": str(source),
                                                 "rel_path": f"tv/Show/Season 1/{original_name}", "media_root": "tv"},
                     force=False, probe_media=lambda _path: _probe(), file_fingerprint=file_fingerprint, timestamp=timestamp,
                     record_event=lambda *_args: None,
                     reserve_preflight=lambda *_args, **_kwargs: ReservePreflight(allowed=True, waiting_reason=None,
                                                                                  required_by_volume={}))
    return {"id": item_id, "source": source, "staging": staging, "library": source.with_suffix(".mkv"),
            "archive": root / "archive/tv/Show/Season 1" / original_name}


def _put_back(connection: DBClient, item_id: int, events: list[tuple[str, dict[str, Any]]] | None = None) -> Path:
    return put_original_back(
        connection, item_id, probe_media=lambda _path: _probe(), file_fingerprint=file_fingerprint,
        content_fingerprint=content_version_fingerprint, timestamp=timestamp,
        record_event=lambda _connection, _item, kind, details: (events if events is not None else []).append((kind, details)),
    )


@pytest.mark.parametrize("original_name", ["episode.mp4", "episode.mkv"])
def test_put_back_returns_the_original_and_keeps_it(connection: DBClient, tmp_path: Path, original_name: str) -> None:
    published = _published(connection, tmp_path, original_name)
    assert published["library"].read_bytes() == COMPRESSED
    events: list[tuple[str, dict[str, Any]]] = []

    assert _put_back(connection, published["id"], events) == published["source"]

    assert published["source"].read_bytes() == ORIGINAL
    assert not published["archive"].exists()
    assert not published["staging"].exists()
    if published["library"] != published["source"]:
        assert not published["library"].exists()
    item = connection.execute(select(library_items)).mappings().one()
    assert (item["source_path"], item["rel_path"]) == (str(published["source"]), f"tv/Show/Season 1/{original_name}")
    assert (item["size_bytes"], item["status"]) == (len(ORIGINAL), "discovered")
    assert connection.execute(select(staged_artifacts)).first() is None
    # Kept for the source that came back, so queueing, dispatch and publishing leave it alone.
    keep = connection.execute(select(motion_keep_decisions)).mappings().one()
    assert (keep["reason_code"], keep["source_fingerprint"]) == ("put_back", library_item_source_fingerprint(item))
    assert library_item_ids_kept_original(connection, [published["id"]]) == {published["id"]}
    assert [kind for kind, _details in events] == [PUT_BACK_EVENT]
    # The episode list words it as the owner's put-back, not a motion-pattern choice.
    assert put_back_rel_paths(connection, [item["rel_path"], "tv/Show/Season 1/other.mkv"]) == {item["rel_path"]}


def test_put_back_refuses_when_the_backup_is_gone(connection: DBClient, tmp_path: Path) -> None:
    published = _published(connection, tmp_path, "episode.mp4")
    published["archive"].unlink()

    with pytest.raises(PutBackRefused) as refused:
        _put_back(connection, published["id"])

    assert refused.value.code == "backup_gone"
    assert published["library"].read_bytes() == COMPRESSED
    assert connection.execute(select(staged_artifacts.c.promoted_at)).scalar_one() is not None


def test_put_back_refuses_a_file_never_replaced(connection: DBClient, tmp_path: Path) -> None:
    published = _published(connection, tmp_path, "episode.mp4")
    _put_back(connection, published["id"])

    with pytest.raises(PutBackRefused) as refused:
        _put_back(connection, published["id"])

    assert refused.value.code == "not_replaced"
    assert published["source"].read_bytes() == ORIGINAL


def test_interrupted_put_back_returns_every_file(connection: DBClient, tmp_path: Path) -> None:
    published = _published(connection, tmp_path, "episode.mp4")
    move = shutil.move
    calls = 0

    def second_move_fails(old: str, new: str) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk went away")
        return move(old, new)

    with patch("mediaforce.encoding.source_publication.shutil.move", side_effect=second_move_fails):
        with pytest.raises(OSError, match="disk went away"):
            _put_back(connection, published["id"])

    assert published["library"].read_bytes() == COMPRESSED
    assert published["archive"].read_bytes() == ORIGINAL
    assert not published["staging"].exists()
    assert connection.execute(select(staged_artifacts.c.promoted_at)).scalar_one() is not None
    assert connection.execute(select(motion_keep_decisions)).first() is None


@pytest.mark.parametrize("killed_after_moves", [1, 2])
def test_put_back_killed_partway_is_returned_then_retried(connection: DBClient, tmp_path: Path, killed_after_moves: int) -> None:
    published = _published(connection, tmp_path, "episode.mp4")
    move = shutil.move
    calls = 0

    def killed(old: str, new: str) -> str:
        nonlocal calls
        result = move(old, new)
        calls += 1
        if calls == killed_after_moves:
            raise KeyboardInterrupt()
        return result

    with patch("mediaforce.encoding.source_publication.shutil.move", side_effect=killed):
        with patch("mediaforce.encoding.put_back.reconcile_source_publish", return_value=False):
            with pytest.raises((KeyboardInterrupt, PutBackRestoreError)):
                _put_back(connection, published["id"])
    # A scan meanwhile leaves the files of the unfinished move alone.
    assert path_in_pending_move(connection, str(published["staging"]))
    assert path_in_pending_move(connection, str(published["source"]))

    assert reconcile_unfinished_put_backs(connection) == 0
    assert published["library"].read_bytes() == COMPRESSED
    assert published["archive"].read_bytes() == ORIGINAL
    assert _put_back(connection, published["id"]) == published["source"]
    assert published["source"].read_bytes() == ORIGINAL


def test_put_back_refuses_a_backup_that_is_not_the_recorded_original(connection: DBClient, tmp_path: Path) -> None:
    published = _published(connection, tmp_path, "episode.mp4")
    published["archive"].write_bytes(b"X" * 1600)

    with pytest.raises(PutBackRefused) as refused:
        _put_back(connection, published["id"])

    assert refused.value.code == "backup_changed"
    assert published["library"].read_bytes() == COMPRESSED
    assert published["archive"].read_bytes() == b"X" * 1600
    assert not path_in_pending_move(connection, str(published["source"]))
