#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import json
from pathlib import Path
import sys
from typing import Any

from mediaforce.tuning.av1_validation_v4 import (
    av1_validation_v4_contains_private_text,
)
from mediaforce.tuning.av1_validation_v4_qualification_request_operation import (
    AV1ValidationV4QualificationRequestOperationError,
    AV1ValidationV4QualificationRequestOperationInputs,
    materialize_av1_validation_v4_qualification_request,
)


_SUMMARY_AUTHORITY_FIELDS = (
    "qualification_execution_authorized",
    "runtime_execution_authorized",
    "media_read_authorized",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Materialize one non-authorizing AV1 v4 qualification request."
        ),
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
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = materialize_av1_validation_v4_qualification_request(
            AV1ValidationV4QualificationRequestOperationInputs(
                repository_root=args.repository_root,
                registry=args.registry,
                manifest_path=args.manifest,
                freeze_path=args.freeze,
                rights_attestation_path=args.rights_attestation,
                preparation_grant_path=args.preparation_grant,
                effective_config_path=args.effective_config,
                preparation_path=args.preparation,
                preparation_measurement_path=args.preparation_measurement,
            )
        )
    except Exception as exc:
        message = _sanitize_error(exc)
        if args.json:
            print(json.dumps({"ok": False, "error": message}, sort_keys=True))
        else:
            print(message, file=sys.stderr)
        return 1
    summary = _summary(result.request, created=result.created)
    if args.json:
        print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    else:
        print(
            "AV1 v4 qualification request materialized: "
            f"{result.request['request_id']}"
        )
    return 0


def _summary(request: Mapping[str, Any], *, created: bool) -> dict[str, Any]:
    return {
        "ok": True,
        "created": created,
        "request_id": request["request_id"],
        "payload_sha256": request["payload_sha256"],
        "state": request["state"],
        "freeze_id": request["freeze_id"],
        "requested_at": request["requested_at"],
        "valid_until": request["valid_until"],
        "preparation_repository_commit": request["preparation_repository"]["commit"],
        "preparation_repository_tree": request["preparation_repository"]["tree"],
        "execution_repository_commit": request["execution_repository"]["commit"],
        "execution_repository_tree": request["execution_repository"]["tree"],
        **{field: request[field] for field in _SUMMARY_AUTHORITY_FIELDS},
    }


def _sanitize_error(exc: Exception) -> str:
    if isinstance(exc, AV1ValidationV4QualificationRequestOperationError):
        message = str(exc)
        if message and not av1_validation_v4_contains_private_text(message):
            return message
    return f"{type(exc).__name__}: qualification request materialization failed"


if __name__ == "__main__":
    raise SystemExit(main())
