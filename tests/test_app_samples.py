import asyncio
import json
from collections.abc import Collection
from typing import Any, cast

import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request
from starlette.routing import Route

from mediaforce.core.config import MediaforceConfig
from mediaforce.web.routes.app_window import register_app_window_routes
from mediaforce.web.runtime import folder_actions
from mediaforce.web.runtime.app_samples import SAMPLE_REMAKE_LIMIT, approve_sample, make_sample, sample_facts, \
    samples_to_remake, start_compressing

PREFIX = "tv/Lucifer/Season 2"


def _option(key: str, megabytes: float, *, mode: str = "normalized") -> dict[str, Any]:
    return {
        "key": key,
        "requires_explicit_selection": False,
        "operator_intent": {
            "schema_version": 2,
            "size_goal": {"mode": mode, "value_mb": 300.0, "reference_runtime_minutes": 45.0},
            "resolution": {"mode": "source", "max_height": None},
            "compression_intent": {"schema_version": 1, "level": "balanced", "confirmed": True},
        },
        "resolved_size_goal": {"target_size_mb": megabytes, "target_size_bytes": int(megabytes * 1_000_000)},
    }


def _clip(path: str, at: float, size: int) -> dict[str, Any]:
    return {"path": path, "timestamp_seconds": at, "duration_seconds": 10.0, "size_bytes": size, "audio": None}


def _folder(**fields: Any) -> dict[str, Any]:
    return {
        "prefix": PREFIX,
        "summary": {"item_count": 18, "total_size_bytes": 69_000_000_000},
        "lifecycle": {"eligible_candidate_count": 16, "eligible_size_bytes": 60_000_000_000},
        "sample_item": {"source_size_bytes": 3_853_264_012},
        "calibration": {
            "draft_hash": "draft-1",
            "browser_review_ready": True,
            "sample_result": {"predicted_total_size_bytes": 296_440_462},
            "review_pairs": [
                {"source_clip": _clip("/review-media/a/source.mp4", 600.0, 9_000_000),
                 "preview_clip": _clip("/review-media/a/preview.mp4", 600.0, 700_000),
                 "compare_clip": _clip("/review-media/a/compare.mkv", 600.0, 1)},
                {"source_clip": _clip("/review-media/b/source.mp4", 1200.0, 9_000_000), "preview_clip": {}},
            ],
        },
        "size_target_analysis": {"status": "over_target", "budget_bytes": 293_736_333,
                                 "predicted_total_size_bytes": 400_000_000, "predicted_to_budget_ratio": 1.36},
        "resolved_operator_intent": {
            "size_goal": {"target_size_bytes": 293_736_333},
            "request": {"schema_version": 2, "size_goal": {"mode": "normalized", "value_mb": 300.0}},
        },
        "size_goal_options": [_option("recommended", 293.736), _option("smaller", 220.3), _option("legacy", 100, mode="ambiguous")],
        "compression_intent_options": [
            {"key": "reference", "selected": False, "compression_intent": {"schema_version": 1, "level": "reference", "confirmed": True}},
            {"key": "balanced", "selected": True, "compression_intent": {"schema_version": 1, "level": "balanced", "confirmed": False}},
        ],
        **fields,
    }


def _status(key: str = "ready_to_compare", action: str | None = "review_sample", failure: str | None = None) -> dict[str, Any]:
    return {"page_state": {"key": key, "primary_action": action, "failure": failure}}


def _route(app: FastAPI, path: str, method: str) -> Any:
    return next(
        route.endpoint for route in app.routes
        if isinstance(route, Route) and route.path == path and method in (route.methods or set())
    )


def _post(endpoint: Any, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload).encode()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request({"type": "http", "method": "POST", "path": "/", "headers": [(b"content-type", b"application/json")]}, receive)
    response = asyncio.run(endpoint(PREFIX, request))
    return response.status_code, json.loads(response.body)


def test_facts_offer_only_playable_moments_and_the_sizes_that_can_be_chosen() -> None:
    facts = sample_facts(_folder(), _status())

    assert (facts["show"], facts["season"], facts["state"], facts["action"]) == ("Lucifer", 2, "ready_to_compare", "review_sample")
    assert facts["moments"] == [{
        "at_seconds": 600.0,
        "original": {"url": "/review-media/a/source.mp4", "size_bytes": 9_000_000},
        "sample": {"url": "/review-media/a/preview.mp4", "size_bytes": 700_000},
    }]
    assert (facts["episode_bytes"], facts["estimated_episode_bytes"]) == (3_853_264_012, 296_440_462)
    assert (facts["season_bytes"], facts["season_episode_count"], facts["goal_bytes"]) == (60_000_000_000, 16, 293_736_333)
    assert [choice["key"] for choice in facts["size_choices"]] == ["recommended", "smaller"]

    not_playable = _folder(calibration={**_folder()["calibration"], "browser_review_ready": False})
    assert sample_facts(not_playable, _status())["moments"] == []


def test_the_real_approval_check_refuses_a_changed_draft_and_asks_again_for_an_important_change(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(folder_actions, "production_action_blocker", lambda *_args: None)
    calibration = {
        "draft_hash": "draft-1",
        "policy": {"video": {"target_vmaf": 90.0}},
        "sample_item": {"resolved_policy": {"video": {"target_vmaf": 95.0}}},
    }

    def save_profile(prefix: str, high_impact: bool, size_tradeoff: bool, draft_hash: str, _token: str) -> dict[str, Any]:
        def unused(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("approval went past the confirmation it needed")

        return folder_actions.save_profile_action(
            cast(MediaforceConfig, None), prefix, now_iso=unused, load_sample_item=unused, load_calibration_state=lambda *_args: dict(calibration),
            calibration_draft_hash=unused, save_calibration_state=unused, load_advice_state=unused,
            record_visual_approval_artifact=unused, merge_advice_state=unused, upsert_override=unused,
            confirm_high_impact=high_impact, confirm_size_tradeoff=size_tradeoff, reviewed_draft_hash=draft_hash,
            require_reviewed_draft=True,
        )

    def approve(confirm: Collection[str], draft_hash: str) -> dict[str, Any]:
        return approve_sample(PREFIX, confirm, draft_hash, save_profile=save_profile, queue_encode=lambda *_args: {})

    assert approve(set(), "draft-1") == {"ok": False, "code": "confirm_high_impact"}
    assert approve({"high_impact"}, "a draft replaced since") == {"ok": False, "code": "sample_changed"}
    assert approve({"high_impact"}, "") == {"ok": False, "code": "sample_changed"}


def test_approving_or_starting_compressing_queues_the_season_and_says_when_it_did_not() -> None:
    saved: list[tuple[Any, ...]] = []
    queued: list[tuple[Any, ...]] = []

    def endpoint(save_result: Any, queue_result: Any, action: str = "approve") -> Any:
        def save(*args: Any) -> dict[str, Any]:
            saved.append(args)
            if isinstance(save_result, Exception):
                raise save_result
            return save_result

        def queue(*args: Any) -> dict[str, Any]:
            queued.append(args)
            if isinstance(queue_result, Exception):
                raise queue_result
            return queue_result

        app = FastAPI()
        register_app_window_routes(
            app, main_window_payload=dict, tv_shows_payload=dict, movies_payload=dict, other_media_payload=dict,
            sample_payload=lambda _prefix: None,
            make_sample_action=lambda *_args: {},
            approve_sample_action=lambda prefix, confirm, draft_hash: approve_sample(
                prefix, confirm, draft_hash, save_profile=save, queue_encode=queue,
            ),
            compress_season_action=lambda prefix: start_compressing(prefix, queue_encode=queue),
        )
        return _route(app, f"/api/app/samples/{{prefix:path}}/{action}", "POST")

    ok = {"ok": True}
    watched = {"reviewed_draft_hash": "draft-1"}
    assert _post(endpoint(ok, ok), {"confirm": ["size_tradeoff", 7], **watched}) == (
        200, {"ok": True, "compressing": True, "reason": None},
    )
    assert saved[-1] == (PREFIX, False, True, "draft-1", "")
    assert _post(endpoint(folder_actions.ReviewedDraftChanged(), ok), watched) == (
        409, {"ok": False, "code": "sample_changed"},
    )
    assert _post(endpoint(ok, {"ok": False, "code": "encode_already_active"}), watched)[1]["compressing"] is True
    assert _post(endpoint(ok, HTTPException(409, "No computer.")), watched) == (
        200, {"ok": True, "compressing": False, "reason": None},
    )
    size_guard = folder_actions.ApprovalNeedsConfirmation("size_tradeoff", "Missed the goal.")
    assert _post(endpoint(size_guard, ok), watched) == (409, {"ok": False, "code": "confirm_size_tradeoff"})
    assert _post(endpoint(HTTPException(400, "No sample."), ok), watched) == (409, {"ok": False, "code": "refused"})
    assert _post(endpoint({"ok": False}, ok), watched) == (409, {"ok": False, "code": "refused"})

    queued.clear()
    compress = asyncio.run(endpoint(None, ok, "compress")(PREFIX))
    assert (compress.status_code, json.loads(compress.body)) == (200, {"ok": True, "compressing": True, "reason": None})
    assert queued == [(PREFIX, folder_actions.APPROVED_SAMPLE_NOTES, False, False, "")]
    for refusal, reason in [
        ({"ok": False, "code": "free_space_reserve"}, "free_space_reserve"),
        (folder_actions.QueueNeedsChoice("confirm_compression_goal", "Choose a goal."), "confirm_compression_goal"),
    ]:
        refused = asyncio.run(endpoint(None, refusal, "compress")(PREFIX))
        assert json.loads(refused.body) == {"ok": True, "compressing": False, "reason": reason}


def test_making_a_sample_retries_the_saved_request_or_asks_at_the_chosen_size() -> None:
    calls: list[tuple[Any, ...]] = []

    def make(folder: dict[str, Any], status: dict[str, Any], size: str | None) -> dict[str, Any]:
        return make_sample(
            folder, status, size,
            ai_tune=lambda *args: calls.append(("new", *args)) or {"ok": True},
            retry_saved=lambda *args: calls.append(("retry", *args)) or {"ok": True},
        )

    assert make(_folder(), _status("needs_help", "retry_sample"), None) == {"ok": True, "code": None}
    assert calls.pop() == ("retry", PREFIX, "", "")

    assert make(_folder(), _status("needs_help", "choose_size"), None) == {"ok": False, "code": "choose_size"}
    assert make(_folder(), _status(), "smaller")["ok"] is True
    kind, prefix, note, host, intent, _token = calls.pop()
    assert (kind, prefix, host) == ("new", PREFIX, "")
    assert "220 MB" in note and "300.0" not in note
    assert intent["compression_intent"] == {"schema_version": 1, "level": "balanced", "confirmed": True}

    assert make(_folder(), _status(), None)["ok"] is True
    _kind, _prefix, note, _host, intent, _token = calls.pop()
    assert "294 MB" in note and "1.4 times" in note
    assert intent == _folder()["resolved_operator_intent"]["request"]

    assert make(_folder(), _status("needs_help", "make_sample", "missing_clips"), None)["ok"] is True
    _kind, _prefix, note, _host, intent, _token = calls.pop()
    assert "294 MB" in note and intent["compression_intent"]["confirmed"] is True

    # A season that never chose its balance is made as close to its size goal as still looks right.
    unchosen = _folder(compression_intent_options=[
        {"key": "reference", "selected": False, "compression_intent": {"level": "reference"}},
        {"key": "balanced", "selected": False, "compression_intent": {"level": "balanced"}},
    ])
    assert make(unchosen, _status("needs_help", "make_sample", "missing_clips"), None)["ok"] is True
    assert calls.pop()[4]["compression_intent"]["level"] == "balanced"
    assert make(_folder(compression_intent_options=[]), _status(), "smaller") == {"ok": False, "code": "choose_balance"}

    # Made again at the season's own size, a sample keeps the season's resolution; a size the owner picks brings its own.
    limited = _folder(resolved_operator_intent={
        **_folder()["resolved_operator_intent"], "resolution": {"mode": "max_height", "max_height": 1080, "requires_confirmation": False},
    })
    assert make(limited, _status("needs_help", "make_sample", "missing_clips"), None)["ok"] is True
    _kind, _prefix, note, _host, intent, _token = calls.pop()
    assert intent["resolution"] == {"mode": "max_height", "max_height": 1080} and "1080p" in note
    assert make(limited, _status(), "smaller")["ok"] is True
    assert calls.pop()[4]["resolution"]["mode"] == "source"

    # A saved request that can no longer run is asked for anew rather than refused.
    refused_retry = make_sample(
        _folder(), _status("needs_help", "retry_sample"), None,
        ai_tune=lambda *args: calls.append(("new", *args)) or {"ok": True},
        retry_saved=lambda *args: calls.append(("retry", *args)) or {"ok": False},
    )
    assert refused_retry == {"ok": True, "code": None}
    assert [call[0] for call in calls] == ["retry", "new"]
    calls.clear()
    assert make(_folder(), _status(), "a size that is not offered") == {"ok": False, "code": "choose_size"}
    assert calls == []


def test_a_running_sample_says_when_it_should_be_ready_only_when_comparable_samples_tell() -> None:
    def running(estimate: dict[str, Any] | None, key: str = "making_test") -> dict[str, Any]:
        return {**_status(key, None), "exact_calibration_job": {"progress": {"estimate": estimate}}}

    assert sample_facts(_folder(), running({"remaining_seconds_low": 600, "remaining_seconds_high": 1500}))["ready_in_seconds"] == 1500
    assert sample_facts(_folder(), running(None))["ready_in_seconds"] is None
    # A sample that waits for a computer has no start yet to count from.
    assert sample_facts(_folder(), running({"remaining_seconds_high": 1500}, "sample_waiting"))["ready_in_seconds"] is None


def test_a_sample_that_needs_making_again_is_made_again_until_it_fails_too_often() -> None:
    def card(prefix: str, action: str, failure: str | None, key: str = "needs_help", recovery: str | None = "test") -> dict[str, Any]:
        return {"prefix": prefix, "page_state": {"key": key, "recovery_kind": recovery, "primary_action": action,
                                                 "failure": failure}}

    failures = {"tv/Failing/Season 1": SAMPLE_REMAKE_LIMIT, "tv/Failed once/Season 1": 1}
    cards = [
        card("tv/Clips gone/Season 1", "make_sample", "missing_clips"),
        card("tv/Failed once/Season 1", "retry_sample", "restarted"),
        card("tv/Failing/Season 1", "retry_sample", "unexplained"),
        card("tv/Stopped/Season 1", "make_sample", "stopped"),
        card("tv/Too small/Season 1", "choose_size", "bound_exhausted"),
        card("tv/Season encode/Season 1", "retry_encode", "unexplained", recovery="season"),
        card("tv/Watch it/Season 1", "review_sample", None, key="ready_to_compare", recovery=None),
    ]
    assert samples_to_remake(cards, lambda prefix: failures.get(prefix, 0)) == [
        "tv/Clips gone/Season 1", "tv/Failed once/Season 1",
    ]
