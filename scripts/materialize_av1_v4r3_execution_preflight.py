#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import json
from pathlib import Path
from typing import Any

from mediaforce.tuning.av1_validation_v4 import (
    av1_validation_v4_contains_private_text,
)
from mediaforce.tuning.av1_validation_v4r3_execution_preflight_operation import (
    AV1V4R3ExecutionPreflightOperationError,
    materialize_av1_v4r3_execution_preflight,
)
from mediaforce.tuning.av1_validation_v4r3_ordinal_window_registry import (
    AV1V4R3OrdinalWindowRegistryBinding,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_custody_registry import (
    AV1V4R3PreparationCustodyRegistryBinding,
)


class _CliUsageError(ValueError):
    pass


class _JsonOnlyArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _CliUsageError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _JsonOnlyArgumentParser(
        description=(
            "Materialize one non-authorizing AV1 v4 revision-3 ordinal "
            "plan/execution-preflight pair."
        ),
        add_help=False,
    )
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--preparation-registry", type=Path, required=True)
    parser.add_argument("--ordinal-registry", type=Path, required=True)
    parser.add_argument("--run-registry-id", required=True)
    parser.add_argument("--rights-attestation", type=Path, required=True)
    parser.add_argument("--owner-principal", required=True)
    parser.add_argument("--confirm-owner-principal", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        if args.confirm_owner_principal != args.owner_principal:
            raise _CliUsageError("owner confirmation does not match")
        rights_attestation = _load_mapping(args.rights_attestation)
        result = materialize_av1_v4r3_execution_preflight(
            preparation_binding=AV1V4R3PreparationCustodyRegistryBinding(
                registry=args.preparation_registry,
                repository_root=args.repository_root,
            ),
            ordinal_binding=AV1V4R3OrdinalWindowRegistryBinding(
                registry=args.ordinal_registry,
                run_registry_id=args.run_registry_id,
            ),
            rights_attestation=rights_attestation,
            owner_principal=args.owner_principal,
        )
    except Exception as exc:
        print(_json({"ok": False, "error": _sanitize_error(exc)}))
        return 1
    print(_json(_summary(result)))
    return 0


def _load_mapping(path: Path) -> Mapping[str, Any]:
    payload = json.loads(path.read_bytes())
    if not isinstance(payload, dict):
        raise ValueError("mapping required")
    return payload


def _summary(result: Any) -> dict[str, Any]:
    plan = result.plan
    preflight = result.preflight
    return {
        "ok": True,
        "plan_created": result.plan_created,
        "preflight_created": result.preflight_created,
        "plan_id": plan["plan_id"],
        "plan_payload_sha256": plan["payload_sha256"],
        "preflight_id": preflight["preflight_id"],
        "preflight_payload_sha256": preflight["payload_sha256"],
        "request_id": plan["r3_request_id"],
        "run_registry_id": plan["run_registry_id"],
        "execution_readiness_state": preflight["execution_readiness_state"],
        "media_read_authorized": preflight["media_read_authorized"],
        "runtime_execution_authorized": preflight["runtime_execution_authorized"],
        "dogfood_authorized": preflight["dogfood_authorized"],
    }


def _sanitize_error(exc: Exception) -> str:
    if isinstance(exc, AV1V4R3ExecutionPreflightOperationError):
        message = str(exc)
        if message and not av1_validation_v4_contains_private_text(message):
            return message
    if isinstance(exc, _CliUsageError):
        return "AV1 v4 r3 execution preflight owner confirmation is invalid"
    return "AV1 v4 r3 execution preflight materialization failed"


def _json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


if __name__ == "__main__":
    raise SystemExit(main())
