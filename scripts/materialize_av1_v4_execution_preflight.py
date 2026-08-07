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
from mediaforce.tuning.av1_validation_v4_execution_preflight_operation import (
    AV1ValidationV4ExecutionPreflightOperationError,
    AV1ValidationV4ExecutionPreflightOperationInputs,
    materialize_av1_validation_v4_execution_preflight,
)


class _CliUsageError(ValueError):
    pass


class _JsonOnlyArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _CliUsageError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _JsonOnlyArgumentParser(
        description=(
            "Materialize the AV1 v4 production execution readiness preflight."
        ),
        add_help=False,
    )
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--freeze", type=Path, required=True)
    parser.add_argument("--rights-attestation", type=Path, required=True)
    parser.add_argument("--preparation-grant", type=Path, required=True)
    parser.add_argument("--effective-config", type=Path, required=True)
    parser.add_argument("--preparation", type=Path, required=True)
    parser.add_argument("--preparation-measurement", type=Path, required=True)
    parser.add_argument("--qualification-request", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        result = materialize_av1_validation_v4_execution_preflight(
            AV1ValidationV4ExecutionPreflightOperationInputs(
                repository_root=args.repository_root,
                registry=args.registry,
                manifest_path=args.manifest,
                freeze_path=args.freeze,
                rights_attestation_path=args.rights_attestation,
                preparation_grant_path=args.preparation_grant,
                effective_config_path=args.effective_config,
                preparation_path=args.preparation,
                preparation_measurement_path=args.preparation_measurement,
                qualification_request_path=args.qualification_request,
            )
        )
    except Exception as exc:
        print(_json({"ok": False, "error": _sanitize_error(exc)}))
        return 1
    print(_json(_summary(result.preflight, created=result.created)))
    return 0


def _summary(preflight: Mapping[str, Any], *, created: bool) -> dict[str, Any]:
    return {
        "ok": True,
        "created": created,
        "preflight_id": preflight["preflight_id"],
        "payload_sha256": preflight["payload_sha256"],
        "state": preflight["state"],
        "execution_ready": preflight["execution_ready"],
        "all_invocation_digests_match": preflight["all_invocation_digests_match"],
        "all_authority_fields_false": preflight["all_authority_fields_false"],
        "blockers": list(preflight["blockers"]),
        "plan_count": len(preflight["plans"]),
        "request_id": preflight["request_id"],
        "freeze_id": preflight["freeze_id"],
        "preparation_id": preflight["preparation_id"],
        "execution_repository_commit": preflight["execution_repository"]["commit"],
        "execution_repository_tree": preflight["execution_repository"]["tree"],
    }


def _sanitize_error(exc: Exception) -> str:
    if isinstance(exc, AV1ValidationV4ExecutionPreflightOperationError):
        message = str(exc)
        if message and not av1_validation_v4_contains_private_text(message):
            return message
    return f"{type(exc).__name__}: execution preflight failed"


def _json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


if __name__ == "__main__":
    raise SystemExit(main())
