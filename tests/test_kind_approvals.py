"""One approval covers every season of a kind of content, and a folder's own decision still comes first (#1047)."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Collection
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI, HTTPException
from test_web_route_security import _json_request, _route_endpoint
from test_page_state import _review_ready
from sqlalchemy import create_engine
from test_content_kinds import _admit_historical_original, _item as _content_item

from mediaforce.core.config import ConfigPaths, MediaforceConfig, upsert_runtime_folder_policy_override
from mediaforce.core.db import DBClient, open_db, reset_engine_cache
from mediaforce.core.db_tables import calibration_jobs, encode_jobs, library_items, metadata, motion_keep_decisions, series_metadata, source_admissions, source_generations, \
    source_transitions, staged_artifacts
from mediaforce.encoding.encode_queue import DEFAULT_QUEUE_NAME, clear_terminal_encode_jobs_for_prefix, encode_run_manifest_files, \
    encode_run_manifest_rel_paths, load_encode_job, save_encode_job, save_queue_state
from mediaforce.encoding.free_space import ReservePreflight
from mediaforce.web.runtime.decision_evidence import CadenceQueuePartition
from mediaforce.web.runtime.encode_runtime import CONTROLLER_RESTART_STOP_REASON, KEPT_ORIGINAL_TERMINAL_REASON, unfinished_child_reason
from mediaforce.library.content_kinds import season_evidence, season_grouping
from mediaforce.library.source_evidence import append_version
from mediaforce.library.workflow_state import build_folder_workflow_state
from mediaforce.encoding.staging import FAR_BELOW_PREDICTION_CHECK
from mediaforce.web.runtime.episode_progress import load_season_episode_progress
from mediaforce.web.runtime.folder_actions import queue_folder_encode_action
from mediaforce.web.runtime.folder_state import save_calibration_state
from mediaforce.web.routes.app_window import register_app_window_routes
from mediaforce.web.runtime.folder_tuning_advice import calibration_policy_hash, review_gate
from mediaforce.web.runtime.kind_approvals import APPROVABLE_KINDS, APPROVAL_NOT_CURRENT_CODE, KIND_IS_ONE_TIME_CODE, \
    KIND_QUEUE_WAITS_CODE, KIND_RETRY_SECONDS, SIZE_GOAL_NOT_SCALABLE_CODE, KindAwareLoader, KindSeason, SeasonKinds, \
    NO_FOLDER_APPROVAL_CODE, SOURCE_KIND_DIFFERS_CODE, approve_kind, approve_kind_action, background_queue_blocker, kinds_payload, load_kind_approval, \
    next_kind_season, record_kind_attempt, stop_kind_approval
from mediaforce.web.runtime.page_state import PageStateFacts, PageStateSources, card_page_states, decide_page_state
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


def _finished_sample(config: MediaforceConfig, prefix: str, at: str = "2026-10-28T00:00:00+00:00") -> None:
    with open_db(config.paths.db_path) as connection:
        connection.execute(calibration_jobs.insert().values(
            job_id=f"sample-{prefix}", prefix=prefix, status="pending_review", lane="sample", action="sample",
            host_json="{}", policy_json="{}", sample_item_json="{}", created_at=at, updated_at=at, finished_at=at))
        connection.commit()


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
    class Kinds:
        def __call__(self, _config: MediaforceConfig, season: str) -> str | None:
            if asked is not None:
                asked.append(season)
            return kinds.get(season)

        def awaiting_measurement(self, _config: MediaforceConfig, season: str) -> bool:
            return kinds.get(season) == "unmeasured"

    return KindAwareLoader(lambda _config, prefix: folders.get(prefix), Kinds())


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


def test_a_folder_approval_comes_first_but_an_unapproved_sample_follows_its_kind(tmp_path: Path) -> None:
    config = _config(tmp_path)
    approve_kind(config, DARKER, APPROVED_SEASON, _approved(config), approved_at="2026-10-29T00:00:00+00:00")
    _finished_sample(config, SEASON)
    show_approval = _approved(config)
    show_approval["accepted_sample_job_id"] = show_approval["job_id"] = "show-sample"

    by_show = effective_production_approval(config, EPISODE, _loader({"tv/Thriller": show_approval}, {SEASON: DARKER}))
    unapproved_own = {key: value for key, value in _approved(config, job_id="awaiting-review").items()
                      if not key.startswith("accepted_")}
    by_own_sample = _loader({SEASON: unapproved_own}, {SEASON: DARKER})

    assert by_show is not None and by_show.source == "folder" and by_show.prefix == "tv/Thriller"
    inherited = effective_production_approval(config, EPISODE, by_own_sample)
    assert inherited is not None and inherited.kind == DARKER
    assert production_calibration(config, SEASON, by_own_sample) == (SEASON, inherited.calibration)
    assert by_own_sample(config, SEASON) == unapproved_own


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


def _queue(
        config: MediaforceConfig, prefix: str, load: KindAwareLoader, *, only_library_item_ids: Collection[int] | None = None,
        persist_jobs: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    item_ids = [_insert_episode(config)] if only_library_item_ids is None else list(only_library_item_ids)
    manifests: list[dict[str, Any]] = []

    def create_manifest(connection: DBClient, manifest_config: MediaforceConfig, **kwargs: Any) -> tuple[dict[str, Any], Path]:
        included = kwargs.get("include_library_item_ids", item_ids)
        items = connection.execute(library_items.select().where(library_items.c.id.in_(included))).mappings().all()
        manifest = {"items": [{"library_item_id": item["id"], "rel_path": item["rel_path"],
                               "resolved_policy": manifest_config.resolve_policy(item["rel_path"])} for item in items]}
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
                only_library_item_ids=only_library_item_ids,
                load_job_state=lambda *_args: None, load_calibration_state=load, review_gate=review_gate,
                upsert_override=upsert_runtime_folder_policy_override, load_active_encode_job_for_prefix_fn=lambda *_args: None,
                clear_terminal_encode_jobs_for_prefix_fn=clear_terminal_encode_jobs_for_prefix if persist_jobs else lambda *_args: None,
                prepare_terminal_encode_job_for_requeue_fn=lambda *_args: None,
                save_encode_job=save_encode_job if persist_jobs else lambda *_args: None,
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


def _encode_job(
        config: MediaforceConfig, prefix: str, status: str, *, job_id: str | None = None, parent_job_id: str | None = None,
        terminal_reason: str | None = None, job_kind: str = "single", created_at: str = "2026-10-26T00:00:00+00:00",
) -> str:
    job_id = job_id or f"{prefix}-{status}"
    with open_db(config.paths.db_path) as connection:
        connection.execute(encode_jobs.insert().values(
            job_id=job_id, prefix=prefix, status=status, manifest_path="run.json", host_json="{}", job_kind=job_kind,
            parent_job_id=parent_job_id, terminal_reason=terminal_reason, created_at=created_at, updated_at=created_at,
        ))
    return job_id


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
    _finished_sample(config, own)
    own_sample = {key: value for key, value in _approved(config).items() if not key.startswith("accepted_")}
    load = _loader({own: own_sample}, {small: DARKER, large: DARKER, own: DARKER})

    assert _next(config, load) is None
    approve_kind(config, DARKER, APPROVED_SEASON, _approved(config), approved_at="2026-10-29T00:00:00+00:00")
    first = _next(config, load)
    assert first is not None and (first.season, first.kind, first.waiting_bytes) == (own, DARKER, 50_000)
    record_kind_attempt(config, first, {"ok": True}, at=0.0)
    second = _next(config, load)
    assert second is not None and second.season == large
    # A wait for the queue to settle is no attempt, so the season is offered again.
    record_kind_attempt(config, second, {"ok": False, "code": KIND_QUEUE_WAITS_CODE}, at=0.0)
    assert _next(config, load) == second
    record_kind_attempt(config, second, {"ok": True}, at=0.0)
    third = _next(config, load)
    assert third is not None and third.season == small
    record_kind_attempt(config, third, {"ok": True}, at=0.0)
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
        facts = kinds_payload(connection, config, load_calibration_state=_loader({}, {"tv/Fresh/Season 1": DARKER}, asked))
    engine.dispose()

    # Only folder approvals are counted, so the page never works out a season's kind just to skip it.
    assert asked == []
    kinds = {kind["kind"]: kind for kind in facts["kinds"]}
    assert [kind["kind"] for kind in facts["kinds"]] == ["darker_live_action", "bright_live_action", "cartoon", "4k"]
    assert kinds[DARKER]["shows"] == [{"prefix": "tv/Drama", "seasons": 1, "waiting_bytes": 1000,
                                       "sample_season": APPROVED_SEASON}]
    assert kinds[DARKER]["sample_season"] is None
    assert kinds[DARKER]["settings"] == []
    assert kinds[DARKER]["approval"] == {"from_prefix": APPROVED_SEASON, "approved_at": "2026-10-26T01:00:00+00:00",
                                         "size_goal_mode": "normalized", "size_goal_mb": 300.0, "library_item_id": None}
    assert kinds["bright_live_action"]["approval"] is None and kinds["cartoon"]["one_time"]
    assert facts["unmeasured_seasons"] == 1
    assert facts["left_out"] == [{"season": "tv/Fresh/Season 1", "kind": "unmeasured", "waiting_bytes": 1000}]


def test_a_kind_offers_each_shows_biggest_season_to_sample_and_names_the_one_being_made(tmp_path: Path) -> None:
    config = _config(tmp_path)
    engine = create_engine("sqlite://")
    with engine.connect() as connection:
        metadata.create_all(connection)
        # Each season measures darker from a published episode's original; the rest of its files wait.
        for measured, waiting in [("tv/Drama/Season 1", 1), ("tv/Drama/Season 2", 2), ("tv/Crime/Season 1", 1),
                                  ("tv/Crime/Season 2", 1)]:
            published = _content_item(connection, f"{measured}/e00.mkv", status="promoted", codec="av1", current_dark=0.0)
            _admit_historical_original(connection, published, dark=0.5)
            for episode in range(1, waiting + 1):
                _content_item(connection, f"{measured}/e0{episode}.mkv", status="discovered", codec="h264", current_dark=0.0)
        for show in ["tv/Drama", "tv/Crime"]:
            connection.execute(series_metadata.insert().values(series_prefix=show, tmdb_genres_json='["Drama"]',
                                                               updated_at="now"))
        folders: dict[str, dict[str, Any]] = {}
        load = _loader(folders, {})

        def kind_facts() -> dict[str, Any]:
            facts = kinds_payload(connection, config, load_calibration_state=load)
            return next(kind for kind in facts["kinds"] if kind["kind"] == DARKER)

        def sample_job(season: str, status: str, created_at: str, lane: str = "sample") -> None:
            connection.execute(calibration_jobs.insert().values(
                job_id=f"{season}-{created_at}", prefix=season, status=status, lane=lane, action="sample",
                host_json="{}", policy_json="{}", sample_item_json="{}", created_at=created_at, updated_at=created_at))

        before = kind_facts()
        sample_job("tv/Crime/Season 1", "running", "2026-10-26T01:00:00+00:00", lane="full")
        folders["tv/Drama/Season 1"] = _approved_on(config, "tv/Drama/Season 1/e00.mkv")
        sample_job("tv/Drama/Season 1", "completed", "2026-10-26T02:00:00+00:00")
        settled = kind_facts()
        sample_job("tv/Crime/Season 2", "pending_review", "2026-10-26T03:00:00+00:00")
        sample_job("tv/Drama/Season 2", "running", "2026-10-26T04:00:00+00:00")
        making = kind_facts()
        # A sample that stopped stays on the page until it is made again, so its trouble and choices show.
        sample_job("tv/Drama/Season 2", "failed", "2026-10-26T05:00:00+00:00")
        failed = kind_facts()
    engine.dispose()

    assert [(show["prefix"], show["sample_season"]) for show in before["shows"]] == [
        ("tv/Drama", "tv/Drama/Season 2"), ("tv/Crime", "tv/Crime/Season 1")]
    assert before["sample_season"] is None and settled["sample_season"] is None
    assert making["sample_season"] == "tv/Drama/Season 2" and failed["sample_season"] == "tv/Drama/Season 2"


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
        _admit_historical_original(connection, published, dark=0.5)
        _keep_original_backup(connection, published)
        thriller = _content_item(connection, EPISODE, status="promoted", codec="av1", current_dark=0.5)
        _admit_historical_original(connection, thriller, dark=0.5)
        # Thriller's show approval was sampled on a darker season; its Season 3 has its own bright approval and the
        # only kept episode, which never stands for the show's setting.
        own_season = "tv/Thriller/Season 3"
        kept = _content_item(connection, f"{own_season}/e01.mkv", status="promoted", codec="av1", current_dark=0.0)
        _admit_historical_original(connection, kept, dark=0.0)
        _keep_original_backup(connection, kept)
        for show in ("tv/Drama", "tv/Thriller"):
            connection.execute(series_metadata.insert().values(
                series_prefix=show, tmdb_genres_json='["Drama"]', updated_at="now"))
        approved = {APPROVED_SEASON: _approved_on(config, f"{APPROVED_SEASON}/e01.mkv"),
                    "tv/Thriller": _approved_on(config, EPISODE), own_season: _approved_on(config, f"{own_season}/e01.mkv")}
        kinds = {APPROVED_SEASON: DARKER, SEASON: DARKER, own_season: "bright_live_action"}

        def facts_with(folders: dict[str, dict[str, Any]]) -> dict[str, Any]:
            facts = kinds_payload(connection, config, load_calibration_state=_loader(folders, kinds))
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


@pytest.mark.parametrize("scope", [SEASON, "tv/Thriller"])
def test_a_current_folder_approval_automatically_offers_its_waiting_season(tmp_path: Path, scope: str) -> None:
    config = _config(tmp_path)
    _waiting_episode(config, EPISODE, 1_000)
    load = _loader({scope: _approved(config)}, {})

    chosen = _next(config, load)

    assert chosen is not None and chosen.season == SEASON and chosen.kind is None
    record_kind_attempt(config, chosen, {"ok": True}, at=0.0)
    assert _next(config, load) is None


@pytest.mark.parametrize("state", ["unapproved", "stale", "paused", "active"])
def test_automatic_folder_admission_preserves_approval_and_queue_guards(tmp_path: Path, state: str) -> None:
    config = _config(tmp_path)
    _waiting_episode(config, EPISODE, 1_000)
    approval = _approved(config)
    if state == "unapproved":
        approval = {key: value for key, value in approval.items() if not key.startswith("accepted_")}
    if state == "stale":
        approval["policy"]["video"]["preset"] = 3
    if state == "paused":
        with open_db(config.paths.db_path) as connection:
            save_queue_state(connection, {"queue_name": DEFAULT_QUEUE_NAME, "is_paused": True, "updated_at": "now"})
    if state == "active":
        _encode_job(config, OTHER_SEASON, "running")

    assert _next(config, _loader({SEASON: approval}, {})) is None


@pytest.mark.parametrize("scope", [SEASON, "tv/Thriller", "tv", ""])
def test_automatic_admission_respects_a_stop_on_its_containing_scope(tmp_path: Path, scope: str) -> None:
    config = _config(tmp_path)
    _waiting_episode(config, EPISODE, 1_000)
    _encode_job(config, scope, "stopped")

    assert _next(config, _loader({SEASON: _approved(config)}, {})) is None


def test_a_new_season_sample_waits_for_review_instead_of_retrying_the_show_approval(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _waiting_episode(config, EPISODE, 1_000)
    pending = {key: value for key, value in _approved(config).items() if not key.startswith("accepted_")}

    assert _next(config, _loader({"tv/Thriller": _approved(config), SEASON: pending}, {})) is None



def _stored_folder_run(
        config: MediaforceConfig, prefix: str, items: dict[int, str], outcomes: dict[int, tuple[str, str | None]], *,
        name: str = "old", created_at: str = "2026-10-26T00:00:00+00:00",
) -> dict[int, str]:
    config.paths.run_manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = config.paths.run_manifest_dir / f"{name}.json"
    manifest_path.write_text(json.dumps({"items": [{"library_item_id": item_id, "rel_path": rel_path}
                                                for item_id, rel_path in items.items()]}))
    parent = _encode_job(config, prefix, "needs_attention", job_id=name, job_kind="folder", created_at=created_at)
    children: dict[int, str] = {}
    for index, item_id in enumerate(items):
        status, reason = outcomes.get(item_id, ("completed", None))
        child = _encode_job(config, prefix, status, job_id=f"{name}-{item_id}", parent_job_id=parent,
                            job_kind="shard", terminal_reason=reason, created_at=created_at)
        children[item_id] = child
        with open_db(config.paths.db_path) as connection:
            connection.execute(encode_jobs.update().where(encode_jobs.c.job_id == child).values(
                manifest_path=str(manifest_path), manifest_indexes_json=json.dumps([index]),
            ))
    with open_db(config.paths.db_path) as connection:
        connection.execute(encode_jobs.update().where(encode_jobs.c.job_id == parent).values(manifest_path=str(manifest_path)))
    return children


def _displayed_episode(config: MediaforceConfig, item_id: int, path: str) -> dict[str, Any]:
    with open_db(config.paths.db_path) as connection:
        workflow = build_folder_workflow_state(connection, path, library_types=config.library_type_map)
        [episode] = load_season_episode_progress(connection, [item for item in workflow.items if item.item_id == item_id], config=config)
    return episode


@pytest.mark.parametrize("checked_state", ["size_held", "failed", "publish_failed", "kept_original"])
@pytest.mark.parametrize("current_status,older_writer", [("needs_attention", False), ("needs_attention", True), ("completed", True)])
def test_checked_copy_or_keep_supersedes_its_run_but_not_an_independent_writer(
        tmp_path: Path, checked_state: str, current_status: str, older_writer: bool,
) -> None:
    config = _config(tmp_path)
    item_id = _waiting_episode(config, EPISODE, 1_000, status="discovered" if checked_state == "kept_original" else "encoded")
    if older_writer:
        _stored_folder_run(config, SEASON, {item_id: EPISODE}, {item_id: ("stopped", CONTROLLER_RESTART_STOP_REASON)},
                           name="earlier-writer")
    children = _stored_folder_run(config, SEASON, {item_id: EPISODE}, {item_id: (current_status, None)},
                                  name="current", created_at="2026-10-27T00:00:00+00:00")
    with open_db(config.paths.db_path) as connection:
        connection.execute(encode_jobs.update().where(encode_jobs.c.job_id == children[item_id]).values(
            progress_json=json.dumps({"failure_analysis": {"kind": "final_size_target_miss"}})))
        if checked_state == "kept_original":
            connection.execute(motion_keep_decisions.insert().values(
                library_item_id=item_id, source_fingerprint=EPISODE, reason_code="mixed_motion", prefix=SEASON, kept_at="now"))
        else:
            validation: dict[str, Any] = {"passed": False, "checks": [{"passed": False, "message": "The copy did not decode."}]}
            if checked_state == "size_held":
                validation.update(checks=[{"passed": False, "message": FAR_BELOW_PREDICTION_CHECK}], size_prediction={"held": True})
            elif checked_state == "publish_failed":
                validation = {"passed": True, "automatic_publish": {"state": "failed"}}
            connection.execute(staged_artifacts.insert().values(
                library_item_id=item_id, staging_path="checked.mkv", validation_json=json.dumps(validation), updated_at="now"))

    episode = _displayed_episode(config, item_id, EPISODE)

    assert episode["stage"] == ("kept_original" if checked_state == "kept_original" else "needs_you")
    assert [reason["code"] for reason in episode["additional_wait_reasons"]] == (["restart_stopped"] if older_writer else [])


@pytest.mark.parametrize("scope", [SEASON, "tv/Thriller", "tv", ""])
@pytest.mark.parametrize("status,reason", [
    ("needs_attention", None), ("failed", None), ("stopped", CONTROLLER_RESTART_STOP_REASON), ("stopped", None),
])
def test_a_problem_file_stays_held_while_other_files_and_seasons_are_offered(
        tmp_path: Path, scope: str, status: str, reason: str | None,
) -> None:
    config = _config(tmp_path)
    problem = _waiting_episode(config, EPISODE, 1_000)
    sibling_path = f"{SEASON}/e02.mkv"
    sibling = _waiting_episode(config, sibling_path, 100)
    elsewhere_path = f"{OTHER_SEASON}/e01.mkv"
    elsewhere = _waiting_episode(config, elsewhere_path, 10)
    _stored_folder_run(config, scope, {problem: EPISODE, sibling: sibling_path}, {problem: (status, reason)})
    load = _loader({SEASON: _approved(config), OTHER_SEASON: _approved(config)}, {})

    chosen = _next(config, load)

    assert chosen is not None and chosen.season == SEASON and chosen.files == (sibling,) and chosen.waiting_bytes == 100
    record_kind_attempt(config, chosen, {"ok": True}, at=0.0)
    next_season = _next(config, load)
    assert next_season is not None and next_season.files == (elsewhere,)


def test_a_newer_completion_only_settles_the_same_files_old_question(tmp_path: Path) -> None:
    config = _config(tmp_path)
    problem = _waiting_episode(config, EPISODE, 1_000)
    sibling_path = f"{SEASON}/e02.mkv"
    sibling = _waiting_episode(config, sibling_path, 100)
    _stored_folder_run(config, "tv/Thriller", {problem: EPISODE, sibling: sibling_path},
                       {problem: ("needs_attention", None)})
    _stored_folder_run(config, "tv/Thriller", {sibling: sibling_path}, {}, name="new-sibling",
                       created_at="2026-10-27T00:00:00+00:00")
    load = _loader({SEASON: _approved(config)}, {})
    chosen = _next(config, load)
    assert chosen is not None and chosen.files == (sibling,)

    _stored_folder_run(config, "tv/Thriller", {problem: EPISODE}, {}, name="new-problem",
                       created_at="2026-10-28T00:00:00+00:00")
    chosen = _next(config, load)
    assert chosen is not None and set(chosen.files) == {problem, sibling}


def test_a_new_problem_between_selection_and_queueing_keeps_admission_waiting(tmp_path: Path) -> None:
    config = _config(tmp_path)
    item_id = _waiting_episode(config, EPISODE, 1_000)
    _stored_folder_run(config, "tv/Thriller", {item_id: EPISODE}, {item_id: ("stopped", CONTROLLER_RESTART_STOP_REASON)})

    with open_db(config.paths.db_path) as connection:
        assert background_queue_blocker(connection, library_item_ids=[item_id]) == {"ok": False, "code": KIND_QUEUE_WAITS_CODE}


def test_completed_files_waiting_for_publication_are_not_offered_again(tmp_path: Path) -> None:
    config = _config(tmp_path)
    staged = _waiting_episode(config, EPISODE, 1_000, status="validated")
    sibling_path = f"{SEASON}/e02.mkv"
    sibling = _waiting_episode(config, sibling_path, 100)
    with open_db(config.paths.db_path) as connection:
        connection.execute(staged_artifacts.insert().values(
            library_item_id=staged, manifest_run_id="finished", staging_path="finished.mkv", source_path="original.mkv",
            staging_size_bytes=100, validated_at="now", updated_at="now",
        ))

    chosen = _next(config, _loader({SEASON: _approved(config)}, {}))

    assert chosen is not None and chosen.files == (sibling,)


@pytest.mark.parametrize("scope", [SEASON, "tv/Thriller"])
def test_automatic_subset_queue_preserves_the_problem_files_custody_record(tmp_path: Path, scope: str) -> None:
    config = _config(tmp_path)
    problem = _waiting_episode(config, EPISODE, 1_000)
    sibling_path = f"{SEASON}/e02.mkv"
    sibling = _waiting_episode(config, sibling_path, 100)
    children = _stored_folder_run(config, scope, {problem: EPISODE, sibling: sibling_path},
                                  {problem: ("stopped", CONTROLLER_RESTART_STOP_REASON)})
    load = _loader({SEASON: _approved(config)}, {})
    chosen = _next(config, load)
    assert chosen is not None
    with open_db(config.paths.db_path) as connection:
        connection.execute(encode_jobs.update().where(encode_jobs.c.job_id == children[problem]).values(
            last_host_json=json.dumps({"key": "retained-computer", "mode": "ssh"}),
        ))
        before = load_encode_job(connection, children[problem])

    result, manifests = _queue(config, chosen.season, load, only_library_item_ids=chosen.files, persist_jobs=True)

    assert result["ok"], result
    assert {item["library_item_id"] for manifest in manifests for item in manifest["items"]} == {sibling}
    with open_db(config.paths.db_path) as connection:
        after = load_encode_job(connection, children[problem])
    assert before is not None and after is not None
    custody_fields = ("job_id", "status", "terminal_reason", "manifest_path", "manifest_indexes", "last_host", "worker_id", "process_pid")
    assert {field: after[field] for field in custody_fields} == {field: before[field] for field in custody_fields}


def test_a_newer_completion_does_not_reconcile_an_earlier_restart_stopped_writer(tmp_path: Path) -> None:
    config = _config(tmp_path)
    item_id = _waiting_episode(config, EPISODE, 1_000)
    children = _stored_folder_run(config, "tv/Thriller", {item_id: EPISODE},
                                  {item_id: ("stopped", CONTROLLER_RESTART_STOP_REASON)})
    _stored_folder_run(config, SEASON, {item_id: EPISODE}, {}, name="later-completion",
                       created_at="2026-10-27T00:00:00+00:00")
    load = _loader({SEASON: _approved(config)}, {})

    assert _next(config, load) is None
    assert _displayed_episode(config, item_id, EPISODE)["stage"] == "needs_you"

    with open_db(config.paths.db_path) as connection:
        connection.execute(encode_jobs.update().where(encode_jobs.c.job_id == children[item_id]).values(status="completed"))
    chosen = _next(config, load)
    assert chosen is not None and chosen.files == (item_id,)


@pytest.mark.parametrize("status,stage", [("running", "compressing"), ("queued", "waiting"), ("needs_attention", "needs_you")])
def test_retained_writer_does_not_hide_current_work_or_its_size_question(tmp_path: Path, status: str, stage: str) -> None:
    config = _config(tmp_path)
    item_id = _waiting_episode(config, EPISODE, 1_000)
    _stored_folder_run(config, "tv/Thriller", {item_id: EPISODE},
                       {item_id: ("stopped", CONTROLLER_RESTART_STOP_REASON)})
    children = _stored_folder_run(config, SEASON, {item_id: EPISODE}, {item_id: (status, None)}, name="current",
                                  created_at="2026-10-27T00:00:00+00:00")
    with open_db(config.paths.db_path) as connection:
        connection.execute(encode_jobs.update().where(encode_jobs.c.job_id == children[item_id]).values(progress_json=json.dumps({
            "progress_state": "encoding", "percent_complete": 37,
            "failure_analysis": {"kind": "quality_floor_size_conflict", "retry_strategy": "needs_operator_approval",
                "target_size_bytes": 500, "proposed_target_size_bytes": 900, "item_rel_path": EPISODE},
        })))
    episode = _displayed_episode(config, item_id, EPISODE)
    assert episode["stage"] == stage
    assert {reason["code"] for reason in episode["additional_wait_reasons"]} == {"restart_stopped"}
    if status == "running":
        assert episode["percent_complete"] == 37
    if status == "needs_attention":
        assert episode["size_question"]["job_id"] == children[item_id]


def test_unknown_held_membership_is_visible_for_every_file_it_holds(tmp_path: Path) -> None:
    config = _config(tmp_path)
    first = _waiting_episode(config, EPISODE, 1_000)
    sibling_path = f"{SEASON}/e02.mkv"
    sibling = _waiting_episode(config, sibling_path, 1_000)
    job_id = _encode_job(config, SEASON, "needs_attention")
    with open_db(config.paths.db_path) as connection:
        connection.execute(encode_jobs.update().where(encode_jobs.c.job_id == job_id).values(
            progress_json=json.dumps({"current_item_rel_path": EPISODE})))
    assert _next(config, _loader({SEASON: _approved(config)}, {})) is None
    assert [_displayed_episode(config, item, path)["stage"] for item, path in ((first, EPISODE), (sibling, sibling_path))] == [
        "needs_you", "needs_you"]


@pytest.mark.parametrize("status,failure_kind", [
    ("stopped", None), ("needs_attention", "worker_restart"), ("needs_attention", "stale_lease"),
])
def test_a_newer_completion_cannot_hide_other_retained_writer_custody(
        tmp_path: Path, status: str, failure_kind: str | None,
) -> None:
    config = _config(tmp_path)
    config.raw["media"].update({"staging_root": str(tmp_path / "staging"), "output_container": "mkv"})
    item_id = _waiting_episode(config, EPISODE, 1_000)
    children = _stored_folder_run(config, "tv/Thriller", {item_id: EPISODE}, {item_id: (status, None)})
    _stored_folder_run(config, SEASON, {item_id: EPISODE}, {}, name="later-completion",
                       created_at="2026-10-27T00:00:00+00:00")
    with open_db(config.paths.db_path) as connection:
        connection.execute(encode_jobs.update().where(encode_jobs.c.job_id == children[item_id]).values(
            last_failure_kind=failure_kind, host_json=json.dumps({"host": "writer.example", "mode": "ssh"}),
            attempt_count=1, started_at="2026-10-26T00:00:00+00:00",
        ))
    load = _loader({SEASON: _approved(config)}, {})

    assert _next(config, load) is None

    from mediaforce.web.runtime.encode_runtime import _remote_cleanup_receipt

    with open_db(config.paths.db_path) as connection:
        job = load_encode_job(connection, children[item_id])
        assert job is not None
        items = json.loads(Path(job["manifest_path"]).read_text())["items"]
        receipt = _remote_cleanup_receipt(config, job, items)
        connection.execute(encode_jobs.update().where(encode_jobs.c.job_id == children[item_id]).values(
            progress_json=json.dumps({"remote_cleanup_verified": receipt}),
        ))
    assert _next(config, load) is not None
    with open_db(config.paths.db_path) as connection:
        connection.execute(encode_jobs.update().where(encode_jobs.c.job_id == children[item_id]).values(attempt_count=2))
    assert _next(config, load) is None


def test_a_catalog_path_change_does_not_hide_a_stopped_writer_for_the_same_item(tmp_path: Path) -> None:
    config = _config(tmp_path)
    item_id = _waiting_episode(config, EPISODE, 1_000)
    _stored_folder_run(config, "tv/Thriller", {item_id: EPISODE}, {item_id: ("stopped", CONTROLLER_RESTART_STOP_REASON)})
    renamed = f"{OTHER_SEASON}/renamed.mkv"
    with open_db(config.paths.db_path) as connection:
        connection.execute(library_items.update().where(library_items.c.id == item_id).values(rel_path=renamed, parent_dir=OTHER_SEASON))

    assert _next(config, _loader({OTHER_SEASON: _approved(config)}, {})) is None
    assert _displayed_episode(config, item_id, renamed)["stage"] == "needs_you"


@pytest.mark.parametrize("indexes", [None, [], [99], [0, 99], "0"])
def test_unknown_shard_membership_cannot_clear_an_older_question(tmp_path: Path, indexes: Any) -> None:
    config = _config(tmp_path)
    item_id = _waiting_episode(config, EPISODE, 1_000)
    _stored_folder_run(config, "tv/Thriller", {item_id: EPISODE}, {item_id: ("needs_attention", None)})
    children = _stored_folder_run(config, SEASON, {item_id: EPISODE}, {}, name="new",
                                  created_at="2026-10-27T00:00:00+00:00")
    with open_db(config.paths.db_path) as connection:
        connection.execute(encode_jobs.update().where(encode_jobs.c.job_id == children[item_id]).values(
            manifest_indexes_json=json.dumps(indexes) if indexes is not None else None,
        ))
        child = load_encode_job(connection, children[item_id])
    assert child is not None
    assert encode_run_manifest_files(child, strict_indexes=True) == []
    assert encode_run_manifest_rel_paths(child)
    assert _next(config, _loader({SEASON: _approved(config)}, {})) is None
    assert _displayed_episode(config, item_id, EPISODE)["stage"] == "needs_you"


def test_an_undone_keep_original_is_not_held_by_its_old_backstop_stop(tmp_path: Path) -> None:
    config = _config(tmp_path)
    item_id = _waiting_episode(config, EPISODE, 1_000)
    children = _stored_folder_run(config, SEASON, {item_id: EPISODE}, {item_id: ("stopped", KEPT_ORIGINAL_TERMINAL_REASON)})
    with open_db(config.paths.db_path) as connection:
        connection.execute(motion_keep_decisions.insert().values(
            library_item_id=item_id, source_fingerprint=EPISODE, reason_code="mixed_motion", prefix=SEASON, kept_at="now",
        ))
        child = load_encode_job(connection, children[item_id])
    load = _loader({SEASON: _approved(config)}, {})
    assert _next(config, load) is None
    assert child is not None and not unfinished_child_reason(child)[2]

    with open_db(config.paths.db_path) as connection:
        connection.execute(motion_keep_decisions.delete().where(motion_keep_decisions.c.library_item_id == item_id))
    chosen = _next(config, load)
    assert chosen is not None and chosen.files == (item_id,)


def test_a_declined_size_exception_is_not_automatically_asked_again(tmp_path: Path) -> None:
    config = _config(tmp_path)
    item_id = _waiting_episode(config, EPISODE, 1_000)
    children = _stored_folder_run(config, SEASON, {item_id: EPISODE}, {item_id: ("needs_attention", None)})
    with open_db(config.paths.db_path) as connection:
        connection.execute(encode_jobs.update().where(encode_jobs.c.job_id == children[item_id]).values(
            progress_json=json.dumps({"failure_analysis": {"owner_size_decision": {"answer": "keep_original"}}}),
        ))

    assert _next(config, _loader({SEASON: _approved(config)}, {})) is None


@pytest.mark.parametrize("identity", [None, True, False, 0, -1, "not-an-item-id"])
def test_legacy_or_malformed_item_identity_uses_its_recorded_path(tmp_path: Path, identity: Any) -> None:
    config = _config(tmp_path)
    _waiting_episode(config, f"{OTHER_SEASON}/unrelated.mkv", 100)
    item_id = _waiting_episode(config, EPISODE, 1_000)
    _stored_folder_run(config, SEASON, {item_id: EPISODE}, {item_id: ("stopped", CONTROLLER_RESTART_STOP_REASON)})
    manifest = config.paths.run_manifest_dir / "old.json"
    manifest.write_text(json.dumps({"items": [{"library_item_id": identity, "rel_path": EPISODE}]}))

    assert _next(config, _loader({SEASON: _approved(config)}, {})) is None


def _approve_stale_kind(config: MediaforceConfig, kind: str) -> None:
    approve_kind(config, kind, APPROVED_SEASON, _approved(config), approved_at="now")
    path = config.paths.web_state_dir / "kinds" / f"{kind}.json"
    saved_kind = json.loads(path.read_text())
    saved_kind["accepted_policy_hash"] = "a different policy"
    path.write_text(json.dumps(saved_kind))


@pytest.mark.parametrize("stale_kind", [False, True])
@pytest.mark.parametrize("kind", [DARKER, "bright_live_action", "unmeasured", "mixed", "cartoon", "4k"])
def test_old_samples_follow_a_reusable_kind_while_its_newest_sample_stays_visible(
        tmp_path: Path, kind: str, stale_kind: bool,
) -> None:
    config = _config(tmp_path)
    if stale_kind and kind in APPROVABLE_KINDS:
        _approve_stale_kind(config, kind)
    folders = {SEASON: _review_ready(), OTHER_SEASON: _review_ready()}
    load = _loader(folders, {SEASON: kind, OTHER_SEASON: kind})
    sources = PageStateSources(load, lambda *_args: {})
    with open_db(config.paths.db_path) as connection:
        for index, season in enumerate([SEASON, OTHER_SEASON]):
            connection.execute(calibration_jobs.insert().values(
                job_id=f"sample-{index}", prefix=season, status="pending_review", lane="sample", action="sample",
                host_json="{}", policy_json="{}", sample_item_json="{}",
                created_at=f"2026-10-29T0{index}:00:00+00:00", updated_at="now",
                finished_at=f"2026-10-29T0{index}:30:00+00:00"))
        connection.commit()
        report = {"seasons": [{"season": season, "kind": kind} for season in folders]}
        with patch("mediaforce.web.runtime.kind_approvals.season_kind_report", return_value=report):
            states = card_page_states(connection, config, [(season, None, None) for season in folders], sources)
        reusable = kind in APPROVABLE_KINDS and not stale_kind
        assert states[SEASON]["key"] == ("waiting_for_measurement" if kind == "unmeasured" else
                                         "waiting_for_kind" if reusable else "ready_to_compare")
        assert states[SEASON]["needs_you"] == (not reusable and kind != "unmeasured")
        assert states[OTHER_SEASON]["key"] == ("waiting_for_measurement" if kind == "unmeasured" else "ready_to_compare")
        assert states[OTHER_SEASON]["needs_you"] == (kind != "unmeasured")
        assert load(config, SEASON) == folders[SEASON]


def test_kind_approval_replaces_old_sample_decision_and_allows_queueing(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _finished_sample(config, SEASON)
    saved = _review_ready()
    load = _loader({SEASON: saved}, {SEASON: DARKER})
    approve_kind(config, DARKER, APPROVED_SEASON, _approved(config), approved_at="2026-10-29T00:00:00+00:00")
    with open_db(config.paths.db_path) as connection:
        with patch("mediaforce.web.runtime.kind_approvals.season_kind_report",
                   return_value={"seasons": [{"season": SEASON, "kind": DARKER}]}):
            states = card_page_states(connection, config, [(SEASON, None, None)], PageStateSources(load, lambda *_args: {}))
    assert states[SEASON]["key"] == "ready_to_make"
    assert not states[SEASON]["needs_you"]
    assert states[SEASON]["approval_kind"] == DARKER
    result, manifests = _queue(config, SEASON, load)
    assert result["ok"] and manifests
    assert load(config, SEASON) == saved


@pytest.mark.parametrize("status", ["running", "queued", "pending_review"])
def test_an_approved_kind_keeps_active_samples_and_review_blocked_decisions(tmp_path: Path, status: str) -> None:
    config = _config(tmp_path)
    load = _loader({SEASON: _review_ready()}, {SEASON: DARKER})
    approve_kind(config, DARKER, APPROVED_SEASON, _approved(config), approved_at="2026-10-29T00:00:00+00:00")
    with open_db(config.paths.db_path) as connection:
        connection.execute(calibration_jobs.insert().values(
            job_id="sample", prefix=SEASON, status=status, lane="sample", action="sample",
            host_json="{}", policy_json="{}", sample_item_json="{}", created_at="2026-10-28T00:00:00+00:00", updated_at="2026-10-28T00:30:00+00:00",
            finished_at="2026-10-28T00:30:00+00:00" if status == "pending_review" else None))
        connection.commit()
        with patch("mediaforce.web.runtime.kind_approvals.season_kind_report",
                   return_value={"seasons": [{"season": SEASON, "kind": DARKER}]}), \
                patch("mediaforce.web.runtime.page_state.quality_risk_blocks_production", return_value=status == "pending_review"):
            states = card_page_states(connection, config, [(SEASON, None, None)], PageStateSources(load, lambda *_args: {}))
    assert states[SEASON]["key"] == {"running": "making_test", "queued": "sample_waiting",
                                     "pending_review": "ready_to_compare"}[status]
    assert states[SEASON]["needs_you"] == (status == "pending_review")


@pytest.mark.parametrize("sample_status", ["pending_review", "completed"])
@pytest.mark.parametrize("stale_kind", [False, True])
def test_kind_page_and_season_states_choose_the_same_waiting_sample(
        tmp_path: Path, stale_kind: bool, sample_status: str,
) -> None:
    config = _config(tmp_path)
    if stale_kind:
        _approve_stale_kind(config, DARKER)
    done = "tv/Done/Season 1"
    folders = {season: _review_ready() for season in [SEASON, OTHER_SEASON, done]}
    load = _loader(folders, {season: DARKER for season in folders})
    with open_db(config.paths.db_path) as connection:
        for index, season in enumerate(folders):
            published = _content_item(connection, f"{season}/e00.mkv", status="promoted", codec="av1", current_dark=0.0)
            _admit_historical_original(connection, published, dark=0.5)
            if season != done:
                _content_item(connection, f"{season}/e01.mkv", status="discovered", codec="h264", current_dark=0.0)
            connection.execute(series_metadata.insert().values(
                series_prefix="/".join(season.split("/")[:2]), tmdb_genres_json='["Drama"]', updated_at="now"))
            connection.execute(calibration_jobs.insert().values(
                job_id=f"sample-{index}", prefix=season, status=sample_status, lane="sample", action="sample",
                host_json="{}", policy_json="{}", sample_item_json="{}",
                created_at=f"2026-10-29T0{index}:00:00+00:00", updated_at="now",
                finished_at=f"2026-10-29T0{index}:30:00+00:00"))
        connection.commit()
        kind = next(row for row in kinds_payload(connection, config, load_calibration_state=load)["kinds"]
                    if row["kind"] == DARKER)
        states = card_page_states(connection, config, [(season, None, None) for season in folders],
                                  PageStateSources(load, lambda *_args: {}))
        if not stale_kind:
            folders[OTHER_SEASON] = _approved_on(config, f"{OTHER_SEASON}/e00.mkv")
            folders[OTHER_SEASON]["accepted_at"] = "2026-10-29T03:00:00+00:00"
            decided = next(row for row in kinds_payload(connection, config, load_calibration_state=load)["kinds"]
                           if row["kind"] == DARKER)
            after = card_page_states(connection, config, [(SEASON, None, None)], PageStateSources(load, lambda *_args: {}))[SEASON]
            assert decided["settings"]
            assert after["key"] == "waiting_for_kind" and not after["needs_you"]
    assert (kind["approval"] is not None) == stale_kind
    assert kind["sample_season"] == OTHER_SEASON
    assert states[kind["sample_season"]]["key"] == "ready_to_compare"
    assert states[SEASON]["key"] == ("ready_to_compare" if stale_kind else "waiting_for_kind")


@pytest.mark.parametrize("finished_at", ["2026-10-29T01:00:00+00:00", "unknown", ""])
@pytest.mark.parametrize("scope", [SEASON, "tv/Thriller"])
def test_newer_or_unknown_samples_and_show_samples_keep_their_decision(
        tmp_path: Path, finished_at: str, scope: str,
) -> None:
    config = _config(tmp_path)
    _waiting_episode(config, EPISODE, 1_000)
    _finished_sample(config, scope, finished_at)
    saved = _review_ready()
    load = _loader({scope: saved}, {SEASON: DARKER})
    approve_kind(config, DARKER, APPROVED_SEASON, _approved(config), approved_at="2026-10-29T00:00:00+00:00")
    assert effective_production_approval(config, EPISODE, load) is None
    assert production_calibration(config, SEASON, load) == (scope, saved)
    assert _next(config, load) is None
    with open_db(config.paths.db_path) as connection:
        state = card_page_states(connection, config, [(scope, None, None)], PageStateSources(load, lambda *_args: {}))[scope]
    assert state["key"] == "ready_to_compare" and state["needs_you"]


def test_never_sampled_seasons_stay_available_while_other_samples_run(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _finished_sample(config, SEASON)
    with open_db(config.paths.db_path) as connection:
        connection.execute(calibration_jobs.update().values(status="running", finished_at=None))
        connection.commit()
        sources = PageStateSources(_loader({SEASON: _review_ready()}, {SEASON: DARKER, OTHER_SEASON: DARKER}), lambda *_args: {})
        states = card_page_states(connection, config, [(SEASON, None, None), (OTHER_SEASON, None, None)], sources)
    assert states[SEASON]["key"] == "making_test"
    assert states[OTHER_SEASON]["key"] == "needs_test"
    assert states[OTHER_SEASON]["primary_action"] == "make_sample"


@pytest.mark.parametrize("parent", ["tv/Thriller", "tv"])
def test_undecided_kind_preserves_an_older_sample_with_a_saved_parent_sample(tmp_path: Path, parent: str) -> None:
    config = _config(tmp_path)
    _finished_sample(config, SEASON)
    _finished_sample(config, OTHER_SEASON, "2026-10-29T01:00:00+00:00")
    load = _loader({SEASON: _review_ready(), OTHER_SEASON: _review_ready(), parent: _review_ready()},
                   {SEASON: DARKER, OTHER_SEASON: DARKER})
    with open_db(config.paths.db_path) as connection:
        report = {"seasons": [{"season": season, "kind": DARKER} for season in [SEASON, OTHER_SEASON]]}
        with patch("mediaforce.web.runtime.kind_approvals.season_kind_report", return_value=report):
            state = card_page_states(connection, config, [(SEASON, None, None)], PageStateSources(load, lambda *_args: {}))[SEASON]
    assert state["key"] == "ready_to_compare" and state["needs_you"]


@pytest.mark.parametrize("measured_kind", [DARKER, "mixed"])
def test_measurement_releases_old_samples_to_the_kind_or_its_own_decision(tmp_path: Path, measured_kind: str) -> None:
    from mediaforce.web.runtime.app_window import app_window_payload

    config = _config(tmp_path)
    saved = _review_ready()
    kinds = {SEASON: "unmeasured"}
    load = _loader({SEASON: saved}, kinds)
    _finished_sample(config, SEASON)
    approve_kind(config, DARKER, APPROVED_SEASON, _approved(config), approved_at="2026-10-29T00:00:00+00:00")
    sources = PageStateSources(load, lambda *_args: {})
    with open_db(config.paths.db_path) as connection:
        waiting = card_page_states(connection, config, [(SEASON, None, None)], sources)[SEASON]
        assert waiting["key"] == "waiting_for_measurement"
        assert not waiting["needs_you"] and waiting["primary_action"] is None
        window = app_window_payload(connection, season_cards=[{"prefix": SEASON, "page_state": waiting}],
                                    computers=[], controller_storage={}, storage_admits_work=False,
                                    library_types=config.library_type_map, observed_at=datetime.now(UTC))
        assert not window["samples"]["waiting_on_you"]
        assert not window["work"]["seasons_needing_you"]
        kinds[SEASON] = measured_kind
        measured = card_page_states(connection, config, [(SEASON, None, None)], sources)[SEASON]
    assert measured["key"] == ("ready_to_make" if measured_kind == DARKER else "ready_to_compare")
    assert measured["needs_you"] == (measured_kind != DARKER)
    assert load(config, SEASON) == saved


@pytest.mark.parametrize("status", ["running", "queued", "failed", "pending_review"])
def test_unmeasured_seasons_keep_running_samples_and_wait_after_they_finish(tmp_path: Path, status: str) -> None:
    config = _config(tmp_path)
    _finished_sample(config, SEASON)
    load = _loader({SEASON: _review_ready()}, {SEASON: "unmeasured"})
    with open_db(config.paths.db_path) as connection:
        connection.execute(calibration_jobs.update().values(status=status))
        connection.commit()
        state = card_page_states(connection, config, [(SEASON, None, None)], PageStateSources(load, lambda *_args: {}))[SEASON]
    expected = {"running": "making_test", "queued": "sample_waiting"}.get(status, "waiting_for_measurement")
    assert state["key"] == expected
    assert not state["needs_you"]


@pytest.mark.parametrize("measured", [False, True])
def test_missing_genres_only_waits_for_measurement_when_original_measurements_are_missing(tmp_path: Path, measured: bool) -> None:
    config = _config(tmp_path)
    with open_db(config.paths.db_path) as connection:
        item_id = _content_item(connection, EPISODE, status="discovered", codec="h264", current_dark=0.5)
        connection.commit()
    kinds = SeasonKinds()
    load = KindAwareLoader(lambda *_args: _review_ready(), kinds)
    sources = PageStateSources(load, lambda *_args: {})
    with open_db(config.paths.db_path) as connection:
        before = card_page_states(connection, config, [(SEASON, None, None)], sources)[SEASON]
        assert before["key"] == "waiting_for_measurement" and not before["needs_you"]
        if measured:
            _admit_historical_original(connection, item_id, dark=0.5)
            connection.commit()
        state = card_page_states(connection, config, [(SEASON, None, None)], sources)[SEASON]
    assert kinds(config, SEASON) == "unmeasured"
    assert state["key"] == ("ready_to_compare" if measured else "waiting_for_measurement")
    assert state["needs_you"] == measured
