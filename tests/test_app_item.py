import json
import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import select

from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db import DBClient, open_db, reset_engine_cache
from mediaforce.core.db_tables import item_events, library_items, staged_artifacts
from mediaforce.web.runtime import app_item
from mediaforce.web.runtime.app_item import item_payload, player_file

SEASON = "tv/Show/Season 1"
NOW = "2026-10-25T12:00:00+00:00"


@pytest.fixture
def config(tmp_path: Path) -> Iterator[MediaforceConfig]:
    yield MediaforceConfig(
        raw={
            "video": {}, "audio": {}, "subtitle": {}, "planning": {}, "overrides": [],
            "media": {"libraries": [{"key": "tv", "path": str(tmp_path / "tv"), "type": "tv"}]},
        },
        paths=ConfigPaths(
            project_root=tmp_path, config_path=tmp_path / "config.toml", db_path=tmp_path / "library.sqlite3",
            run_manifest_dir=tmp_path / "runs", web_state_dir=tmp_path / "web", review_dir=tmp_path / "review",
            runtime_settings_path=tmp_path / "runtime.json", runtime_reservation_dir=tmp_path / "reservations",
        ),
    )
    reset_engine_cache()


@pytest.fixture(autouse=True)
def plenty_of_room() -> Iterator[None]:
    """The host's own free space never decides a test; the no-room test sets its own."""
    room = SimpleNamespace(total=0, used=0, free=10 ** 15)
    with patch.object(app_item.shutil, "disk_usage", return_value=room):
        yield


def _episode(connection: DBClient, root: Path, number: int, *, size: int, status: str = "discovered") -> int:
    rel_path = f"{SEASON}/Show.S01E{number:02d}.mkv"
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"N" * size)
    connection.execute(library_items.insert().values(
        source_path=str(path), rel_path=rel_path, media_root="tv", parent_dir=SEASON, file_name=path.name,
        container=".mkv", size_bytes=size, mtime_ns=1, fingerprint=rel_path, audio_summary_json="[]",
        subtitle_summary_json="[]", status=status, last_scan_id="scan", discovered_at=NOW, last_seen_at=NOW,
        updated_at=NOW,
    ))
    return connection.execute(select(library_items.c.id).where(library_items.c.rel_path == rel_path)).scalar_one()


def _replaced(connection: DBClient, root: Path, number: int, *, size: int, original_size: int) -> int:
    item_id = _episode(connection, root, number, size=size, status="promoted")
    backup = root / "archive" / SEASON / f"Show.S01E{number:02d}.mp4"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_bytes(b"O" * original_size)
    connection.execute(staged_artifacts.insert().values(
        library_item_id=item_id, staging_path=str(root / "staging" / f"{number}.mkv"), source_size_bytes=original_size,
        bytes_saved=original_size - size, promoted_at=NOW, archived_source_path=str(backup), updated_at=NOW,
    ))
    return item_id


def test_a_season_lists_each_episode_with_its_sizes_and_backup(config: MediaforceConfig, tmp_path: Path) -> None:
    with open_db(config.paths.db_path) as connection:
        waiting = _episode(connection, tmp_path, 2, size=900)
        replaced = _replaced(connection, tmp_path, 1, size=100, original_size=700)
        connection.commit()

    facts = item_payload(config, SEASON, sound=lambda _connection, _prefix: None)

    assert facts is not None
    assert (facts["show"], facts["season"], facts["show_prefix"]) == ("Show", 1, "tv/Show")
    first, second = facts["files"]
    assert (first["library_item_id"], first["episode"], first["stage"]) == (replaced, 1, "published")
    assert (first["bytes"], first["original_bytes"], first["original_backup"]) == (100, 700, True)
    assert (second["library_item_id"], second["episode"], second["original_bytes"], second["original_backup"]) == (
        waiting, 2, None, False)


def test_a_failed_copy_that_could_not_be_made_again_says_why(config: MediaforceConfig, tmp_path: Path) -> None:
    validation = {
        "passed": False,
        "checks": [{"message": "picture matches the original", "passed": False}],
        "automatic_publish": {"state": "failed", "reason": "Couldn't make it again.", "blockers": ["original_unreachable"]},
    }
    with open_db(config.paths.db_path) as connection:
        item_id = _episode(connection, tmp_path, 1, size=900, status="encoded")
        connection.execute(staged_artifacts.insert().values(
            library_item_id=item_id, staging_path=str(tmp_path / "staging" / "1.mkv"), source_size_bytes=900,
            bytes_saved=800, validation_json=json.dumps(validation), updated_at=NOW,
        ))
        connection.commit()

    facts = item_payload(config, SEASON, sound=lambda _connection, _prefix: None)

    assert facts is not None
    [file] = facts["files"]
    assert (file["stage"], file["validation"]) == ("needs_you", "failed")
    assert file["wait_reason"] == {"code": "not_made_again", "blockers": ["original_unreachable"]}


@pytest.mark.parametrize("publish_state", ["failed", "unsafe", "waiting", None])
def test_a_passed_copy_with_stopped_publishing_waits_without_holding_other_files(
    config: MediaforceConfig, tmp_path: Path, publish_state: str | None,
) -> None:
    validation = {"passed": True, "automatic_publish": {"state": publish_state}}
    with open_db(config.paths.db_path) as connection:
        item_id = _episode(connection, tmp_path, 1, size=900, status="validated")
        _replaced(connection, tmp_path, 2, size=100, original_size=700)
        _episode(connection, tmp_path, 3, size=900)
        connection.execute(staged_artifacts.insert().values(
            library_item_id=item_id, staging_path=str(tmp_path / "staging" / "1.mkv"), source_size_bytes=900,
            bytes_saved=800, validation_json=json.dumps(validation), updated_at=NOW,
        ))
        connection.commit()

    facts = item_payload(config, SEASON, sound=lambda _connection, _prefix: None)

    assert facts is not None
    stopped, published, untouched = facts["files"]
    if publish_state in {"failed", "unsafe"}:
        assert stopped["stage"] == "needs_you"
        assert stopped["wait_reason"] == {"code": f"publish_{publish_state}"}
    else:
        assert stopped["stage"] == "checking"
        assert stopped["wait_reason"] is None
    assert stopped["validation"] is None
    assert published["stage"] == "published"
    assert untouched["stage"] == "held"


def test_a_deleted_backup_leaves_nothing_to_compare(config: MediaforceConfig, tmp_path: Path) -> None:
    with open_db(config.paths.db_path) as connection:
        replaced = _replaced(connection, tmp_path, 1, size=100, original_size=700)
        connection.execute(item_events.insert().values(
            library_item_id=replaced, created_at=NOW, event_type="originals_removed_confirmed", details_json="{}",
        ))
        connection.commit()

    facts = item_payload(config, SEASON, sound=lambda _connection, _prefix: None)

    assert facts is not None
    assert facts["files"][0]["original_backup"] is False


def test_a_folder_with_no_files_is_not_found(config: MediaforceConfig) -> None:
    with open_db(config.paths.db_path):
        pass

    assert item_payload(config, SEASON, sound=lambda _connection, _prefix: None) is None


def _remux_by_copy(source: Path, target: Path) -> None:
    target.write_bytes(source.read_bytes())


def test_the_player_reads_each_side_from_where_it_is(config: MediaforceConfig, tmp_path: Path) -> None:
    with open_db(config.paths.db_path) as connection:
        replaced = _replaced(connection, tmp_path, 1, size=100, original_size=700)
        untouched = _episode(connection, tmp_path, 2, size=900)
        connection.commit()

    with patch.object(app_item, "_remux", side_effect=_remux_by_copy) as remux:
        compressed = player_file(config, replaced, "compressed")
        original = player_file(config, replaced, "original")
        as_it_is = player_file(config, untouched, "original")
        again = player_file(config, replaced, "compressed")

    assert compressed is not None and compressed.read_bytes() == b"N" * 100
    assert original is not None and original.read_bytes() == b"O" * 700
    assert as_it_is is not None and as_it_is.read_bytes() == b"N" * 900
    # The same file is remuxed once and then served from the cache.
    assert again == compressed and remux.call_count == 3
    assert player_file(config, untouched, "compressed") is None
    assert player_file(config, replaced, "sideways") is None


def test_the_player_cache_keeps_only_the_last_few_files(config: MediaforceConfig, tmp_path: Path) -> None:
    with open_db(config.paths.db_path) as connection:
        ids = [_episode(connection, tmp_path, number, size=10 + number) for number in range(1, 10)]
        connection.commit()

    with patch.object(app_item, "_remux", side_effect=_remux_by_copy):
        played = [player_file(config, item_id, "original") for item_id in ids]

    cached = sorted((config.paths.web_state_dir / "player").glob("*.mp4"))
    assert len(cached) == app_item.PLAYER_CACHE_KEEP
    assert played[-1] in cached


def test_the_player_refuses_when_there_is_no_room(config: MediaforceConfig, tmp_path: Path) -> None:
    with open_db(config.paths.db_path) as connection:
        item_id = _episode(connection, tmp_path, 1, size=100)
        connection.commit()

    room = SimpleNamespace(total=0, used=0, free=app_item.PLAYER_FREE_MARGIN_BYTES)
    with patch.object(app_item.shutil, "disk_usage", return_value=room), \
            patch.object(app_item, "_remux", side_effect=_remux_by_copy) as remux:
        with pytest.raises(app_item.PlayerUnavailable) as unavailable:
            player_file(config, item_id, "original")

    assert unavailable.value.code == "no_room"
    remux.assert_not_called()


def test_an_interrupted_preparation_is_cleaned_up_later(config: MediaforceConfig, tmp_path: Path) -> None:
    with open_db(config.paths.db_path) as connection:
        item_id = _episode(connection, tmp_path, 1, size=100)
        connection.commit()
    cache = config.paths.web_state_dir / "player"
    cache.mkdir(parents=True)
    stale = cache / f".old{app_item.PARTIAL_SUFFIX}"
    stale.write_bytes(b"half")
    old = time.time() - app_item.PLAYER_REMUX_TIMEOUT_SECONDS - 60
    os.utime(stale, (old, old))
    running = cache / f".running{app_item.PARTIAL_SUFFIX}"
    running.write_bytes(b"half")

    with patch.object(app_item, "_remux", side_effect=_remux_by_copy):
        assert player_file(config, item_id, "original") is not None

    assert not stale.exists()
    assert running.exists()


def test_a_file_larger_than_the_cache_is_not_prepared(config: MediaforceConfig, tmp_path: Path) -> None:
    with open_db(config.paths.db_path) as connection:
        item_id = _episode(connection, tmp_path, 1, size=100)
        connection.commit()

    with patch.object(app_item, "PLAYER_CACHE_BYTES", 99), patch.object(app_item, "_remux", side_effect=_remux_by_copy) as remux:
        with pytest.raises(app_item.PlayerUnavailable) as unavailable:
            player_file(config, item_id, "original")

    assert unavailable.value.code == "too_large"
    remux.assert_not_called()


def test_a_stalled_probe_is_refused_with_a_code(config: MediaforceConfig, tmp_path: Path) -> None:
    with open_db(config.paths.db_path) as connection:
        item_id = _episode(connection, tmp_path, 1, size=100)
        connection.commit()

    with patch.object(app_item.subprocess, "run", side_effect=subprocess.TimeoutExpired("ffprobe", 60)):
        with pytest.raises(app_item.PlayerUnavailable) as unavailable:
            player_file(config, item_id, "original")

    assert unavailable.value.code == "too_slow"


def test_a_comparison_keeps_both_halves_in_the_cache(config: MediaforceConfig, tmp_path: Path) -> None:
    with open_db(config.paths.db_path) as connection:
        replaced = _replaced(connection, tmp_path, 1, size=40, original_size=70)
        others = [_episode(connection, tmp_path, number, size=10) for number in range(2, 4)]
        connection.commit()

    with patch.object(app_item, "PLAYER_CACHE_BYTES", 120), patch.object(app_item, "_remux", side_effect=_remux_by_copy):
        for item_id in others:
            player_file(config, item_id, "original")
        compressed = player_file(config, replaced, "compressed")
        original = player_file(config, replaced, "original")

    assert compressed is not None and compressed.is_file()
    assert original is not None and original.is_file()


def test_a_comparison_too_large_for_the_cache_is_refused(config: MediaforceConfig, tmp_path: Path) -> None:
    with open_db(config.paths.db_path) as connection:
        replaced = _replaced(connection, tmp_path, 1, size=40, original_size=70)
        connection.commit()

    with patch.object(app_item, "PLAYER_CACHE_BYTES", 100), patch.object(app_item, "_remux", side_effect=_remux_by_copy):
        assert player_file(config, replaced, "compressed") is not None
        with pytest.raises(app_item.PlayerUnavailable) as unavailable:
            player_file(config, replaced, "original")

    assert unavailable.value.code == "too_large"


def test_an_old_copy_of_the_same_file_is_not_kept_as_its_pair(config: MediaforceConfig, tmp_path: Path) -> None:
    with open_db(config.paths.db_path) as connection:
        replaced = _replaced(connection, tmp_path, 1, size=40, original_size=50)
        connection.commit()
    cache = config.paths.web_state_dir / "player"
    cache.mkdir(parents=True)
    stale = cache / f"{replaced}-original-999-1.mp4"
    stale.write_bytes(b"S" * 60)

    with patch.object(app_item, "PLAYER_CACHE_BYTES", 100), patch.object(app_item, "_remux", side_effect=_remux_by_copy):
        assert player_file(config, replaced, "compressed") is not None
        assert player_file(config, replaced, "original") is not None

    assert not stale.exists()


def test_a_whole_show_lists_every_season_in_order(config: MediaforceConfig, tmp_path: Path) -> None:
    with open_db(config.paths.db_path) as connection:
        second = _episode(connection, tmp_path, 1, size=10)
        connection.execute(library_items.update().where(library_items.c.id == second).values(
            rel_path="tv/Show/Season 2/Show.S02E01.mkv", parent_dir="tv/Show/Season 2"))
        first = _episode(connection, tmp_path, 3, size=10)
        connection.commit()

    facts = item_payload(config, "tv/Show", sound=lambda _connection, _prefix: None)

    assert facts is not None
    assert (facts["kind"], facts["show"], facts["show_prefix"], facts["season"]) == ("tv_series", "Show", "tv/Show", None)
    assert [(row["library_item_id"], row["season"], row["episode"]) for row in facts["files"]] == [(first, 1, 3), (second, 2, 1)]
