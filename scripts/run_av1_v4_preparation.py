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
from mediaforce.tuning.av1_validation_v4_preparation_operation import (
    default_av1_validation_v4_preparation_operation_inputs,
    run_av1_validation_v4_preparation_operation,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Consume one AV1 v4 preparation grant and create a non-media "
            "prepared-unfrozen bundle."
        ),
    )
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--rights-attestation", type=Path, required=True)
    parser.add_argument("--preparation-grant", type=Path, required=True)
    parser.add_argument("--repository-commit", required=True)
    parser.add_argument("--repository-tree", required=True)
    parser.add_argument(
        "--source-path",
        action="append",
        default=[],
        metavar="ASSET_ID=ABSOLUTE_PATH",
    )
    parser.add_argument(
        "--instance-path",
        action="append",
        default=[],
        metavar="ROLE=ABSOLUTE_PATH",
    )
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--ffprobe", type=Path, required=True)
    parser.add_argument("--ab-av1", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        source_paths = _parse_path_mapping(args.source_path, "source path")
        instance_paths = _parse_path_mapping(args.instance_path, "instance path")
        inputs = default_av1_validation_v4_preparation_operation_inputs(
            workspace=args.workspace,
            manifest_path=args.manifest,
            rights_attestation_path=args.rights_attestation,
            preparation_grant_path=args.preparation_grant,
            repository_commit=args.repository_commit,
            repository_tree=args.repository_tree,
            source_paths=source_paths,
            dedicated_instance_paths=instance_paths,
            ffmpeg_path=args.ffmpeg,
            ffprobe_path=args.ffprobe,
            ab_av1_path=args.ab_av1,
        )
        result = run_av1_validation_v4_preparation_operation(inputs)
    except Exception as exc:
        message = str(exc)
        if av1_validation_v4_contains_private_text(message):
            message = f"{type(exc).__name__}: preparation failed"
        if args.json:
            print(json.dumps({"ok": False, "error": message}, sort_keys=True))
        else:
            print(message, file=sys.stderr)
        return 1
    summary = {
        "ok": True,
        "claim_id": result.claim["claim_id"],
        "measurement_id": result.measurement["measurement_id"],
        "preparation_id": result.preparation["preparation_id"],
        "state": result.preparation["state"],
        "media_bytes_read_count": result.preparation["media_bytes_read_count"],
        "qualification_execution_authorized": result.preparation[
            "qualification_execution_authorized"
        ],
        "manifest_freeze_authorized": result.preparation[
            "manifest_freeze_authorized"
        ],
    }
    if args.json:
        print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    else:
        print(
            "AV1 v4 preparation complete: "
            f"{summary['preparation_id']} ({summary['state']})"
        )
    return 0


def _parse_path_mapping(values: Sequence[str], label: str) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        key, separator, raw_path = value.partition("=")
        if not separator or not key or not raw_path or key in result:
            raise ValueError(f"invalid or duplicate {label} mapping")
        result[key] = Path(raw_path)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
