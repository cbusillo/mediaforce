import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from mediaforce.tuning.av1_cold_start_evaluation import (
    AV1ColdStartValidationError,
    assert_preregistered_av1_cold_start_validation_manifest,
    build_av1_cold_start_validation_report,
    format_av1_cold_start_validation_report,
    load_av1_cold_start_validation_evidence_set,
    load_av1_cold_start_validation_manifest,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate an AV1 cold-start preregistration or aggregate its redacted evidence",
    )
    actions = parser.add_subparsers(dest="action", required=True)

    validate = actions.add_parser("validate", help="Validate one canonical preregistration manifest")
    validate.add_argument("manifest", type=Path)
    validate.add_argument("--json", action="store_true", dest="json_output")

    report = actions.add_parser("report", help="Build a deterministic aggregate acceptance report")
    report.add_argument("manifest", type=Path)
    report.add_argument("evidence", type=Path)
    report.add_argument("--as-of", required=True)
    report.add_argument("--runtime-state", choices=("paused", "available"), default="paused")
    report.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = load_av1_cold_start_validation_manifest(args.manifest)
        assert_preregistered_av1_cold_start_validation_manifest(manifest)
        if args.action == "validate":
            payload = {
                "manifest_id": manifest.manifest_id,
                "state": manifest.state,
                "cell_plan_count": len(manifest.cell_plans),
                "registered_case_count": len(manifest.cases),
                "runtime_execution_authorized": False,
            }
            if args.json_output:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(
                    f"manifest={manifest.manifest_id} state={manifest.state} "
                    f"plans={len(manifest.cell_plans)} cases={len(manifest.cases)} "
                    "runtime_execution_authorized=false"
                )
            return 0

        evidence_set = load_av1_cold_start_validation_evidence_set(args.evidence)
        report = build_av1_cold_start_validation_report(
            manifest,
            evidence_set,
            as_of=args.as_of,
            runtime_state=args.runtime_state,
        )
        if args.json_output:
            print(json.dumps(report.to_payload(), indent=2, sort_keys=True))
        else:
            print(format_av1_cold_start_validation_report(report))
        return 0 if report.supports_publication_review else 2
    except (AV1ColdStartValidationError, OSError) as exc:
        print(f"AV1 cold-start validation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
