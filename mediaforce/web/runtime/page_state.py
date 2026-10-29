"""The one state a folder page or library card leads with, and the action it offers.

The server decides it from database rows and saved samples, never from the hour-long job notice the
page payload carries, so a page and its library card agree and nothing changes when a notice expires.
The Mac app maps the key to wording.

Order, first match wins:

1. ``making_season``: an encode run is working on the scope (the workflow state's wider-run rules).
2. ``needs_help`` / ``season``: an encode run stopped on one of the scope's files, unless the page's
   own sample is newer than the stop. The newer sample is how the owner answers a stopped run, so
   its state leads instead.
3. ``needs_help`` / ``files``: a finished file is blocked, unless a fresh sample awaits review; a
   blocked file is made again only after a fresh sample is approved.
4. The page's own sample, in its order: ``making_test``, ``sample_waiting``, ``needs_help`` /
   ``test`` for a sample that stopped, ``ready_to_compare`` for a sample awaiting review. A sample
   outranks validating, finishing and finished because it is the owner's newest request for the
   scope, and approving it changes which settings its files are published under.
5. ``ready_to_check``, ``finish_blocked`` / ``ready_to_finish``, ``finished``: the workflow lanes. A
   file the owner kept original for its motion pattern is settled, so a scope whose remaining files
   are all published or kept original is ``finished``.
6. ``ready_to_make``: the approval governing the scope (the nearest folder holding one, see
   ``production_approval``) is current, records its contract, and the review evidence still allows
   production. Under such an approval a scope where neither queueing nor an override the page
   offers would take or start a check for any file, and some file is held for its measured motion
   pattern, is ``motion_held`` instead, so nothing offers to compress files queueing would refuse.
   The counts come from the scope's lifecycle payload, which reads the queue's own motion-pattern
   gate. It is quiet when no one can change the outcome, and asks for attention when the show's
   encode-as-is decision could accept some of the files.
7. ``needs_help`` / ``test``: the page's saved sample cannot be reviewed.
8. ``needs_test``.

The page's own sample is the newest sample job and the saved sample at exactly its folder. A sample
of one episode inside a season, or of the show around it, never sets the season's sample state.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.encoding.encode_queue import load_encode_job
from mediaforce.tuning.calibration_jobs import load_latest_job, load_latest_jobs_for_prefixes
from mediaforce.tuning.quality_risk import build_quality_risk_contract
from mediaforce.web.runtime.folder_actions import quality_risk_blocks_production
from mediaforce.web.runtime.folder_tuning_advice import review_gate
from mediaforce.web.runtime.job_runtime import retryable_saved_sample_job, sample_target_size_failure
from mediaforce.web.runtime.production_approval import EffectiveApproval, LoadCalibrationState, \
    cached_calibration_loader, effective_production_approval, following_kind

PageStateKey = Literal[
    "needs_test",
    "waiting_for_kind",
    "sample_waiting",
    "making_test",
    "ready_to_compare",
    "ready_to_make",
    "motion_held",
    "making_season",
    "ready_to_check",
    "finish_blocked",
    "ready_to_finish",
    "finished",
    "needs_help",
]
PageTone = Literal["quiet", "active", "ready", "success", "attention"]
RecoveryKind = Literal["test", "season", "files"]
PageAction = Literal[
    "make_sample",
    "retry_sample",
    "choose_size",
    "review_sample",
    "queue_encode",
    "retry_encode",
    "review_files",
    "validate_outputs",
    "promote_outputs",
    "decide_motion",
]
FailureKind = Literal[
    "arithmetic_infeasible",
    "bound_exhausted",
    "quality_conflict",
    "missing_clips",
    "restarted",
    "no_access",
    "size_under_target",
    "size_over_target",
    "size_unverified",
    "goal_too_small",
    "stopped",
    "unexplained",
    "other",
]
LoadAdviceState = Callable[[MediaforceConfig, str], dict[str, Any] | None]

SAMPLE_RUNNING_STATUSES = frozenset({"starting", "running", "stopping"})
SAMPLE_WAITING_STATUSES = frozenset({"queued", "retry_backoff"})
SAMPLE_FAILED_STATUSES = frozenset({"failed", "stopped", "needs_attention"})
# A size goal that cannot be met stops the same way on every retry; the owner chooses another size.
SIZE_GOAL_FAILURES: frozenset[FailureKind] = frozenset({"arithmetic_infeasible", "bound_exhausted", "quality_conflict"})
_SAMPLE_TARGET_FAILURES: dict[str, FailureKind] = {
    "infeasible": "arithmetic_infeasible",
    "bound_exhausted": "bound_exhausted",
    "quality_conflict": "quality_conflict",
}
_ENCODE_QUALITY_FLOOR_REASONS = ("target_band_violates_quality_floor", "target_requires_crossing_quality_floor")


@dataclass(frozen=True, slots=True)
class SourceJob:
    """The job or sample that decided a page state."""

    id: str
    kind: Literal["sample", "encode"]
    prefix: str
    status: str

    def to_payload(self) -> dict[str, str]:
        return {"id": self.id, "kind": self.kind, "prefix": self.prefix, "status": self.status}


@dataclass(frozen=True, slots=True)
class PageState:
    key: PageStateKey
    tone: PageTone
    recovery_kind: RecoveryKind | None = None
    primary_action: PageAction | None = None
    failure: FailureKind | None = None
    source_job: SourceJob | None = None
    # Which approval governs the folder: its own or a parent folder's ("folder"), or its kind of content's ("kind").
    approval_source: str | None = None
    approval_kind: str | None = None
    needs_you: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "tone": self.tone,
            "recovery_kind": self.recovery_kind,
            "primary_action": self.primary_action,
            "failure": self.failure,
            "source_job": self.source_job.to_payload() if self.source_job is not None else None,
            "approval_source": self.approval_source,
            "approval_kind": self.approval_kind,
            "needs_you": self.needs_you,
        }


@dataclass(frozen=True, slots=True)
class PageStateFacts:
    """What a page state is decided from, all read from the database and saved samples."""

    prefix: str
    # The folder's workflow state payload, after the status payload's shared-storage check.
    workflow: Mapping[str, Any] | None
    # The encode run the workflow lane came from (its ``source_job_id``).
    encode_job: Mapping[str, Any] | None = None
    # The newest sample job at exactly this folder.
    sample_job: Mapping[str, Any] | None = None
    # The saved sample at exactly this folder.
    calibration: Mapping[str, Any] | None = None
    # The approval governing this folder, from the nearest folder holding one.
    approval: EffectiveApproval | None = None
    approval_review_blocked: bool = False
    following_kind: str | None = None
    # The staged-file summary, when the caller read one.
    staged_integrity: Mapping[str, Any] | None = None
    # The scope's lifecycle payload with its motion-pattern counts, for a TV scope.
    lifecycle: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class PageStateSources:
    load_calibration_state: LoadCalibrationState
    load_advice_state: LoadAdviceState


def decide_page_state(facts: PageStateFacts) -> PageState:
    state = _decide_page_state(facts)
    state = replace(state, needs_you=_needs_you(state, facts))
    approval = facts.approval
    if approval is None:
        return state
    return replace(state, approval_source=approval.source, approval_kind=approval.kind)


def _needs_you(state: PageState, facts: PageStateFacts) -> bool:
    return state.key in {"ready_to_compare", "needs_help"} or state.primary_action == "decide_motion" or (
        state.key == "making_season" and bool(object_dict(facts.workflow).get("needs_owner"))
    )


def _decide_page_state(facts: PageStateFacts) -> PageState:
    workflow = object_dict(facts.workflow)
    lane = str(workflow.get("primary_lane") or "none")
    encode_job = object_dict(facts.encode_job)
    sample_job = object_dict(facts.sample_job)
    sample_status = str(sample_job.get("status") or "")
    calibration = object_dict(facts.calibration) if facts.calibration is not None else None
    if facts.following_kind and sample_status not in SAMPLE_RUNNING_STATUSES | SAMPLE_WAITING_STATUSES:
        sample_job, sample_status, calibration = {}, "", None

    if lane == "processing":
        return PageState("making_season", "active", source_job=_source_job(encode_job, "encode"))
    if lane == "attention" and not _sample_answers_stop(sample_job, encode_job):
        failure = _encode_failure(encode_job)
        return PageState(
            "needs_help",
            "attention",
            recovery_kind="season",
            primary_action="choose_size" if failure in SIZE_GOAL_FAILURES else "retry_encode",
            failure=failure,
            source_job=_source_job(encode_job, "encode"),
        )

    gate = review_gate(dict(calibration)) if calibration is not None else None
    browser_ready, review_recovery = _review_availability(calibration, gate)
    sample_active = sample_status in SAMPLE_RUNNING_STATUSES | SAMPLE_WAITING_STATUSES
    awaits_review = bool(gate and gate.get("status") == "needs_approval" and browser_ready and not sample_active)
    calibration_source = _calibration_source(facts.prefix, calibration, sample_job)

    if sample_status in SAMPLE_RUNNING_STATUSES:
        return PageState("making_test", "active", source_job=_source_job(sample_job, "sample"))
    if sample_status in SAMPLE_WAITING_STATUSES:
        return PageState(
            "sample_waiting",
            "attention" if sample_status == "retry_backoff" else "quiet",
            source_job=_source_job(sample_job, "sample"),
        )
    if lane == "blocked" and not awaits_review:
        return PageState("needs_help", "attention", recovery_kind="files", primary_action="review_files")
    if sample_status in SAMPLE_FAILED_STATUSES and not _settled_by_approval(sample_job, calibration):
        failure = _sample_failure(sample_job)
        return PageState(
            "needs_help",
            "attention",
            recovery_kind="test",
            primary_action=(
                "choose_size" if failure in SIZE_GOAL_FAILURES
                else "retry_sample" if retryable_saved_sample_job(dict(sample_job))
                else "make_sample"
            ),
            failure=failure,
            source_job=_source_job(sample_job, "sample"),
        )
    if awaits_review:
        return PageState("ready_to_compare", "ready", primary_action="review_sample", source_job=calibration_source)

    if lane == "validate":
        return PageState("ready_to_check", "ready", primary_action=_workflow_action(workflow, "validate_outputs"))
    if lane == "promote":
        if _staged_files_block_finish(facts.staged_integrity):
            return PageState("finish_blocked", "attention")
        return PageState("ready_to_finish", "ready", primary_action=_workflow_action(workflow, "promote_outputs"))
    if lane == "complete" or workflow.get("state") == "complete":
        return PageState("finished", "success")

    approval = facts.approval
    if approval is not None and approval.current and approval.contract is not None:
        if not facts.approval_review_blocked:
            motion_held = _motion_held_state(facts.lifecycle, workflow)
            if motion_held is not None:
                return motion_held
            return PageState(
                "ready_to_make",
                "ready",
                primary_action="queue_encode",
                source_job=SourceJob(
                    str(approval.calibration.get("accepted_sample_job_id") or ""),
                    "sample",
                    approval.prefix,
                    "approved",
                ),
            )
        if approval.prefix == facts.prefix and browser_ready:
            # The owner's review was rejected or measured facts now block it: review the sample again.
            return PageState("ready_to_compare", "ready", primary_action="review_sample", source_job=calibration_source)
    if calibration is not None and (
            review_recovery
            or (gate is not None and gate.get("status") == "missing_review_media")
            or (calibration.get("job_id") and not calibration.get("draft_hash") and not browser_ready)
    ):
        return PageState(
            "needs_help",
            "attention",
            recovery_kind="test",
            primary_action="make_sample",
            failure="missing_clips",
            source_job=calibration_source,
        )
    if facts.following_kind:
        return PageState("waiting_for_kind", "quiet", approval_source="kind", approval_kind=facts.following_kind)
    return PageState("needs_test", "quiet", primary_action="make_sample")


def folder_page_state(
        connection: DBClient,
        config: MediaforceConfig,
        prefix: str,
        workflow: Mapping[str, Any] | None,
        sources: PageStateSources,
        *,
        staged_integrity: Mapping[str, Any] | None = None,
        lifecycle: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The page state for one folder page."""
    facts = _facts(
        connection,
        config,
        prefix,
        workflow,
        sources,
        sample_job=load_latest_job(connection, prefix),
        load_calibration=cached_calibration_loader(sources.load_calibration_state),
        review_blocked=_review_blocked_checker(config, sources.load_advice_state),
        staged_integrity=staged_integrity,
        lifecycle=lifecycle,
    )
    return decide_page_state(facts).to_payload()


def card_page_states(
        connection: DBClient,
        config: MediaforceConfig,
        cards: Iterable[tuple[str, Mapping[str, Any] | None, Mapping[str, Any] | None]],
        sources: PageStateSources,
) -> dict[str, dict[str, Any]]:
    """The page state each library card shows, as its folder page decides it, keyed by prefix.

    Cards carry no staged-file summary, so a card ready to finish does not check for blocked files.
    """
    card_list = list(cards)
    latest_jobs = load_latest_jobs_for_prefixes(connection, [prefix for prefix, _workflow, _lifecycle in card_list])
    load_calibration = cached_calibration_loader(sources.load_calibration_state)
    review_blocked = _review_blocked_checker(config, sources.load_advice_state)
    return {
        prefix: decide_page_state(_facts(
            connection,
            config,
            prefix,
            workflow,
            sources,
            sample_job=latest_jobs.get(prefix),
            load_calibration=load_calibration,
            review_blocked=review_blocked,
            lifecycle=lifecycle,
        )).to_payload()
        for prefix, workflow, lifecycle in card_list
    }


def _facts(
        connection: DBClient,
        config: MediaforceConfig,
        prefix: str,
        workflow: Mapping[str, Any] | None,
        sources: PageStateSources,
        *,
        sample_job: Mapping[str, Any] | None,
        load_calibration: LoadCalibrationState,
        review_blocked: Callable[[EffectiveApproval], bool],
        staged_integrity: Mapping[str, Any] | None = None,
        lifecycle: Mapping[str, Any] | None = None,
) -> PageStateFacts:
    source_job_id = str(object_dict(workflow).get("source_job_id") or "")
    approval = effective_production_approval(config, prefix, load_calibration)
    return PageStateFacts(
        prefix=prefix,
        workflow=workflow,
        encode_job=load_encode_job(connection, source_job_id) if source_job_id else None,
        sample_job=sample_job if _is_sample_job(sample_job) else None,
        calibration=load_calibration(config, prefix),
        approval=approval,
        following_kind=following_kind(config, prefix, load_calibration),
        approval_review_blocked=review_blocked(approval) if approval is not None and approval.current else False,
        staged_integrity=staged_integrity,
        lifecycle=lifecycle,
    )


def _review_blocked_checker(
        config: MediaforceConfig,
        load_advice_state: LoadAdviceState,
) -> Callable[[EffectiveApproval], bool]:
    """The review check queueing makes before it starts a run under an approval, once per approval."""
    blocked_by_prefix: dict[str, bool] = {}

    def blocked(approval: EffectiveApproval) -> bool:
        if approval.prefix not in blocked_by_prefix:
            calibration = approval.calibration
            advice = object_dict(load_advice_state(config, approval.prefix))
            contract = build_quality_risk_contract(
                prefix=approval.prefix,
                sample_item=object_dict(calibration.get("sample_item")),
                current_policy=object_dict(calibration.get("policy")),
                preview_policy=object_dict(calibration.get("policy")),
                operator_request=object_dict(advice.get("operator_request")) or None,
                calibration=calibration,
                advice_state=advice,
            )
            blocked_by_prefix[approval.prefix] = quality_risk_blocks_production(contract)
        return blocked_by_prefix[approval.prefix]

    return blocked


def _is_sample_job(job: Mapping[str, Any] | None) -> bool:
    return job is not None and str(job.get("lane") or job.get("mode") or "sample") == "sample"


def _source_job(job: Mapping[str, Any], kind: Literal["sample", "encode"]) -> SourceJob | None:
    job_id = str(job.get("job_id") or "")
    if not job_id:
        return None
    return SourceJob(job_id, kind, str(job.get("prefix") or ""), str(job.get("status") or ""))


def _calibration_source(
        prefix: str,
        calibration: Mapping[str, Any] | None,
        sample_job: Mapping[str, Any],
) -> SourceJob | None:
    job_id = str(object_dict(calibration).get("job_id") or "")
    if not job_id:
        return None
    if str(sample_job.get("job_id") or "") == job_id:
        return _source_job(sample_job, "sample")
    return SourceJob(job_id, "sample", prefix, "saved")


def _timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _sample_time(job: Mapping[str, Any]) -> datetime | None:
    """When the owner asked for a sample still in progress, or when a finished one ended."""
    if str(job.get("status") or "") in SAMPLE_RUNNING_STATUSES | SAMPLE_WAITING_STATUSES:
        return _timestamp(job.get("created_at"))
    return _timestamp(job.get("finished_at") or job.get("updated_at") or job.get("created_at"))


def _sample_answers_stop(sample_job: Mapping[str, Any], encode_job: Mapping[str, Any]) -> bool:
    sample_at = _sample_time(sample_job) if sample_job else None
    stopped_at = _timestamp(
        encode_job.get("finished_at") or encode_job.get("last_failure_at") or encode_job.get("updated_at")
    )
    return sample_at is not None and stopped_at is not None and sample_at > stopped_at


def _settled_by_approval(sample_job: Mapping[str, Any], calibration: Mapping[str, Any] | None) -> bool:
    """An approval saved after a sample stopped answers it, as queueing already treats it."""
    accepted_at = _timestamp(object_dict(calibration).get("accepted_at"))
    failed_at = _sample_time(sample_job)
    return accepted_at is not None and failed_at is not None and failed_at < accepted_at


def _review_availability(
        calibration: Mapping[str, Any] | None,
        gate: Mapping[str, Any] | None,
) -> tuple[bool, bool]:
    """Whether the saved sample can be compared in the browser, and whether it needs to be made again."""
    if calibration is None:
        return False, False
    pairs = [
        pair
        for pair in map(object_dict, object_list(calibration.get("review_pairs")))
        if object_dict(pair.get("source_clip")).get("path") and object_dict(pair.get("preview_clip")).get("path")
    ]
    browser_ready = calibration.get("browser_review_ready") is True
    if pairs and browser_ready:
        return True, False
    purged = any(
        calibration.get(flag) is True
        for flag in ("compare_clips_purged", "preview_clips_purged", "source_clips_purged")
    )
    download_ready = bool(pairs) or any(
        object_dict(clip).get("path") for clip in object_list(calibration.get("compare_clips"))
    )
    recovery = (
            purged
            or download_ready
            or browser_ready
            or calibration.get("review_media_ready") is True
            or object_dict(gate).get("status") == "missing_review_media"
    )
    return False, recovery


def _motion_held_state(lifecycle: Mapping[str, Any] | None, workflow: Mapping[str, Any]) -> PageState | None:
    """``motion_held`` when nothing the page offers would take or start a check for any file, and some
    file stays original for its motion pattern; None otherwise.

    What the page offers is queueing the scope, an override of its lifecycle holds when it has one,
    and a show's older-seasons action. A scope without a lifecycle payload reads its workflow counts.
    """
    if lifecycle is None:
        counts = object_dict(workflow.get("counts"))
        startable = int(counts.get("encode_candidates") or 0)
        held_for_motion = int(counts.get("motion_held") or 0)
        as_is = 0
    else:
        older = object_dict(lifecycle.get("older_seasons"))
        protected_startable = (
            int(lifecycle.get("held_candidate_count") or 0)
            - int(lifecycle.get("protected_motion_held_candidate_count") or 0)
            if lifecycle.get("can_override_holds")
            else 0
        )
        startable = (
            int(lifecycle.get("eligible_candidate_count") or 0)
            + int(lifecycle.get("motion_check_candidate_count") or 0)
            + protected_startable
            + int(older.get("candidate_count") or 0)
            + int(older.get("cadence_check_candidate_count") or 0)
        )
        held_for_motion = (
            int(lifecycle.get("motion_held_candidate_count") or 0)
            + int(lifecycle.get("protected_motion_held_candidate_count") or 0)
        )
        as_is = int(lifecycle.get("motion_as_is_candidate_count") or 0)
    if startable or not held_for_motion:
        return None
    if as_is:
        return PageState("motion_held", "attention", primary_action="decide_motion")
    return PageState("motion_held", "quiet")


def _workflow_action(workflow: Mapping[str, Any], action: PageAction) -> PageAction | None:
    next_action = object_dict(workflow.get("next_action"))
    return action if next_action.get("enabled") is not False else None


def _staged_files_block_finish(staged_integrity: Mapping[str, Any] | None) -> bool:
    summary = object_dict(staged_integrity)
    return bool(summary.get("database_truncated")) or int(summary.get("blocker_count") or 0) > 0


def _sample_failure(job: Mapping[str, Any]) -> FailureKind:
    target_failure = sample_target_size_failure(dict(job))
    if target_failure is not None:
        return _SAMPLE_TARGET_FAILURES[target_failure]
    return _failure_from_error(job.get("error"))


def _encode_failure(job: Mapping[str, Any]) -> FailureKind:
    error = str(job.get("error") or "")
    if any(reason in error for reason in _ENCODE_QUALITY_FLOOR_REASONS):
        return "quality_conflict"
    return _failure_from_error(error)


def _failure_from_error(error: Any) -> FailureKind:
    normalized = str(error or "").strip().lower()
    if not normalized:
        return "unexplained"
    if "restart" in normalized or "interrupted" in normalized:
        return "restarted"
    if "permission" in normalized or "denied" in normalized:
        return "no_access"
    if "final output size missed the approved target band" in normalized:
        if "status=under_target" in normalized:
            return "size_under_target"
        if "status=over_target" in normalized:
            return "size_over_target"
        return "size_unverified"
    if "suitable crf" in normalized or "quality target" in normalized:
        return "goal_too_small"
    if "stopped" in normalized:
        return "stopped"
    return "other"
