import argparse
import json
from pathlib import Path
import sys
from typing import Sequence, TypeAlias

from mediaforce.core.config import DEFAULT_CONFIG_PATH, load_config
from mediaforce.core.db import open_readonly_db
from mediaforce.tuning.av1_cold_start_evaluation import (
    AV1ColdStartValidationError,
    AV1ColdStartValidationManifestV1,
    assert_preregistered_av1_cold_start_validation_manifest,
    build_av1_cold_start_validation_report,
    format_av1_cold_start_validation_report,
    load_av1_cold_start_validation_evidence_set,
    load_av1_cold_start_validation_manifest,
)
from mediaforce.tuning.av1_validation_v2 import (
    AV1ValidationManifestV2,
    AV1ValidationV2Error,
    assert_preregistered_av1_validation_manifest_v2,
    assert_preregistered_av1_validation_v2_eligibility,
    load_av1_validation_manifest_v2,
    load_av1_validation_v2_eligibility,
)
from mediaforce.tuning.av1_validation_partition import (
    AV1ValidationPartitionError,
    assert_private_artifact_path,
    av1_validation_partition_key_id,
    av1_validation_partition_public_summary,
    build_av1_validation_private_partition,
    create_av1_validation_partition_key,
    load_av1_validation_partition_key,
    load_av1_validation_private_partition,
    validate_av1_validation_partition_current_inputs,
    validate_av1_validation_private_partition,
    write_av1_validation_private_partition,
)
from mediaforce.tuning.av1_validation_partition_inventory import (
    load_av1_validation_partition_inventory,
)


ValidationManifest: TypeAlias = (
    AV1ColdStartValidationManifestV1 | AV1ValidationManifestV2
)
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate an AV1 cold-start preregistration or aggregate its redacted evidence",
    )
    actions = parser.add_subparsers(dest="action", required=True)

    validate = actions.add_parser(
        "validate", help="Validate one canonical preregistration manifest"
    )
    validate.add_argument("manifest", type=Path)
    validate.add_argument("--json", action="store_true", dest="json_output")

    eligibility = actions.add_parser(
        "validate-eligibility",
        help="Validate the pinned machine-local v2 aggregate eligibility attestation",
    )
    eligibility.add_argument("attestation", type=Path)
    eligibility.add_argument("--json", action="store_true", dest="json_output")

    create_key = actions.add_parser(
        "create-partition-key",
        help="Create one owner-only machine-local key for the private v2 source partition",
    )
    create_key.add_argument("key", type=Path)
    create_key.add_argument("--json", action="store_true", dest="json_output")

    build_partition = actions.add_parser(
        "build-partition",
        help="Build the private v2 source partition without authorizing execution",
    )
    build_partition.add_argument("manifest", type=Path)
    build_partition.add_argument("eligibility", type=Path)
    build_partition.add_argument("--key", type=Path, required=True)
    build_partition.add_argument("--expected-token-key-id", required=True)
    build_partition.add_argument("--output", type=Path, required=True)
    build_partition.add_argument("--selected-at", required=True)
    build_partition.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    build_partition.add_argument("--json", action="store_true", dest="json_output")

    validate_partition = actions.add_parser(
        "validate-partition",
        help="Validate the exact private v2 source partition against current locked inputs",
    )
    validate_partition.add_argument("manifest", type=Path)
    validate_partition.add_argument("eligibility", type=Path)
    validate_partition.add_argument("partition", type=Path)
    validate_partition.add_argument("--key", type=Path, required=True)
    validate_partition.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    validate_partition.add_argument("--json", action="store_true", dest="json_output")

    report = actions.add_parser(
        "report", help="Build a deterministic aggregate acceptance report"
    )
    report.add_argument("manifest", type=Path)
    report.add_argument("evidence", type=Path)
    report.add_argument("--as-of", required=True)
    report.add_argument(
        "--runtime-state", choices=("paused", "available"), default="paused"
    )
    report.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action == "create-partition-key":
            assert_private_artifact_path(args.key, repository_root=REPOSITORY_ROOT)
            token_key_id = create_av1_validation_partition_key(args.key)
            payload = {
                "created": True,
                "token_key_id": token_key_id,
                "runtime_execution_authorized": False,
                "derivation_execution_authorized": False,
                "holdout_execution_authorized": False,
            }
            _print_partition_payload(payload, json_output=args.json_output)
            return 0

        if args.action in {"build-partition", "validate-partition"}:
            return _run_partition_action(args)

        if args.action == "validate-eligibility":
            assert_private_artifact_path(
                args.attestation,
                repository_root=REPOSITORY_ROOT,
            )
            eligibility = load_av1_validation_v2_eligibility(args.attestation)
            assert_preregistered_av1_validation_v2_eligibility(eligibility)
            payload = {
                "eligibility_valid": True,
                "runtime_execution_authorized": False,
                "derivation_execution_authorized": False,
                "holdout_execution_authorized": False,
            }
            _print_partition_payload(payload, json_output=args.json_output)
            return 0

        manifest = _load_manifest(args.manifest)
        if isinstance(manifest, AV1ValidationManifestV2):
            assert_preregistered_av1_validation_manifest_v2(manifest)
        else:
            assert_preregistered_av1_cold_start_validation_manifest(manifest)
        if args.action == "validate":
            payload = _validation_payload(manifest)
            if args.json_output:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(
                    f"manifest={manifest.manifest_id} state={payload['state']} "
                    f"plans={len(manifest.cell_plans)} cases={len(manifest.cases)} "
                    "runtime_execution_authorized=false "
                    f"holdout_execution_authorized={str(payload['holdout_execution_authorized']).lower()}"
                )
            return 0

        if isinstance(manifest, AV1ValidationManifestV2):
            raise AV1ValidationV2Error(
                "AV1 v2 holdout reports remain blocked until a separate execution authorization exists"
            )
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
    except OSError:
        print(
            "AV1 cold-start validation failed: private or repository input is unreadable",
            file=sys.stderr,
        )
        return 1
    except (
        AV1ColdStartValidationError,
        AV1ValidationPartitionError,
        AV1ValidationV2Error,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as exc:
        print(f"AV1 cold-start validation failed: {exc}", file=sys.stderr)
        return 1


def _run_partition_action(args: argparse.Namespace) -> int:
    manifest = load_av1_validation_manifest_v2(args.manifest)
    assert_preregistered_av1_validation_manifest_v2(manifest)
    assert_private_artifact_path(args.eligibility, repository_root=REPOSITORY_ROOT)
    assert_private_artifact_path(args.key, repository_root=REPOSITORY_ROOT)
    eligibility = load_av1_validation_v2_eligibility(args.eligibility)
    assert_preregistered_av1_validation_v2_eligibility(eligibility)
    token_key = load_av1_validation_partition_key(args.key)
    if args.action == "build-partition":
        assert_private_artifact_path(args.output, repository_root=REPOSITORY_ROOT)
        config = load_config(args.config)
        with open_readonly_db(config.paths.db_path) as connection:
            inventory = load_av1_validation_partition_inventory(
                connection,
                config=config,
            )
        partition = build_av1_validation_private_partition(
            manifest=manifest,
            eligibility_attestation_id=eligibility.attestation_id,
            eligibility_payload_sha256=eligibility.payload_sha256,
            sources=inventory.sources,
            expectations=inventory.expectations,
            token_key=token_key,
            expected_token_key_id=args.expected_token_key_id,
            selected_at=args.selected_at,
        )
        write_av1_validation_private_partition(args.output, partition)
    else:
        assert_private_artifact_path(args.partition, repository_root=REPOSITORY_ROOT)
        partition = load_av1_validation_private_partition(args.partition)
        if av1_validation_partition_key_id(token_key) != partition.token_key_id:
            raise AV1ValidationPartitionError(
                "AV1 partition key does not match the frozen token-key ID"
            )
        validate_av1_validation_private_partition(
            partition,
            manifest=manifest,
            token_key=token_key,
        )
        config = load_config(args.config)
        with open_readonly_db(config.paths.db_path) as connection:
            inventory = load_av1_validation_partition_inventory(
                connection,
                config=config,
            )
        validate_av1_validation_partition_current_inputs(
            partition,
            manifest=manifest,
            sources=inventory.sources,
            expectations=inventory.expectations,
            token_key=token_key,
        )
    _print_partition_payload(
        av1_validation_partition_public_summary(partition),
        json_output=args.json_output,
    )
    return 0


def _print_partition_payload(payload: dict[str, object], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    fields = [
        f"{key}={str(value).lower() if isinstance(value, bool) else value}"
        for key, value in payload.items()
    ]
    print(" ".join(fields))


def _load_manifest(path: Path) -> ValidationManifest:
    payload = json.loads(path.read_bytes())
    if not isinstance(payload, dict):
        raise AV1ColdStartValidationError(
            "AV1 validation manifest must be a JSON object"
        )
    schema_version = payload.get("schema_version")
    if schema_version == 1:
        return load_av1_cold_start_validation_manifest(path)
    if schema_version == 2:
        return load_av1_validation_manifest_v2(path)
    raise AV1ColdStartValidationError(
        "AV1 validation manifest schema version is unsupported"
    )


def _validation_payload(manifest: ValidationManifest) -> dict[str, object]:
    if isinstance(manifest, AV1ValidationManifestV2):
        return {
            "manifest_id": manifest.manifest_id,
            "schema_version": 2,
            "state": "preregistered_derivation_only",
            "authority": "derivation_only",
            "cell_plan_count": len(manifest.cell_plans),
            "candidate_cell_count": sum(
                plan.mode == "publication_candidate" for plan in manifest.cell_plans
            ),
            "fallback_cell_count": sum(
                plan.mode == "fallback_conformance" for plan in manifest.cell_plans
            ),
            "excluded_cell_count": len(manifest.excluded_cells),
            "registered_case_count": len(manifest.cases),
            "runtime_execution_authorized": False,
            "holdout_execution_authorized": False,
        }
    return {
        "manifest_id": manifest.manifest_id,
        "schema_version": 1,
        "state": manifest.state,
        "cell_plan_count": len(manifest.cell_plans),
        "registered_case_count": len(manifest.cases),
        "runtime_execution_authorized": False,
        "holdout_execution_authorized": False,
    }


if __name__ == "__main__":
    raise SystemExit(main())
