import json
from pathlib import Path
from typing import Any

from mediaforce.core.config import (
    FOLDER_POLICY_OVERRIDES_KEY,
    ConfigPaths,
    MediaforceConfig,
    upsert_runtime_folder_policy_override,
)
from mediaforce.web.runtime.show_settings_runtime import use_show_settings

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
        tuned: frozenset[str] = frozenset({SEASON}),
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
        season_has_tuning=lambda season_prefix: season_prefix in tuned,
        clear_tuning=clear_tuning,
    )
    return result, cleared


def test_a_season_drops_only_its_own_settings_and_sample(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _save_overrides(config)

    result, cleared = _use(config, f"/{SEASON}/")

    assert result["ok"] and result["changed"]
    assert cleared == [SEASON]
    # The show keeps its settings, and a file's own saved settings inside the season are left alone.
    assert _saved_prefixes(config) == [SHOW, EPISODE]


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
