"""Reading every season at once decides what reading each season alone decides (#1030)."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from mediaforce.core.db import DBClient, open_db, reset_engine_cache
from mediaforce.core.db_tables import library_items
from mediaforce.library import workflow_state
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


def _item(connection: DBClient, rel_path: str) -> None:
    connection.execute(library_items.insert().values(
        source_path=f"/media/{rel_path}", rel_path=rel_path, media_root=rel_path.split("/")[0],
        parent_dir=str(Path(rel_path).parent), file_name=Path(rel_path).name, container="mkv", size_bytes=1,
        mtime_ns=1, fingerprint=rel_path, audio_summary_json="[]", subtitle_summary_json="[]",
        status="discovered", last_scan_id="scan", discovered_at=NOW, last_seen_at=NOW, updated_at=NOW,
    ))
    connection.commit()
