"""Recorded staged-file check outcomes shared by integrity and workflow projections."""

import json
from typing import Any, Literal

from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.encoding.staging import FAR_BELOW_PREDICTION_CHECK


def held_size_prediction(raw_validation: Any) -> dict[str, Any] | None:
    """The size comparison of a file whose only failed check is being far smaller than predicted."""
    try:
        payload = object_dict(json.loads(str(raw_validation or "{}")))
    except json.JSONDecodeError:
        return None
    size_prediction = object_dict(payload.get("size_prediction"))
    if payload.get("passed") is not False or not size_prediction.get("held"):
        return None
    failed = [
        str(object_dict(check).get("message") or "")
        for check in object_list(payload.get("checks"))
        if object_dict(check).get("passed") is False
    ]
    if failed != [FAR_BELOW_PREDICTION_CHECK]:
        return None
    return size_prediction


def staged_validation_outcome(raw_validation: Any) -> Literal["size_held", "failed"] | None:
    """Whether a staged file's recorded check waits on the owner, and why; None when it does not."""
    if held_size_prediction(raw_validation) is not None:
        return "size_held"
    if staged_validation_passed(raw_validation) is False:
        return "failed"
    return None


def staged_validation_passed(raw_validation: Any) -> bool | None:
    if not isinstance(raw_validation, str) or not raw_validation.strip():
        return None
    try:
        payload = json.loads(raw_validation)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("passed"), bool):
        return None
    return bool(payload["passed"])
