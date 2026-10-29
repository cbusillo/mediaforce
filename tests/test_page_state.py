"""The state a folder page or library card leads with (#901).

The first part decides from plain facts, porting the season page and library card cases the
frontend decided before. The second part reads real database rows for each confirmed case.
"""

import json
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db import DBClient, open_db, reset_engine_cache
from mediaforce.core.db_tables import encode_jobs, library_items, staged_artifacts
from mediaforce.encoding.encode_queue import load_latest_encode_job
from mediaforce.library.workflow_state import build_folder_workflow_state
from mediaforce.tuning.calibration_jobs import list_queue_summary, save_job
from mediaforce.web import app as web_app
from mediaforce.web.runtime import dashboard_payloads
from mediaforce.web.runtime.folder_tuning_advice import calibration_policy_hash
from mediaforce.web.runtime.page_state import PageStateFacts, PageStateSources, card_page_states, \
    decide_page_state, folder_page_state
from mediaforce.web.runtime.production_approval import EffectiveApproval, production_approval_contract

SHOW = "tv/show"
SEASON = f"{SHOW}/Season 1"
EPISODE = f"{SEASON}/episode-1.mkv"

STOP = "2026-10-15T08:07:38+00:00"
BEFORE_STOP = "2026-10-15T07:00:00+00:00"
AFTER_STOP = "2026-10-15T15:16:00+00:00"


def _review_ready(**fields: Any) -> dict[str, Any]:
    """A saved sample whose comparison clips the browser can play, awaiting approval."""
    return {
        "mode": "sample",
        "job_id": "sample-new",
        "draft_hash": "draft-new",
        "review_media_ready": True,
        "browser_review_ready": True,
        "review_pairs": [{"source_clip": {"path": "/review/source.mp4"}, "preview_clip": {"path": "/review/new.mp4"}}],
        "policy": {"video": {"preset": 6}},
        **fields,
    }


def _approved(job_id: str = "sample-new", **fields: Any) -> dict[str, Any]:
    """A saved sample approved as it stands, recording what the owner asked for."""
    calibration = _review_ready(**{
        "job_id": job_id,
        "accepted_sample_job_id": job_id,
        "accepted_at": AFTER_STOP,
        "accepted_draft_hash": "draft-new",
        "sample_item": {"resolved_operator_intent": {"request": {"size_goal": {"mode": "absolute", "value_mb": 300}}}},
        **fields,
    })
    calibration["accepted_policy_hash"] = calibration_policy_hash(calibration)
    return calibration


def _approval(calibration: dict[str, Any], prefix: str = SEASON) -> EffectiveApproval:
    return EffectiveApproval(prefix, calibration, production_approval_contract(calibration))


def _workflow(lane: str, state: str | None = None, *, enabled: bool = True, job_id: str | None = None) -> dict[str, Any]:
    return {
        "primary_lane": lane,
        "state": state or lane,
        "next_action": {"kind": "none", "enabled": enabled},
        "blockers": ["A finished file didn't pass its check."] if lane == "blocked" else [],
        "source_job_id": job_id,
    }


def _encode(status: str, error: str | None = None, finished_at: str = STOP, prefix: str = SEASON) -> dict[str, Any]:
    return {"job_id": "encode-old", "prefix": prefix, "status": status, "error": error, "finished_at": finished_at}


def _sample(status: str, *, finished_at: str | None = AFTER_STOP, created_at: str = AFTER_STOP,
            **fields: Any) -> dict[str, Any]:
    return {
        "job_id": "sample-new",
        "prefix": SEASON,
        "lane": "sample",
        "action": "baseline",
        "status": status,
        "created_at": created_at,
        "finished_at": finished_at,
        **fields,
    }


def _decide(**facts: Any) -> dict[str, Any]:
    facts.setdefault("workflow", _workflow("encode"))
    return decide_page_state(PageStateFacts(prefix=facts.pop("prefix", SEASON), **facts)).to_payload()


# Each case the frontend's season page and library cards decided before #901, as facts and the server's state.
PORTED_CASES: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
    (
        "a newly discovered season is not ready to compress",
        {},
        {"key": "needs_test", "tone": "quiet", "primary_action": "make_sample"},
    ),
    (
        "a waiting sample",
        {"sample_job": _sample("queued", finished_at=None)},
        {"key": "sample_waiting", "tone": "quiet", "source_job": {"id": "sample-new", "kind": "sample",
                                                                  "prefix": SEASON, "status": "queued"}},
    ),
    (
        "a sample waiting to retry",
        {"sample_job": _sample("retry_backoff", finished_at=None)},
        {"key": "sample_waiting", "tone": "attention"},
    ),
    (
        "a running sample",
        {"sample_job": _sample("running", finished_at=None)},
        {"key": "making_test", "tone": "active"},
    ),
    (
        "a working season stays working",
        {"workflow": _workflow("processing", job_id="encode-old"), "encode_job": _encode("running")},
        {"key": "making_season", "tone": "active", "source_job": {"id": "encode-old", "kind": "encode",
                                                                   "prefix": SEASON, "status": "running"}},
    ),
    (
        "a stopped season, ahead of an older approved sample",
        {
            "workflow": _workflow("attention", "needs_attention", job_id="encode-old"),
            "encode_job": _encode("needs_attention", "Encode queue job was interrupted by a web process restart."),
            "calibration": _approved(),
            "approval": _approval(_approved()),
        },
        {"key": "needs_help", "recovery_kind": "season", "primary_action": "retry_encode", "failure": "restarted"},
    ),
    (
        "a stopped exact episode",
        {
            "prefix": EPISODE,
            "workflow": _workflow("attention", "needs_attention", job_id="encode-old"),
            "encode_job": _encode("needs_attention", "Final output size missed the approved target band.", prefix=EPISODE),
        },
        {"key": "needs_help", "recovery_kind": "season", "failure": "size_unverified"},
    ),
    (
        "a newer sample awaiting review outranks stale encode attention",
        {
            "workflow": _workflow("attention", "needs_attention", job_id="encode-old"),
            "encode_job": _encode("needs_attention", "Final output size missed the approved target band."),
            "sample_job": _sample("completed"),
            "calibration": _review_ready(),
        },
        {"key": "ready_to_compare", "primary_action": "review_sample"},
    ),
    (
        "a sample finished before the stop does not hide it",
        {
            "workflow": _workflow("attention", "needs_attention", job_id="encode-old"),
            "encode_job": _encode("needs_attention", "Final output size missed the approved target band."),
            "sample_job": _sample("completed", finished_at=BEFORE_STOP, created_at=BEFORE_STOP),
            "calibration": _review_ready(),
        },
        {"key": "needs_help", "recovery_kind": "season"},
    ),
    (
        "review evidence over the generic folder state",
        {"calibration": _review_ready()},
        {"key": "ready_to_compare", "tone": "ready"},
    ),
    (
        "a fresh sample for review while a finished file is blocked",
        {"workflow": _workflow("blocked"), "calibration": _review_ready()},
        {"key": "ready_to_compare"},
    ),
    (
        "a blocked file with the approved sample already in place",
        {"workflow": _workflow("blocked"), "calibration": _approved(), "approval": _approval(_approved())},
        {"key": "needs_help", "recovery_kind": "files", "primary_action": "review_files"},
    ),
    (
        "file check blockers ahead of sample setup, without an encode retry",
        {"workflow": _workflow("blocked")},
        {"key": "needs_help", "recovery_kind": "files", "primary_action": "review_files"},
    ),
    (
        "a saved sample that never produced comparison clips",
        {"calibration": {"mode": "sample", "job_id": "sample-1", "review_media_ready": False}},
        {"key": "needs_help", "recovery_kind": "test", "failure": "missing_clips", "primary_action": "make_sample"},
    ),
    (
        "a measured sample whose review clips are missing",
        {"calibration": {"mode": "sample", "job_id": "sample-1", "draft_hash": "draft-1", "review_media_ready": False}},
        {"key": "needs_help", "recovery_kind": "test", "failure": "missing_clips"},
    ),
    (
        "an approved sample returns to review when its review is rejected",
        {"calibration": _approved(), "approval": _approval(_approved()), "approval_review_blocked": True},
        {"key": "ready_to_compare"},
    ),
    (
        "an approval stands when only non-policy draft details changed",
        {"calibration": _approved(draft_hash="draft-refreshed"), "approval": _approval(_approved())},
        {"key": "ready_to_make", "primary_action": "queue_encode"},
    ),
    (
        "an approval stands when only downloadable legacy review media remains",
        {
            "calibration": _approved(browser_review_ready=False, review_pairs=[],
                                     compare_clips=[{"path": "/review/compare.mov"}]),
            "approval": _approval(_approved()),
        },
        {"key": "ready_to_make"},
    ),
    (
        "a legacy approval whose review media is gone needs a fresh sample",
        {"calibration": {"mode": "sample", "job_id": "sample-1", "draft_hash": "draft-1",
                         "accepted_draft_hash": "draft-1", "review_media_ready": False}},
        {"key": "needs_help", "recovery_kind": "test"},
    ),
    (
        "a stopped sample that can be retried as it was",
        {"sample_job": _sample("failed", error="Sample worker stopped unexpectedly")},
        {"key": "needs_help", "recovery_kind": "test", "primary_action": "retry_sample", "failure": "stopped"},
    ),
    ("ready to check", {"workflow": _workflow("validate")}, {"key": "ready_to_check", "primary_action": "validate_outputs"}),
    (
        "a check waiting for shared storage offers no action",
        {"workflow": _workflow("validate", enabled=False)},
        {"key": "ready_to_check", "primary_action": None},
    ),
    (
        "finishing with files still to account for",
        {"workflow": _workflow("promote"), "staged_integrity": {"blocker_count": 2, "database_truncated": False}},
        {"key": "finish_blocked", "tone": "attention"},
    ),
    (
        "finishing on a card, which reads no staged-file summary",
        {"workflow": _workflow("promote")},
        {"key": "ready_to_finish", "primary_action": "promote_outputs"},
    ),
    ("finished", {"workflow": _workflow("complete")}, {"key": "finished", "tone": "success"}),
    (
        "a held season with its approved sample",
        {"workflow": _workflow("none", "held"), "calibration": _approved(), "approval": _approval(_approved())},
        {"key": "ready_to_make"},
    ),
]


@pytest.mark.parametrize(("facts", "expected"), [(facts, expected) for _name, facts, expected in PORTED_CASES],
                         ids=[name for name, _facts, _expected in PORTED_CASES])
def test_ported_season_and_card_states(facts: dict[str, Any], expected: dict[str, Any]) -> None:
    state = _decide(**facts)

    assert {key: state[key] for key in expected} == expected


def test_attention_is_authoritative_for_sample_judgments_and_blocked_files_during_processing() -> None:
    ready = _decide(calibration=_review_ready())
    retrying = _decide(sample_job=_sample("retry_backoff", finished_at=None))
    compressing_with_blocked_file = _decide(workflow=_workflow("processing") | {"needs_owner": True})

    assert ready["needs_you"] is True
    assert retrying["needs_you"] is False
    assert _decide(workflow=_workflow("blocked") | {"needs_owner": True},
                   sample_job=_sample("retry_backoff", finished_at=None))["needs_you"] is False
    assert compressing_with_blocked_file["key"] == "making_season"
    assert compressing_with_blocked_file["needs_you"] is True
    assert _decide(workflow=_workflow("attention"), encode_job=_encode("failed"))["needs_you"] is True
    lifecycle = {"motion_held_candidate_count": 1, "motion_as_is_candidate_count": 1}
    assert _decide(workflow=_workflow("processing"), lifecycle=lifecycle)["needs_you"] is False
    assert _decide(approval=_approval(_approved()), lifecycle=lifecycle)["needs_you"] is True
    assert _decide(approval=_approval(_approved()),
                   lifecycle=lifecycle | {"eligible_candidate_count": 1})["needs_you"] is False
    assert _decide(approval=_approval(_approved()), lifecycle=lifecycle | {"motion_as_is_candidate_count": 0})["needs_you"] is False


@pytest.mark.parametrize(("lane", "key"), [
    ("attention", "needs_help"),
    ("blocked", "needs_help"),
    ("encode", "ready_to_make"),
    ("validate", "ready_to_check"),
    ("promote", "ready_to_finish"),
    ("processing", "making_season"),
    ("complete", "finished"),
])
def test_the_workflow_leads_over_an_older_approved_sample(lane: str, key: str) -> None:
    state = _decide(workflow=_workflow(lane), calibration=_approved(), approval=_approval(_approved()))

    assert state["key"] == key


@pytest.mark.parametrize(("lane", "state"), [("validate", "ready_to_validate"), ("promote", "ready_to_promote"),
                                             ("complete", "complete")])
def test_a_sample_in_progress_leads_over_checking_finishing_and_finished(lane: str, state: str) -> None:
    """The newest request for the scope leads; approving it changes which settings its files publish under."""
    workflow = _workflow(lane, state)

    assert _decide(workflow=workflow, sample_job=_sample("running", finished_at=None))["key"] == "making_test"
    assert _decide(workflow=workflow, sample_job=_sample("queued", finished_at=None))["key"] == "sample_waiting"
    assert _decide(workflow=workflow, sample_job=_sample("completed"), calibration=_review_ready())["key"] == (
        "ready_to_compare"
    )


@pytest.mark.parametrize(("error", "failure"), [
    ("Final output size missed the approved target band: status=under_target, actual=1, target=2.", "size_under_target"),
    ("Final output size missed the approved target band: status=over_target, actual=3, target=2.", "size_over_target"),
    # A size miss leads over the fresh baseline that also failed, so the owner chooses a fresh goal.
    (
        "Final output size missed the approved target band: status=missing_target, target=None. "
        "The fresh baseline search also failed: Failed to find a suitable crf for the quality target.",
        "size_unverified",
    ),
    ("Failed to find a suitable crf for the quality target.", "goal_too_small"),
    ("Permission denied: /media/tv/show/episode-1.mkv", "no_access"),
    ("Stopped by the owner.", "stopped"),
    ("ssh: connection reset", "other"),
    (None, "unexplained"),
    ("conflicts with the configured quality floor (target_band_violates_quality_floor)", "quality_conflict"),
])
def test_a_stopped_run_records_what_stopped_it(error: str | None, failure: str) -> None:
    """The page words the stop from this kind alone, so a stale sample's error never replaces it."""
    state = _decide(
        workflow=_workflow("attention", "needs_attention", job_id="encode-old"),
        encode_job=_encode("needs_attention", error),
        sample_job=_sample("failed", finished_at=BEFORE_STOP, error="Failed to find a suitable crf."),
    )

    assert (state["recovery_kind"], state["failure"]) == ("season", failure)


@pytest.mark.parametrize(("trace", "failure"), [
    ({"status": "quality_conflict"}, "quality_conflict"),
    ({"status": "bound_exhausted"}, "bound_exhausted"),
    ({"status": "infeasible"}, "arithmetic_infeasible"),
    ({"status": "infeasible", "selection_reason": "target_lower_bound_exceeds_source_relative_cap"}, "bound_exhausted"),
])
def test_a_sample_stopped_by_its_size_goal_offers_another_size(trace: dict[str, Any], failure: str) -> None:
    state = _decide(sample_job=_sample("failed", result={"target_size_trace": trace}))

    assert state["failure"] == failure
    assert state["primary_action"] == "choose_size"


# --- Confirmed cases, from database rows -----------------------------------------------------


@pytest.fixture
def config(tmp_path: Path) -> Iterator[MediaforceConfig]:
    yield MediaforceConfig(
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
    reset_engine_cache()


def _cards(workflows: dict[str, dict[str, Any]]) -> list[tuple[str, dict[str, Any], None]]:
    return [(prefix, workflow, None) for prefix, workflow in workflows.items()]


def _sources(calibrations: Mapping[str, dict[str, Any]]) -> PageStateSources:
    return PageStateSources(
        load_calibration_state=lambda _config, prefix: calibrations.get(prefix),
        load_advice_state=lambda _config, _prefix: None,
    )


def _insert_item(connection: DBClient, rel_path: str, *, status: str = "discovered", staged: bool = False) -> int:
    item_id = int(connection.execute(library_items.insert().values(
        source_path=f"/media/{rel_path}", rel_path=rel_path, media_root="tv",
        parent_dir=str(Path(rel_path).parent), file_name=Path(rel_path).name, container="mkv",
        size_bytes=1, mtime_ns=1, fingerprint=rel_path, audio_summary_json="[]",
        subtitle_summary_json="[]", status=status, priority_score=0, recommendation="encode",
        recommendation_reason="fixture", last_scan_id="scan",
        discovered_at=BEFORE_STOP, last_seen_at=BEFORE_STOP, updated_at=BEFORE_STOP,
    )).inserted_primary_key[0])
    if staged:
        connection.execute(staged_artifacts.insert().values(
            library_item_id=item_id, staging_path=f"/staging/{rel_path}", updated_at=BEFORE_STOP,
        ))
    return item_id


def _insert_encode_job(connection: DBClient, job_id: str, *, prefix: str, status: str, kind: str = "single",
                       parent: str | None = None, manifest: Path | None = None, index: int | None = None,
                       error: str | None = None, finished_at: str = STOP) -> None:
    connection.execute(encode_jobs.insert().values(
        job_id=job_id, prefix=prefix, job_kind=kind, parent_job_id=parent, status=status,
        manifest_path=str(manifest or "/runs/none.json"),
        manifest_indexes_json=json.dumps([index]) if index is not None else None,
        host_json="{}", error=error, created_at=BEFORE_STOP, finished_at=finished_at, updated_at=finished_at,
    ))


def _insert_sample_job(connection: DBClient, job_id: str, *, prefix: str, status: str,
                       created_at: str = AFTER_STOP, finished_at: str | None = AFTER_STOP) -> None:
    save_job(connection, {
        "job_id": job_id, "prefix": prefix, "status": status, "lane": "sample", "action": "baseline",
        "created_at": created_at, "updated_at": finished_at or created_at, "finished_at": finished_at,
    })


def _page_state(connection: DBClient, config: MediaforceConfig, prefix: str,
                calibrations: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    workflow = build_folder_workflow_state(connection, prefix, library_types=config.library_type_map).to_payload()
    return folder_page_state(connection, config, prefix, workflow, _sources(calibrations))


def test_case_1_an_episodes_fresh_sample_outranks_its_older_stopped_encode(config: MediaforceConfig) -> None:
    """The sample finished long ago, so the page's hour-long sample notice is gone; its DB row still orders it."""
    with open_db(config.paths.db_path) as connection:
        _insert_item(connection, EPISODE)
        _insert_encode_job(connection, "encode-old", prefix=EPISODE, status="needs_attention",
                           error="Final output size missed the approved target band.")
        _insert_sample_job(connection, "sample-new", prefix=EPISODE, status="completed")
        connection.commit()
        notice = web_app._load_job_state(connection, config, EPISODE)
        state = _page_state(connection, config, EPISODE, {EPISODE: _review_ready()})

    # On main the page read the sample's order from this notice, which is gone an hour after it finished.
    assert notice is None
    assert state["key"] == "ready_to_compare"
    assert state["source_job"]["id"] == "sample-new"


def test_case_2_cards_say_ready_to_compress_only_under_a_current_approval(config: MediaforceConfig) -> None:
    seasons = [f"{SHOW}/Season {number}" for number in range(1, 9)]
    with open_db(config.paths.db_path) as connection:
        for season in seasons:
            _insert_item(connection, f"{season}/episode.mkv")
        # Seven seasons wait for a sample: more than the six a queue summary lists per lane.
        for number, season in enumerate(seasons[1:], start=2):
            _insert_sample_job(connection, f"sample-{number}", prefix=season, status="queued", finished_at=None)
        connection.commit()
        workflows = {
            season: build_folder_workflow_state(connection, season, library_types=config.library_type_map).to_payload()
            for season in seasons
        }
        listed = list_queue_summary(connection)["sample"]["queued"]
        without_approval = card_page_states(connection, config, _cards(workflows), _sources({}))
        under_show_approval = card_page_states(connection, config, _cards(workflows), _sources({SHOW: _approved()}))

    assert {workflow["primary_lane"] for workflow in workflows.values()} == {"encode"}
    assert len(listed) == 6
    # Candidates alone are not an approved setup.
    assert without_approval[seasons[0]]["key"] == "needs_test"
    assert {without_approval[season]["key"] for season in seasons[1:]} == {"sample_waiting"}
    # The show's approval governs a season without its own sample; its card names where it lives.
    assert under_show_approval[seasons[0]]["key"] == "ready_to_make"
    assert under_show_approval[seasons[0]]["source_job"]["prefix"] == SHOW


def test_case_3_a_show_run_stopped_on_another_season_leaves_this_seasons_staged_files(
        config: MediaforceConfig,
) -> None:
    season_2 = f"{SHOW}/Season 2"
    manifest = config.paths.run_manifest_dir / "show-run.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"items": [{"rel_path": f"{SEASON}/episode.mkv"},
                                              {"rel_path": f"{season_2}/episode.mkv"}]}))
    with open_db(config.paths.db_path) as connection:
        _insert_item(connection, f"{SEASON}/episode.mkv", status="encoded", staged=True)
        _insert_item(connection, f"{season_2}/episode.mkv")
        _insert_encode_job(connection, "show-run", prefix=SHOW, status="needs_attention", kind="folder",
                           manifest=manifest, error="Permission denied reading one file.")
        _insert_encode_job(connection, "part-1", prefix=SHOW, status="completed", kind="shard", parent="show-run",
                           manifest=manifest, index=0)
        _insert_encode_job(connection, "part-2", prefix=SHOW, status="needs_attention", kind="shard",
                           parent="show-run", manifest=manifest, index=1)
        connection.commit()
        latest_run = load_latest_encode_job(connection, SEASON)
        staged = _page_state(connection, config, SEASON, {})
        stopped = _page_state(connection, config, season_2, {})

    # On main the season page treated this run as its own because the season's files are not all held.
    assert latest_run is not None and latest_run["status"] == "needs_attention"
    assert staged["key"] == "ready_to_check"
    assert stopped["key"] == "needs_help"
    assert stopped["failure"] == "no_access"
    assert stopped["source_job"] == {"id": "show-run", "kind": "encode", "prefix": SHOW, "status": "needs_attention"}


def test_case_4_a_seasons_sample_state_ignores_an_episodes_sample(config: MediaforceConfig) -> None:
    with open_db(config.paths.db_path) as connection:
        _insert_item(connection, EPISODE)
        _insert_sample_job(connection, "episode-sample", prefix=EPISODE, status="running", finished_at=None)
        connection.commit()
    status = dashboard_payloads.folder_status_payload(
        config,
        SEASON,
        load_exact_job_state=web_app._load_job_state,
        load_overlapping_job_state=web_app._load_overlapping_job_state,
        load_retryable_sample_job_state=web_app._load_retryable_sample_job_state,
        load_scan_status=lambda *_args, **_kwargs: None,
        load_active_encode_job_for_prefix=lambda *_args: None,
        page_state=lambda connection, prefix, workflow, integrity, lifecycle: folder_page_state(
            connection, config, prefix, workflow, _sources({SEASON: _approved()}), staged_integrity=integrity,
            lifecycle=lifecycle,
        ),
    )

    # On main the season page read this field, the episode's sample, as the season's own.
    assert status["calibration_job"]["prefix"] == EPISODE
    assert status["page_state"]["key"] == "ready_to_make"


def test_case_5_a_fresh_sample_settles_a_size_conflict_however_late_it_is_approved(
        config: MediaforceConfig,
) -> None:
    conflict = ("The approved target size conflicts with the configured quality floor "
                "(target_band_violates_quality_floor); target=300000000 bytes, best_reachable=420000000 bytes.")
    late_approval = (datetime.fromisoformat(AFTER_STOP) + timedelta(hours=3)).astimezone(UTC).isoformat()
    with open_db(config.paths.db_path) as connection:
        _insert_item(connection, f"{SEASON}/episode.mkv")
        _insert_encode_job(connection, "encode-old", prefix=SEASON, status="needs_attention", error=conflict)
        connection.commit()
        before = _page_state(connection, config, SEASON, {SEASON: _approved("sample-old")})
        _insert_sample_job(connection, "sample-new", prefix=SEASON, status="completed")
        connection.commit()
        approved = _approved("sample-new", accepted_at=late_approval)
        after = _page_state(connection, config, SEASON, {SEASON: approved})

    assert before["failure"] == "quality_conflict"
    assert before["primary_action"] == "choose_size"
    assert after["key"] == "ready_to_make"
    assert after["failure"] is None


def test_case_6_a_running_sample_leads_over_files_ready_to_check(config: MediaforceConfig) -> None:
    with open_db(config.paths.db_path) as connection:
        _insert_item(connection, f"{SEASON}/episode.mkv", status="encoded", staged=True)
        connection.commit()
        checking = _page_state(connection, config, SEASON, {})
        _insert_sample_job(connection, "sample-new", prefix=SEASON, status="running", finished_at=None)
        connection.commit()
        sampling = _page_state(connection, config, SEASON, {})

    assert checking["key"] == "ready_to_check"
    assert sampling["key"] == "making_test"


def test_status_payload_and_card_agree(config: MediaforceConfig) -> None:
    with open_db(config.paths.db_path) as connection:
        _insert_item(connection, f"{SEASON}/episode.mkv")
        _insert_sample_job(connection, "sample-new", prefix=SEASON, status="completed")
        connection.commit()
        workflow = build_folder_workflow_state(connection, SEASON, library_types=config.library_type_map).to_payload()
        card = card_page_states(connection, config, [(SEASON, workflow, None)], _sources({SEASON: _review_ready()}))
        page = folder_page_state(connection, config, SEASON, workflow, _sources({SEASON: _review_ready()}))

    assert card[SEASON] == page
    assert page["key"] == "ready_to_compare"


def test_library_cards_carry_the_page_state(config: MediaforceConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_app, "_load_calibration_state", lambda _config, prefix: {SHOW: _approved()}.get(prefix))
    with open_db(config.paths.db_path) as connection:
        _insert_item(connection, f"{SEASON}/episode.mkv")
        connection.commit()
        cards = web_app._list_library_detail_cards(config, connection)

    assert [(card.prefix, (card.page_state or {}).get("key")) for card in cards] == [(SEASON, "ready_to_make")]


def _show_run(connection: DBClient, config: MediaforceConfig, files: list[str], failing_index: int,
              error: str = "Final output size missed the approved target band: status=over_target, actual=2, "
                           "target=1, lower=1, upper=1.") -> None:
    """A show-wide run that stopped on one file and finished or never reached the others."""
    manifest = config.paths.run_manifest_dir / "show-run.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"items": [{"rel_path": rel_path} for rel_path in files]}))
    _insert_encode_job(connection, "show-run", prefix=SHOW, status="needs_attention", kind="folder",
                       manifest=manifest, error=error)
    _insert_encode_job(connection, "part-failed", prefix=SHOW, status="needs_attention", kind="shard",
                       parent="show-run", manifest=manifest, index=failing_index, error=error)


def _promote(connection: DBClient, rel_path: str) -> None:
    item_id = connection.execute(library_items.select().where(library_items.c.rel_path == rel_path)).mappings().one()["id"]
    connection.execute(library_items.update().where(library_items.c.id == item_id).values(status="promoted"))
    connection.execute(staged_artifacts.update().where(staged_artifacts.c.library_item_id == item_id).values(
        promoted_at=AFTER_STOP))


def _states(connection: DBClient, config: MediaforceConfig, prefixes: list[str],
            calibrations: Mapping[str, dict[str, Any]]) -> dict[str, tuple[str, str]]:
    """Each folder's workflow lane and page state key, on its page and on its card."""
    states = {}
    for prefix in prefixes:
        workflow = build_folder_workflow_state(connection, prefix, library_types=config.library_type_map).to_payload()
        page = folder_page_state(connection, config, prefix, workflow, _sources(calibrations))
        card = card_page_states(connection, config, [(prefix, workflow, None)], _sources(calibrations))[prefix]
        assert card == page
        states[prefix] = (workflow["primary_lane"], page["key"])
    return states


def test_a_stopped_show_runs_kept_and_published_file_leaves_nothing_stopped(config: MediaforceConfig) -> None:
    kept = f"{SEASON}/episode-1.mkv"
    season_2 = f"{SHOW}/Season 2"
    with open_db(config.paths.db_path) as connection:
        _insert_item(connection, kept, status="encoded", staged=True)
        _insert_item(connection, f"{SEASON}/episode-2.mkv")
        _insert_item(connection, f"{season_2}/episode-1.mkv")
        _show_run(connection, config, [kept, f"{SEASON}/episode-2.mkv", f"{season_2}/episode-1.mkv"], 0)
        # The owner kept the file the run stopped on, and it was published.
        _promote(connection, kept)
        connection.commit()
        states = _states(connection, config, [SHOW, SEASON, season_2, kept], {SHOW: _approved()})

    assert states == {
        SHOW: ("encode", "ready_to_make"),
        SEASON: ("encode", "ready_to_make"),
        season_2: ("encode", "ready_to_make"),
        kept: ("complete", "finished"),
    }


def test_a_stopped_show_run_leaves_the_seasons_it_never_reached_ready(config: MediaforceConfig) -> None:
    stopped = f"{SEASON}/episode-1.mkv"
    season_2 = f"{SHOW}/Season 2"
    with open_db(config.paths.db_path) as connection:
        _insert_item(connection, stopped)
        _insert_item(connection, f"{season_2}/episode-1.mkv")
        _insert_item(connection, f"{SHOW}/Season 3/episode-1.mkv")
        _show_run(connection, config, [stopped, f"{season_2}/episode-1.mkv"], 0)
        connection.commit()
        approved = _states(connection, config, [season_2, f"{SHOW}/Season 3"], {SHOW: _approved()})
        unapproved = _states(connection, config, [season_2], {})
        stopped_here = _states(connection, config, [SEASON, stopped], {SHOW: _approved()})
        # What the season page read on main: the stopped show run, as if it were the season's own.
        latest_run = load_latest_encode_job(connection, season_2)

    assert approved == {season_2: ("encode", "ready_to_make"), f"{SHOW}/Season 3": ("encode", "ready_to_make")}
    assert unapproved == {season_2: ("encode", "needs_test")}
    # The file the run stopped on is still unresolved, so its season and the file say so.
    assert stopped_here == {SEASON: ("attention", "needs_help"), stopped: ("attention", "needs_help")}
    assert latest_run is not None and latest_run["job_id"] == "show-run"


def test_running_folder_keeps_owner_child_problems_in_its_attention_fact(config: MediaforceConfig) -> None:
    with open_db(config.paths.db_path) as connection:
        _insert_item(connection, EPISODE)
        _insert_encode_job(connection, "running-parent", prefix=SEASON, kind="folder", status="running")
        connection.execute(encode_jobs.update().where(encode_jobs.c.job_id == "running-parent").values(
            progress_json=json.dumps({"unfinished_breakdown": [
                {"reason": "quality_floor_size_conflict", "label": "waiting for your OK to use more space",
                 "count": 1, "needs_owner": True},
            ]}),
        ))
        workflow = build_folder_workflow_state(connection, SEASON, library_types=config.library_type_map).to_payload()
        assert workflow["counts"]["blocked"] == 0
        assert workflow["needs_owner"] is True
        state = _page_state(connection, config, SEASON, {})
        assert state["key"] == "making_season"
        assert state["needs_you"] is True


@pytest.mark.parametrize(("status", "key"), [("running", "making_test"), ("retry_backoff", "sample_waiting")])
def test_active_sample_replaces_blocked_file_words_and_attention(status: str, key: str) -> None:
    state = _decide(workflow=_workflow("blocked") | {"needs_owner": True},
                    sample_job=_sample(status, finished_at=None))
    assert state["key"] == key
    assert state["needs_you"] is False


def test_approved_new_sample_settles_old_run_attention() -> None:
    state = _decide(workflow=_workflow("attention") | {"needs_owner": True}, encode_job=_encode("failed"),
                    sample_job=_sample("completed"), calibration=_approved(), approval=_approval(_approved()))
    assert state["key"] == "ready_to_make"
    assert state["needs_you"] is False


@pytest.mark.parametrize("child_status", ["needs_attention", "queued"])
def test_running_show_only_flags_seasons_with_their_own_decisions(config: MediaforceConfig, child_status: str) -> None:
    from mediaforce.web.runtime import encode_runtime
    season_2 = f"{SHOW}/Season 2"
    problem_files = [f"{SEASON}/episode-{index}.mkv" for index in range(7)] + [f"{season_2}/problem.mkv"]
    quiet_season = f"{SHOW}/Season 3"
    files = problem_files + [f"{quiet_season}/working.mkv"]
    manifest = config.paths.run_manifest_dir / "active-show.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"items": [{"rel_path": path} for path in files]}))
    with open_db(config.paths.db_path) as connection:
        for path in files:
            _insert_item(connection, path)
        _insert_encode_job(connection, "parent", prefix=SHOW, kind="folder", status="running", manifest=manifest)
        children = []
        for index, path in enumerate(files):
            status = child_status if path in problem_files else "running"
            job_id = f"part-{index}"
            _insert_encode_job(connection, job_id, prefix=SHOW, kind="shard", status=status,
                               parent="parent", manifest=manifest, index=index)
            children.append({"job_id": job_id, "status": status, "manifest_indexes": [index],
                             "waiting_reason": encode_runtime.HOST_WINDOW_IMPOSSIBLE_MARKER,
                             "progress": {"current_item_rel_path": path}})
        groups = encode_runtime._unfinished_child_breakdown(children)
        assert len(groups[0]["child_job_ids"]) == len(problem_files)
        assert len(groups[0]["items"]) < len(problem_files)
        connection.execute(encode_jobs.update().where(encode_jobs.c.job_id == "parent").values(
            progress_json=json.dumps({"unfinished_breakdown": groups})))
        for prefix, needs_you in [(SEASON, True), (season_2, True), (quiet_season, False)]:
            state = _page_state(connection, config, prefix, {})
            assert state["key"] == "making_season"
            assert state["needs_you"] is needs_you

        _insert_encode_job(connection, "new-success", prefix=SHOW, kind="shard", status="completed",
                           parent="parent", manifest=manifest, index=len(problem_files) - 1)
        assert _page_state(connection, config, season_2, {})["needs_you"] is False

        manifest.unlink()
        assert _page_state(connection, config, quiet_season, {})["needs_you"] is True
