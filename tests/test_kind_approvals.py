"""One approval covers every season of a kind of content, and a folder's own decision still comes first (#1047)."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db import open_db, reset_engine_cache
from mediaforce.core.db_tables import library_items
from mediaforce.encoding.free_space import ReservePreflight
from mediaforce.web import app as web_app
from mediaforce.web.runtime.decision_evidence import CadenceQueuePartition
from mediaforce.web.runtime.folder_actions import queue_folder_encode_action
from mediaforce.web.runtime.folder_tuning_advice import calibration_policy_hash, review_gate
from mediaforce.web.runtime.kind_approvals import APPROVAL_NOT_CURRENT_CODE, KIND_IS_ONE_TIME_CODE, \
    SIZE_GOAL_NOT_SCALABLE_CODE, KindAwareLoader, SeasonKinds, approve_kind, load_kind_approval, stop_kind_approval
from mediaforce.web.runtime.production_approval import effective_production_approval, production_approval_matches, \
    production_calibration

APPROVED_SEASON = "tv/Drama/Season 1"
SEASON = "tv/Thriller/Season 2"
EPISODE = f"{SEASON}/Thriller.S02E01.mkv"
OTHER_SEASON = "tv/Sitcom/Season 1"
DARKER = "darker_live_action"


def _config(tmp_path: Path) -> MediaforceConfig:
    return MediaforceConfig(
        raw={"video": {}, "audio": {}, "subtitle": {}, "planning": {"series_lifecycle_mode": "auto"},
             "media": {"libraries": [{"key": "tv", "path": str(tmp_path / "tv"), "type": "tv", "availability": "production"}]}},
        paths=ConfigPaths(project_root=tmp_path, config_path=tmp_path / "config.toml", db_path=tmp_path / "library.sqlite3",
                          run_manifest_dir=tmp_path / "runs", web_state_dir=tmp_path / "web", review_dir=tmp_path / "review",
                          runtime_settings_path=tmp_path / "runtime.json", runtime_reservation_dir=tmp_path / "reservations"),
    )


def _approved(config: MediaforceConfig, *, size_mode: str = "normalized", job_id: str = "sample-job") -> dict[str, Any]:
    policy = config.resolve_policy(APPROVED_SEASON)
    policy["video"].update({"compression_intent_schema_version": 1, "compression_intent": "balanced",
                            "compression_intent_source": "test", "compression_intent_confirmed": True})
    calibration: dict[str, Any] = {
        "mode": "sample", "job_id": job_id, "accepted_sample_job_id": "sample-job",
        "accepted_at": "2026-10-25T12:00:00+00:00", "policy": policy,
        "sample_item": {"resolved_policy": policy, "resolved_operator_intent": {"request": {
            "compression_intent": "balanced", "size_goal": {"mode": size_mode, "value_mb": 300.0}}}},
    }
    calibration["accepted_policy_hash"] = calibration_policy_hash(calibration)
    return calibration


def _loader(folders: dict[str, dict[str, Any]], kinds: dict[str, str], asked: list[str] | None = None) -> KindAwareLoader:
    def kind_of(_config: MediaforceConfig, season: str) -> str | None:
        if asked is not None:
            asked.append(season)
        return kinds.get(season)

    return KindAwareLoader(lambda _config, prefix: folders.get(prefix), kind_of)


def test_only_a_current_live_action_approval_per_45_minutes_can_cover_a_kind(tmp_path: Path) -> None:
    config = _config(tmp_path)

    assert approve_kind(config, "cartoon", APPROVED_SEASON, _approved(config), approved_at="now")["code"] == KIND_IS_ONE_TIME_CODE
    assert approve_kind(config, DARKER, APPROVED_SEASON, _approved(config, job_id="resampled"),
                        approved_at="now")["code"] == APPROVAL_NOT_CURRENT_CODE
    assert approve_kind(config, DARKER, APPROVED_SEASON, _approved(config, size_mode="absolute"),
                        approved_at="now")["code"] == SIZE_GOAL_NOT_SCALABLE_CODE
    assert load_kind_approval(config, DARKER) is None

    assert approve_kind(config, DARKER, APPROVED_SEASON, _approved(config), approved_at="now")["ok"]
    saved = load_kind_approval(config, DARKER)
    assert saved is not None and saved["from_prefix"] == APPROVED_SEASON
    assert stop_kind_approval(config, DARKER) and load_kind_approval(config, DARKER) is None


def test_a_never_sampled_season_of_an_approved_kind_is_governed_by_it(tmp_path: Path) -> None:
    config = _config(tmp_path)
    approve_kind(config, DARKER, APPROVED_SEASON, _approved(config), approved_at="now")
    load = _loader({}, {SEASON: DARKER, OTHER_SEASON: "bright_live_action"})

    approval = effective_production_approval(config, EPISODE, load)
    assert approval is not None and approval.source == "kind" and approval.kind == DARKER
    assert approval.prefix == SEASON and approval.current
    assert production_calibration(config, SEASON, load)[0] == SEASON
    assert effective_production_approval(config, f"{OTHER_SEASON}/Sitcom.S01E01.mkv", load) is None
    # A show folder is no season, so a kind never covers it as a whole.
    assert effective_production_approval(config, "tv/Thriller", load) is None


def test_a_folder_approval_or_a_seasons_own_sample_comes_before_its_kind(tmp_path: Path) -> None:
    config = _config(tmp_path)
    approve_kind(config, DARKER, APPROVED_SEASON, _approved(config), approved_at="now")
    show_approval = _approved(config)
    show_approval["accepted_sample_job_id"] = show_approval["job_id"] = "show-sample"

    by_show = effective_production_approval(config, EPISODE, _loader({"tv/Thriller": show_approval}, {SEASON: DARKER}))
    unapproved_own = {key: value for key, value in _approved(config, job_id="awaiting-review").items()
                      if not key.startswith("accepted_")}
    by_own_sample = _loader({SEASON: unapproved_own}, {SEASON: DARKER})

    assert by_show is not None and by_show.source == "folder" and by_show.prefix == "tv/Thriller"
    assert effective_production_approval(config, EPISODE, by_own_sample) is None
    assert production_calibration(config, SEASON, by_own_sample) == (SEASON, unapproved_own)


def test_no_seasons_kind_is_read_until_a_kind_is_approved(tmp_path: Path) -> None:
    config = _config(tmp_path)
    asked: list[str] = []

    assert effective_production_approval(config, EPISODE, _loader({}, {SEASON: DARKER}, asked)) is None
    assert asked == []


def test_a_seasons_kind_is_reused_until_it_is_stale(tmp_path: Path) -> None:
    config = _config(tmp_path)
    now = [0.0]
    kinds = SeasonKinds(seconds=60, clock=lambda: now[0])
    reads: list[str] = []

    def grouping(_connection: object, season: str, **_kwargs: object) -> Any:
        reads.append(season)
        return type("Grouping", (), {"kind": DARKER})()

    config.paths.db_path.touch()
    with patch("mediaforce.web.runtime.kind_approvals.open_readonly_db"), \
            patch("mediaforce.web.runtime.kind_approvals.season_grouping", side_effect=grouping):
        assert kinds(config, SEASON) == DARKER
        now[0] = 59
        kinds(config, SEASON)
        now[0] = 61
        kinds(config, SEASON)
    assert reads == [SEASON, SEASON]


def _insert_episode(config: MediaforceConfig) -> int:
    timestamp = datetime.now(tz=UTC).isoformat(timespec="seconds")
    with open_db(config.paths.db_path) as connection:
        result = connection.execute(library_items.insert().values(
            source_path=f"/media/{EPISODE}", rel_path=EPISODE, media_root="tv", parent_dir=SEASON,
            file_name=Path(EPISODE).name, container="mkv", size_bytes=1_000_000_000, mtime_ns=1,
            fingerprint=EPISODE, duration_seconds=2_700.0, video_codec="h264", width=1920, height=1080,
            audio_summary_json="[]", subtitle_summary_json="[]", status="discovered", priority_score=50,
            recommendation="priority_encode", recommendation_reason="fixture", last_scan_id="scan",
            discovered_at=timestamp, last_seen_at=timestamp, updated_at=timestamp,
        ))
    return int(result.inserted_primary_key[0])


def _queue(config: MediaforceConfig, prefix: str, load: KindAwareLoader) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    item_id = _insert_episode(config)
    manifests: list[dict[str, Any]] = []

    def create_manifest(_connection: object, manifest_config: MediaforceConfig, **_kwargs: object) -> tuple[dict[str, Any], Path]:
        manifest = {"items": [{"library_item_id": item_id, "rel_path": EPISODE,
                               "resolved_policy": manifest_config.resolve_policy(EPISODE)}]}
        manifests.append(manifest)
        return manifest, config.paths.run_manifest_dir / "run.json"

    def cadence_clears(*_args: object, library_item_ids: list[int], **_kwargs: object) -> CadenceQueuePartition:
        return CadenceQueuePartition(frozenset(library_item_ids), frozenset(), frozenset(), frozenset(), frozenset(), None, {})

    with patch("mediaforce.web.runtime.folder_actions.create_folder_manifest", side_effect=create_manifest), \
            patch("mediaforce.web.runtime.folder_actions.cadence_queue_partition", side_effect=cadence_clears), \
            patch("mediaforce.web.runtime.folder_actions.load_config", side_effect=lambda _path: _config(config.paths.project_root)):
        try:
            result = queue_folder_encode_action(
                config, prefix, "", False, now_iso=lambda: "2026-10-26T00:00:00+00:00",
                load_job_state=lambda *_args: None, load_calibration_state=load, review_gate=review_gate,
                upsert_override=web_app._upsert_override, load_active_encode_job_for_prefix_fn=lambda *_args: None,
                clear_terminal_encode_jobs_for_prefix_fn=lambda *_args: None,
                prepare_terminal_encode_job_for_requeue_fn=lambda *_args: None, save_encode_job=lambda *_args: None,
                load_advice_state=lambda *_args: {}, reserve_preflight=lambda *_args: ReservePreflight(True, None, {}),
            )
        finally:
            reset_engine_cache()
    return result, manifests


def test_a_never_sampled_season_of_an_approved_kind_is_queued_under_it(tmp_path: Path) -> None:
    config = _config(tmp_path)
    approve_kind(config, DARKER, APPROVED_SEASON, _approved(config), approved_at="now")
    load = _loader({}, {SEASON: DARKER})

    result, manifests = _queue(config, SEASON, load)

    assert result["ok"], result
    manifest = manifests[0]
    item = manifest["items"][0]
    assert manifest["selection"]["production_approval_kind"] == DARKER
    assert item["production_approval"] == {"source": "kind", "prefix": SEASON, "kind": DARKER}
    assert production_approval_matches(effective_production_approval(config, EPISODE, load), manifest, item)


def test_a_season_outside_every_approved_kind_is_not_queued(tmp_path: Path) -> None:
    config = _config(tmp_path)
    approve_kind(config, DARKER, APPROVED_SEASON, _approved(config), approved_at="now")

    with pytest.raises(HTTPException):
        _queue(config, SEASON, _loader({}, {SEASON: "bright_live_action"}))
