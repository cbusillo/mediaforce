import errno
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from storage_outage import storage_outage

from sqlalchemy import select

from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db import DBClient, open_db, reset_engine_cache
from mediaforce.core.db_tables import item_events, library_items, staged_artifacts
from mediaforce.encoding.staging import FAR_BELOW_PREDICTION_CHECK
from mediaforce.web.runtime.app_item import item_payload, player_source

SEASON = "tv/Show/Season 1"
NOW = "2026-10-25T12:00:00+00:00"


@pytest.fixture
def config(tmp_path: Path) -> Iterator[MediaforceConfig]:
    yield MediaforceConfig(
        raw={
            "video": {}, "audio": {}, "subtitle": {}, "planning": {}, "overrides": [],
            "media": {"libraries": [{"key": "tv", "path": str(tmp_path / "tv"), "type": "tv"}],
                      "staging_root": str(tmp_path / "staging")},
        },
        paths=ConfigPaths(
            project_root=tmp_path, config_path=tmp_path / "config.toml", db_path=tmp_path / "library.sqlite3",
            run_manifest_dir=tmp_path / "runs", web_state_dir=tmp_path / "web", review_dir=tmp_path / "review",
            runtime_settings_path=tmp_path / "runtime.json", runtime_reservation_dir=tmp_path / "reservations",
        ),
    )
    reset_engine_cache()


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

    facts = item_payload(config, SEASON, sound=lambda _prefix: None)

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

    facts = item_payload(config, SEASON, sound=lambda _prefix: None)

    assert facts is not None
    [file] = facts["files"]
    assert (file["stage"], file["validation"]) == ("needs_you", "failed")
    assert file["wait_reason"] == {"code": "not_made_again", "blockers": ["original_unreachable"]}


@pytest.mark.parametrize("passed", [True, None, False])
@pytest.mark.parametrize("publish_state", ["failed", "unsafe", "waiting", None])
def test_a_passed_copy_with_stopped_publishing_waits_without_holding_other_files(
    config: MediaforceConfig, tmp_path: Path, publish_state: str | None, passed: bool | None,
) -> None:
    validation = {"passed": passed, "automatic_publish": {"state": publish_state, "error": "ValueError"}}
    with open_db(config.paths.db_path) as connection:
        item_id = _episode(connection, tmp_path, 1, size=900, status="validated")
        _replaced(connection, tmp_path, 2, size=100, original_size=700)
        _episode(connection, tmp_path, 3, size=900)
        connection.execute(staged_artifacts.insert().values(
            library_item_id=item_id, staging_path=str(tmp_path / "staging" / "1.mkv"), source_size_bytes=900,
            bytes_saved=800, validation_json=json.dumps(validation), updated_at=NOW,
        ))
        connection.commit()

    facts = item_payload(config, SEASON, sound=lambda _prefix: None)

    assert facts is not None
    stopped, published, untouched = facts["files"]
    if passed is False:
        assert stopped["stage"] == "needs_you"
        assert stopped["wait_reason"] == {"code": "check_failed"}
    elif publish_state in {"failed", "unsafe"}:
        assert stopped["stage"] == "needs_you"
        assert stopped["wait_reason"] == {"code": f"publish_{publish_state}"}
    else:
        assert stopped["stage"] == "checking"
        assert stopped["wait_reason"] is None
    assert stopped["validation"] == ("failed" if passed is False else None)
    assert published["stage"] == "published"
    assert untouched["stage"] == "held"


def test_a_deleted_backup_leaves_nothing_to_compare(config: MediaforceConfig, tmp_path: Path) -> None:
    with open_db(config.paths.db_path) as connection:
        replaced = _replaced(connection, tmp_path, 1, size=100, original_size=700)
        connection.execute(item_events.insert().values(
            library_item_id=replaced, created_at=NOW, event_type="originals_removed_confirmed", details_json="{}",
        ))
        connection.commit()

    facts = item_payload(config, SEASON, sound=lambda _prefix: None)

    assert facts is not None
    assert facts["files"][0]["original_backup"] is False


def test_a_folder_with_no_files_is_not_found(config: MediaforceConfig) -> None:
    with open_db(config.paths.db_path):
        pass

    assert item_payload(config, SEASON, sound=lambda _prefix: None) is None


def test_the_player_reads_each_side_from_where_it_is(config: MediaforceConfig, tmp_path: Path) -> None:
    with open_db(config.paths.db_path) as connection:
        replaced = _replaced(connection, tmp_path, 1, size=100, original_size=700)
        untouched = _episode(connection, tmp_path, 2, size=900)
        connection.commit()

    compressed = player_source(config, replaced, "compressed")
    original = player_source(config, replaced, "original")
    as_it_is = player_source(config, untouched, "original")

    assert compressed is not None and compressed.read_bytes() == b"N" * 100
    assert original is not None and original.read_bytes() == b"O" * 700
    assert as_it_is is not None and as_it_is.read_bytes() == b"N" * 900
    assert player_source(config, untouched, "compressed") is None
    assert player_source(config, replaced, "sideways") is None


def _held(connection: DBClient, root: Path, number: int, *, held_copy: Path) -> int:
    item_id = _episode(connection, root, number, size=900, status="validated")
    held_copy.parent.mkdir(parents=True, exist_ok=True)
    held_copy.write_bytes(b"H" * 40)
    validation = {
        "passed": False, "size_prediction": {"held": True},
        "checks": [{"passed": False, "message": FAR_BELOW_PREDICTION_CHECK}],
    }
    connection.execute(staged_artifacts.insert().values(
        library_item_id=item_id, staging_path=str(held_copy), staging_size_bytes=40, source_size_bytes=900,
        bytes_saved=860, validation_json=json.dumps(validation), updated_at=NOW,
    ))
    return item_id


def test_a_size_held_file_plays_its_held_copy_against_the_file_as_it_is(config: MediaforceConfig, tmp_path: Path) -> None:
    with open_db(config.paths.db_path) as connection:
        held = _held(connection, tmp_path, 1, held_copy=tmp_path / "staging" / SEASON / "Show.S01E01.mkv")
        connection.commit()

    compressed = player_source(config, held, "compressed")
    original = player_source(config, held, "original")
    facts = item_payload(config, SEASON, sound=lambda _prefix: None)

    assert compressed is not None and compressed.read_bytes() == b"H" * 40
    assert original is not None and original.read_bytes() == b"N" * 900
    assert facts is not None
    assert facts["files"][0]["held_copy"] == {"bytes": 40, "present": True}


@pytest.mark.parametrize(("error", "reason"), [(errno.ETIMEDOUT, "storage"), (errno.EACCES, "permission")])
def test_player_waits_when_a_held_copy_cannot_be_checked(
        config: MediaforceConfig, tmp_path: Path, error: int, reason: str,
) -> None:
    path = tmp_path / "staging" / SEASON / "episode.mkv"
    with open_db(config.paths.db_path) as connection:
        item = _held(connection, tmp_path, 1, held_copy=path)
    with storage_outage(path, error=error):
        assert player_source(config, item, "compressed") is None
        facts = item_payload(config, SEASON, sound=lambda _prefix: None)
    assert facts is not None
    assert reason in facts["files"][0]["held_copy"]["waiting_reason"].lower()
    assert player_source(config, item, "compressed") == path


def test_a_held_copy_that_is_gone_is_refused_and_said(config: MediaforceConfig, tmp_path: Path) -> None:
    held_copy = tmp_path / "staging" / SEASON / "Show.S01E01.mkv"
    with open_db(config.paths.db_path) as connection:
        held = _held(connection, tmp_path, 1, held_copy=held_copy)
        connection.commit()
    held_copy.unlink()

    facts = item_payload(config, SEASON, sound=lambda _prefix: None)

    assert player_source(config, held, "compressed") is None
    assert facts is not None
    assert facts["files"][0]["held_copy"] == {"bytes": 40, "present": False}


def test_a_held_copy_outside_every_staging_root_is_never_served(config: MediaforceConfig, tmp_path: Path) -> None:
    with open_db(config.paths.db_path) as connection:
        held = _held(connection, tmp_path, 1, held_copy=tmp_path / "elsewhere" / "Show.S01E01.mkv")
        connection.commit()
    (tmp_path / "staging").mkdir(exist_ok=True)

    assert player_source(config, held, "compressed") is None
    assert player_source(config, held, "original") is not None


def test_a_whole_show_lists_every_season_in_order(config: MediaforceConfig, tmp_path: Path) -> None:
    with open_db(config.paths.db_path) as connection:
        second = _episode(connection, tmp_path, 1, size=10)
        connection.execute(library_items.update().where(library_items.c.id == second).values(
            rel_path="tv/Show/Season 2/Show.S02E01.mkv", parent_dir="tv/Show/Season 2"))
        first = _episode(connection, tmp_path, 3, size=10)
        connection.commit()

    facts = item_payload(config, "tv/Show", sound=lambda _prefix: None)

    assert facts is not None
    assert (facts["kind"], facts["show"], facts["show_prefix"], facts["season"]) == ("tv_series", "Show", "tv/Show", None)
    assert [(row["library_item_id"], row["season"], row["episode"]) for row in facts["files"]] == [(first, 1, 3), (second, 2, 1)]
