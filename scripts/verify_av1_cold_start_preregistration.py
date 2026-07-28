import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Sequence, TypeAlias
import uuid

from mediaforce.core.config import DEFAULT_CONFIG_PATH, MediaforceConfig, load_config
from mediaforce.core.db import open_readonly_db
from mediaforce.tuning.av1_cold_start import assert_av1_cold_start_public_payload_safe
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
    build_av1_validation_v2_derivation_authorization,
    load_av1_validation_manifest_v2,
    load_av1_validation_v2_eligibility,
)
from mediaforce.tuning.av1_validation_derivation import (
    AV1_VALIDATION_DERIVATION_AGENT_REVIEW_MARKER,
    AV1ValidationDerivationCandidateProposal,
    AV1ValidationDerivationError,
    AV1ValidationDerivationPlan,
    AV1ValidationDerivationReviewDecision,
    av1_validation_derivation_candidate_evaluation_public_summary,
    av1_validation_derivation_plan_public_summary,
    av1_validation_derivation_statistics_contract_sha256,
    build_av1_validation_derivation_plan,
    build_av1_validation_derivation_review_attestation,
    build_av1_validation_derivation_review_envelope,
    evaluate_av1_validation_derivation_candidate,
    load_av1_validation_derivation_attempts,
    load_av1_validation_derivation_candidate_proposal,
    load_av1_validation_derivation_plan,
    load_av1_validation_derivation_terminal_records,
    validate_av1_validation_derivation_artifact_root_binding,
    write_av1_validation_derivation_candidate_proposal,
    write_av1_validation_derivation_plan,
    write_av1_validation_derivation_review_envelope,
)
from mediaforce.web.runtime.av1_validation_derivation import (
    av1_validation_derivation_artifact_root,
    av1_validation_derivation_execution_environment_sha256,
    av1_validation_derivation_runtime_context_sha256,
    finalize_av1_validation_derivation_candidate_lock,
    load_current_av1_validation_derivation_observations,
    record_av1_validation_derivation_visual_verdict,
    run_av1_validation_derivation_assignment,
)
from mediaforce.tuning.av1_validation_partition import (
    AV1ValidationPartitionError,
    AV1ValidationPrivatePartition,
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
_AGENT_REVIEW_MARKER = AV1_VALIDATION_DERIVATION_AGENT_REVIEW_MARKER
_AGENT_REVIEW_MAX_SECONDS = 1800


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


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

    create_derivation_plan = actions.add_parser(
        "create-derivation-plan",
        help="Create the immutable owner-only v2 derivation authorization and 24-slot worklist",
    )
    create_derivation_plan.add_argument("manifest", type=Path)
    create_derivation_plan.add_argument("eligibility", type=Path)
    create_derivation_plan.add_argument("partition", type=Path)
    create_derivation_plan.add_argument("--key", type=Path, required=True)
    create_derivation_plan.add_argument("--valid-until", required=True)
    create_derivation_plan.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    create_derivation_plan.add_argument("--json", action="store_true", dest="json_output")

    validate_derivation_plan = actions.add_parser(
        "validate-derivation-plan",
        help="Validate the private v2 derivation plan against current locked inputs",
    )
    validate_derivation_plan.add_argument("manifest", type=Path)
    validate_derivation_plan.add_argument("eligibility", type=Path)
    validate_derivation_plan.add_argument("partition", type=Path)
    validate_derivation_plan.add_argument("plan", type=Path)
    validate_derivation_plan.add_argument("--key", type=Path, required=True)
    validate_derivation_plan.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    validate_derivation_plan.add_argument("--json", action="store_true", dest="json_output")

    run_derivation = actions.add_parser(
        "run-derivation-assignment",
        help="Run one exact authorized derivation assignment through unchanged measured full search",
    )
    run_derivation.add_argument("manifest", type=Path)
    run_derivation.add_argument("partition", type=Path)
    run_derivation.add_argument("plan", type=Path)
    run_derivation.add_argument("assignment_id")
    run_derivation.add_argument("--key", type=Path, required=True)
    run_derivation.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    run_derivation.add_argument("--json", action="store_true", dest="json_output")

    derivation_status = actions.add_parser(
        "derivation-status",
        help="Report privacy-safe derivation attempt and terminal counts",
    )
    derivation_status.add_argument("plan", type=Path)
    derivation_status.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    derivation_status.add_argument("--json", action="store_true", dest="json_output")

    record_verdict = actions.add_parser(
        "record-derivation-verdict",
        help="Append one human visual verdict and freeze its terminal derivation record",
    )
    record_verdict.add_argument("partition", type=Path)
    record_verdict.add_argument("plan", type=Path)
    record_verdict.add_argument("assignment_id")
    record_verdict.add_argument("--verdict", choices=("approved", "rejected"), required=True)
    record_verdict.add_argument("--concern-tag", action="append", default=[])
    record_verdict.add_argument("--evidence-id", action="append", default=[])
    record_verdict.add_argument("--moment-index", action="append", type=int, default=[])
    record_verdict.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    record_verdict.add_argument("--json", action="store_true", dest="json_output")

    build_proposal = actions.add_parser(
        "build-derivation-proposal",
        help="Build one deterministic unapproved candidate proposal or exact no-go",
    )
    build_proposal.add_argument("manifest", type=Path)
    build_proposal.add_argument("partition", type=Path)
    build_proposal.add_argument("plan", type=Path)
    build_proposal.add_argument("cell_plan_id")
    build_proposal.add_argument("--key", type=Path, required=True)
    build_proposal.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    build_proposal.add_argument("--json", action="store_true", dest="json_output")

    record_review = actions.add_parser(
        "record-derivation-review",
        help="Record one immutable proposal-bound independent review lane",
    )
    record_review.add_argument("plan", type=Path)
    record_review.add_argument("cell_plan_id")
    record_review.add_argument("--lane", choices=(
        "architecture",
        "statistical_model_contract",
        "privacy_security",
        "experimental_design",
        "adversarial",
    ), required=True)
    record_review.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    record_review.add_argument("--json", action="store_true", dest="json_output")

    finalize_lock = actions.add_parser(
        "finalize-derivation-lock",
        help="Finalize one owner-only candidate lock after all five clean review lanes",
    )
    finalize_lock.add_argument("manifest", type=Path)
    finalize_lock.add_argument("partition", type=Path)
    finalize_lock.add_argument("plan", type=Path)
    finalize_lock.add_argument("cell_plan_id")
    finalize_lock.add_argument("--key", type=Path, required=True)
    finalize_lock.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    finalize_lock.add_argument("--json", action="store_true", dest="json_output")

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

        if args.action in {
            "create-derivation-plan",
            "validate-derivation-plan",
            "run-derivation-assignment",
            "derivation-status",
            "record-derivation-verdict",
            "build-derivation-proposal",
            "record-derivation-review",
            "finalize-derivation-lock",
        }:
            return _run_derivation_action(args)

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
        AV1ValidationDerivationError,
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


def _run_derivation_action(args: argparse.Namespace) -> int:
    if args.action in {"create-derivation-plan", "validate-derivation-plan"}:
        manifest, partition, token_key = _load_current_derivation_inputs(args)
        config = load_config(args.config)
        runtime_context_sha256 = (
            av1_validation_derivation_runtime_context_sha256(config)
        )
        quality_metrics = {
            assignment.quality_metric
            for assignment in partition.assignments
            if assignment.role == "derivation"
        }
        if len(quality_metrics) != 1:
            raise AV1ValidationDerivationError(
                "AV1 derivation partition quality metric is not uniform"
            )
        execution_environment_sha256 = (
            av1_validation_derivation_execution_environment_sha256(
                quality_metric=next(iter(quality_metrics)),
            )
        )
        statistics_contract_sha256 = (
            av1_validation_derivation_statistics_contract_sha256(manifest)
        )
        if args.action == "create-derivation-plan":
            authorization = build_av1_validation_v2_derivation_authorization(
                manifest=manifest,
                selection_lock_sha256=partition.selection_lock_sha256,
                derivation_partition_sha256=partition.derivation_partition_sha256,
                runtime_context_sha256=runtime_context_sha256,
                execution_environment_sha256=execution_environment_sha256,
                statistics_contract_sha256=statistics_contract_sha256,
                authorized_at=_now_iso(),
                valid_until=args.valid_until,
            )
            plan = build_av1_validation_derivation_plan(
                manifest=manifest,
                partition=partition,
                authorization=authorization,
                runtime_context_sha256=runtime_context_sha256,
            )
            artifact_root = _derivation_artifact_root_for_plan(
                config=load_config(args.config),
                plan=plan,
            )
            write_av1_validation_derivation_plan(artifact_root, plan)
        else:
            plan, _artifact_root = _load_canonical_derivation_plan(
                plan_path=args.plan,
                config_path=args.config,
            )
            if (
                plan.authorization.execution_environment_sha256
                != execution_environment_sha256
                or plan.authorization.statistics_contract_sha256
                != statistics_contract_sha256
            ):
                raise AV1ValidationDerivationError(
                    "AV1 derivation plan execution contract drifted"
                )
            rebuilt = build_av1_validation_derivation_plan(
                manifest=manifest,
                partition=partition,
                authorization=plan.authorization,
                runtime_context_sha256=runtime_context_sha256,
            )
            if rebuilt != plan:
                raise AV1ValidationDerivationError(
                    "AV1 derivation plan does not match current locked inputs"
                )
        _print_partition_payload(
            av1_validation_derivation_plan_public_summary(plan),
            json_output=args.json_output,
        )
        return 0

    if args.action == "run-derivation-assignment":
        for path in (args.partition, args.plan, args.key):
            assert_private_artifact_path(path, repository_root=REPOSITORY_ROOT)
        manifest = load_av1_validation_manifest_v2(args.manifest)
        partition = load_av1_validation_private_partition(args.partition)
        plan, artifact_root = _load_canonical_derivation_plan(
            plan_path=args.plan,
            config_path=args.config,
        )
        token_key = load_av1_validation_partition_key(args.key)
        attempt = run_av1_validation_derivation_assignment(
            config_path=args.config,
            manifest=manifest,
            partition=partition,
            token_key=token_key,
            plan=plan,
            assignment_id=args.assignment_id,
            attempts_directory=artifact_root / "attempts",
            terminal_records_directory=artifact_root / "terminal-records",
        )
        _print_partition_payload(
            {
                "attempt_id": attempt.attempt_id,
                "attempt_sha256": attempt.payload_sha256,
                "status": attempt.status,
                "review_required": attempt.status == "review_pending",
                "holdout_execution_authorized": False,
                "public_bundle_activation_allowed": False,
            },
            json_output=args.json_output,
        )
        return 0 if attempt.status == "review_pending" else 2

    if args.action == "derivation-status":
        plan, artifact_root = _load_canonical_derivation_plan(
            plan_path=args.plan,
            config_path=args.config,
        )
        attempts_directory = artifact_root / "attempts"
        records_directory = artifact_root / "terminal-records"
        attempts = (
            load_av1_validation_derivation_attempts(attempts_directory)
            if attempts_directory.exists()
            else ()
        )
        records = (
            load_av1_validation_derivation_terminal_records(records_directory)
            if records_directory.exists()
            else ()
        )
        attempt_counts = {
            status: sum(attempt.status == status for attempt in attempts)
            for status in ("review_pending", "failed", "excluded", "stopped")
        }
        record_counts = {
            status: sum(record.status == status for record in records)
            for status in ("observed", "failed", "excluded", "stopped")
        }
        _print_partition_payload(
            {
                "plan_id": plan.plan_id,
                "registered_assignment_count": len(plan.assignments),
                "attempt_count": len(attempts),
                "terminal_record_count": len(records),
                **{f"attempt_{key}_count": value for key, value in attempt_counts.items()},
                **{f"terminal_{key}_count": value for key, value in record_counts.items()},
                "holdout_execution_authorized": False,
                "public_bundle_activation_allowed": False,
            },
            json_output=args.json_output,
        )
        return 0

    if args.action == "record-derivation-verdict":
        for path in (args.partition, args.plan):
            assert_private_artifact_path(path, repository_root=REPOSITORY_ROOT)
        partition = load_av1_validation_private_partition(args.partition)
        plan, artifact_root = _load_canonical_derivation_plan(
            plan_path=args.plan,
            config_path=args.config,
        )
        attempts = load_av1_validation_derivation_attempts(
            artifact_root / "attempts"
        )
        attempt = next(
            (item for item in attempts if item.assignment_id == args.assignment_id),
            None,
        )
        if attempt is None:
            raise AV1ValidationDerivationError(
                "AV1 derivation assignment has no immutable attempt"
            )
        terminal = record_av1_validation_derivation_visual_verdict(
            config_path=args.config,
            plan=plan,
            partition=partition,
            attempt=attempt,
            terminal_records_directory=artifact_root / "terminal-records",
            verdict=args.verdict,
            concern_tags=args.concern_tag,
            evidence_ids=args.evidence_id,
            moment_indexes=args.moment_index,
            recorded_at=_now_iso(),
        )
        _print_partition_payload(
            {
                "terminal_record_sha256": terminal.payload_sha256,
                "status": terminal.status,
                "holdout_execution_authorized": False,
                "public_bundle_activation_allowed": False,
            },
            json_output=args.json_output,
        )
        return 0 if terminal.status == "observed" else 2

    if args.action == "build-derivation-proposal":
        for path in (
            args.partition,
            args.key,
            args.plan,
        ):
            assert_private_artifact_path(path, repository_root=REPOSITORY_ROOT)
        manifest = load_av1_validation_manifest_v2(args.manifest)
        assert_preregistered_av1_validation_manifest_v2(manifest)
        partition = _load_derivation_partition_for_evaluation(
            manifest=manifest,
            partition_path=args.partition,
            key_path=args.key,
            config_path=args.config,
        )
        plan, artifact_root = _load_canonical_derivation_plan(
            plan_path=args.plan,
            config_path=args.config,
        )
        attempts = load_av1_validation_derivation_attempts(
            artifact_root / "attempts"
        )
        records = load_av1_validation_derivation_terminal_records(
            artifact_root / "terminal-records"
        )
        current_observations = load_current_av1_validation_derivation_observations(
            config_path=args.config,
            records=records,
        )
        proposed_at = _now_iso()
        evaluation = evaluate_av1_validation_derivation_candidate(
            manifest=manifest,
            plan=plan,
            partition=partition,
            cell_plan_id=args.cell_plan_id,
            attempts=attempts,
            records=records,
            current_observations=current_observations,
            proposed_at=proposed_at,
        )
        summary = av1_validation_derivation_candidate_evaluation_public_summary(
            evaluation
        )
        _print_partition_payload(summary, json_output=args.json_output)
        if evaluation.proposal is None:
            return 2
        write_av1_validation_derivation_candidate_proposal(
            artifact_root,
            plan=plan,
            proposal=evaluation.proposal,
        )
        return 0

    if args.action == "record-derivation-review":
        plan, artifact_root = _load_canonical_derivation_plan(
            plan_path=args.plan,
            config_path=args.config,
        )
        proposal = load_av1_validation_derivation_candidate_proposal(
            artifact_root,
            plan=plan,
            cell_plan_id=args.cell_plan_id,
        )
        reviewer_token, review_evidence, decision = _run_code_agent_review(
            proposal=proposal,
            lane=args.lane,
        )
        review_evidence_sha256 = f"sha256:{hashlib.sha256(review_evidence).hexdigest()}"
        review = build_av1_validation_derivation_review_attestation(
            proposal=proposal,
            lane=args.lane,
            reviewer_token=reviewer_token,
            review_evidence_sha256=review_evidence_sha256,
            decision=decision,
            reviewed_at=_now_iso(),
        )
        envelope = build_av1_validation_derivation_review_envelope(
            review=review,
            evidence=review_evidence,
        )
        write_av1_validation_derivation_review_envelope(
            artifact_root,
            plan=plan,
            proposal=proposal,
            envelope=envelope,
        )
        _print_partition_payload(
            {
                "proposal_id": proposal.proposal_id,
                "review_lane": review.lane,
                "decision": review.decision,
                "review_sha256": review.payload_sha256,
                "candidate_lock_created": False,
                "holdout_execution_authorized": False,
            },
            json_output=args.json_output,
        )
        return 0 if review.decision == "approved" else 2

    if args.action == "finalize-derivation-lock":
        for path in (
            args.plan,
            args.partition,
            args.key,
        ):
            assert_private_artifact_path(path, repository_root=REPOSITORY_ROOT)
        manifest = load_av1_validation_manifest_v2(args.manifest)
        assert_preregistered_av1_validation_manifest_v2(manifest)
        partition = load_av1_validation_private_partition(args.partition)
        token_key = load_av1_validation_partition_key(args.key)
        validate_av1_validation_private_partition(
            partition,
            manifest=manifest,
            token_key=token_key,
        )
        lock_envelope = finalize_av1_validation_derivation_candidate_lock(
            config_path=args.config,
            manifest=manifest,
            partition=partition,
            token_key=token_key,
            plan_path=args.plan,
            cell_plan_id=args.cell_plan_id,
            now_iso=_now_iso,
        )
        candidate_lock = lock_envelope.candidate_lock
        _print_partition_payload(
            {
                "candidate_lock_id": candidate_lock.candidate_lock_id,
                "candidate_lock_sha256": candidate_lock.payload_sha256,
                "candidate_lock_envelope_sha256": lock_envelope.payload_sha256,
                "candidate_lock_reviewed": True,
                "holdout_execution_authorized": False,
                "public_bundle_activation_allowed": False,
            },
            json_output=args.json_output,
        )
        return 0

    raise AV1ValidationDerivationError("AV1 derivation action is unsupported")


def _load_current_derivation_inputs(
        args: argparse.Namespace,
) -> tuple[AV1ValidationManifestV2, AV1ValidationPrivatePartition, bytes]:
    for path in (args.eligibility, args.partition, args.key):
        assert_private_artifact_path(path, repository_root=REPOSITORY_ROOT)
    manifest = load_av1_validation_manifest_v2(args.manifest)
    assert_preregistered_av1_validation_manifest_v2(manifest)
    eligibility = load_av1_validation_v2_eligibility(args.eligibility)
    assert_preregistered_av1_validation_v2_eligibility(eligibility)
    partition = load_av1_validation_private_partition(args.partition)
    token_key = load_av1_validation_partition_key(args.key)
    validate_av1_validation_private_partition(
        partition,
        manifest=manifest,
        token_key=token_key,
    )
    config = load_config(args.config)
    with open_readonly_db(config.paths.db_path) as connection:
        inventory = load_av1_validation_partition_inventory(connection, config=config)
    validate_av1_validation_partition_current_inputs(
        partition,
        manifest=manifest,
        sources=inventory.sources,
        expectations=inventory.expectations,
        token_key=token_key,
    )
    return manifest, partition, token_key


def _load_derivation_partition_for_evaluation(
        *,
        manifest: AV1ValidationManifestV2,
        partition_path: Path,
        key_path: Path,
        config_path: Path,
) -> AV1ValidationPrivatePartition:
    partition = load_av1_validation_private_partition(partition_path)
    token_key = load_av1_validation_partition_key(key_path)
    validate_av1_validation_private_partition(
        partition,
        manifest=manifest,
        token_key=token_key,
    )
    config = load_config(config_path)
    with open_readonly_db(config.paths.db_path) as connection:
        inventory = load_av1_validation_partition_inventory(connection, config=config)
    validate_av1_validation_partition_current_inputs(
        partition,
        manifest=manifest,
        sources=inventory.sources,
        expectations=inventory.expectations,
        token_key=token_key,
    )
    return partition


def _derivation_artifact_root_for_plan(
        *,
        config: MediaforceConfig,
        plan: AV1ValidationDerivationPlan,
) -> Path:
    root = av1_validation_derivation_artifact_root(config, plan)
    assert_private_artifact_path(root, repository_root=REPOSITORY_ROOT)
    return root


def _load_canonical_derivation_plan(
        *,
        plan_path: Path,
        config_path: Path,
        config: MediaforceConfig | None = None,
) -> tuple[AV1ValidationDerivationPlan, Path]:
    assert_private_artifact_path(plan_path, repository_root=REPOSITORY_ROOT)
    plan = load_av1_validation_derivation_plan(plan_path)
    artifact_root = _derivation_artifact_root_for_plan(
        config=config or load_config(config_path),
        plan=plan,
    )
    if plan_path.expanduser().resolve() != (artifact_root / "plan.json").resolve():
        raise AV1ValidationDerivationError(
            "AV1 derivation plan must use the plan-global canonical private state root"
        )
    validate_av1_validation_derivation_artifact_root_binding(
        artifact_root,
        plan,
    )
    return plan, artifact_root


def _run_code_agent_review(
        *,
        proposal: AV1ValidationDerivationCandidateProposal,
        lane: str,
) -> tuple[str, bytes, AV1ValidationDerivationReviewDecision]:
    code_binary = shutil.which("code")
    if code_binary is None:
        raise AV1ValidationDerivationError(
            "AV1 derivation review requires the Every Code executable"
        )
    resolved_binary = Path(code_binary).resolve(strict=True)
    review_run_id = str(uuid.uuid4())
    prompt = _agent_review_prompt(
        proposal=proposal,
        lane=lane,
        review_run_id=review_run_id,
    )
    command = [
        str(resolved_binary),
        "-a",
        "never",
        "exec",
        "-s",
        "read-only",
        "--json",
        "--max-seconds",
        str(_AGENT_REVIEW_MAX_SECONDS),
        "-",
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=REPOSITORY_ROOT,
            input=prompt,
            text=True,
            capture_output=True,
            timeout=_AGENT_REVIEW_MAX_SECONDS + 30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation Every Code review did not complete"
        ) from exc
    if completed.returncode != 0:
        raise AV1ValidationDerivationError(
            "AV1 derivation Every Code review failed"
        )
    final_message, completion_seen = _completed_agent_message(completed.stdout)
    if not completion_seen or final_message is None:
        raise AV1ValidationDerivationError(
            "AV1 derivation Every Code review lacks completed-run evidence"
        )
    decision = _agent_review_decision(
        final_message,
        proposal=proposal,
        lane=lane,
        review_run_id=review_run_id,
    )
    binary_sha256 = hashlib.sha256(resolved_binary.read_bytes()).hexdigest()
    evidence = json.dumps(
        {
            "schema": "mediaforce.av1_derivation_agent_review_run",
            "schema_version": 1,
            "review_run_id": review_run_id,
            "reviewer_token": f"agent:{review_run_id}",
            "proposal_id": proposal.proposal_id,
            "proposal_payload_sha256": proposal.payload_sha256,
            "lane": lane,
            "decision": decision,
            "code_binary_sha256": f"sha256:{binary_sha256}",
            "prompt_sha256": f"sha256:{hashlib.sha256(prompt.encode('utf-8')).hexdigest()}",
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "returncode": completed.returncode,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"agent:{review_run_id}", evidence, decision


def _completed_agent_message(stdout: str) -> tuple[str | None, bool]:
    messages: list[str] = []
    completed_message: str | None = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        message = event.get("msg")
        if not isinstance(message, dict):
            continue
        if message.get("type") == "agent_message" and isinstance(
            message.get("message"), str
        ):
            messages.append(message["message"])
        if (
            message.get("type") == "task_lifecycle"
            and message.get("phase") == "quiescent"
            and isinstance(message.get("last_agent_message"), str)
        ):
            completed_message = message["last_agent_message"]
    if not messages or completed_message != messages[-1]:
        return None, False
    return messages[-1], True


def _agent_review_decision(
        message: str,
        *,
        proposal: AV1ValidationDerivationCandidateProposal,
        lane: str,
        review_run_id: str,
) -> AV1ValidationDerivationReviewDecision:
    nonempty_lines = [line.strip() for line in message.splitlines() if line.strip()]
    marker_lines = [
        line for line in nonempty_lines if line.startswith(_AGENT_REVIEW_MARKER)
    ]
    if (
        not nonempty_lines
        or len(marker_lines) != 1
        or marker_lines[0] != nonempty_lines[-1]
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation review lacks one terminal structured verdict"
        )
    try:
        marker = json.loads(marker_lines[0][len(_AGENT_REVIEW_MARKER):])
    except (json.JSONDecodeError, TypeError) as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation review verdict is invalid"
        ) from exc
    if not isinstance(marker, dict) or set(marker) != {
        "decision",
        "lane",
        "proposal_id",
        "proposal_payload_sha256",
        "review_run_id",
    }:
        raise AV1ValidationDerivationError(
            "AV1 derivation review verdict contract is invalid"
        )
    decision = marker.get("decision")
    if (
        marker.get("proposal_id") != proposal.proposal_id
        or marker.get("proposal_payload_sha256") != proposal.payload_sha256
        or marker.get("lane") != lane
        or marker.get("review_run_id") != review_run_id
        or decision not in {"approved", "rejected"}
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation review verdict does not match its run, proposal, and lane"
        )
    return decision


def _agent_review_prompt(
        *,
        proposal: AV1ValidationDerivationCandidateProposal,
        lane: str,
        review_run_id: str,
) -> str:
    marker = {
        "decision": "approved|rejected",
        "lane": lane,
        "proposal_id": proposal.proposal_id,
        "proposal_payload_sha256": proposal.payload_sha256,
        "review_run_id": review_run_id,
    }
    return (
        "Perform one independent, read-only AV1 derivation candidate review. "
        "Do not modify files, invoke another agent, reveal opaque tokens, or infer private media identity. "
        f"Review lane: {lane}. Review the repository implementation and this canonical proposal payload:\n"
        f"{json.dumps(proposal.to_payload(), sort_keys=True, separators=(',', ':'))}\n"
        "Reject on any actionable gate failure; otherwise approve. Explain findings concisely. "
        "End with exactly one final marker line using valid JSON and replace the decision placeholder:\n"
        f"{_AGENT_REVIEW_MARKER}{json.dumps(marker, sort_keys=True, separators=(',', ':'))}"
    )


def _print_partition_payload(payload: dict[str, object], *, json_output: bool) -> None:
    assert_av1_cold_start_public_payload_safe(payload)
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
