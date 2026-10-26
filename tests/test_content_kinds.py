import json
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine

from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import library_items, metadata, series_metadata, source_admissions, source_generations
from mediaforce.core.evidence import stable_json_hash
from mediaforce.core.utils import timestamp
from mediaforce.library.content_kinds import EpisodeTraits, season_kind, season_kind_report
from mediaforce.library.source_evidence import HISTORICAL_OUTPUT_PROFILES, append_measurement, append_version, json_text


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
