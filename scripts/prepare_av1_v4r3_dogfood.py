#!/usr/bin/env python3
from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from pathlib import Path
import sys
from typing import Any

from mediaforce.tuning.av1_validation_v4r3_dogfood_preparation import (
    AV1V4R3DogfoodPreparationError,
    prepare_av1_v4r3_dogfood,
)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        if argv:
            raise ValueError("arguments are not supported")
        payload = json.loads(sys.stdin.buffer.read())
        if not isinstance(payload, dict):
            raise ValueError("request must be an object")
        result = prepare_av1_v4r3_dogfood(
            repository_root=Path(str(payload["repository_root"])),
            preparation_registry=Path(str(payload["preparation_registry"])),
            ordinal_registry=Path(str(payload["ordinal_registry"])),
            rights_attestation=_mapping(payload["rights_attestation"]),
            owner_principal=str(payload["owner_principal"]),
            confirmed_owner_principal=str(payload["confirm_owner_principal"]),
            preparation_grant_valid_until=str(payload["preparation_grant_valid_until"]),
            ordinal_1_valid_until=str(payload["ordinal_1_valid_until"]),
            source_paths=_text_mapping(payload["source_paths"]),
            dedicated_instance_paths=_text_mapping(payload["dedicated_instance_paths"]),
            quality_temp_paths=_text_mapping(payload["quality_temp_paths"]),
            tool_paths={
                key: Path(value)
                for key, value in _text_mapping(payload["tool_paths"]).items()
            },
        )
    except BaseException as exc:
        print(_json({"ok": False, "error": _error(exc)}))
        return 1
    print(_json(_summary(result)))
    return 0


def _summary(result: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "state": "ready_for_execution_grant_decision",
        "created": dict(result.created),
        "preparation_grant_id": result.grant["grant_id"],
        "preparation_claim_id": result.claim["claim_id"],
        "key_custody_id": result.custody["custody_id"],
        "preparation_bundle_id": result.bundle["bundle_id"],
        "preparation_measurement_id": result.measurement["measurement_id"],
        "effective_config_hmac_id": result.bundle["effective_config_hmac_id"],
        "freeze_id": result.freeze["freeze_id"],
        "request_id": result.request["request_id"],
        "plan_id": result.plan["plan_id"],
        "preflight_id": result.preflight["preflight_id"],
        "ordinal_1_grant_id": result.ordinal_grant["grant_id"],
        "plan_closes_at": result.plan["plan_closes_at"],
        "ordinal_1_valid_until": result.ordinal_grant["valid_until"],
        "media_read_authorized": False,
        "qualification_execution_authorized": False,
        "runtime_execution_authorized": False,
        "dogfood_authorized": False,
    }


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("mapping required")
    return dict(value)


def _text_mapping(value: Any) -> dict[str, str]:
    mapping = _mapping(value)
    if any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in mapping.items()
    ):
        raise ValueError("text mapping required")
    return {str(key): str(item) for key, item in mapping.items()}


def _error(exc: BaseException) -> str:
    if isinstance(exc, AV1V4R3DogfoodPreparationError):
        return str(exc)
    return "AV1 v4 r3 dogfood preparation failed"


def _json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


if __name__ == "__main__":
    raise SystemExit(main())
