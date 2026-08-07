#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
from pathlib import Path
import sys

from mediaforce.tuning.av1_validation_v4 import (
    av1_validation_v4_contains_private_text,
)
from mediaforce.tuning.av1_validation_v4_freeze_operation import (
    AV1ValidationV4FreezeOperationInputs,
    materialize_av1_validation_v4_manifest_freeze,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Materialize one owner-approved AV1 v4 manifest revision-2 freeze."
        ),
    )
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
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
        result = materialize_av1_validation_v4_manifest_freeze(
            AV1ValidationV4FreezeOperationInputs(
                repository_root=args.repository_root,
                registry=args.registry,
                manifest_path=args.manifest,
                rights_attestation_path=args.rights_attestation,
                preparation_grant_path=args.preparation_grant,
                effective_config_path=args.effective_config,
                preparation_path=args.preparation,
                preparation_measurement_path=args.preparation_measurement,
            )
        )
    except Exception as exc:
        message = str(exc)
        if av1_validation_v4_contains_private_text(message):
            message = f"{type(exc).__name__}: freeze materialization failed"
        if args.json:
            print(json.dumps({"ok": False, "error": message}, sort_keys=True))
        else:
            print(message, file=sys.stderr)
        return 1
    freeze = result.freeze
    summary = {
        "ok": True,
        "freeze_id": freeze["freeze_id"],
        "payload_sha256": freeze["payload_sha256"],
        "state": freeze["state"],
        "manifest_revision_2_owner_freeze_approved": freeze[
            "manifest_revision_2_owner_freeze_approved"
        ],
        "manifest_freeze_authorized": freeze["manifest_freeze_authorized"],
        "qualification_execution_authorized": freeze[
            "qualification_execution_authorized"
        ],
    }
    if args.json:
        print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    else:
        print(f"AV1 v4 manifest freeze materialized: {freeze['freeze_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
