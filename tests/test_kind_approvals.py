"""One approval covers every season of a kind of content, and a folder's own decision still comes first (#1047)."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI, HTTPException
from test_web_route_security import _json_request, _route_endpoint
from sqlalchemy import create_engine
from test_content_kinds import _admit_historical_original, _item as _content_item

from mediaforce.core.config import ConfigPaths, MediaforceConfig, upsert_runtime_folder_policy_override
from mediaforce.core.db import open_db, reset_engine_cache
from mediaforce.core.db_tables import encode_jobs, library_items, metadata, series_metadata, source_admissions, source_generations, \
    source_transitions, staged_artifacts
from mediaforce.encoding.encode_queue import DEFAULT_QUEUE_NAME, save_queue_state
from mediaforce.encoding.free_space import ReservePreflight
from mediaforce.web.runtime.decision_evidence import CadenceQueuePartition
from mediaforce.library.content_kinds import season_evidence, season_grouping
from mediaforce.library.source_evidence import append_version
from mediaforce.web.runtime.folder_actions import queue_folder_encode_action
from mediaforce.web.runtime.folder_state import save_calibration_state
from mediaforce.web.routes.app_window import register_app_window_routes
from mediaforce.web.runtime.folder_tuning_advice import calibration_policy_hash, review_gate
from mediaforce.web.runtime.kind_approvals import APPROVAL_NOT_CURRENT_CODE, KIND_IS_ONE_TIME_CODE, \
    KIND_QUEUE_WAITS_CODE, KIND_RETRY_SECONDS, SIZE_GOAL_NOT_SCALABLE_CODE, KindAwareLoader, KindSeason, SeasonKinds, \
    NO_FOLDER_APPROVAL_CODE, SOURCE_KIND_DIFFERS_CODE, approve_kind, approve_kind_action, background_queue_blocker, kinds_payload, load_kind_approval, \
    next_kind_season, record_kind_attempt, stop_kind_approval
from mediaforce.web.runtime.page_state import PageStateFacts, decide_page_state
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


def test_a_seasons_kind_is_read_again_only_when_its_evidence_changes(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _insert_episode(config)
    with open_db(config.paths.db_path) as connection:
        connection.execute(series_metadata.insert().values(series_prefix="tv/Thriller", tmdb_genres_json='["Drama"]',
                                                           updated_at="2026-10-26T00:00:00+00:00"))
    kinds = SeasonKinds()
    try:
        with patch("mediaforce.web.runtime.kind_approvals.season_grouping", wraps=season_grouping) as grouping, \
                patch("mediaforce.web.runtime.kind_approvals.season_evidence", wraps=season_evidence) as evidence:
            kinds(config, SEASON)
            kinds(config, SEASON)
            kinds(config, OTHER_SEASON)
            assert (grouping.call_count, evidence.call_count) == (2, 1)
            with open_db(config.paths.db_path) as connection:
                connection.execute(series_metadata.update().values(tmdb_genres_json='["Animation"]'))
            kinds(config, SEASON)
            assert (grouping.call_count, evidence.call_count) == (3, 1)
            with open_db(config.paths.db_path) as connection:
                connection.execute(source_generations.insert().values(generation_id="new", created_at="now"))
                version = append_version(connection, generation_id="new", role="input", identity={}, profile={}, recorded_at="now")
                connection.execute(source_admissions.insert().values(
                    admission_id="new", generation_id="new", version_id=version, tier="probable", basis="readable-source",
                    checks_json="{}", recorded_at="now"))
            kinds(config, SEASON)
            assert (grouping.call_count, evidence.call_count) == (4, 2)
            # A publish settled in place, as a rollback does, adds no row but still changes what a season reads.
            with open_db(config.paths.db_path) as connection:
                connection.execute(source_transitions.insert().values(
                    operation_id="publish", library_item_id=None, before_version_id=version, after_version_id=version,
                    expected_revision=0, kind="publish", outcome="prepared", evidence_json="{}", recorded_at="now"))
            kinds(config, SEASON)
            with open_db(config.paths.db_path) as connection:
                connection.execute(source_transitions.update().values(outcome="rolled_back"))
            kinds(config, SEASON)
            assert (grouping.call_count, evidence.call_count) == (6, 4)
    finally:
        reset_engine_cache()


def test_a_kinds_approval_is_never_saved_as_a_folders_own(tmp_path: Path) -> None:
    config = _config(tmp_path)
    approve_kind(config, DARKER, APPROVED_SEASON, _approved(config), approved_at="now")
    kind_copy = load_kind_approval(config, DARKER)
    assert kind_copy is not None

    with pytest.raises(ValueError):
        save_calibration_state(tmp_path / "season.json", kind_copy, calibration_draft_hash=lambda _payload: "hash")
    assert not (tmp_path / "season.json").exists()


def _insert_episode(config: MediaforceConfig) -> int:
    return _waiting_episode(config, EPISODE, 1_000_000_000)


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
                upsert_override=upsert_runtime_folder_policy_override, load_active_encode_job_for_prefix_fn=lambda *_args: None,
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


def _waiting_episode(config: MediaforceConfig, rel_path: str, size_bytes: int, *, status: str = "discovered") -> int:
    timestamp = datetime.now(tz=UTC).isoformat(timespec="seconds")
    with open_db(config.paths.db_path) as connection:
        result = connection.execute(library_items.insert().values(
            source_path=f"/media/{rel_path}", rel_path=rel_path, media_root="tv", parent_dir=rel_path.rsplit("/", 1)[0],
            file_name=Path(rel_path).name, container="mkv", size_bytes=size_bytes, mtime_ns=1, fingerprint=rel_path,
            duration_seconds=2_700.0, video_codec="h264", width=1920, height=1080, audio_summary_json="[]",
            subtitle_summary_json="[]", status=status, priority_score=50, recommendation="priority_encode",
            recommendation_reason="fixture", last_scan_id="scan", discovered_at=timestamp, last_seen_at=timestamp,
            updated_at=timestamp,
        ))
    return int(result.inserted_primary_key[0])


def _encode_job(config: MediaforceConfig, prefix: str, status: str) -> None:
    with open_db(config.paths.db_path) as connection:
        connection.execute(encode_jobs.insert().values(
            job_id=f"{prefix}-{status}", prefix=prefix, status=status, manifest_path="run.json", host_json="{}",
            created_at="2026-10-26T00:00:00+00:00", updated_at="2026-10-26T00:00:00+00:00",
        ))


def _next(config: MediaforceConfig, load: KindAwareLoader, *, now: float = 0.0) -> KindSeason | None:
    try:
        with open_db(config.paths.db_path) as connection:
            return next_kind_season(connection, config, load_calibration_state=load, now=now)
    finally:
        reset_engine_cache()


def test_seasons_a_kind_covers_are_offered_one_at_a_time_largest_first(tmp_path: Path) -> None:
    config = _config(tmp_path)
    small, large, own = "tv/Small/Season 1", "tv/Large/Season 1", "tv/Own/Season 1"
    for season, size in ((small, 1_000), (large, 9_000), (own, 50_000)):
        _waiting_episode(config, f"{season}/e01.mkv", size)
    _waiting_episode(config, f"{small}/e02.mkv", 20_000, status="promoted")
    own_sample = {key: value for key, value in _approved(config).items() if not key.startswith("accepted_")}
    load = _loader({own: own_sample}, {small: DARKER, large: DARKER, own: DARKER})

    assert _next(config, load) is None
    approve_kind(config, DARKER, APPROVED_SEASON, _approved(config), approved_at="now")
    first = _next(config, load)
    assert first is not None and (first.season, first.kind, first.waiting_bytes) == (large, DARKER, 9_000)
    record_kind_attempt(config, first, {"ok": True}, at=0.0)
    second = _next(config, load)
    assert second is not None and second.season == small
    # A wait for the queue to settle is no attempt, so the season is offered again.
    record_kind_attempt(config, second, {"ok": False, "code": KIND_QUEUE_WAITS_CODE}, at=0.0)
    assert _next(config, load) == second
    record_kind_attempt(config, second, {"ok": True}, at=0.0)
    # The season with its own sample is never offered, and attempts outlive this process.
    assert _next(config, load) is None


def test_a_refused_season_is_tried_again_later_or_when_its_files_change(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _waiting_episode(config, EPISODE, 1_000)
    approve_kind(config, DARKER, APPROVED_SEASON, _approved(config), approved_at="now")
    load = _loader({}, {SEASON: DARKER})
    chosen = _next(config, load)
    assert chosen is not None

    record_kind_attempt(config, chosen, {"ok": False, "message": "Not enough free space."}, at=0.0)
    assert _next(config, load, now=3600.0) is None
    assert _next(config, load, now=KIND_RETRY_SECONDS + 1) == chosen
    record_kind_attempt(config, chosen, {"ok": True}, at=0.0)
    _waiting_episode(config, f"{SEASON}/Thriller.S02E02.mkv", 2_000)
    again = _next(config, load)
    assert again is not None and again.season == SEASON and again.files != chosen.files


def test_no_season_is_offered_while_work_is_paused_running_or_left_with_the_director(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _waiting_episode(config, EPISODE, 1_000)
    approve_kind(config, DARKER, APPROVED_SEASON, _approved(config), approved_at="now")
    load = _loader({}, {SEASON: DARKER})

    with open_db(config.paths.db_path) as connection:
        save_queue_state(connection, {"queue_name": DEFAULT_QUEUE_NAME, "is_paused": True, "updated_at": "now"})
        assert background_queue_blocker(connection) == {"ok": False, "code": KIND_QUEUE_WAITS_CODE}
    assert _next(config, load) is None
    with open_db(config.paths.db_path) as connection:
        save_queue_state(connection, {"queue_name": DEFAULT_QUEUE_NAME, "is_paused": False, "updated_at": "now"})
    _encode_job(config, OTHER_SEASON, "running")
    assert _next(config, load) is None
    with open_db(config.paths.db_path) as connection:
        connection.execute(encode_jobs.update().values(status="completed"))
        assert background_queue_blocker(connection) is None
    _encode_job(config, SEASON, "needs_attention")
    assert _next(config, load) is None
    with open_db(config.paths.db_path) as connection:
        connection.execute(encode_jobs.delete().where(encode_jobs.c.prefix == SEASON))
    offered = _next(config, load)
    assert offered is not None and offered.season == SEASON
    reset_engine_cache()


def test_the_kinds_facts_say_what_each_kind_covers_and_how_it_is_decided(tmp_path: Path) -> None:
    config = _config(tmp_path)
    engine = create_engine("sqlite://")
    with engine.connect() as connection:
        metadata.create_all(connection)
        published = _content_item(connection, "tv/Drama/Season 1/e01.mkv", status="promoted", codec="av1", current_dark=0.0)
        _admit_historical_original(connection, published, dark=0.5)
        _content_item(connection, "tv/Drama/Season 1/e02.mkv", status="discovered", codec="h264", current_dark=0.0)
        _content_item(connection, "tv/Fresh/Season 1/e01.mkv", status="discovered", codec="h264", current_dark=0.5)
        connection.execute(series_metadata.insert().values(series_prefix="tv/Drama", tmdb_genres_json='["Drama"]',
                                                           updated_at="now"))
        approve_kind(config, DARKER, APPROVED_SEASON, _approved(config), approved_at="2026-10-26T01:00:00+00:00")

        asked: list[str] = []
        facts = kinds_payload(connection, config, load_calibration_state=_loader({}, {"tv/Fresh/Season 1": DARKER}, asked),
                              kind_of=lambda _config, season: asked.append(season))
    engine.dispose()

    # Only folder approvals are counted, so the page never works out a season's kind just to skip it.
    assert asked == []
    kinds = {kind["kind"]: kind for kind in facts["kinds"]}
    assert [kind["kind"] for kind in facts["kinds"]] == ["darker_live_action", "bright_live_action", "cartoon", "4k"]
    assert kinds[DARKER]["shows"] == [{"prefix": "tv/Drama", "seasons": 1, "waiting_bytes": 1000}]
    assert kinds[DARKER]["settings"] == []
    assert kinds[DARKER]["approval"] == {"from_prefix": APPROVED_SEASON, "approved_at": "2026-10-26T01:00:00+00:00",
                                         "size_goal_mode": "normalized", "size_goal_mb": 300.0, "library_item_id": None}
    assert kinds["bright_live_action"]["approval"] is None and kinds["cartoon"]["one_time"]
    assert facts["unmeasured_seasons"] == 1
    assert facts["left_out"] == [{"season": "tv/Fresh/Season 1", "kind": "unmeasured", "waiting_bytes": 1000}]


def _keep_original_backup(connection: Any, item_id: int) -> None:
    connection.execute(staged_artifacts.insert().values(
        library_item_id=item_id, staging_path=f"/staging/{item_id}.mkv", promoted_at="2026-10-25T00:00:00+00:00",
        archived_source_path=f"/archive/{item_id}.mkv", updated_at="2026-10-25T00:00:00+00:00"))


def test_an_approved_show_of_the_kind_lends_its_setting_with_an_episode_to_watch_it_on(tmp_path: Path) -> None:
    config = _config(tmp_path)
    engine = create_engine("sqlite://")
    with engine.connect() as connection:
        metadata.create_all(connection)
        # Drama's Season 1 was sampled, approved and published; Thriller has its own approval but no backup kept.
        published = _content_item(connection, f"{APPROVED_SEASON}/e01.mkv", status="promoted", codec="av1", current_dark=0.5)
        _keep_original_backup(connection, published)
        _content_item(connection, EPISODE, status="promoted", codec="av1", current_dark=0.5)
        # Thriller's show approval was sampled on a darker season; its Season 3 has its own bright approval and the
        # only kept episode, which never stands for the show's setting.
        own_season = "tv/Thriller/Season 3"
        kept = _content_item(connection, f"{own_season}/e01.mkv", status="promoted", codec="av1", current_dark=0.5)
        _keep_original_backup(connection, kept)
        approved = {APPROVED_SEASON: _approved_on(config, f"{APPROVED_SEASON}/e01.mkv"),
                    "tv/Thriller": _approved_on(config, EPISODE), own_season: _approved_on(config, f"{own_season}/e01.mkv")}
        kinds = {APPROVED_SEASON: DARKER, SEASON: DARKER, own_season: "bright_live_action"}

        def facts_with(folders: dict[str, dict[str, Any]]) -> dict[str, Any]:
            facts = kinds_payload(connection, config, load_calibration_state=_loader(folders, kinds),
                                  kind_of=lambda _config, season: kinds.get(season))
            return {kind["kind"]: kind for kind in facts["kinds"]}

        lent = facts_with(approved)
        per_episode = {prefix: _approved_on(config, f"{prefix}/e01.mkv") for prefix in approved}
        for calibration in per_episode.values():
            calibration["sample_item"]["resolved_operator_intent"]["request"]["size_goal"]["mode"] = "absolute"
            calibration["accepted_policy_hash"] = calibration_policy_hash(calibration)
        not_lent = facts_with(per_episode)
    engine.dispose()

    # The show with an episode to watch comes first.
    assert lent[DARKER]["settings"] == [
        {"show": "tv/Drama", "from_prefix": APPROVED_SEASON, "size_goal_mode": "normalized", "size_goal_mb": 300.0,
         "library_item_id": published},
        {"show": "tv/Thriller", "from_prefix": "tv/Thriller", "size_goal_mode": "normalized", "size_goal_mb": 300.0,
         "library_item_id": None},
    ]
    assert lent["bright_live_action"]["settings"] == [
        {"show": "tv/Thriller", "from_prefix": own_season, "size_goal_mode": "normalized", "size_goal_mb": 300.0,
         "library_item_id": kept},
    ]
    # A size per episode cannot cover a kind, so the page never offers it.
    assert not_lent[DARKER]["settings"] == []


def _approved_on(config: MediaforceConfig, sample_path: str | None) -> dict[str, Any]:
    calibration = _approved(config)
    if sample_path is not None:
        calibration["sample_item"]["rel_path"] = sample_path
    return calibration


def test_only_a_folders_own_approval_sampled_on_that_kind_extends_to_it(tmp_path: Path) -> None:
    config = _config(tmp_path)
    bright_season, darker_season = "tv/Show/Season 1", "tv/Show/Season 2"
    kinds = {APPROVED_SEASON: DARKER, SEASON: DARKER, OTHER_SEASON: "cartoon", bright_season: "bright_live_action",
             darker_season: DARKER}

    def extend(kind: str, prefix: str, approved: dict[str, dict[str, Any]]) -> dict[str, Any]:
        return approve_kind_action(config, kind, prefix, load_calibration_state=_loader(approved, kinds),
                                   kind_of=lambda _config, season: kinds.get(season), approved_at="now")

    darker_sample = {APPROVED_SEASON: _approved_on(config, f"{APPROVED_SEASON}/e01.mkv")}
    toon_sample = {OTHER_SEASON: _approved_on(config, f"{OTHER_SEASON}/e01.mkv")}
    # A show approved on its bright season does not become the darker kind's, whichever season is named.
    show_on_bright = {"tv/Show": _approved_on(config, f"{bright_season}/e01.mkv")}
    assert extend(DARKER, darker_season, show_on_bright)["code"] == SOURCE_KIND_DIFFERS_CODE
    assert extend("bright_live_action", APPROVED_SEASON, darker_sample)["code"] == SOURCE_KIND_DIFFERS_CODE
    assert extend(DARKER, OTHER_SEASON, toon_sample)["code"] == SOURCE_KIND_DIFFERS_CODE
    assert extend("cartoon", OTHER_SEASON, toon_sample)["code"] == KIND_IS_ONE_TIME_CODE
    assert extend(DARKER, APPROVED_SEASON, {APPROVED_SEASON: _approved_on(config, None)})["code"] == SOURCE_KIND_DIFFERS_CODE
    assert load_kind_approval(config, DARKER) is None
    assert extend("bright_live_action", darker_season, show_on_bright)["ok"]
    assert extend(DARKER, f"{APPROVED_SEASON}/e01.mkv", darker_sample)["ok"]
    assert extend(DARKER, SEASON, darker_sample)["code"] == NO_FOLDER_APPROVAL_CODE
    assert extend(DARKER, OTHER_SEASON, {})["code"] == NO_FOLDER_APPROVAL_CODE


def test_stopping_touches_only_a_kinds_approval(tmp_path: Path) -> None:
    config = _config(tmp_path)
    attempts = config.paths.web_state_dir / "kinds" / "attempts.json"
    attempts.parent.mkdir(parents=True)
    attempts.write_text("{}")

    assert not stop_kind_approval(config, "attempts")
    assert attempts.exists()


def test_a_seasons_page_state_says_which_approval_governs_it(tmp_path: Path) -> None:
    config = _config(tmp_path)
    approve_kind(config, DARKER, APPROVED_SEASON, _approved(config), approved_at="now")
    approval = effective_production_approval(config, SEASON, _loader({}, {SEASON: DARKER}))

    state = decide_page_state(PageStateFacts(prefix=SEASON, workflow=None, encode_job=None, sample_job=None,
                                             calibration=None, approval=approval)).to_payload()

    assert (state["approval_source"], state["approval_kind"]) == ("kind", DARKER)


def test_the_kind_routes_report_refusals_as_conflicts() -> None:
    app = FastAPI()
    calls: list[tuple[str, ...]] = []
    register_app_window_routes(
        app, main_window_payload=dict, tv_shows_payload=dict, movies_payload=dict, other_media_payload=dict,
        sample_payload=lambda _prefix: None, approve_sample_action=lambda *_args: {}, make_sample_action=lambda *_args: {},
        compress_season_action=lambda _prefix: {}, kinds_payload=lambda: {"kinds": []},
        approve_kind_action=lambda kind, prefix: calls.append((kind, prefix)) or {"ok": kind == DARKER, "code": "x"},
        stop_kind_action=lambda kind: calls.append((kind,)) or {"ok": True},
    )
    approve = _route_endpoint(app, "/api/app/kinds/{kind}/approve", "POST")

    assert json.loads(_route_endpoint(app, "/api/app/kinds", "GET")().body) == {"kinds": []}
    assert asyncio.run(approve(DARKER, _json_request({"from_prefix": "/tv/Drama/"}))).status_code == 200
    assert asyncio.run(approve("cartoon", _json_request({"from_prefix": "tv/Toon"}))).status_code == 409
    assert _route_endpoint(app, "/api/app/kinds/{kind}/stop", "POST")(DARKER).status_code == 200
    assert calls == [(DARKER, "tv/Drama"), ("cartoon", "tv/Toon"), (DARKER,)]
