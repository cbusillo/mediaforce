import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, event, select

from mediaforce import cli
from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import library_items, metadata, motion_keep_decisions, series_metadata, source_admissions, source_generations, source_measurements
from mediaforce.core.evidence import stable_json_hash
from mediaforce.core.utils import timestamp
from mediaforce.library.content_kinds import EpisodeTraits, season_kind, season_kind_report
from mediaforce.library.source_evidence import HISTORICAL_OUTPUT_PROFILES, append_measurement, append_version, json_text, \
    load_source_evidence, load_source_evidence_bulk
from mediaforce.web.runtime.folder_state import state_slug
from mediaforce.web.runtime.folder_tuning_advice import calibration_policy_hash


@pytest.fixture
def connection() -> Iterator[DBClient]:
    engine = create_engine("sqlite://")
    with engine.connect() as database:
        database.exec_driver_sql("PRAGMA foreign_keys=ON")
        metadata.create_all(database)
        database.commit()
        yield database
    engine.dispose()


def _episode(dark: float, *, repeated: float = 0.05, height: int = 1080) -> EpisodeTraits:
    return EpisodeTraits(dark_frame_fraction=dark, repeated_frame_fraction=repeated, height=height, width=height * 16 // 9)


def _fingerprint(dark: float) -> dict[str, object]:
    return {"schema_version": 1, "analysis": {"aggregate": {"dark_frame_fraction": dark, "duplicate_like_frame_fraction": 0.05}},
            "decision": {"status": "measured", "traits": ["typical"]}}


def test_close_measurements_of_one_show_land_in_one_kind() -> None:
    darker = season_kind([_episode(0.31), _episode(0.17), _episode(0.24), _episode(0.45)], animated=False)
    bright = season_kind([_episode(0.02), _episode(0.12), _episode(0.08)], animated=False)

    assert darker.kind == "darker_live_action"
    assert bright.kind == "bright_live_action"


def test_a_season_spanning_two_kinds_is_mixed() -> None:
    assert season_kind([_episode(0.5), _episode(0.4), _episode(0.01), _episode(0.03)], animated=False).kind == "mixed"
    assert season_kind([_episode(0.5), _episode(0.4, height=2160)], animated=False).kind == "mixed"


def test_cartoons_and_4k_never_join_a_live_action_kind() -> None:
    assert season_kind([_episode(0.4), _episode(0.5)], animated=True).kind == "cartoon"
    assert season_kind([_episode(0.4, height=2160)], animated=False).kind == "4k"
    assert season_kind([_episode(0.4)], animated=None).kind == "unmeasured"
    assert season_kind([_episode(0.4, repeated=0.6), _episode(0.4, repeated=0.7)], animated=False).kind == "mixed"


def test_unmeasured_episodes_do_not_vote() -> None:
    assert season_kind([None, _episode(0.4), None], animated=False).kind == "darker_live_action"
    assert season_kind([None, None], animated=False).kind == "unmeasured"


def test_a_known_4k_original_counts_without_a_fingerprint() -> None:
    unmeasured_4k = EpisodeTraits(dark_frame_fraction=None, repeated_frame_fraction=None, height=2160, width=3840)
    unmeasured_hd = EpisodeTraits(dark_frame_fraction=None, repeated_frame_fraction=None, height=1080, width=1920)

    assert season_kind([_episode(0.4), _episode(0.5), unmeasured_4k], animated=False).kind == "mixed"
    assert season_kind([_episode(0.4), unmeasured_hd], animated=False).kind == "darker_live_action"


def _item(connection: DBClient, rel_path: str, *, status: str, codec: str, current_dark: float) -> int:
    now = timestamp()
    result = connection.execute(library_items.insert().values(
        source_path=f"/media/{rel_path}", rel_path=rel_path, media_root="tv", parent_dir=rel_path.rsplit("/", 1)[0],
        file_name=rel_path.rsplit("/", 1)[1], container=".mkv", size_bytes=1000, mtime_ns=1, fingerprint=rel_path,
        content_version_fingerprint=f"sample-{rel_path}", video_codec=codec, width=1920, height=1080, status=status,
        media_fingerprint_json=json.dumps(_fingerprint(current_dark)), audio_summary_json="[]",
        subtitle_summary_json="[]", last_scan_id="fixture", discovered_at=now,
        last_seen_at=now, updated_at=now,
    ))
    assert result.inserted_primary_key is not None
    return int(result.inserted_primary_key[0])


def _admit_historical_original(connection: DBClient, item_id: int, *, dark: float) -> None:
    now = timestamp()
    generation = f"generation-{item_id}"
    connection.execute(source_generations.insert().values(generation_id=generation, created_at=now))
    output = append_version(connection, generation_id=generation, role="output", recorded_at=now,
                            identity={"size_bytes": 1000, "sample_fingerprint": f"output-{item_id}"},
                            profile={"video_codec": "av1"})
    connection.execute(library_items.update().where(library_items.c.id == item_id).values(
        active_source_version_id=output, content_version_fingerprint=f"output-{item_id}"))
    original = append_version(connection, generation_id=generation, role="input", recorded_at=now,
                              identity={"size_bytes": 5000, "sample_fingerprint": f"original-{item_id}",
                                        "unavailability": "removed"},
                              profile={"video_codec": "h264", "width": 1920, "height": 1080})
    rule = stable_json_hash(HISTORICAL_OUTPUT_PROFILES)
    plan = {"plan_sha256": f"plan-{item_id}"}
    connection.execute(source_admissions.insert().values(
        admission_id=f"admission-{item_id}", generation_id=generation, version_id=original, tier="probable",
        basis="historical-plan", recorded_at=now,
        checks_json=json_text({"codec_rule_hash": rule, "encode_link": True, "unavailability": "removed", "plans": [plan]}),
    ))
    append_measurement(connection, version_id=original, kind="media_fingerprint", binding="historical-plan-sampled",
                       summary=_fingerprint(dark), provenance={"basis": "historical-plan", "codec_rule_hash": rule, **plan},
                       measured_at=None, recorded_at=now)


def test_report_groups_waiting_seasons_by_original_traits_only(connection: DBClient) -> None:
    published = _item(connection, "tv/Drama/Season 1/e01.mkv", status="promoted", codec="av1", current_dark=0.0)
    _admit_historical_original(connection, published, dark=0.5)
    _item(connection, "tv/Drama/Season 1/e02.mkv", status="discovered", codec="h264", current_dark=0.0)
    _item(connection, "tv/Drama/Season 2/e01.mkv", status="discovered", codec="h264", current_dark=0.5)
    _item(connection, "tv/Drama/Season 3/e01.mkv", status="discovered", codec="h264", current_dark=0.5)
    connection.execute(series_metadata.insert().values(series_prefix="tv/Drama", tmdb_genres_json='["Drama"]',
                                                       updated_at=timestamp()))

    report = season_kind_report(connection, library_types={"tv": "tv"},
                                folder_approved=lambda prefix: prefix == "tv/Drama/Season 3")

    seasons = {row["season"]: row for row in report["seasons"]}
    # The published episode's output measures bright; only its original's darkness counts.
    assert seasons["tv/Drama/Season 1"]["kind"] == "darker_live_action"
    # A waiting episode's current file is not yet an admitted original.
    assert seasons["tv/Drama/Season 2"]["kind"] == "unmeasured"
    assert "tv/Drama/Season 3" not in seasons
    assert report["totals"]["folder_approved"]["seasons"] == 1


def test_bulk_evidence_matches_individual_reads_and_report_queries_stay_bounded(connection: DBClient) -> None:
    item_ids = []
    for episode in range(3):
        item_id = _item(connection, f"tv/Drama/Season 1/e{episode:02}.mkv", status="promoted", codec="av1",
                        current_dark=0.0)
        _admit_historical_original(connection, item_id, dark=0.5)
        item_ids.append(item_id)
    _item(connection, "tv/Drama/Season 1/waiting.mkv", status="discovered", codec="h264", current_dark=0.0)
    connection.execute(series_metadata.insert().values(series_prefix="tv/Drama", tmdb_genres_json='["Drama"]',
                                                       updated_at=timestamp()))

    expected = {item_id: load_source_evidence(connection, item_id) for item_id in item_ids}
    assert load_source_evidence_bulk(connection, item_ids) == expected

    query_count = 0

    def count_query(*_args: object) -> None:
        nonlocal query_count
        query_count += 1

    event.listen(connection.engine, "before_cursor_execute", count_query)
    try:
        season_kind_report(connection, library_types={"tv": "tv"}, folder_approved=lambda _prefix: False)
        first_queries = query_count
        for episode in range(3, 30):
            item_id = _item(connection, f"tv/Drama/Season 1/e{episode:02}.mkv", status="promoted", codec="av1", current_dark=0.0)
            _admit_historical_original(connection, item_id, dark=0.5)
        query_count = 0
        first = season_kind_report(connection, library_types={"tv": "tv"}, folder_approved=lambda _prefix: False)
        assert query_count == first_queries
        original = expected[item_ids[0]]
        assert original.original_version_id is not None
        append_measurement(
            connection, version_id=original.original_version_id, kind="media_fingerprint",
            binding="historical-plan-sampled", summary=_fingerprint(0.0),
            provenance={"basis": "historical-plan", "codec_rule_hash": stable_json_hash(HISTORICAL_OUTPUT_PROFILES),
                        "plan_sha256": f"plan-{item_ids[0]}"}, measured_at=None, recorded_at=timestamp(),
        )
        query_count = 0
        refreshed = season_kind_report(connection, library_types={"tv": "tv"}, folder_approved=lambda _prefix: False)
        refreshed_queries = query_count
    finally:
        event.remove(connection.engine, "before_cursor_execute", count_query)

    assert first["seasons"][0]["kind"] == "darker_live_action"
    assert refreshed["seasons"][0]["measured"] == first["seasons"][0]["measured"] - 1
    assert refreshed_queries == first_queries


def test_large_evidence_batch_respects_sqlite_parameter_limits(connection: DBClient) -> None:
    item_ids = []
    for episode in range(600):
        item_id = _item(connection, f"tv/Drama/Season 1/{episode}.mkv", status="promoted", codec="av1", current_dark=0.0)
        _admit_historical_original(connection, item_id, dark=0.5)
        item_ids.append(item_id)
    for row in connection.execute(select(source_admissions.c.admission_id, source_measurements.c.measurement_id)
                                  .join(source_measurements, source_measurements.c.version_id == source_admissions.c.version_id)):
        connection.execute(source_admissions.update().where(source_admissions.c.admission_id == row.admission_id)
                           .values(basis="readable-source", checks_json=json_text({"measurement_ids": [row.measurement_id]})))
    extra = append_measurement(connection, version_id=None, kind="media_fingerprint", binding="unbound",
                               summary=_fingerprint(0.5), provenance={}, measured_at=None, recorded_at="now")
    connection.execute(source_admissions.update().where(source_admissions.c.admission_id == f"admission-{item_ids[0]}")
                       .values(checks_json=json_text({"measurement_ids": [extra]})))
    raw = connection.connection.driver_connection
    assert isinstance(raw, sqlite3.Connection)
    previous_limit = raw.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 550)
    try:
        assert set(load_source_evidence_bulk(connection, item_ids)) == set(item_ids)
    finally:
        raw.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, previous_limit)


def _config(root: Path) -> MediaforceConfig:
    return MediaforceConfig(raw={"media": {"source_roots": {"tv": str(root / "tv")}}},
                            paths=ConfigPaths(project_root=root, config_path=root / "config.toml", db_path=root / "db.sqlite3",
                                              run_manifest_dir=root / "runs", web_state_dir=root / "web", review_dir=root / "review",
                                              runtime_settings_path=root / "settings.json"))


def _save_approval(config: MediaforceConfig, prefix: str, *, job_id: str) -> None:
    calibration: dict[str, Any] = {
        "mode": "sample", "job_id": job_id, "draft_hash": "draft", "review_media_ready": True,
        "browser_review_ready": True, "policy": {"video": {"preset": 6}},
        "review_pairs": [{"source_clip": {"path": "/review/source.mp4"}, "preview_clip": {"path": "/review/new.mp4"}}],
        "accepted_sample_job_id": "sample-approved", "accepted_at": "2026-10-25T12:00:00+00:00",
        "accepted_draft_hash": "draft",
    }
    calibration["accepted_policy_hash"] = calibration_policy_hash(calibration)
    config.paths.web_state_dir.mkdir(parents=True, exist_ok=True)
    (config.paths.web_state_dir / f"{state_slug(prefix)}.json").write_text(json.dumps(calibration))


def test_only_a_current_folder_approval_takes_a_season_out_of_waiting(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _save_approval(config, "tv/Drama", job_id="sample-approved")
    _save_approval(config, "tv/Other/Season 1", job_id="sample-resampled")

    assert cli._current_folder_approval(config, "tv/Drama/Season 2")
    assert not cli._current_folder_approval(config, "tv/Other/Season 1")
    assert not cli._current_folder_approval(config, "tv/Unknown/Season 1")


def test_a_file_kept_original_no_longer_waits(connection: DBClient) -> None:
    kept = _item(connection, "tv/Drama/Season 1/e01.mkv", status="discovered", codec="h264", current_dark=0.0)
    connection.execute(motion_keep_decisions.insert().values(
        library_item_id=kept, source_fingerprint="sample-tv/Drama/Season 1/e01.mkv", reason_code="motion",
        prefix="tv/Drama/Season 1", kept_at=timestamp(),
    ))

    report = season_kind_report(connection, library_types={"tv": "tv"}, folder_approved=lambda _prefix: False)

    assert report["seasons"] == []


@pytest.mark.parametrize("codec,roots", [("wmv2", ["tv"]), ("h264", [])])
def test_unmeasured_seasons_outside_automatic_intake_name_the_prerequisite(
        connection: DBClient, codec: str, roots: list[str],
) -> None:
    _item(connection, "tv/Old show/Season 1/e1.mkv", status="discovered", codec=codec, current_dark=0.5)
    row = season_kind_report(connection, library_types={"tv": "tv"}, folder_approved=lambda _prefix: False,
                             measurement_roots=roots)["seasons"][0]
    assert row["kind"] == "unmeasured"
    assert row["measurement_reasons"] == ["automatic_measurement_unavailable"]
