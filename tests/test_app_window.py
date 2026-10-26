import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from mediaforce.core.db import DBClient, open_db
from mediaforce.core.db_tables import encode_jobs, item_events, library_items, motion_keep_decisions, staged_artifacts
from mediaforce.encoding.encode_queue import DEFAULT_QUEUE_NAME, save_queue_state
from mediaforce.web.runtime import host_status
from mediaforce.web.runtime.app_window import app_window_payload, tv_shows_payload

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


def _computer(key: str, **fields: Any) -> dict[str, Any]:
    return {
        "key": key, "label": key, "message": "", "available": True, "capabilities": ["encode_queue"],
        "active_encode_count": 0, "active_flag": "active", "schedule_open": True, "queue_active": True, **fields,
    }


def _keep_original(
        connection: DBClient, item_id: int, rel_path: str, *, source_fingerprint: str | None = None, at: str = NOW,
) -> None:
    connection.execute(
        motion_keep_decisions.insert().values(
            library_item_id=item_id, source_fingerprint=source_fingerprint or f"fp-{rel_path}", reason_code="mixed",
            prefix=str(Path(rel_path).parent), kept_at=at,
        )
    )
    connection.commit()


def _payload(
        connection: DBClient,
        *,
        cards: list[dict[str, Any]] | None = None,
        computers: list[dict[str, Any]] | None = None,
        storage_admits_work: bool = True,
) -> dict[str, Any]:
    return app_window_payload(
        connection,
        library_types=LIBRARY_TYPES,
        season_cards=cards or [],
        computers=computers or [],
        controller_storage=READY_STORAGE,
        storage_admits_work=storage_admits_work,
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


def test_tv_shows_list_every_show_the_sidebar_counts(connection: DBClient) -> None:
    _item(connection, "tv/ER/Season 1/e1.mkv", size=1000)
    _replaced(connection, "tv/House/Season 1/e1.mkv", saved=4000, at=NOW)
    _item(connection, "tv/Psych/Season 1/e1.mkv", size=70)
    _item(connection, "tv/ER/pilot.mkv", size=300)
    _item(connection, "tv/Cosmos/e1.mkv", size=200)
    _item(connection, "tv/Cosmos/e2.mkv", size=250)
    _item(connection, "tv/Cosmos/e3.mkv", size=50, missing=True)
    _item(connection, "movies/Heat (1995)/Heat.mkv", size=50)
    cards = [{"prefix": "tv/ER/Season 1", "item_count": 1, "total_size_bytes": 1000, "projected_reclaim_bytes": 600}]

    shows = {show["show"]: show for show in tv_shows_payload(connection, library_types=LIBRARY_TYPES, season_cards=cards)["shows"]}

    assert shows["ER"]["loose_files"] == {"state": "not_in_season_folder", "episode_count": 1, "bytes": 300}
    assert (shows["ER"]["season_count"], shows["ER"]["episode_count"], shows["ER"]["bytes"]) == (1, 2, 1300)
    assert shows["Cosmos"]["loose_files"] == {"state": "not_in_season_folder", "episode_count": 2, "bytes": 450}
    assert (shows["Cosmos"]["season_count"], shows["Cosmos"]["seasons"], shows["Cosmos"]["show_prefix"]) == (0, [], "tv/Cosmos")
    # Seasons with nothing left to compress have no season card, but their shows are still listed.
    house, psych = shows["House"]["seasons"], shows["Psych"]["seasons"]
    assert [(season["state"], season["episode_count"], season["bytes"]) for season in house] == [("finished", 1, 1000)]
    assert [(season["state"], season["episode_count"], season["bytes"]) for season in psych] == [("no_work", 1, 70)]
    assert shows["ER"]["seasons"][0]["state"] is None
    sidebar = _payload(connection, cards=cards)["library"]["tv"]
    assert len(shows) == sidebar["show_count"]
    assert sum(show["episode_count"] for show in shows.values()) == sidebar["episode_count"]


def test_season_is_finished_only_when_every_file_is_replaced_or_kept(connection: DBClient) -> None:
    _replaced(connection, "tv/Futurama/Season 3/e1.mkv", saved=100, at="2026-10-22T01:00:00+00:00")
    _replaced(connection, "tv/Futurama/Season 3/e2.mkv", saved=200, at="2026-10-22T02:00:00+00:00")
    kept_path = "tv/Futurama/Season 3/e3.mkv"
    _keep_original(connection, _item(connection, kept_path), kept_path, at="2026-10-21T00:00:00+00:00")
    _replaced(connection, "tv/Futurama/Season 5/e1.mkv", saved=50, at=NOW)
    lapsed_path = "tv/Futurama/Season 5/e2.mkv"
    _keep_original(connection, _item(connection, lapsed_path), lapsed_path, source_fingerprint="an older source")
    missing_path = "tv/Futurama/Season 6/e1.mkv"
    _keep_original(connection, _item(connection, missing_path, missing=True), missing_path)
    _replaced(connection, "tv/Futurama/Season 4/e1.mkv", saved=999, at="2026-10-23T00:00:00+00:00")
    _item(connection, "tv/Futurama/Season 4/e2.mkv")

    payload = _payload(connection)

    [finished] = payload["recently_finished_seasons"]
    assert (finished["prefix"], finished["season"], finished["episode_count"]) == ("tv/Futurama/Season 3", 3, 2)
    assert finished["bytes_saved"] == 300
    assert finished["kept_original_count"] == 1
    assert finished["finished_at"] == "2026-10-22T02:00:00+00:00"
    assert payload["space_saved"] == {"bytes": 1349, "item_count": 4, "episode_count": 4, "season_count": 1}
    assert payload["kept_original"]["kept_item_count"] == 1
    assert payload["kept_original"]["kept"] == [
        {**_identity("tv/Futurama/Season 3"), "reason": "motion_mixed", "item_count": 1},
    ]


def test_season_kept_entirely_original_is_finished_without_counting_as_space_saved(connection: DBClient) -> None:
    _replaced(connection, "tv/Lucifer/Season 1/e1.mkv", saved=700, at="2026-10-20T00:00:00+00:00")
    for episode, at in (("e1", "2026-10-23T01:00:00+00:00"), ("e2", "2026-10-23T03:00:00+00:00")):
        path = f"tv/Lucifer/Season 2/{episode}.mkv"
        _keep_original(connection, _item(connection, path), path, at=at)
    unsettled_path = "tv/Lucifer/Season 3/e1.mkv"
    _keep_original(connection, _item(connection, unsettled_path), unsettled_path, at=NOW)
    _item(connection, "tv/Lucifer/Season 3/e2.mkv")

    payload = _payload(connection)

    assert [season["prefix"] for season in payload["recently_finished_seasons"]] == [
        "tv/Lucifer/Season 2", "tv/Lucifer/Season 1",
    ]
    kept_season = payload["recently_finished_seasons"][0]
    assert (kept_season["episode_count"], kept_season["kept_original_count"], kept_season["bytes_saved"]) == (0, 2, 0)
    assert kept_season["finished_at"] == "2026-10-23T03:00:00+00:00"
    assert payload["space_saved"] == {"bytes": 700, "item_count": 1, "episode_count": 1, "season_count": 1}


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
    computers = [
        _computer("studio", active_encode_count=1, active_flag="busy"),
        _computer("mbp"),
        _computer("night", schedule_open=False, active_flag="idle"),
        _computer("cooling", active_flag="cooldown"),
        _computer("sampler", capabilities=["sample_calibration"]),
        _computer("mini", available=False, check_failure="password_needed"),
        _computer("server", available=False, missing_mounts=["/Volumes/media"]),
        _computer("odd", available=False),
        _computer("staging", controller_storage_issue="Controller storage needs a fresh check.", queue_active=False),
        _computer("refused", queue_active=False),
        _computer("new", available=False, message=host_status._HOST_STATUS_PENDING_MESSAGE),
    ]

    states = [(computer["key"], computer["state"]) for computer in _payload(connection, computers=computers)["computers"]]

    assert states == [
        ("studio", "busy"), ("mbp", "ready"), ("night", "outside_schedule"), ("cooling", "cooling_down"),
        ("sampler", "not_encoding"), ("mini", "password_needed"), ("server", "storage_unreachable"),
        ("odd", "not_ready"), ("staging", "storage_unreachable"), ("refused", "not_ready"), ("new", "checking"),
    ]


@pytest.mark.parametrize(
    "paused,storage_admits_work,queued,computer_fields,expected_state,expected_reasons",
    [
        (False, True, False, {}, "idle", []),
        (True, True, False, {}, "paused", []),
        (False, False, False, {}, "stopped", ["storage_unavailable"]),
        (True, False, False, {}, "paused", ["storage_unavailable"]),
        (False, True, True, {"schedule_open": False}, "stopped", ["no_computer_ready"]),
        (False, True, False, {"schedule_open": False}, "idle", []),
        (False, True, True, {"controller_storage_issue": "Storage needs a check.", "queue_active": False}, "stopped",
         ["no_computer_ready"]),
    ],
)
def test_work_says_whether_it_is_paused_and_why_it_stopped(
        connection: DBClient, paused: bool, storage_admits_work: bool, queued: bool, computer_fields: dict[str, Any],
        expected_state: str, expected_reasons: list[str],
) -> None:
    save_queue_state(connection, {"queue_name": DEFAULT_QUEUE_NAME, "is_paused": paused, "updated_at": NOW})
    connection.commit()
    if queued:
        _encode_job(connection, "tv/A/Season 1", status="queued")

    work = _payload(
        connection, computers=[_computer("mbp", **computer_fields)], storage_admits_work=storage_admits_work,
    )["work"]

    assert (work["state"], work["paused_by_owner"], work["stop_reasons"]) == (expected_state, paused, expected_reasons)


def test_work_names_each_season_being_compressed_once(connection: DBClient) -> None:
    _encode_job(connection, "tv/A/Season 2", status="running", host_key="mbp")
    _encode_job(connection, "tv/B/Season 1", status="queued")
    connection.execute(
        encode_jobs.insert().values(
            job_id="job-shard", prefix="tv/A/Season 2", job_kind="shard", status="running", host_json="{}",
            manifest_path="/runs/manifest.json", created_at=NOW, updated_at=NOW,
        )
    )
    connection.commit()

    work = _payload(connection, computers=[_computer("mbp")])["work"]

    assert [(season["show"], season["season"]) for season in work["running_seasons"]] == [("A", 2)]


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


@pytest.mark.parametrize("absence,state,expected", [
    ("away", "retrying", "away"),
    ("reconnecting", "retrying", "reconnecting"),
    ("unreachable_long", "retrying", "unreachable_long"),
    (None, "action_required", "recovery_required"),
    (None, "retrying", "reconnecting"),
])
def test_computer_recovery_state_reaches_native_app(
        connection: DBClient, absence: str | None, state: str, expected: str,
) -> None:
    row = _computer("worker", available=False, recovery={
        "status": state, "absence_state": absence, "reason": "Storage check needs help",
    })
    [computer] = _payload(connection, computers=[row])["computers"]
    assert computer["state"] == expected
    assert computer["attention_reason"] == row["recovery"]["reason"]
