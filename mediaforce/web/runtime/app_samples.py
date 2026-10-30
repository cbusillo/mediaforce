from collections.abc import Callable, Collection, Iterable, Mapping
from typing import Any

from fastapi import HTTPException

from mediaforce.core.type_defs import float_value, int_value, object_dict, object_list
from mediaforce.tuning.size_goals import DECIMAL_MEGABYTE_BYTES, measured_followup_request, sample_goal_request, \
    with_compression_intent
from mediaforce.web.runtime.app_window import season_identity
from mediaforce.web.runtime.folder_actions import APPROVED_SAMPLE_NOTES, ApprovalNeedsConfirmation, QueueNeedsChoice, \
    ReviewedDraftChanged
from mediaforce.web.runtime.page_state import SIZE_GOAL_FAILURES

SaveProfileFn = Callable[[str, bool, bool, str, str], Mapping[str, Any]]
QueueEncodeFn = Callable[[str, str, bool, bool, str], Mapping[str, Any]]
AiTuneFn = Callable[[str, str, str, dict[str, Any] | None, str], Mapping[str, Any]]
RetrySampleFn = Callable[[str, str, str], Mapping[str, Any]]

REFUSED = {"ok": False, "code": "refused"}
# A season that never chose its balance of size and detail gets a sample as close to its size goal as still looks right.
DEFAULT_BALANCE = "balanced"
# How many samples in a row may fail before a season's sample waits on the owner instead of being made again.
SAMPLE_REMAKE_LIMIT = 3
# A sample the owner stopped, or whose size goal can't be reached, waits on the owner rather than being made again.
_NOT_REMADE_FAILURES = SIZE_GOAL_FAILURES | {"stopped"}


def sample_facts(folder: Mapping[str, Any], status: Mapping[str, Any]) -> dict[str, Any]:
    calibration = object_dict(folder.get("calibration"))
    page_state = object_dict(status.get("page_state"))
    lifecycle = object_dict(folder.get("lifecycle"))
    sample_item = object_dict(calibration.get("sample_item")) or object_dict(folder.get("sample_item"))
    return {
        **season_identity(str(folder["prefix"])),
        "state": page_state.get("key"),
        "wait_detail": page_state.get("wait_detail"),
        "action": page_state.get("primary_action"),
        "failure": page_state.get("failure"),
        "moments": _moments(calibration),
        "coverage_summary": object_dict(
            object_dict(calibration.get("sample_item")).get("representative_selection")
        ).get("coverage_summary"),
        "episode_bytes": int_value(sample_item.get("source_size_bytes")),
        "estimated_episode_bytes": int_value(
            object_dict(calibration.get("sample_result")).get("predicted_total_size_bytes")
        ),
        "season_bytes": int_value(
            _given(lifecycle.get("eligible_size_bytes"), object_dict(folder.get("summary")).get("total_size_bytes"))
        ),
        "season_episode_count": int_value(
            _given(lifecycle.get("eligible_candidate_count"), folder.get("encode_candidate_count"))
        ),
        "goal_bytes": _goal_bytes(folder),
        "size_status": object_dict(folder.get("size_target_analysis")).get("status"),
        "draft_hash": calibration.get("draft_hash"),
        "ready_in_seconds": _ready_in_seconds(status) if page_state.get("key") == "making_test" else None,
        "size_choices": [
            {"key": choice["key"], "target_bytes": int_value(object_dict(choice["resolved_size_goal"]).get("target_size_bytes"))}
            for choice in _size_choices(folder)
        ],
    }


def _ready_in_seconds(status: Mapping[str, Any]) -> int | None:
    """The latest the running sample should be ready by, from comparable finished samples; None without enough of them."""
    estimate = object_dict(object_dict(object_dict(status.get("exact_calibration_job")).get("progress")).get("estimate"))
    remaining = estimate.get("remaining_seconds_high")
    return int(remaining) if isinstance(remaining, int | float) else None


def approve_sample(
        prefix: str,
        confirm: Collection[str],
        reviewed_draft_hash: str,
        *,
        save_profile: SaveProfileFn,
        queue_encode: QueueEncodeFn,
) -> dict[str, Any]:
    """Approve the sample the owner watched and start compressing the season, like the season page's approval."""
    try:
        saved = save_profile(prefix, "high_impact" in confirm, "size_tradeoff" in confirm, reviewed_draft_hash, "")
    except ReviewedDraftChanged:
        return {"ok": False, "code": "sample_changed"}
    except ApprovalNeedsConfirmation as needed:
        return {"ok": False, "code": f"confirm_{needed.confirm}"}
    except HTTPException:
        return REFUSED
    if not saved.get("ok"):
        return REFUSED
    return start_compressing(prefix, queue_encode=queue_encode)


def start_compressing(prefix: str, *, queue_encode: QueueEncodeFn) -> dict[str, Any]:
    """Queue the approved season, with the same holds as the season page's approval."""
    try:
        queued = queue_encode(prefix, APPROVED_SAMPLE_NOTES, False, False, "")
    except QueueNeedsChoice as refused:
        queued = {"code": refused.code}
    except HTTPException:
        queued = {}
    compressing = bool(queued.get("ok")) or queued.get("code") == "encode_already_active"
    return {"ok": True, "compressing": compressing, "reason": None if compressing else queued.get("code")}


def make_sample(
        folder: Mapping[str, Any],
        status: Mapping[str, Any],
        size: str | None,
        *,
        ai_tune: AiTuneFn,
        retry_saved: RetrySampleFn,
) -> dict[str, Any]:
    """Make the sample again: retry the saved request, or ask for a new one at the chosen or current size."""
    prefix = str(folder["prefix"])
    action = object_dict(status.get("page_state")).get("primary_action")
    if size is None and action == "choose_size":
        return {"ok": False, "code": "choose_size"}
    result: Mapping[str, Any] = {}
    if size is None and action == "retry_sample":
        try:
            result = retry_saved(prefix, "", "")
        except HTTPException:
            result = {}
    try:
        # A saved request that can't run again, such as one under older settings or on a computer gone since, is asked
        # for anew.
        if not result.get("ok"):
            request = _sample_request(folder, size, remake=action == "review_sample")
            if isinstance(request, str):
                return {"ok": False, "code": request}
            note, operator_intent = request
            result = ai_tune(prefix, note, "", operator_intent, "")
    except HTTPException:
        return REFUSED
    return {"ok": True, "code": None} if result.get("ok") else REFUSED


def samples_to_remake(cards: Iterable[Mapping[str, Any]], failed_in_a_row: Callable[[str], int]) -> list[str]:
    """The seasons whose sample needs making again, which Mediaforce makes again by itself at the season's size goal
    (#1149), until that season's samples have failed SAMPLE_REMAKE_LIMIT times in a row."""
    remade = []
    for card in cards:
        state = object_dict(card.get("page_state"))
        if (state.get("key") == "needs_help" and state.get("recovery_kind") == "test"
                and state.get("primary_action") in ("make_sample", "retry_sample")
                and state.get("failure") not in _NOT_REMADE_FAILURES
                and failed_in_a_row(str(card["prefix"])) < SAMPLE_REMAKE_LIMIT):
            remade.append(str(card["prefix"]))
    return remade


def _sample_request(
        folder: Mapping[str, Any], size: str | None, *, remake: bool,
) -> tuple[str, dict[str, Any] | None] | str:
    if size is None and remake:
        return _measured_request(folder), _current_operator_intent(folder)
    choices = _size_choices(folder)
    if size:
        choice = next((option for option in choices if option["key"] == size), None)
    else:
        choice = next((option for option in choices if not option.get("requires_explicit_selection")), None)
    if choice is None:
        return "choose_size"
    balances = object_list(folder.get("compression_intent_options"))
    balance = next((option for option in balances if option.get("selected")), None) or next(
        (option for option in balances if option.get("key") == DEFAULT_BALANCE), None
    )
    if balance is None:
        return "choose_balance"
    intent = with_compression_intent(object_dict(choice["operator_intent"]), object_dict(balance.get("compression_intent")))
    resolution = None if size else _current_resolution(folder)
    if resolution is not None:
        # A sample made at the season's own size keeps its resolution; a size the owner picks brings its own.
        intent = {**intent, "resolution": resolution}
        choice = {**choice, "operator_intent": intent}
    return sample_goal_request(choice), intent


def _measured_request(folder: Mapping[str, Any]) -> str:
    analysis = object_dict(folder.get("size_target_analysis"))
    compression_intent = object_dict(object_dict(object_dict(folder.get("calibration")).get("sample_item")).get("compression_intent"))
    accepts_under_target = (
            analysis.get("status") == "under_target"
            and compression_intent.get("schema_version") == 1
            and compression_intent.get("confirmed") is True
            and compression_intent.get("accepts_under_target_result") is True
    )
    request = measured_followup_request(analysis, accepts_under_target=accepts_under_target)
    if request:
        return request
    goal_bytes = _goal_bytes(folder)
    goal = f"{round(goal_bytes / DECIMAL_MEGABYTE_BYTES)} MB" if goal_bytes > 0 else "the requested size"
    return (
        f"Keep the {goal} whole-episode goal and current resolution. Make another representative test that addresses "
        "the operator's review concerns without changing the size target."
    )


def _current_resolution(folder: Mapping[str, Any]) -> dict[str, Any] | None:
    resolution = object_dict(object_dict(folder.get("resolved_operator_intent")).get("resolution"))
    if resolution.get("mode") not in ("source", "max_height") or resolution.get("requires_confirmation"):
        return None
    return {"mode": resolution["mode"], "max_height": resolution.get("max_height")}


def _current_operator_intent(folder: Mapping[str, Any]) -> dict[str, Any] | None:
    request = object_dict(object_dict(folder.get("resolved_operator_intent")).get("request"))
    if request.get("schema_version") not in (1, 2):
        return None
    if object_dict(request.get("size_goal")).get("mode") not in ("normalized", "absolute"):
        return None
    return request


def _size_choices(folder: Mapping[str, Any]) -> list[dict[str, Any]]:
    choices = []
    for option in object_list(folder.get("size_goal_options")):
        goal = object_dict(option.get("resolved_size_goal"))
        mode = object_dict(object_dict(option.get("operator_intent")).get("size_goal")).get("mode")
        if (float_value(goal.get("target_size_mb")) > 0 and int_value(goal.get("target_size_bytes")) > 0
                and mode in ("normalized", "absolute")):
            choices.append(option)
    return choices


def _goal_bytes(folder: Mapping[str, Any]) -> int:
    resolved = object_dict(object_dict(folder.get("resolved_operator_intent")).get("size_goal"))
    return int_value(object_dict(folder.get("size_target_analysis")).get("budget_bytes")) or int_value(
        resolved.get("target_size_bytes")
    )


def _moments(calibration: Mapping[str, Any]) -> list[dict[str, Any]]:
    if calibration.get("browser_review_ready") is not True:
        return []
    moments = []
    for pair in object_list(calibration.get("review_pairs")):
        original, sample = object_dict(pair.get("source_clip")), object_dict(pair.get("preview_clip"))
        if original.get("path") and sample.get("path"):
            moments.append({
                "at_seconds": float_value(original.get("timestamp_seconds")),
                "original": {"url": original["path"], "size_bytes": int_value(original.get("size_bytes"))},
                "sample": {"url": sample["path"], "size_bytes": int_value(sample.get("size_bytes"))},
            })
    return moments


def _given(value: Any, fallback: Any) -> Any:
    return fallback if value is None else value
