import copy
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
import tomllib
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import select

from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db import DBClient, open_db, reset_engine_cache
from mediaforce.core.db_tables import library_items, staged_artifacts
from mediaforce.library import candidate_selection, workflow_state
from mediaforce.library.candidate_selection import CandidateDecision, _load_rows, project_candidates
from mediaforce.library.media_scopes import MediaScope, media_scope_from_prefix, scopes_overlap
from mediaforce.web import app as web_app

NOW = "2026-10-25T00:00:00+00:00"
# Neighbours that share a string prefix catch a match that forgets the path separator.
PREFIXES = [
    "tv",
    "tv/Show 1",
    "tv/Show 1/Season 1",
    "tv/Show 1/Season 1/e1.mkv",
    "tv/Show 1/Season 10",
    "tv/Show 1/Season 10/e1.mkv",
    "tv/Show 1/Season 1 Extras/e1.mkv",
    "tv/Show 1/loose.mkv",
    "tv/Show 10/Season 1/e1.mkv",
    "movies/Film/Film.mkv",
]


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[DBClient]:
    with open_db(tmp_path / "library.sqlite3") as db:
        yield db
    reset_engine_cache()


@pytest.fixture
def config(tmp_path: Path) -> MediaforceConfig:
    with (Path(__file__).parent / "fixtures/defaults.toml").open("rb") as handle:
        raw = copy.deepcopy(tomllib.load(handle))
    raw["media"]["libraries"] = [
        {"key": "shows", "path": str(tmp_path / "shows"), "type": "tv", "availability": "production"},
        {"key": "anime", "path": str(tmp_path / "anime"), "type": "tv", "availability": "production"},
        {"key": "films", "path": str(tmp_path / "films"), "type": "movie", "availability": "production"},
    ]
    return MediaforceConfig(
        raw=raw,
        paths=ConfigPaths(
            project_root=tmp_path,
            config_path=tmp_path / "config.toml",
            db_path=tmp_path / "library.sqlite3",
            run_manifest_dir=tmp_path / "runs",
            web_state_dir=tmp_path / "web",
            review_dir=tmp_path / "review",
            runtime_settings_path=tmp_path / "runtime.json",
        ),
    )


def _scopes(prefixes: list[str]) -> list[MediaScope]:
    return [media_scope_from_prefix(prefix, match=match) for prefix in prefixes
            for match in ("descendants", "exact_item")]


def test_jobs_found_by_prefix_are_the_jobs_each_scope_overlaps() -> None:
    job_prefixes = [*PREFIXES, "tv/Show 1/Season 1", "", None, "/tv/Show 1/"]
    rows = [{"job_id": f"job-{index}", "prefix": prefix} for index, prefix in enumerate(job_prefixes)]
    overlapping = workflow_state._overlapping_job_rows(rows)

    for scope in [media_scope_from_prefix("", match="descendants"), *_scopes(PREFIXES)]:
        expected = [
            row for row in rows
            if scopes_overlap(scope, media_scope_from_prefix(str(row["prefix"] or ""), match="descendants"))
        ]
        assert overlapping(scope) == expected, (scope.prefix, scope.match)


def test_reading_many_scopes_keeps_the_rows_each_scope_reads_alone(connection: DBClient) -> None:
    for rel_path in PREFIXES[3:]:
        _item(connection, rel_path)
    scopes = [
        media_scope_from_prefix("tv/Show 1/Season 1", match="descendants"),
        media_scope_from_prefix("tv/Show 10/Season 1/e1.mkv", match="exact_item"),
        media_scope_from_prefix("tv/Show 1/loose.mkv", match="exact_item"),
        *(media_scope_from_prefix(f"tv/Empty/Season {number}", match="descendants")
          for number in range(workflow_state.PREFIX_QUERY_BATCH_SIZE)),
    ]
    alone = {
        (row["item_id"], row["rel_path"])
        for scope in scopes
        for row in workflow_state._load_item_rows(connection, scope)
    }

    together = [(row["item_id"], row["rel_path"]) for row in workflow_state._load_item_rows_for_scopes(connection, scopes)]

    assert together == sorted(alone, key=lambda row: row[1])
    assert [rel_path for _item_id, rel_path in together] == [
        "tv/Show 1/Season 1/e1.mkv", "tv/Show 1/loose.mkv", "tv/Show 10/Season 1/e1.mkv",
    ]


def test_season_group_by_folder_matches_grouping_each_file() -> None:
    library_types = {"tv": "tv", "movies": "movie"}
    group = web_app._season_group_by_folder(library_types)
    paths = [
        *PREFIXES,
        "tv/Show 1/Season 1/e2.mkv",
        "tv/Show 1/Season 1/",
        "tv/Show 1/Season 1/.",
        "tv/Show 1/Season 1/ ",
        "tv/Show 1/Season 1/Extras/e1.mkv",
        "tv/Show 1",
        "tv/Show 1/Season 1",
    ]

    assert [group(path) for path in paths] == [
        web_app._tv_season_folder_group(path, library_types=library_types) for path in paths
    ]


def patch_attr(target: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    return patch.object(target, name, *args, **kwargs)


def test_tv_root_projection_matches_full_projection_and_reads_mutations(
        connection: DBClient, config: MediaforceConfig,
) -> None:
    _item(connection, "shows/Show 1/Season 1/e1.mkv")
    _item(connection, "anime/Show 2/Season 1/e1.mkv")
    _item(connection, "films/Film/Film.mkv")
    original_project_candidates = web_app.project_candidates

    def project_all_rows(
            db: DBClient,
            candidate_config: MediaforceConfig,
            **_scope_options: Any,
    ) -> list[CandidateDecision]:
        return original_project_candidates(db, candidate_config)

    projected = web_app._list_library_detail_cards(config, connection)
    with patch_attr(web_app, "project_candidates", side_effect=project_all_rows):
        full = web_app._list_library_detail_cards(config, connection)

    assert projected == full
    assert {card.prefix for card in projected} == {
        "anime/Show 2/Season 1", "shows/Show 1/Season 1",
    }

    _item(connection, "shows/Show 1/Season 1/e2.mkv")
    refreshed = web_app._list_library_detail_cards(config, connection)
    with patch_attr(web_app, "project_candidates", side_effect=project_all_rows):
        refreshed_full = web_app._list_library_detail_cards(config, connection)

    assert refreshed == refreshed_full
    assert {card.prefix: card.item_count for card in refreshed} == {
        "anime/Show 2/Season 1": 1, "shows/Show 1/Season 1": 2,
    }


def test_tv_detail_cards_are_empty_without_tv_roots(
        connection: DBClient, config: MediaforceConfig,
) -> None:
    config.raw["media"]["libraries"] = [config.raw["media"]["libraries"][2]]
    _item(connection, "films/Film/Film.mkv")

    with patch_attr(web_app, "project_candidates") as project:
        assert web_app._list_library_detail_cards(config, connection) == []

    project.assert_not_called()


def test_tv_root_candidate_projection_matches_full_library_without_loading_other_roots(
        connection: DBClient,
        config: MediaforceConfig,
) -> None:
    paths = [
        "shows/Alpha/Season 1/e1.mkv",
        "shows/Alpha/Season 2/e1.mkv",
        "shows/Beta/Season 1/e1.mkv",
        "anime/Gamma/Season 1/e1.mkv",
        "anime/Gamma/Season 2/e1.mkv",
        "films/Movie/Movie.mkv",
    ]
    for rel_path in paths:
        _item(connection, rel_path)
    promoted_id = connection.execute(
        select(library_items.c.id).where(library_items.c.rel_path == paths[0])
    ).scalar_one()
    connection.execute(library_items.update().where(library_items.c.id == promoted_id).values(status="promoted"))
    connection.execute(staged_artifacts.insert().values(
        library_item_id=promoted_id,
        staging_path="/staging/shows/Alpha/Season 1/e1.mkv",
        promoted_at=NOW,
        updated_at=NOW,
    ))
    connection.commit()
    now = datetime.fromisoformat(NOW).astimezone(UTC)
    canonical = project_candidates(connection, config, now=now)
    load_rows = _load_rows

    for root in ("shows", "anime"):
        loaded: list[str] = []

        def record_rows(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            rows = load_rows(*args, **kwargs)
            loaded.extend(str(row["rel_path"]) for row in rows)
            return rows

        with patch_attr(candidate_selection, "_load_rows", side_effect=record_rows):
            scoped = project_candidates(connection, config, prefixes=[root], now=now)

        expected: dict[str, CandidateDecision] = {
            str(decision.row["rel_path"]): decision for decision in canonical
            if str(decision.row["rel_path"]).startswith(f"{root}/")
        }
        assert {str(decision.row["rel_path"]): decision for decision in scoped} == expected
        assert set(loaded) == expected.keys()


def _item(connection: DBClient, rel_path: str) -> None:
    connection.execute(library_items.insert().values(
        source_path=f"/media/{rel_path}", rel_path=rel_path, media_root=rel_path.split("/")[0],
        parent_dir=str(Path(rel_path).parent), file_name=Path(rel_path).name, container="mkv", size_bytes=1,
        mtime_ns=1, fingerprint=rel_path, audio_summary_json="[]", subtitle_summary_json="[]",
        status="discovered", last_scan_id="scan", discovered_at=NOW, last_seen_at=NOW, updated_at=NOW,
    ))
    connection.commit()
