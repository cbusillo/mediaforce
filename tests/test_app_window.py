import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from mediaforce.core.db import DBClient, open_db
from mediaforce.core.db_tables import encode_jobs, item_events, library_items, motion_keep_decisions, staged_artifacts
from mediaforce.encoding.encode_queue import DEFAULT_QUEUE_NAME, save_queue_state
from mediaforce.hosts.types import HostStatus
from mediaforce.web.runtime import host_status
from mediaforce.web.runtime.app_window import app_window_payload

LIBRARY_TYPES = {"tv": "tv", "movies": "movie", "other": "other"}
READY_STORAGE = {"mounts": [{"mount_point": "/Volumes/media", "status": "ready", "last_failure_kind": None}]}
NOW = "2026-10-24T12:00:00+00:00"


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[DBClient]:
    with open_db(tmp_path / "library.sqlite3") as db:
        yield db


def _item(connection: DBClient, rel_path: str, *, size: int = 1000, status: str = "discovered", missing: bool = False) -> int:
    result = connection.execute(
        library_items.insert().values(
            source_path=f"/media/{rel_path}",
            rel_path=rel_path,
            media_root=rel_path.split("/")[0],
            parent_dir=str(Path(rel_path).parent),
            file_name=Path(rel_path).name,
            container=".mkv",
            size_bytes=size,
            mtime_ns=1,
            fingerprint=f"fp-{rel_path}",
            audio_summary_json="[]",
            subtitle_summary_json="[]",
            status=status,
            missing_since=NOW if missing else None,
            last_scan_id="scan-1",
            discovered_at=NOW,
            last_seen_at=NOW,
            updated_at=NOW,
        )
    )
    connection.commit()
    return int(result.inserted_primary_key[0])


def _replaced(connection: DBClient, rel_path: str, *, saved: int, at: str, original_size: int = 5000) -> int:
    item_id = _item(connection, rel_path, size=original_size - saved, status="promoted")
    connection.execute(
        staged_artifacts.insert().values(
            library_item_id=item_id,
            staging_path=f"/staging/{rel_path}",
            source_size_bytes=original_size,
            bytes_saved=saved,
            promoted_at=at,
            archived_source_path=f"/media/transcode/_replaced/{rel_path}",
            updated_at=at,
        )
    )
    connection.commit()
    return item_id


def _event(connection: DBClient, item_id: int, event_type: str, at: str) -> None:
    connection.execute(
        item_events.insert().values(library_item_id=item_id, created_at=at, event_type=event_type, details_json="{}")
    )
    connection.commit()


def _encode_job(connection: DBClient, prefix: str, *, status: str = "completed", host_key: str = "") -> None:
    connection.execute(
        encode_jobs.insert().values(
            job_id=f"job-{prefix}-{status}",
            prefix=prefix,
            status=status,
            manifest_path="/runs/manifest.json",
            host_json=json.dumps({"key": host_key}),
            created_at=NOW,
            updated_at=NOW,
        )
    )
    connection.commit()


def _host(key: str, *, available: bool = False, **fields: Any) -> HostStatus:
    return HostStatus(
        key=key, label=key, mode="ssh", priority=1, capabilities=["encode_queue"], available=available,
        message="", missing_paths=[], **fields,
    )


def _payload(
        connection: DBClient,
        *,
        cards: list[dict[str, Any]] | None = None,
        hosts: list[HostStatus] | None = None,
        storage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return app_window_payload(
        connection,
        library_types=LIBRARY_TYPES,
        season_cards=cards or [],
        host_statuses=hosts or [],
        controller_storage=storage or READY_STORAGE,
        observed_at=datetime(2026, 10, 24, 12, tzinfo=UTC),
    )


def test_library_totals_count_present_files_and_bytes_still_original(connection: DBClient) -> None:
    _item(connection, "tv/House/Season 1/e1.mkv", size=300)
    _item(connection, "tv/House/Season 2/e1.mkv", size=200)
    _item(connection, "tv/Psych/Season 1/e1.mkv", size=100, missing=True)
    _replaced(connection, "tv/House/Season 2/e2.mkv", saved=4000, at=NOW)
    _item(connection, "movies/Heat (1995)/Heat.mkv", size=50)
    _item(connection, "other/Concert/part1.mkv", size=7)

    library = _payload(connection)["library"]

    assert library["tv"] == {
        "show_count": 1, "season_count": 2, "episode_count": 3, "total_bytes": 1500, "original_bytes": 500,
    }
    assert library["movies"] == {"title_count": 1, "total_bytes": 50, "original_bytes": 50}
    assert library["other"] == {"item_count": 1, "total_bytes": 7, "original_bytes": 7}


def test_season_is_finished_only_when_every_file_is_replaced_or_kept(connection: DBClient) -> None:
    _replaced(connection, "tv/Futurama/Season 3/e1.mkv", saved=100, at="2026-10-22T01:00:00+00:00")
    _replaced(connection, "tv/Futurama/Season 3/e2.mkv", saved=200, at="2026-10-22T02:00:00+00:00")
    kept_id = _item(connection, "tv/Futurama/Season 3/e3.mkv")
    connection.execute(
        motion_keep_decisions.insert().values(
            library_item_id=kept_id, source_fingerprint="fp", reason_code="mixed",
            prefix="tv/Futurama/Season 3", kept_at=NOW,
        )
    )
    connection.commit()
    _replaced(connection, "tv/Futurama/Season 4/e1.mkv", saved=999, at="2026-10-23T00:00:00+00:00")
    _item(connection, "tv/Futurama/Season 4/e2.mkv")

    payload = _payload(connection)

    [finished] = payload["recently_finished_seasons"]
    assert (finished["prefix"], finished["season"], finished["episode_count"]) == ("tv/Futurama/Season 3", 3, 2)
    assert finished["bytes_saved"] == 300
    assert finished["kept_original_count"] == 1
    assert finished["finished_at"] == "2026-10-22T02:00:00+00:00"
    assert payload["space_saved"] == {"bytes": 1299, "item_count": 3, "episode_count": 3, "season_count": 1}
    assert payload["kept_original"]["kept"] == [
        {**_identity("tv/Futurama/Season 3"), "reason": "motion_mixed", "item_count": 1},
    ]


def _identity(prefix: str) -> dict[str, Any]:
    show_prefix, season_folder = prefix.rsplit("/", 1)
    return {
        "prefix": prefix, "show_prefix": show_prefix, "show": show_prefix.split("/")[1],
        "season_folder": season_folder, "season": int(season_folder.split()[-1]),
    }


def test_biggest_not_started_leaves_out_shows_with_any_work(connection: DBClient) -> None:
    _item(connection, "tv/ER/Season 1/e1.mkv", size=900)
    _item(connection, "tv/ER/Season 2/e1.mkv", size=900)
    _item(connection, "tv/Psych/Season 1/e1.mkv", size=500)
    _item(connection, "tv/House/Season 1/e1.mkv", size=5000)
    _encode_job(connection, "tv/House/Season 1")
    _item(connection, "tv/Lucifer/Season 1/e1.mkv", size=4000)
    _replaced(connection, "tv/Lucifer/Season 2/e1.mkv", saved=10, at=NOW)
    _item(connection, "tv/Monk/Season 1/e1.mkv", size=3000)
    cards = [
        {"prefix": "tv/Psych/Season 1", "page_state": {"key": "ready_to_compare", "primary_action": "review_sample"}},
        {"prefix": "tv/Monk/Season 1", "page_state": {"key": "ready_to_make", "primary_action": "queue_encode"}},
    ]

    biggest = _payload(connection, cards=cards)["biggest_not_started"]

    assert [show["show"] for show in biggest] == ["ER", "Psych"]
    assert biggest[0] == {
        "show_prefix": "tv/ER", "show": "ER", "bytes": 1800, "episode_count": 2, "season_count": 2,
        "waiting_sample_prefix": None,
    }
    assert biggest[1]["waiting_sample_prefix"] == "tv/Psych/Season 1"


def test_samples_split_into_waiting_on_owner_and_approved_not_started(connection: DBClient) -> None:
    cards = [
        {"prefix": "tv/A/Season 1", "page_state": {"key": "needs_help", "recovery_kind": "test",
                                                   "primary_action": "make_sample", "failure": "missing_clips"}},
        {"prefix": "tv/B/Season 2", "page_state": {"key": "ready_to_compare", "primary_action": "review_sample"}},
        {"prefix": "tv/C/Season 1", "page_state": {"key": "ready_to_make", "primary_action": "queue_encode"}},
        {"prefix": "tv/D/Season 1", "page_state": {"key": "needs_help", "recovery_kind": "files"}},
        {"prefix": "tv/E/Season 1", "page_state": {"key": "needs_test", "primary_action": "make_sample"}},
    ]

    samples = _payload(connection, cards=cards)["samples"]

    assert [(sample["prefix"], sample["state"], sample["action"]) for sample in samples["waiting_on_you"]] == [
        ("tv/B/Season 2", "ready_to_compare", "review_sample"),
        ("tv/A/Season 1", "needs_help", "make_sample"),
    ]
    assert samples["waiting_on_you"][1]["failure"] == "missing_clips"
    assert [sample["prefix"] for sample in samples["approved_not_started"]] == ["tv/C/Season 1"]


def test_computer_states_are_codes(connection: DBClient) -> None:
    _encode_job(connection, "tv/A/Season 1", status="running", host_key="studio")
    pending = _host("new")
    pending.message = host_status._HOST_STATUS_PENDING_MESSAGE
    hosts = [
        _host("studio", available=True),
        _host("mbp", available=True),
        _host("mini", check_failure="password_needed", setup_requires_password=True),
        _host("server", missing_mounts=["/Volumes/media"]),
        pending,
    ]

    computers = _payload(connection, hosts=hosts)["computers"]

    assert [(computer["key"], computer["state"], computer["running_count"]) for computer in computers] == [
        ("studio", "busy", 1),
        ("mbp", "ready", 0),
        ("mini", "password_needed", 0),
        ("server", "storage_unreachable", 0),
        ("new", "checking", 0),
    ]


@pytest.mark.parametrize(
    "paused,storage_status,expected_state,expected_reasons",
    [
        (False, "ready", "idle", []),
        (True, "ready", "paused", []),
        (False, "retrying", "stopped", ["storage_unreachable"]),
        (True, "action_required", "paused", ["storage_unreachable"]),
    ],
)
def test_work_says_whether_it_is_paused_and_why_it_stopped(
        connection: DBClient, paused: bool, storage_status: str, expected_state: str, expected_reasons: list[str],
) -> None:
    save_queue_state(connection, {"queue_name": DEFAULT_QUEUE_NAME, "is_paused": paused, "updated_at": NOW})
    connection.commit()
    storage = {"mounts": [{"mount_point": "/Volumes/media", "status": storage_status, "last_failure_kind": "mount_absent"}]}

    work = _payload(connection, storage=storage)["work"]

    assert (work["state"], work["paused_by_owner"], work["stop_reasons"]) == (expected_state, paused, expected_reasons)


def test_original_backups_leave_out_originals_already_removed(connection: DBClient) -> None:
    _replaced(connection, "tv/A/Season 1/e1.mkv", saved=1, at=NOW, original_size=700)
    removed = _replaced(connection, "tv/A/Season 1/e2.mkv", saved=1, at=NOW, original_size=900)
    _event(connection, removed, "originals_removed_confirmed", NOW)

    assert _payload(connection)["original_backups"] == {"count": 1, "bytes": 700}


def test_events_newest_first_with_last_change(connection: DBClient) -> None:
    item_id = _replaced(connection, "tv/A/Season 1/e1.mkv", saved=42, at=NOW)
    _event(connection, item_id, "encoding_completed", "2026-10-24T01:00:00+00:00")
    _event(connection, item_id, "automatic_publish_waiting", "2026-10-24T02:00:00+00:00")
    _event(connection, item_id, "promotion_completed", "2026-10-24T03:00:00+00:00")

    payload = _payload(connection)

    assert [(event["kind"], event["bytes_saved"]) for event in payload["events"]] == [
        ("promotion_completed", 42), ("encoding_completed", None),
    ]
    assert payload["last_change"] == payload["events"][0]
    assert payload["last_change"]["season"]["prefix"] == "tv/A/Season 1"


def test_held_seasons_carry_their_reason_codes(connection: DBClient) -> None:
    cards = [
        {"prefix": "tv/Pitt/Season 2", "page_state": {"key": "needs_test"},
         "lifecycle": {"seasons": [{"hold_reasons": [{"code": "current_season"}, {"code": "recent_acquisition"}]}]}},
        {"prefix": "tv/Expanse/Season 1", "page_state": {"key": "motion_held"}, "lifecycle": {"seasons": []}},
        {"prefix": "tv/Monk/Season 1", "page_state": {"key": "needs_test"}, "lifecycle": None},
    ]

    held = _payload(connection, cards=cards)["kept_original"]["held"]

    assert [(season["prefix"], season["reasons"]) for season in held] == [
        ("tv/Pitt/Season 2", ["current_season", "recent_acquisition"]),
        ("tv/Expanse/Season 1", ["motion_pattern"]),
    ]
