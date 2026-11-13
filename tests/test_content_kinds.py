import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import create_engine, event, select

from mediaforce import cli
from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import library_items, metadata, motion_keep_decisions, series_metadata, source_admissions, source_generations, source_measurements, source_versions, source_transitions, staged_artifacts, target_production_outcomes
from mediaforce.core.evidence import stable_json_hash
from mediaforce.core.utils import timestamp
from mediaforce.library.content_kinds import season_kind, season_kind_report
from mediaforce.library.calculation_reuse import LibraryCalculations
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
        with LibraryCalculations().request(database):
            yield database
    engine.dispose()


def _fingerprint(dark: float, *, repeated: float = 0.05) -> dict[str, object]:
    return {"schema_version": 1, "analysis": {"aggregate": {"dark_frame_fraction": dark, "duplicate_like_frame_fraction": repeated}},
            "decision": {"status": "measured", "traits": ["typical"]}}


def _measured_season(connection: DBClient, season: str, originals: list[tuple[float, float]]) -> None:
    """A season whose published episodes' originals measured each (darkness, repeated frames), and one still waiting."""
    for episode, (dark, repeated) in enumerate(originals):
        published = _item(connection, f"{season}/e0{episode}.mkv", status="promoted", codec="av1", current_dark=0.0)
        _admit_historical_original(connection, published, dark=dark, repeated=repeated)
    _item(connection, f"{season}/e99.mkv", status="discovered", codec="h264", current_dark=0.5)


def test_every_measured_live_action_season_is_one_look_however_dark_or_repetitive(connection: DBClient) -> None:
    seasons = {
        "tv/Noir/Season 1": [(0.31, 0.05), (0.24, 0.05)],  # was darker
        "tv/Sunny/Season 1": [(0.02, 0.05), (0.08, 0.05)],  # was bright
        "tv/Dusk/Season 1": [(0.12, 0.05), (0.15, 0.05)],  # was between the two, so in no kind
        "tv/Split/Season 1": [(0.5, 0.05), (0.4, 0.05), (0.01, 0.05), (0.03, 0.05)],  # was mixed
        "tv/Stills/Season 1": [(0.4, 0.6), (0.4, 0.7), (0.3, 0.05)],  # repeated frames could never win, so was mixed
        "tv/Toons/Season 1": [(0.4, 0.6)],
        "tv/Unknown/Season 1": [(0.4, 0.05)],
    }
    for season, originals in seasons.items():
        _measured_season(connection, season, originals)
    for show, genres in [("Noir", '["Drama"]'), ("Sunny", '["Reality"]'), ("Dusk", '["Drama"]'), ("Split", '["Comedy"]'),
                         ("Stills", '["Documentary"]'), ("Toons", '["Animation", "Comedy"]')]:
        connection.execute(series_metadata.insert().values(series_prefix=f"tv/{show}", tmdb_genres_json=genres, updated_at=timestamp()))

    report = season_kind_report(connection, library_types={"tv": "tv"}, folder_approved=lambda _prefix: False)

    kinds = {row["season"]: (row["kind"], row["reason"]) for row in report["seasons"]}
    assert kinds == {**{season: ("live_action", None) for season in list(seasons)[:5]},
                     "tv/Toons/Season 1": ("cartoon", None), "tv/Unknown/Season 1": ("unmeasured", "genres_unknown")}


def test_unmeasured_episodes_do_not_vote() -> None:
    assert season_kind([False, True, False], animated=False).kind == "live_action"
    assert season_kind([False, False], animated=True).kind == "unmeasured"


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


def _admit_historical_original(connection: DBClient, item_id: int, *, dark: float, height: int = 1080, repeated: float = 0.05) -> None:
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
                              profile={"video_codec": "h264", "width": height * 16 // 9, "height": height})
    rule = stable_json_hash(HISTORICAL_OUTPUT_PROFILES)
    plan = {"plan_sha256": f"plan-{item_id}"}
    connection.execute(source_admissions.insert().values(
        admission_id=f"admission-{item_id}", generation_id=generation, version_id=original, tier="probable",
        basis="historical-plan", recorded_at=now,
        checks_json=json_text({"codec_rule_hash": rule, "encode_link": True, "unavailability": "removed", "plans": [plan]}),
    ))
    append_measurement(connection, version_id=original, kind="media_fingerprint", binding="historical-plan-sampled",
                       summary=_fingerprint(dark, repeated=repeated), provenance={"basis": "historical-plan", "codec_rule_hash": rule, **plan},
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
    # The published episode's admitted original is measured; its output never counts.
    assert seasons["tv/Drama/Season 1"]["kind"] == "live_action"
    # A waiting episode's current file is not yet an admitted original.
    assert seasons["tv/Drama/Season 2"]["kind"] == "unmeasured"
    assert "tv/Drama/Season 3" not in seasons
    assert report["totals"]["folder_approved"]["seasons"] == 1


def test_a_4k_original_takes_its_kind_from_its_other_traits(connection: DBClient) -> None:
    for season, heights in (("Season 1", (2160, 2160)), ("Season 2", (2160, 1080))):
        for episode, height in enumerate(heights):
            published = _item(connection, f"tv/Space/{season}/e0{episode}.mkv", status="promoted", codec="av1", current_dark=0.0)
            _admit_historical_original(connection, published, dark=0.5, height=height)
        _item(connection, f"tv/Space/{season}/e09.mkv", status="discovered", codec="h264", current_dark=0.5)
    connection.execute(series_metadata.insert().values(series_prefix="tv/Space", tmdb_genres_json='["Drama"]', updated_at=timestamp()))

    report = season_kind_report(connection, library_types={"tv": "tv"}, folder_approved=lambda _prefix: False)

    # A season all in 4K and one only partly in 4K both measure like any other season of their look.
    assert {row["season"]: row["kind"] for row in report["seasons"]} == {
        "tv/Space/Season 1": "live_action", "tv/Space/Season 2": "live_action",
    }


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

    def report() -> dict[str, Any]:
        with LibraryCalculations().request(connection):
            return season_kind_report(connection, library_types={"tv": "tv"}, folder_approved=lambda _prefix: False)

    event.listen(connection.engine, "before_cursor_execute", count_query)
    try:
        report()
        first_queries = query_count
        for episode in range(3, 30):
            item_id = _item(connection, f"tv/Drama/Season 1/e{episode:02}.mkv", status="promoted", codec="av1", current_dark=0.0)
            _admit_historical_original(connection, item_id, dark=0.5)
        query_count = 0
        first = report()
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
        refreshed = report()
        refreshed_queries = query_count
    finally:
        event.remove(connection.engine, "before_cursor_execute", count_query)

    assert first["seasons"][0]["kind"] == "live_action"
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


@pytest.mark.parametrize("codec,roots,reason", [("wmv2", ["tv"], "original_format_unidentified"),
                                              ("h264", [], "measurement_library_disabled")])
def test_unmeasured_seasons_outside_automatic_intake_name_the_prerequisite(
        connection: DBClient, codec: str, roots: list[str], reason: str,
) -> None:
    _item(connection, "tv/Old show/Season 1/e1.mkv", status="discovered", codec=codec, current_dark=0.5)
    row = season_kind_report(connection, library_types={"tv": "tv"}, folder_approved=lambda _prefix: False,
                             measurement_roots=roots)["seasons"][0]
    assert row["kind"] == "unmeasured"
    assert row["measurement_reasons"] == [reason]


@pytest.mark.parametrize("change", ["measurement", "admission", "genre", "source", "missing", "kept", "library_type", "measurement_root", "approval", "version", "pending_change", "finished_encode", "production_receipt", "deleted_measurement"])
def test_reused_classifications_follow_changed_saved_inputs(connection: DBClient, change: str) -> None:
    from unittest.mock import patch
    from mediaforce.library import content_kinds

    prefix = "tv/Drama/Season 1"
    item_id = _item(connection, f"{prefix}/published.mkv", status="promoted", codec="av1", current_dark=0.0)
    _admit_historical_original(connection, item_id, dark=0.5)
    waiting_id = _item(connection, f"{prefix}/waiting.mkv", status="discovered", codec="h264", current_dark=0.0)
    connection.execute(series_metadata.insert().values(series_prefix="tv/Drama", tmdb_genres_json='["Drama"]', updated_at="now"))
    library_types = {"tv": "tv"}
    measurement_roots = {"tv"}
    approved = False

    def report() -> dict[str, Any]:
        return season_kind_report(connection, library_types=library_types, folder_approved=lambda _: approved,
                                  measurement_roots=measurement_roots)

    with patch.object(content_kinds, "load_source_evidence_bulk", wraps=load_source_evidence_bulk) as load:
        initial = report()
        assert report() == initial
        assert load.call_count == 1
        if change == "measurement":
            connection.execute(source_measurements.update().values(summary_json=json.dumps({"schema_version": 1, "analysis": {}})))
        elif change == "admission":
            connection.execute(source_admissions.update().values(tier="unknown"))
        elif change == "version":
            connection.execute(source_versions.update().where(source_versions.c.role == "input").values(profile_json='{"width": 3840, "height": 2160}'))
        elif change == "pending_change":
            active = connection.execute(select(library_items.c.active_source_version_id).where(library_items.c.id == item_id)).scalar_one()
            connection.execute(source_transitions.insert().values(operation_id="moving", library_item_id=item_id,
                before_version_id=active, after_version_id=active, expected_revision=0, kind="publish", outcome="prepared", evidence_json="{}", recorded_at="now"))
        elif change == "finished_encode":
            connection.execute(staged_artifacts.insert().values(library_item_id=waiting_id, staging_path="output.mkv", updated_at="now",
                encode_completed_at="now", encode_origin="queue", encode_command_json='["-c:v", "libx264"]'))
        elif change == "production_receipt":
            payload = {"encode_command_json": '["-c:v", "libx264"]', "encode_origin": "queue"}
            connection.execute(target_production_outcomes.insert().values(receipt_id="receipt", library_item_id=waiting_id,
                observation_id="observation", manifest_run_id="run", item_index=0, encode_job_id="job", promoted_content_fingerprint="output",
                payload_json=json.dumps(payload), payload_sha256=stable_json_hash(payload), recorded_at="now"))
        elif change == "deleted_measurement":
            connection.execute(source_measurements.delete())
        elif change == "genre":
            connection.execute(series_metadata.update().values(tmdb_genres_json='["Animation"]'))
        elif change == "source":
            connection.execute(library_items.update().where(library_items.c.id == item_id).values(content_version_fingerprint="changed"))
        elif change == "missing":
            connection.execute(library_items.update().where(library_items.c.id == item_id).values(missing_since="now"))
        elif change == "kept":
            connection.execute(motion_keep_decisions.insert().values(library_item_id=waiting_id, source_fingerprint=f"sample-{prefix}/waiting.mkv",
                                                                    prefix=prefix, kept_at="now", reason_code="motion"))
        elif change == "library_type":
            library_types["tv"] = "movie"
        elif change == "measurement_root":
            measurement_roots.clear()
        else:
            approved = True
        changed = report()
        assert load.call_count == 2
    with LibraryCalculations().request(connection):
        assert report() == changed
    # A measuring root or the original's picture size changes what is read, not the kinds it gives.
    if change not in {"measurement_root", "version"}:
        assert changed != initial


def test_lapsed_keep_with_only_a_file_fingerprint_refreshes_measurement_reasons(connection: DBClient) -> None:
    prefix = "tv/Drama/Season 1"
    _item(connection, f"{prefix}/waiting.mkv", status="discovered", codec="h264", current_dark=0.0)
    kept = _item(connection, f"{prefix}/kept.mkv", status="discovered", codec="wmv2", current_dark=0.0)
    connection.execute(library_items.update().where(library_items.c.id == kept).values(content_version_fingerprint=None, fingerprint="before"))
    connection.execute(motion_keep_decisions.insert().values(
        library_item_id=kept, source_fingerprint="before", prefix=prefix, kept_at="now", reason_code="motion",
    ))

    def report() -> dict[str, Any]:
        return season_kind_report(connection, library_types={"tv": "tv"}, folder_approved=lambda _: False)

    assert report()["seasons"][0]["measurement_reasons"] == []
    connection.execute(library_items.update().where(library_items.c.id == kept).values(fingerprint="after"))
    changed = report()
    assert changed["seasons"][0]["measurement_reasons"] == ["original_format_unidentified"]
    with LibraryCalculations().request(connection):
        assert report() == changed


@pytest.mark.parametrize("scoped", [False, True])
def test_admission_membership_handles_more_sources_than_one_sqlite_bind_batch(connection: DBClient, scoped: bool) -> None:
    from mediaforce.library.content_kinds import _admitted_item_ids

    ids = set()
    for index in range(600):
        item_id = _item(connection, f"tv/Drama/Season 1/{index}.mkv", status="discovered", codec="h264", current_dark=0.0)
        _admit_historical_original(connection, item_id, dark=0.5)
        ids.add(item_id)
    driver = cast(sqlite3.Connection, connection.connection.driver_connection)
    previous = driver.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 500)
    try:
        assert _admitted_item_ids(connection, item_ids=ids if scoped else None) == ids
    finally:
        driver.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, previous)
