import re
from typing import Any

from mediaforce.advising.failures import ASSISTANT_FAILURE_CODES
from mediaforce.core.type_defs import object_dict


ASSISTANT_FAILURE_KINDS = frozenset({"assistant_unavailable", "assistant_invalid_response"})
_ASSISTANT_FAILURE_TRACE_PATTERN = re.compile(
    rf"(?:^|\n)attempt \d+: ({'|'.join(sorted(ASSISTANT_FAILURE_CODES))})(?::|$)",
    re.IGNORECASE,
)


def proposal_recovery(
        payload: dict[str, Any],
        *,
        deterministic_detail: str | None = None,
        stale_plan: bool = False,
) -> dict[str, Any] | None:
    if stale_plan:
        return {
            "cause": "stale_plan",
            "headline": "Sample plan is out of date",
            "detail": (
                "Your compression goal changed after this plan was made. Nothing was queued. "
                "Prepare the sample again to use the current goal."
            ),
            "nothing_queued": True,
            "action": "prepare_again",
            "same_request_retryable": True,
        }
    if bool(payload.get("can_queue")):
        return None
    if has_assistant_failure(payload):
        return {
            "cause": "assistant_failure",
            "headline": "Mediaforce could not prepare this sample",
            "detail": "The assistant did not answer or returned unusable output. Your request and worker are unchanged.",
            "nothing_queued": True,
            "action": "prepare_again",
            "same_request_retryable": True,
        }
    resolved_deterministic_detail = str(deterministic_detail or "").strip()
    if resolved_deterministic_detail:
        return _deterministic_recovery(resolved_deterministic_detail)
    disposition = str(payload.get("request_disposition") or "").strip().lower()
    if disposition == "unclear":
        return {
            "cause": "unclear_request",
            "headline": "Clarify the sample request",
            "detail": "Mediaforce needs a clearer request before it can prepare a sample. Add the outcome or concern you want to test.",
            "nothing_queued": True,
            "action": "edit_request",
            "same_request_retryable": False,
        }
    detail = str(payload.get("message") or "").strip()
    return _deterministic_recovery(detail)


def _deterministic_recovery(detail: str) -> dict[str, Any]:
    return {
        "cause": "deterministic_blocker",
        "headline": "Sample plan is blocked",
        "detail": detail or "A current setting or evidence requirement blocks this sample plan.",
        "nothing_queued": True,
        "action": "change_request",
        "same_request_retryable": False,
    }


def has_assistant_failure(payload: dict[str, Any]) -> bool:
    if str(payload.get("failure_kind") or "").strip() in ASSISTANT_FAILURE_KINDS:
        return True
    if str(payload.get("request_disposition") or "").strip().lower() == "unavailable":
        return True
    trace = object_dict(payload.get("trace"))
    raw_response = str(trace.get("raw_response") or "").lower()
    return _ASSISTANT_FAILURE_TRACE_PATTERN.search(raw_response) is not None
