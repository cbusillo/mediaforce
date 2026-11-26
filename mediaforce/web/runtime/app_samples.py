from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from threading import Lock
from typing import Any
from pathlib import Path

from fastapi import HTTPException

from mediaforce.library.representatives import sample_coverage_summary
from mediaforce.reviewing.clips import review_file_from_url
from mediaforce.core.type_defs import float_value, int_value, object_dict, object_list
from mediaforce.tuning.compression_intent import compression_intent_from_payload, compression_intent_options
from mediaforce.tuning.size_goals import DECIMAL_MEGABYTE_BYTES, guided_size_goal_options, measured_followup_request, sample_goal_request, \
    with_compression_intent, operator_intent_from_policy
from mediaforce.tuning.stream_budget import item_output_size_goal
from mediaforce.web.runtime.app_window import file_identity, season_identity
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
_FIRST_SAMPLE_QUESTIONS = frozenset({"choose_size", "choose_balance", "sample_blocked"})


class SamplePreparations:
    already_preparing = "already_preparing"

    def __init__(self) -> None:
        self._active: set[str] = set()
        self._lock = Lock()

    def __contains__(self, prefix: str) -> bool:
        with self._lock:
            return prefix in self._active

    def run(self, prefix: str, prepare: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        with self._lock:
            if prefix in self._active:
                return {"ok": False, "code": self.already_preparing, "message": "Mediaforce is already preparing a sample for this season."}
            self._active.add(prefix)
        try:
            return prepare()
        finally:
            with self._lock:
                self._active.remove(prefix)


def sample_facts(folder: Mapping[str, Any], status: Mapping[str, Any]) -> dict[str, Any]:
    calibration = object_dict(folder.get("calibration"))
    page_state = object_dict(status.get("page_state"))
    lifecycle = object_dict(folder.get("lifecycle"))
    sample_item = object_dict(calibration.get("sample_item")) or object_dict(folder.get("sample_item"))
    selection = object_dict(object_dict(calibration.get("sample_item")).get("representative_selection"))
    balances = _balance_choices(folder)
    effective_balance = _effective_balance(balances)
    if page_state.get("primary_action") == "retry_sample":
        retry_policy = object_dict(object_dict(status.get("retryable_sample_job")).get("policy"))
        retry_balance = operator_intent_from_policy(object_dict(retry_policy.get("video"))).compression_intent
        selected = {option["key"] for option in compression_intent_options(retry_balance) if option["selected"]}
        effective_balance = next((option for option in balances if option["key"] in selected), None)
    prefix = str(folder["prefix"])
    scope = object_dict(folder.get("media_scope"))
    return {
        **(file_identity(prefix, str(scope["title"]), str(scope["domain"])) if scope.get("domain") in {"movie", "other"} else season_identity(prefix)),
        "state": page_state.get("key"),
        "wait_detail": page_state.get("wait_detail"),
        "action": page_state.get("primary_action"),
        "failure": page_state.get("failure"),
        "moments": sample_moments(calibration),
        "coverage_summary": sample_coverage_summary(
            object_dict(selection.get("coverage")), is_tv=object_dict(folder.get("media_scope")).get("domain") == "tv",
        ) or selection.get("coverage_summary"),
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
        "picture": _picture(folder),
        "balance_choices": [
            {"key": choice["key"], "label": choice["title"], "detail": str(choice.get("detail") or ""),
             "selected": choice == effective_balance}
            for choice in balances
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
        scope: str = "season",
        representative_kind: str | None = None,
        approve_kind: Callable[[str, str, str], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    if scope != "season" and (scope != representative_kind or approve_kind is None):
        return {"ok": False, "code": "approval_scope_changed"}
    try:
        saved = save_profile(prefix, "high_impact" in confirm, "size_tradeoff" in confirm, reviewed_draft_hash, "")
    except ReviewedDraftChanged as exc:
        return {"ok": False, "code": "sample_changed", **_client_error_message(exc)}
    except ApprovalNeedsConfirmation as needed:
        return {"ok": False, "code": f"confirm_{needed.confirm}", **_client_error_message(needed)}
    except HTTPException as exc:
        return {**REFUSED, **_client_error_message(exc)}
    if not saved.get("ok"):
        return {**REFUSED, **_message(saved.get("message"))}
    if scope != "season" and approve_kind is not None:
        extended = approve_kind(scope, prefix, reviewed_draft_hash)
        if not extended.get("ok"):
            return {**start_compressing(prefix, queue_encode=queue_encode), "kind_code": extended.get("code") or "refused"}
    return start_compressing(prefix, queue_encode=queue_encode)


def start_compressing(prefix: str, *, queue_encode: QueueEncodeFn) -> dict[str, Any]:
    """Queue the approved season, with the same holds as the season page's approval."""
    try:
        queued = queue_encode(prefix, APPROVED_SAMPLE_NOTES, False, False, "")
    except QueueNeedsChoice as refused:
        queued = {"code": refused.code, **_client_error_message(refused)}
    except HTTPException as exc:
        queued = _client_error_message(exc)
    compressing = bool(queued.get("ok")) or queued.get("code") == "encode_already_active"
    return {"ok": True, "compressing": compressing, "reason": None if compressing else queued.get("code"),
            **({} if compressing else _message(queued.get("message")))}


def make_sample(
        folder: Mapping[str, Any],
        status: Mapping[str, Any],
        size: str | None,
        balance: str | None = None,
        *,
        ai_tune: AiTuneFn,
        retry_saved: RetrySampleFn,
        keep_full_height: bool | None = None,
        group_size_mb: float | None = None,
) -> dict[str, Any]:
    """Make the sample again: retry the saved request, or ask for a new one at the chosen or current size.

    When the season's original is taller than the library's tallest picture, the sample is made at the height the
    owner chose for it, else at the season's current one.
    """
    prefix = str(folder["prefix"])
    action = object_dict(status.get("page_state")).get("primary_action")
    if group_size_mb is not None:
        policy = object_dict(folder.get("policy"))
        video = {**object_dict(policy.get("video")), "size_goal_mode": "normalized", "target_runtime_minutes": 45,
                 "target_size_mb": group_size_mb, "target_size_bytes": round(group_size_mb * DECIMAL_MEGABYTE_BYTES)}
        folder = {**folder, "size_goal_options": guided_size_goal_options(
            video, item_runtime_seconds=float_value(object_dict(folder.get("sample_item")).get("duration_seconds")),
            normalized_track_extra_bytes=int_value(item_output_size_goal(folder).get("normalized_track_extra_bytes")),
            default_video_policy=video, audio_policy=object_dict(policy.get("audio")), subtitle_policy=object_dict(policy.get("subtitle")),
        )}
        size = "recommended"
    if size is None and action == "choose_size":
        return {"ok": False, "code": "choose_size"}
    result: Mapping[str, Any] = {}
    retry = object_dict(status.get("retryable_sample_job"))
    picture = _picture(folder)
    height_changes = picture is not None and keep_full_height is not None and keep_full_height != picture["keeps_full_height"]
    if size is None and balance is None and action == "retry_sample" and not height_changes:
        try:
            result = retry_saved(prefix, "", "")
        except HTTPException:
            result = {}
    try:
        # A saved request that can't run again, such as one under older settings or on a computer gone since, is asked
        # for anew.
        if not result.get("ok"):
            request = _sample_request(
                folder, size, balance, remake=action == "review_sample",
                retry=retry if action == "retry_sample" and (balance is not None or retry.get("policy")) else None,
                resolution=_picture_resolution(picture, keep_full_height),
            )
            if isinstance(request, str):
                return {"ok": False, "code": request}
            note, operator_intent = request
            result = ai_tune(prefix, note, "", operator_intent, "")
    except HTTPException as exc:
        return {**REFUSED, **_client_error_message(exc)}
    if result.get("ok"):
        return {"ok": True, "code": None}
    return {**REFUSED, **_message(result.get("message"))}


def _client_error_message(exc: HTTPException) -> dict[str, str]:
    return _message(exc.detail) if 400 <= exc.status_code < 500 else {}


def _message(value: object) -> dict[str, str]:
    return {"message": value} if isinstance(value, str) and value.strip() else {}


def _can_prepare_group_sample(kind: Mapping[str, Any], state: Mapping[str, Any]) -> bool:
    return (state.get("key") in {"needs_test", "waiting_for_kind"}
            or kind.get("selected_size_mb") is not None and state.get("key") == "ready_to_compare")


def kind_sample_candidates(kinds: Iterable[Mapping[str, Any]], cards: Iterable[Mapping[str, Any]]) -> dict[str, list[str]]:
    states = {str(card["prefix"]): object_dict(card.get("page_state")) for card in cards}
    groups: dict[str, list[str]] = {}
    for kind in kinds:
        if (not kind.get("shows") or kind.get("approval") or kind.get("sample_season")
                or (kind.get("settings") and not kind.get("one_time"))):
            continue
        candidates = [str(show["sample_season"]) for show in object_list(kind.get("shows"))
                      if show.get("sample_season") and _can_prepare_group_sample(kind, states.get(str(show["sample_season"]), {}))]
        groups[str(kind["kind"])] = candidates
    return groups


@dataclass(frozen=True, slots=True)
class SampleStartWait:
    prefix: str | None
    reason: str


def prepare_waiting_samples(
        cards: Iterable[Mapping[str, Any]], kinds: Iterable[Mapping[str, Any]], *,
        load_payloads: Callable[[str], tuple[dict[str, Any], dict[str, Any]] | None],
        failed_in_a_row: Callable[[str], int],
        make: Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]],
        make_group: Callable[[str, Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]] | None = None,
        previous: Mapping[str, SampleStartWait] | None = None,
) -> dict[str, SampleStartWait]:
    cards = list(cards)
    groups = {str(group["kind"]): group for group in kinds}
    for season in samples_to_remake(cards, failed_in_a_row):
        payloads = load_payloads(season)
        if payloads is not None and samples_to_remake([{**payloads[1], "prefix": season}], failed_in_a_row):
            make(*payloads)
    waits: dict[str, SampleStartWait] = {}
    for kind, candidates in kind_sample_candidates(groups.values(), cards).items():
        wait = None
        started = False
        for season in candidates:
            payloads = load_payloads(season)
            if payloads is None or not _can_prepare_group_sample(groups[kind], object_dict(payloads[1].get("page_state"))):
                continue
            result = make_group(kind, *payloads) if make_group else make(*payloads)
            if result.get("ok") or result.get("code") == SamplePreparations.already_preparing:
                started = True
                break
            if wait is None:
                wait = SampleStartWait(season, _start_refusal(payloads[0], result))
        if started:
            continue
        prior = (previous or {}).get(kind)
        if wait is None:
            waits[kind] = SampleStartWait(None, "retry_backoff")
        elif wait.reason in {"choose_size", "choose_balance"}:
            waits[kind] = wait
        else:
            repeated = prior is not None and prior.reason in {"retry_backoff", "sample_blocked"}
            waits[kind] = SampleStartWait(wait.prefix, "sample_blocked" if repeated else "retry_backoff")
    return waits


def _start_refusal(folder: Mapping[str, Any], result: Mapping[str, Any]) -> str:
    code = str(result.get("code") or "refused")
    return code if _season_offers_the_choice(folder, code) else "refused"


def _season_offers_the_choice(folder: Mapping[str, Any], code: str) -> bool:
    if code == "choose_size":
        return bool(_size_choices(folder))
    if code == "choose_balance":
        return bool(_balance_choices(folder))
    return True


def with_first_sample_question(facts: dict[str, Any], waits: Iterable[SampleStartWait]) -> dict[str, Any]:
    reason = next((wait.reason for wait in waits if wait.prefix == facts["prefix"]), None)
    if reason not in _FIRST_SAMPLE_QUESTIONS or facts["action"] not in (None, "make_sample"):
        return facts
    return {**facts, "action": "choose_size" if reason == "choose_size" else "make_sample"}


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
        folder: Mapping[str, Any], size: str | None, balance: str | None, *, remake: bool,
        retry: dict[str, Any] | None = None, resolution: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any] | None] | str:
    current_intent = _current_operator_intent(folder)
    remake = remake and (current_intent is not None or _goal_bytes(folder) > 0)
    if size is None and balance is None and remake and (resolution is None or current_intent is None
                                                         or current_intent.get("resolution") == resolution):
        return _measured_request(folder), current_intent
    balances = _balance_choices(folder)
    if balance is not None:
        balance_choice = next((option for option in balances if option["key"] == balance), None)
        if balance_choice is None:
            return "choose_balance"
    else:
        balance_choice = _effective_balance(balances)
    if size is None and (remake or retry is not None):
        current = current_intent
        goal_mb = _goal_bytes(folder) / DECIMAL_MEGABYTE_BYTES
        if retry is not None:
            policy = object_dict(retry.get("policy"))
            if not policy:
                return "choose_size"
            saved = operator_intent_from_policy(
                object_dict(policy.get("video")), audio_policy=object_dict(policy.get("audio")),
                subtitle_policy=object_dict(policy.get("subtitle")),
            )
            current = saved.request_payload()
            sample_item = object_dict(folder.get("sample_item")) or object_dict(retry.get("sample_item"))
            resolved = saved.size_goal.resolve(float_value(sample_item.get("duration_seconds")) or None)
            if saved.resolution.requires_confirmation:
                return "choose_size"
            if saved.size_goal.value_bytes is None and (choice := _size_choice(folder, None)) is not None:
                current["size_goal"] = choice["operator_intent"]["size_goal"]
                goal_mb = choice["resolved_size_goal"]["target_size_mb"]
            elif resolved.requires_confirmation:
                return "choose_size"
            else:
                goal_mb = (resolved.target_size_bytes or 0) / DECIMAL_MEGABYTE_BYTES
        if current is None:
            return "choose_size"
        if balance is None and object_dict(current.get("compression_intent")).get("confirmed") is True:
            intent = current
        else:
            if balance_choice is None:
                return "choose_balance"
            intent = with_compression_intent(current, object_dict(balance_choice.get("compression_intent")))
        if resolution is not None and intent.get("resolution") != resolution:
            intent = {**intent, "resolution": resolution}
        elif remake:
            return _measured_request(folder, compression_intent=object_dict(intent.get("compression_intent"))), intent
        return sample_goal_request({"operator_intent": intent, "resolved_size_goal": {"target_size_mb": goal_mb}}), intent
    choice = _size_choice(folder, size)
    if choice is None:
        return "choose_size"
    if balance_choice is None:
        return "choose_balance"
    intent = with_compression_intent(
        object_dict(choice["operator_intent"]), object_dict(balance_choice.get("compression_intent"))
    )
    if resolution is not None:
        intent = {**intent, "resolution": resolution}
    return sample_goal_request({**choice, "operator_intent": intent}), intent


def _measured_request(folder: Mapping[str, Any], *, compression_intent: dict[str, Any] | None = None) -> str:
    analysis = object_dict(folder.get("size_target_analysis"))
    if compression_intent is None:
        compression_intent = object_dict(object_dict(object_dict(folder.get("calibration")).get("sample_item")).get("compression_intent"))
    accepts_under_target = compression_intent_from_payload(compression_intent).accepts_under_target_result
    request = measured_followup_request(analysis, accepts_under_target=accepts_under_target)
    if request:
        return request
    goal_bytes = _goal_bytes(folder)
    goal = f"{round(goal_bytes / DECIMAL_MEGABYTE_BYTES)} MB" if goal_bytes > 0 else "the requested size"
    return (
        f"Keep the {goal} whole-episode goal and current resolution. Make another representative test that addresses "
        "the operator's review concerns without changing the size target."
    )


def _picture(folder: Mapping[str, Any]) -> dict[str, Any] | None:
    """The 4K choice, offered when the season's sample episode is taller than the library's tallest picture: keep
    its height, or bring it down to that limit. Without a choice saved here it follows the library and comes down."""
    limit = int_value(folder.get("tallest_picture"))
    height = int_value(object_dict(folder.get("sample_item")).get("height"))
    if limit <= 0 or height <= limit:
        return None
    resolution = object_dict(object_dict(folder.get("resolved_operator_intent")).get("resolution"))
    keeps = not resolution.get("requires_confirmation") and (
        resolution.get("mode") == "source" or (resolution.get("mode") == "max_height" and int_value(resolution.get("max_height")) >= height))
    return {"source_height": height, "limit": limit, "keeps_full_height": keeps}


def _picture_resolution(picture: Mapping[str, Any] | None, keep_full_height: bool | None) -> dict[str, Any] | None:
    """The resolution the owner chose for a season that offers the 4K choice; None leaves the season's own."""
    if picture is None or keep_full_height is None:
        return None
    return {"mode": "source", "max_height": None} if keep_full_height else {"mode": "max_height", "max_height": picture["limit"]}


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


def _size_choice(folder: Mapping[str, Any], size: str | None) -> dict[str, Any] | None:
    choices = _size_choices(folder)
    if size:
        return next((option for option in choices if option["key"] == size), None)
    return next((option for option in choices if not option.get("requires_explicit_selection")), None)


def _effective_balance(balances: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((option for option in balances if option.get("selected")), None) or next(
        (option for option in balances if option.get("key") == DEFAULT_BALANCE), None,
    )


def _balance_choices(folder: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        option for option in object_list(folder.get("compression_intent_options"))
        if isinstance(option.get("key"), str) and isinstance(option.get("title"), str)
        and object_dict(option.get("compression_intent"))
    ]


def _goal_bytes(folder: Mapping[str, Any]) -> int:
    resolved = object_dict(object_dict(folder.get("resolved_operator_intent")).get("size_goal"))
    return int_value(object_dict(folder.get("size_target_analysis")).get("budget_bytes")) or int_value(
        resolved.get("target_size_bytes")
    )


def _estimated_original_bytes(item: Mapping[str, Any], clip: Mapping[str, Any]) -> int | None:
    duration = float_value(item.get("duration_seconds"))
    source_bytes = int_value(item.get("source_size_bytes"))
    seconds = min(float_value(clip.get("duration_seconds")), duration - float_value(clip.get("timestamp_seconds")))
    if source_bytes > 0 and seconds > 0 and duration > 0:
        return round(source_bytes * seconds / duration)
    return None


def sample_moments(calibration: Mapping[str, Any], *, review_dir: Path | None = None) -> list[dict[str, Any]]:
    if calibration.get("browser_review_ready") is not True:
        return []
    moments = []
    for pair in object_list(calibration.get("review_pairs")):
        original, sample = object_dict(pair.get("source_clip")), object_dict(pair.get("preview_clip"))
        if original.get("path") and sample.get("path"):
            if review_dir is not None and not all(
                (path := review_file_from_url(review_dir, str(clip["path"]))) is not None and path.is_file()
                for clip in (original, sample)
            ):
                continue
            production_bytes = int_value(sample.get("production_size_bytes")) or None
            original_bytes = _estimated_original_bytes(object_dict(calibration.get("sample_item")), original) if production_bytes else None
            moments.append({
                "at_seconds": float_value(original.get("timestamp_seconds")),
                "original": {"url": original["path"], "size_bytes": int_value(original.get("size_bytes")), "moment_bytes": original_bytes},
                "sample": {"url": sample["path"], "size_bytes": int_value(sample.get("size_bytes")), "moment_bytes": production_bytes},
            })
    return moments


def _given(value: Any, fallback: Any) -> Any:
    return fallback if value is None else value
