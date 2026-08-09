#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from mediaforce.tuning.av1_validation_v4r3_one_ordinal_runner import (
    AV1V4R3OneOrdinalRunnerError,
    run_av1_v4r3_one_ordinal,
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
        description="Execute one already-claimed AV1 v4 revision-3 ordinal.",
        add_help=False,
    )
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--preparation-registry", type=Path, required=True)
    parser.add_argument("--ordinal-registry", type=Path, required=True)
    parser.add_argument("--run-registry-id", required=True)
    parser.add_argument("--rights-attestation", type=Path, required=True)
    parser.add_argument("--ordinal", type=int, required=True)
    parser.add_argument("--owner-principal", required=True)
    parser.add_argument("--confirm-owner-principal", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        from mediaforce.execution import search_quality_for_source

        result = run_av1_v4r3_one_ordinal(
            preparation_binding=AV1V4R3PreparationCustodyRegistryBinding(
                registry=args.preparation_registry,
                repository_root=args.repository_root,
            ),
            ordinal_binding=AV1V4R3OrdinalWindowRegistryBinding(
                registry=args.ordinal_registry,
                run_registry_id=args.run_registry_id,
            ),
            rights_attestation=_load_mapping(args.rights_attestation),
            owner_principal=args.owner_principal,
            confirmed_owner_principal=args.confirm_owner_principal,
            ordinal=args.ordinal,
            search_quality_for_source=search_quality_for_source,
        )
    except BaseException as exc:
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
    admission = result.admission
    outcome = result.outcome
    return {
        "ok": True,
        "ordinal": result.ordinal,
        "asset_id": result.asset_id,
        "success": result.success,
        "admission_id": admission["admission_id"],
        "admission_payload_sha256": admission["payload_sha256"],
        "started_id": result.started["started_id"],
        "outcome_id": outcome["outcome_id"],
        "public_summary": result.public_summary,
        "dogfood_authorized": admission["dogfood_authorized"],
    }


def _sanitize_error(exc: BaseException) -> str:
    if isinstance(exc, AV1V4R3OneOrdinalRunnerError):
        return str(exc)
    if isinstance(exc, _CliUsageError):
        return "AV1 v4 r3 one-ordinal arguments are invalid"
    return "AV1 v4 r3 one-ordinal execution failed"


def _json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


if __name__ == "__main__":
    raise SystemExit(main())
