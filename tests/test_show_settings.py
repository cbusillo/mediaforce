import json
from pathlib import Path
from typing import Any

from mediaforce.core.config import (
    FOLDER_POLICY_OVERRIDES_KEY,
    ConfigPaths,
    MediaforceConfig,
    upsert_runtime_folder_policy_override,
)
from mediaforce.core.db import open_db, reset_engine_cache
from mediaforce.core.db_tables import library_items
from mediaforce.web.runtime.show_settings_runtime import prefixes_with_samples, use_show_settings

SHOW = "tv/Big Brother (US)"
SEASON = f"{SHOW}/Season 19"
EPISODE = f"{SEASON}/Big.Brother.S19E01.mkv"


def _config(tmp_path: Path) -> MediaforceConfig:
    return MediaforceConfig(
        raw={
            "video": {},
            "audio": {},
            "subtitle": {},
            "planning": {},
            "media": {"libraries": [{"key": "tv", "path": str(tmp_path / "tv"), "type": "tv"}]},
            "overrides": [],
        },
        paths=ConfigPaths(
            project_root=tmp_path,
            config_path=tmp_path / "config.toml",
            db_path=tmp_path / "library.sqlite3",
            run_manifest_dir=tmp_path / "runs",
            web_state_dir=tmp_path / "web",
            review_dir=tmp_path / "review",
            runtime_settings_path=tmp_path / "runtime.json",
            runtime_reservation_dir=tmp_path / "reservations",
        ),
    )


def _saved_prefixes(config: MediaforceConfig) -> list[str]:
    overrides = json.loads(config.paths.runtime_settings_path.read_text())[FOLDER_POLICY_OVERRIDES_KEY]
    return [str(override["path_prefix"]) for override in overrides]


def _save_overrides(config: MediaforceConfig) -> None:
    for prefix, size_mb in ((SHOW, 220.0), (SEASON, 300.0), (EPISODE, 260.0)):
        upsert_runtime_folder_policy_override(
            config.paths.runtime_settings_path, prefix, {"video": {"target_size_mb": size_mb}},
        )


def _use(
        config: MediaforceConfig,
        prefix: str,
        *,
        show_approved: bool = True,
        tuned: frozenset[str] = frozenset({SEASON, EPISODE}),
        clear_result: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    cleared: list[str] = []

    def clear_tuning(season_prefix: str) -> dict[str, Any]:
        cleared.append(season_prefix)
        return clear_result or {"ok": True}

    result = use_show_settings(
        config,
        prefix,
        show_has_approval=lambda show_prefix: show_approved and show_prefix == SHOW,
        tuned_prefixes=lambda _season_prefix: tuned,
        clear_tuning=clear_tuning,
    )
    return result, cleared


def test_a_season_drops_its_own_and_its_episodes_settings_and_samples(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _save_overrides(config)

    result, cleared = _use(config, f"/{SEASON}/")

    assert result["ok"] and result["changed"]
    # An episode's own sample would still govern it, so it is cleared too, before the season's.
    assert cleared == [EPISODE, SEASON]
    assert _saved_prefixes(config) == [SHOW]


def test_an_episode_sample_alone_still_counts_as_a_change(tmp_path: Path) -> None:
    config = _config(tmp_path)
    upsert_runtime_folder_policy_override(config.paths.runtime_settings_path, SHOW, {"video": {}})

    result, cleared = _use(config, SEASON, tuned=frozenset({EPISODE}))

    assert result["ok"] and result["changed"]
    assert cleared == [EPISODE]


def test_samples_are_found_for_the_season_and_the_files_inside_it(tmp_path: Path) -> None:
    config = _config(tmp_path)
    other_season_episode = f"{SHOW}/Season 2/Big.Brother.S02E01.mkv"
    with open_db(config.paths.db_path) as connection:
        for rel_path in (EPISODE, f"{SEASON}/Big.Brother.S19E02.mkv", other_season_episode):
            connection.execute(library_items.insert().values(
                source_path=f"/media/{rel_path}", rel_path=rel_path, media_root="tv",
                parent_dir=str(Path(rel_path).parent), file_name=Path(rel_path).name, container="mkv",
                size_bytes=1, mtime_ns=1, fingerprint=rel_path, audio_summary_json="[]",
                subtitle_summary_json="[]", status="discovered", priority_score=0, recommendation="skip",
                recommendation_reason="fixture", last_scan_id="scan",
                discovered_at="2026-10-15T00:00:00+00:00", last_seen_at="2026-10-15T00:00:00+00:00",
                updated_at="2026-10-15T00:00:00+00:00",
            ))
        found = prefixes_with_samples(
            connection, SEASON, has_sample=lambda prefix: prefix in {SEASON, EPISODE, SHOW, other_season_episode},
        )
    reset_engine_cache()

    assert found == [SEASON, EPISODE]


def test_a_season_already_on_the_show_settings_changes_nothing(tmp_path: Path) -> None:
    config = _config(tmp_path)
    upsert_runtime_folder_policy_override(config.paths.runtime_settings_path, SHOW, {"video": {}})

    result, cleared = _use(config, SEASON, tuned=frozenset())

    assert result["ok"] and not result["changed"]
    assert cleared == []
    assert _saved_prefixes(config) == [SHOW]


def test_refusals_keep_the_season_settings(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _save_overrides(config)

    refusals = [
        _use(config, SEASON, show_approved=False),
        _use(config, SHOW),
        _use(config, EPISODE),
        _use(config, SEASON, clear_result={"ok": False, "message": "A calibration job is still active."}),
    ]

    assert [result["ok"] for result, _cleared in refusals] == [False, False, False, False]
    assert refusals[3][0]["message"] == "A calibration job is still active."
    assert _saved_prefixes(config) == [SHOW, SEASON, EPISODE]
